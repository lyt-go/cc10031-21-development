"""标定复验领域服务：按换算出处复验一段采集窗口的取值与量纲。

一张复验单要同时回答四件事：

1. **采用范围**——本轮复验真正核对过的样本时段（半开区间 ``[start, end)``）；
2. **计量单位**——每条样本自己带着实际单位，混用 mV 与 V 的样本不会被抹平；
3. **已核 / 待续查**——一轮只核了前半段时，剩余时段显式留作下一轮续查；
4. **历史引用**——每次提交都把实际读到的样本、规则和版本写进运行快照，
   后续切到新标定后，仍能回看旧版当时引用了什么、算出了什么。

状态流转::

    草稿 → 复验中 ──提交复验──→ 待续查 ──续查──→ 复验中 …
                     └──────────→ 已复验

``已复验`` 之后不能续查；要核对新到位的标定，必须按提交记录另起复验单。
续查只处理提交快照里剩下的待续查时段，不会把已经核对过的时段重新算一遍。
单次运行里，同一时刻若被两次标定同时覆盖（闭区间边界交叠），按
``effective_start → version → 编号`` 择优，并把全部候选写进运行快照。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.services import conversion_index as ci
from app.services import time_window as tw
from app.services.base import BaseModuleService
from app.store import store

RUN_DIR = "reverify_runs"
FINAL_STATUS = "已复验"


class ReverifySessionService(BaseModuleService):
    MODULE = "reverify_session"
    ENTITY = "复验单"
    OPERATION_TERM = "复验"
    LIST_FIELDS = [
        "复验编号", "测点编号", "标定引用", "复验规则", "采用范围", "计量单位",
        "已核时段", "待续查时段", "复验版本", "复验时间",
    ]
    # 非列表字段：视图与动作会读写，但不参与表格列和 CSV 导出。
    EXTRA_FIELDS = [
        "会话版本", "父复验编号", "来源提交", "复验结论", "样本数", "边界交叠",
        "重开自", "单点时刻", "全量范围", "来源血缘",
    ]
    REQUIRED_FIELDS = ["复验编号", "测点编号", "复验规则"]
    STATUS_ORDER = ["草稿", "复验中", "待续查", "已复验"]
    ACTION_RULES = {
        "提交复验": "待续查",
        "续查": "待续查",
        "标记完成": "已复验",
        "按提交记录重开": "草稿",
        "单点复验": "草稿",
    }
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------ 工具

    def _runs_path(self, session_key: str):
        return self._data_dir(RUN_DIR) / f"{ci.safe_key(session_key)}_runs.jsonl"

    def runs(self, session_id: int) -> list[dict[str, Any]]:
        session = store.find(self.MODULE, session_id)
        return self._read_jsonl(self._runs_path(session["复验编号"])) if session else []

    def run_detail(self, session_id: int, run_no: int) -> dict[str, Any] | None:
        for run in self.runs(session_id):
            if int(run.get("run", 0)) == int(run_no):
                return run
        return None

    def _next_id(self) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(self.MODULE)), default=0) + 1

    def _unique_session_key(self, base: str) -> str:
        existing = {str(row.get("复验编号", "")) for row in store.rows(self.MODULE)}
        if base not in existing:
            return base
        index = 2
        while f"{base}#{index}" in existing:
            index += 1
        return f"{base}#{index}"

    def _entries(self, point_key: str) -> list[dict[str, Any]]:
        return ci.load_entries(self._data_dir("."), point_key)

    @staticmethod
    def _parse_ranges(text: Any) -> list[dict[str, str]]:
        """把 "a ~ b；c ~ d" 解析回半开区间列表；用于种子数据或外部登记的单据。"""
        raw = str(text or "").strip()
        if not raw or raw == "无":
            return []
        ranges = []
        for piece in raw.replace("，", "；").split("；"):
            bounds = [part.strip() for part in piece.split("~")]
            if len(bounds) != 2:
                continue
            window = tw.window_period(bounds[0], bounds[1])
            if window:
                ranges.append(window)
        return tw.merge_periods(ranges)

    def _hydrate(self, session: dict[str, Any]) -> None:
        """补齐内部时段字段：外部登记或种子数据只有可读文本，没有区间结构。"""
        if "pending_ranges" not in session:
            session["pending_ranges"] = self._parse_ranges(session.get("待续查时段"))
        if "reviewed" not in session:
            session["reviewed"] = self._parse_ranges(session.get("已核时段"))
        if "unit_name" not in session:
            session["unit_name"] = session.get("计量单位", "")
        if "rule_reference" not in session:
            session["rule_reference"] = session.get("标定引用", "")

    def _new_row(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": self._next_id(),
            "已核时段": "无",
            "复验版本": "v1",
            "复验时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "会话版本": "0",
            "父复验编号": "",
            "来源提交": "",
            "复验结论": "未开始",
            "样本数": "0",
            "边界交叠": "无",
            "重开自": "",
            "单点时刻": "",
            "来源血缘": "",
            "status": "草稿",
            "pending": True,
            "abnormal": False,
        }
        row.update(payload)
        store.rows(self.MODULE).append(row)
        return row

    # -------------------------------------------------------------- 建单

    def open_session(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """按一条提交记录或一组换算出处建复验单。"""
        point_key = str(values.get("测点编号") or "").strip()
        rule = str(values.get("复验规则") or "").strip()
        if not point_key or not rule:
            return None, "复验单必须指定测点编号与复验规则（换算规则或标定引用）"
        window = self._resolve_window(point_key, rule, values)
        if isinstance(window, str):
            return None, window
        session_key = str(values.get("复验编号") or "").strip() or self._unique_session_key(
            f"REV-{point_key}-{len(store.rows(self.MODULE)) + 1}")
        row = self._new_row({
            "复验编号": session_key,
            "测点编号": point_key,
            "标定引用": window["reference"],
            "复验规则": rule,
            "采用范围": tw.period_text(window["range"]),
            "计量单位": window["unit"],
            "待续查时段": tw.period_text(window["range"]),
            "全量范围": window["range"],
            "window": window["range"],
            "pending_ranges": [window["range"]],
            "reviewed": [],
            "unit_name": window["unit"],
            "rule_reference": window["reference"],
            "calibrate_refs": window["refs"],
        })
        return row, (
            f"复验单 {session_key} 已建立，采用范围 {row['采用范围']}，"
            f"单位 {row['计量单位']}"
        )

    def _resolve_window(
        self, point_key: str, rule: str, values: dict[str, Any],
    ) -> dict[str, Any] | str:
        """确定采用范围：显式区间优先；否则取命中条目与显式单边的交集包络。

        匹配顺序：显式「标定引用」优先（从血缘或提交记录带过来的原引用），
        其次才按「复验规则」模糊匹配换算规则/标定编号。
        """
        entries = self._entries(point_key)
        requested = str(values.get("标定引用") or "").strip()
        if requested:
            matches = [entry for entry in entries if ci.matches_rule(entry, requested)]
        else:
            matches = [entry for entry in entries if ci.matches_rule(entry, rule)]
        if not matches:
            return f"测点 {point_key} 没有匹配「{requested or rule}」的换算出处索引"
        starts = [entry["start"] for entry in matches if entry.get("start")]
        ends = [entry["end"] for entry in matches if entry.get("end")]
        if not starts or not ends:
            return f"测点 {point_key} 的换算出处索引没有样本区间"
        start, end = min(starts), max(ends)
        if values.get("起始时刻"):
            start = max(start, tw.format_time(values["起始时刻"]))
        if values.get("截止时刻"):
            end = min(end, tw.format_time(values["截止时刻"]))
        window = tw.window_period(start, tw.bucket_end(end))
        if window is None:
            return f"复验范围 {start} ~ {end} 不合法"
        chosen = self._pick_reference(matches, str(values.get("标定引用") or ""))
        return {
            "range": window,
            "unit": str(values.get("计量单位") or chosen.get("unit") or "V"),
            "reference": str(values.get("标定引用") or chosen.get("reference") or ""),
            "refs": [entry.get("reference") for entry in matches],
        }

    @staticmethod
    def _pick_reference(matches: list[dict[str, Any]], requested: str) -> dict[str, Any]:
        if requested:
            for entry in matches:
                if entry.get("reference") == requested:
                    return entry
        return max(matches, key=ci.rank)

    # -------------------------------------------------------------- 单次运行

    def _allocate_scope(self, session: dict[str, Any], values: dict[str, Any]) -> dict[str, str] | str:
        """从待续查时段里取出本次要核的范围，并裁剪掉已核部分。"""
        pending = list(session.get("pending_ranges") or [])
        if not pending:
            return "没有待续查时段"
        start = values.get("起始时刻")
        end = values.get("截止时刻")
        moment = values.get("样本时刻") or values.get("timestamp")
        if moment and not (start and end):
            target = tw.format_time(moment)
            for item in pending:
                if tw.in_period(target, item):
                    # 单点范围裁剪到待续查边界，避免桶尾越过尚未核过的时段。
                    return tw.intersect_period(
                        {"start": target, "end": tw.bucket_end(target)},
                        item["start"], item["end"],
                    )
            return f"样本时刻 {target} 不在待续查时段内"
        if start and end:
            requested = tw.window_period(tw.format_time(start), tw.bucket_end(end))
            if requested is None:
                return f"复验范围 {start} ~ {end} 不合法"
            clipped = [
                piece for piece in (
                    tw.intersect_period(item, requested["start"], requested["end"])
                    for item in pending
                ) if piece
            ]
            if not clipped:
                return (
                    f"复验范围 {tw.period_text(requested)} 不在待续查时段 "
                    f"{session.get('待续查时段')} 内"
                )
            return tw.merge_periods(clipped)[0]
        return pending[0]

    def _execute(
        self,
        session: dict[str, Any],
        scope: dict[str, str],
        *,
        action: str,
        note: str,
    ) -> dict[str, Any]:
        """在一段范围内采集样本、择优、判定量纲，然后追加运行快照并推进时段。

        ``pin_reference`` 只在"按提交记录重开"的子单上打开：那类单要核对旧版当时
        的引用，边界交叠处不能滑到后来获准的更正版。普通提交与续查都按当前
        ``effective_start → version`` 择优，因此续查能读到已经获准采用的更正版。
        """
        pinned = session.get("标定引用") if session.get("pin_reference") else None
        samples, overlaps = ci.collect_samples(
            self._entries(session["测点编号"]), scope,
            pin_reference=pinned,
        )
        expected_unit = str(session.get("计量单位") or session.get("unit_name") or "")
        units_seen = sorted({str(sample.get("unit")) for sample in samples})
        mismatched = [sample for sample in samples
                      if expected_unit and str(sample.get("unit")) != expected_unit]
        problems: list[str] = []
        if not samples:
            problems.append("区间内没有可核对样本")
        if mismatched:
            problems.append(
                f"{len(mismatched)} 条样本的实际单位不是 {expected_unit}"
            )
        references = sorted({str(sample.get("reference")) for sample in samples})
        conclusion = "通过" if not problems else "存在差异"
        run_index = len(self.runs(session["id"])) + 1
        run = {
            "run": run_index,
            "session": session["复验编号"],
            "action": action,
            "point": session["测点编号"],
            "scope": scope,
            "rule": session.get("复验规则"),
            "requested_reference": session.get("标定引用"),
            "actual_references": references,
            "unit": expected_unit,
            "units_seen": units_seen,
            "sample_count": len(samples),
            "samples": samples,
            "overlaps": overlaps,
            "problems": problems,
            "conclusion": conclusion,
            "note": note,
            "digest": hashlib.sha256(
                json.dumps(samples, ensure_ascii=False, sort_keys=True, default=str)
                .encode("utf-8")
            ).hexdigest()[:16],
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        self._write_jsonl(self._runs_path(session["复验编号"]), [run])
        # 时段推进：已核取并集，待续查扣掉本次范围。
        session["reviewed"] = tw.merge_periods([*session.get("reviewed", []), scope])
        session["pending_ranges"] = tw.subtract_period(
            session.get("pending_ranges") or [], scope["start"], scope["end"])
        session["已核时段"] = tw.periods_text(session["reviewed"])
        session["待续查时段"] = tw.periods_text(session["pending_ranges"])
        # 采用范围指整单累计真正核过的范围；单次范围在运行快照的 scope 里。
        session["采用范围"] = tw.periods_text(session["reviewed"])
        session["复验结论"] = conclusion
        session["复验版本"] = f"v{run_index}"
        session["复验时间"] = run["recorded_at"]
        session["样本数"] = str(int(session.get("样本数", "0") or 0) + len(samples))
        session["来源提交"] = f"RUN-{run_index}"
        session["会话版本"] = str(run_index)
        session["status"] = FINAL_STATUS if not session["pending_ranges"] else "待续查"
        session["pending"] = session["status"] != FINAL_STATUS
        session["abnormal"] = conclusion == "存在差异"
        if overlaps:
            session["边界交叠"] = "；".join(
                f"{item['timestamp']}→{item['chosen']}" for item in overlaps)
        return {
            "session": session["复验编号"],
            "run": run_index,
            "action": action,
            "scope": scope,
            "unit": expected_unit,
            "units_seen": units_seen,
            "samples": [
                {
                    "timestamp": sample["timestamp"],
                    "value": sample["value"],
                    "unit": sample["unit"],
                    "raw_value": sample.get("raw_value"),
                    "source_unit": sample.get("source_unit"),
                    "scale": sample.get("scale"),
                    "offset": sample.get("offset"),
                    "reference": sample.get("reference"),
                    "source": sample.get("source"),
                }
                for sample in samples
            ],
            "sample_count": len(samples),
            "overlaps": overlaps,
            "actual_references": references,
            "conclusion": conclusion,
            "problems": problems,
            "reviewed": session["已核时段"],
            "pending": session["待续查时段"],
            "status": session["status"],
            "digest": run["digest"],
        }

    # -------------------------------------------------------------- 动作实现

    def submit_scope(self, session: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        scope = self._allocate_scope(session, values)
        if isinstance(scope, str):
            return None, scope
        result = self._execute(session, scope, action="提交复验", note=str(values.get("备注") or ""))
        return session, self._summary(session, result)

    def continue_scope(self, session: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if not session.get("pending_ranges"):
            return None, f"复验单 {session['复验编号']} 没有待续查时段，可直接标记完成"
        scope = self._allocate_scope(session, values)
        if isinstance(scope, str):
            return None, scope
        result = self._execute(session, scope, action="续查", note=str(values.get("备注") or ""))
        return session, self._summary(session, result)

    @staticmethod
    def _summary(session: dict[str, Any], result: dict[str, Any]) -> str:
        return (
            f"复验单 {session['复验编号']} 已{result['action']} "
            f"{tw.period_text(result['scope'])}，核对 {result['sample_count']} 条样本"
            f"（单位 {result['unit'] or '—'}，引用 {'、'.join(result.get('actual_references', [])) or '—'}），"
            f"结论 {result['conclusion']}；待续查 {session['待续查时段']}"
        )

    def start_session(self, session: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """显式把复验单置为复验中；不核样本，只推进状态。"""
        if session.get("status") == FINAL_STATUS:
            return None, f"复验单 {session['复验编号']} 已完成，请另起复验单"
        session["status"] = "复验中"
        session["pending"] = True
        session["复验时间"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        return session, f"复验单 {session['复验编号']} 已进入复验中"

    def finish_session(self, session: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if session.get("pending_ranges"):
            return None, f"仍有待续查时段 {session['待续查时段']}，请先续查"
        if session.get("status") == FINAL_STATUS:
            return session, f"复验单 {session['复验编号']} 已经是完成状态"
        session["status"] = FINAL_STATUS
        session["pending"] = False
        session["复验时间"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        return session, f"复验单 {session['复验编号']} 已完成复验"

    def reopen_from_run(self, session: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """按提交记录重开：取某次运行快照里的引用与时段，另起一张复验单核对旧版。"""
        runs = self.runs(session["id"])
        if not runs:
            return None, f"复验单 {session['复验编号']} 还没有提交记录"
        run_no = values.get("提交记录", values.get("run"))
        if run_no in (None, ""):
            run = runs[-1]
        else:
            run = next((item for item in runs if int(item.get("run", 0)) == int(run_no)), None)
            if run is None:
                return None, f"提交记录 RUN-{run_no} 不存在"
        scope = run.get("scope") or {}
        if not tw.period_bounds(scope):
            return None, f"提交记录 RUN-{run['run']} 缺少有效范围"
        reference = str(run.get("requested_reference") or session.get("标定引用") or "")
        session_key = self._unique_session_key(f"{session['复验编号']}-R{run['run']}")
        row = self._new_row({
            "复验编号": session_key,
            "测点编号": session["测点编号"],
            "标定引用": reference,
            "复验规则": str(values.get("复验规则") or session.get("复验规则")),
            "采用范围": tw.period_text(scope),
            "计量单位": str(run.get("unit") or session.get("计量单位")),
            "待续查时段": tw.period_text(scope),
            "全量范围": scope,
            "父复验编号": session["复验编号"],
            "来源提交": f"RUN-{run['run']}",
            "重开自": f"{session['复验编号']}#RUN-{run['run']}",
            "window": scope,
            "pending_ranges": [scope],
            "reviewed": [],
            "unit_name": str(run.get("unit") or session.get("计量单位")),
            "rule_reference": reference,
            "calibrate_refs": [reference],
            "source_digest": run.get("digest"),
            "pin_reference": True,
        })
        return row, (
            f"已按 {session['复验编号']} 的 RUN-{run['run']} 重开复验单 {session_key}，"
            f"原引用 {reference}，范围 {row['采用范围']}，单位 {row['计量单位']}"
        )

    def reverify_point(self, session: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """单点复验：围绕单个样本时刻另起一张复验单，独立记录范围与量纲。"""
        moment = tw.format_time(values.get("样本时刻") or values.get("timestamp") or "")
        if not moment:
            return None, "单点复验需要提供样本时刻"
        scope = tw.window_period(moment, tw.bucket_end(moment))
        if scope is None:
            return None, f"样本时刻 {moment} 不合法"
        reference = str(values.get("标定引用") or session.get("标定引用") or "")
        unit = str(values.get("计量单位") or session.get("计量单位") or "")
        session_key = self._unique_session_key(f"{session['复验编号']}-P{moment}")
        row = self._new_row({
            "复验编号": session_key,
            "测点编号": session["测点编号"],
            "标定引用": reference,
            "复验规则": str(values.get("复验规则") or session.get("复验规则")),
            "采用范围": tw.period_text(scope),
            "计量单位": unit,
            "待续查时段": tw.period_text(scope),
            "全量范围": scope,
            "父复验编号": session["复验编号"],
            "单点时刻": moment,
            "window": scope,
            "pending_ranges": [scope],
            "reviewed": [],
            "unit_name": unit,
            "rule_reference": reference,
            "calibrate_refs": [reference],
        })
        return row, (
            f"已另起单点复验单 {session_key}，样本时刻 {moment}，"
            f"范围 {row['采用范围']}，单位 {unit or '—'}，引用 {reference or '—'}"
        )

    # -------------------------------------------------------------- 动作路由

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        session = store.find(self.MODULE, entry_id)
        if session is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        self._hydrate(session)
        if session.get("status") == FINAL_STATUS and action in {"提交复验", "续查"}:
            return None, (
                f"复验单 {session['复验编号']} 已完成，"
                f"如需核对新标定请用「按提交记录重开」另起复验单"
            )
        handlers = {
            "启动复验": self.start_session,
            "提交复验": self.submit_scope,
            "续查": self.continue_scope,
            "标记完成": self.finish_session,
            "按提交记录重开": self.reopen_from_run,
            "单点复验": self.reverify_point,
        }
        handler = handlers.get(action)
        if handler is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        return handler(session, values)

    # -------------------------------------------------------------- 只读视图

    def detail(self, session_id: int) -> dict[str, Any] | None:
        session = store.find(self.MODULE, session_id)
        if session is None:
            return None
        self._hydrate(session)
        runs = self.runs(session_id)
        payload = dict(session)
        payload["提交记录数"] = len(runs)
        payload["提交记录"] = [
            {
                "run": run.get("run"),
                "action": run.get("action"),
                "scope": run.get("scope"),
                "requested_reference": run.get("requested_reference"),
                "actual_references": run.get("actual_references"),
                "unit": run.get("unit"),
                "units_seen": run.get("units_seen"),
                "sample_count": run.get("sample_count"),
                "conclusion": run.get("conclusion"),
                "digest": run.get("digest"),
                "recorded_at": run.get("recorded_at"),
                "samples": run.get("samples"),
                "overlaps": run.get("overlaps"),
            }
            for run in runs
        ]
        return payload
