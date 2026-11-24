"""采集血缘领域服务：维护测点原始采样经上送、标定、降采样到归并窗口的血缘链路。

血缘链路里的「标定版本」保存的是**原引用**（标定编号 + 版本），而不是"当前最新"。
因此后续新增标定后，旧链路仍能按原引用核对当时采用的换算资料，不会漂移。

另外提供两件和复验直接相关的只读能力：

* ``locate_series``：按测点、实际单位、采集区间、换算规则定位时序产物；
* ``start_reverify``：从血缘查询直接按错值区间发起复验单。
"""
from __future__ import annotations

import time
from typing import Any

from app.services import conversion_index as ci
from app.services import time_window as tw
from app.services.base import BaseModuleService
from app.store import store


class SeriesLineageService(BaseModuleService):
    MODULE = "series_lineage"
    ENTITY = "血缘链路"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["链路编号", "测点编号", "上送批次", "标定版本", "降采样任务", "归并窗口", "血缘版本", "采集时间"]
    EXTRA_FIELDS = ["换算出处", "样本索引", "标定生效起点", "标定生效终点"]
    REQUIRED_FIELDS = ["链路编号", "测点编号", "上送批次"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {
        "提交校验": "待校验",
        "确认生效": "已生效",
        "标记停用": "已停用",
        # 只读动作，不改状态；返回定位结果供血缘页面展示。
        "定位样本": "待校验",
        "发起复验": "待校验",
    }
    # 数据血缘操作：生成传感器测点全链路血缘关系数据追溯边缘采接到窗口归并
    NEGATIVE_ACTIONS = ["标记停用"]

    def build_lineage(self, point_key: str) -> dict[str, Any]:
        """为指定测点生成全链路血缘关系数据，追溯从边缘采接到窗口归并的完整处理链条。"""
        # 扫描该测点上送批次
        batches = [r for r in store.rows("upload_batch") if point_key in (r.get("分片范围") or "")]
        # 扫描该测点标定记录（按版本择优，保留原引用）
        cals = [r for r in store.rows("unit_calibrate") if r.get("测点编号") == point_key]
        cals.sort(key=lambda row: (
            str(row.get("生效起点", "")), str(row.get("标定版本", "")),
            int(row.get("id", 0)),
        ))
        # 扫描该测点降采样任务
        ds_tasks = [r for r in store.rows("downsample") if r.get("源序列") == point_key]
        # 扫描该测点归并窗口
        rollups = [r for r in store.rows("rollup_window") if r.get("测点编号") == point_key]

        lineage_id = f"LINEAGE-{point_key}"
        batch_id = batches[0]["批次编号"] if batches else "N/A"
        cal = cals[-1] if cals else None
        cal_id = ci.reference_of(cal) if cal else "N/A"
        ds_id = ds_tasks[0]["任务编号"] if ds_tasks else "N/A"
        rollup_id = rollups[0]["窗口编号"] if rollups else "N/A"

        lineage_entry = {
            "链路编号": lineage_id,
            "测点编号": point_key,
            "上送批次": batch_id,
            "标定版本": cal_id,
            "降采样任务": ds_id,
            "归并窗口": rollup_id,
            "换算出处": ci.source_of(cal) if cal else "N/A",
            "标定生效起点": tw.format_time(cal.get("生效起点", "")) if cal else "",
            "标定生效终点": tw.format_time(cal.get("生效终点", "")) if cal else "",
            # 数据校验输出：记录血缘校验结果到完整性报告数据文件
            "血缘版本": "v1.0",
            "采集时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        # 索引里该测点的版本数量：血缘要知道自己引用的是第几版。
        entries = ci.load_entries(self._data_dir("."), point_key)
        lineage_entry["样本索引"] = str(len(entries))
        # 将血缘关系写入血缘链路数据文件
        self._write_jsonl(
            self._data_dir("lineage_traces") / f"{ci.safe_key(point_key)}_lineage.jsonl",
            [lineage_entry]
        )
        # 验证并注册血缘
        lineage_row = self._auto_create_seed(self.MODULE, lineage_entry)
        return {"lineage_id": lineage_id, "point": point_key,
                "batch": batch_id, "calibrate": cal_id,
                "downsample": ds_id, "rollup": rollup_id,
                "stages_traced": sum(1 for v in [batch_id, cal_id, ds_id, rollup_id] if v != "N/A")}

    def verify_lineage_chain(self, lineage_id: int) -> dict[str, Any]:
        """校验血缘链路中所有节点的数据文件是否可追溯，写入血缘完整性校验报告文件。"""
        entry = store.find(self.MODULE, lineage_id)
        if entry is None:
            return {"error": f"血缘链路 {lineage_id} 不存在"}
        point_key = entry["测点编号"]
        # 逐层检查数据文件存在性
        checks = {
            "edge_samples": self._data_dir("edge_samples") / f"{ci.safe_key(point_key)}.jsonl",
            "calibrated": self._data_dir("calibrated") / f"{ci.safe_key(point_key)}_calibrated.jsonl",
            "downsampled": list(self._data_dir("downsampled").glob(f"*{point_key}*_downsampled.jsonl")) if self._data_dir("downsampled").exists() else [],
            "rollup_windows": list(self._data_dir("rollup_windows").glob(f"*{point_key}*_rollup.jsonl")) if self._data_dir("rollup_windows").exists() else [],
        }
        results = {}
        for stage, path in checks.items():
            if isinstance(path, list):
                results[stage] = {"exists": len(path) > 0, "file_count": len(path)}
            else:
                results[stage] = {"exists": path.exists(), "path": str(path)}
        all_ok = all(v["exists"] for v in results.values())
        if all_ok:
            self.run_action(lineage_id, "确认生效")
        # 记录血缘校验结果到完整性报告数据文件
        self._write_jsonl(
            self._data_dir("lineage_traces") / f"{ci.safe_key(entry['链路编号'])}_verify.jsonl",
            [{"lineage_id": entry["链路编号"], "checks": results,
              "all_ok": all_ok, "verified_at": time.time()}]
        )
        return {"lineage_id": entry["链路编号"], "checks": results, "chain_ok": all_ok}

    # -- 按换算出处定位时序产物 --

    def locate_series(
        self,
        *,
        point: str | None = None,
        unit: str | None = None,
        start: Any = None,
        end: Any = None,
        rule: str | None = None,
    ) -> dict[str, Any]:
        """按测点、实际单位、采集区间与换算规则定位时序产物，附带原引用。

        区间按半开 ``[start, end)`` 过滤；``end`` 落在某个样本时刻上时按该样本
        所在采样桶的桶尾扩一位，让"采集到 10:03"这类查询能包含 10:03 的样本。
        """
        hits = ci.locate(self._data_dir("."), point=point, unit=unit,
                         start=start, end=tw.bucket_end(end) if end else None, rule=rule)
        return {
            "query": {"point": point or "", "unit": unit or "",
                      "start": tw.format_time(start) if start else "",
                      "end": tw.format_time(end) if end else "",
                      "rule": rule or ""},
            "total": len(hits),
            "items": [
                {
                    "index_id": hit.get("index_id"),
                    "point": hit.get("point"),
                    "unit": hit.get("unit"),
                    "reference": hit.get("reference"),
                    "calibrate_id": hit.get("calibrate_id"),
                    "version": hit.get("version"),
                    "source": hit.get("source"),
                    "artifact": hit.get("artifact"),
                    "matched_count": hit.get("matched_count"),
                    "sample_count": hit.get("sample_count"),
                    "effective_start": hit.get("effective_start"),
                    "effective_end": hit.get("effective_end"),
                    "samples": hit.get("samples"),
                }
                for hit in hits
            ],
        }

    def start_reverify(self, lineage_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """从一条血缘链路直接按错值区间发起复验单，并带上原引用。"""
        entry = store.find(self.MODULE, lineage_id)
        if entry is None:
            return {"error": f"血缘链路 {lineage_id} 不存在"}
        point_key = str(values.get("测点编号") or entry.get("测点编号") or "")
        reference = str(values.get("标定引用") or entry.get("标定版本") or "")
        rule = str(values.get("复验规则") or reference or entry.get("标定版本") or "")
        from app.services.reverify_session import ReverifySessionService

        service = ReverifySessionService()
        session, message = service.open_session({
            "测点编号": point_key,
            "复验规则": rule,
            "标定引用": reference,
            "起始时刻": values.get("起始时刻"),
            "截止时刻": values.get("截止时刻"),
            "计量单位": values.get("计量单位") or entry.get("目标单位"),
        })
        if session is None:
            return {"error": message}
        session["来源血缘"] = entry.get("链路编号")
        return {"session": session["复验编号"], "id": session["id"],
                "message": message, "lineage": entry.get("链路编号"),
                "reference": reference, "scope": session.get("采用范围")}

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "定位样本":
            point_key = str(values.get("测点编号") or entry.get("测点编号") or "")
            located = self.locate_series(
                point=point_key,
                unit=values.get("计量单位"),
                start=values.get("起始时刻"),
                end=values.get("截止时刻"),
                rule=values.get("换算规则") or values.get("复验规则"),
            )
            entry["定位结果"] = located
            entry["定位时间"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            return entry, (
                f"血缘 {entry['链路编号']} 按换算出处定位到 {located['total']} 个时序产物"
                if located["total"] else "血缘未定位到匹配的时序产物"
            )
        if action == "发起复验":
            result = self.start_reverify(entry_id, values)
            if "error" in result:
                return None, result["error"]
            entry["最近复验单"] = result["session"]
            return entry, result["message"]
        return super().run_action(entry_id, action, values)
