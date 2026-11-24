"""单位标定领域服务：维护传感器原始值到目标计量单位的换算系数与零点偏移标定。

每次确认标定都会把**实际采用的样本**登记成一条换算出处索引：记录标定版本、
生效区间、原始单位、换算后的计量单位、换算规则与来源。同一测点可以并存多个
版本，索引按版本分条保留，后一次标定不会覆盖前一次，历史血缘仍可按原引用核对。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services import conversion_index as ci
from app.services import time_window as tw
from app.services.base import BaseModuleService
from app.store import store


class UnitCalibrateService(BaseModuleService):
    MODULE = "unit_calibrate"
    ENTITY = "标定记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "标定编号", "测点编号", "原始单位", "目标单位", "换算系数", "零点偏移",
        "标定方式", "标定版本", "换算出处", "生效起点", "生效终点", "标定时间",
    ]
    EXTRA_FIELDS = ["标定版本", "换算出处", "生效起点", "生效终点"]
    REQUIRED_FIELDS = ["标定编号", "测点编号", "原始单位"]
    STATUS_ORDER = ["草稿", "待标定", "已标定", "已失效"]
    ACTION_RULES = {
        "提交标定": "待标定",
        "确认标定": "已标定",
        "标记失效": "已失效",
    }
    NEGATIVE_ACTIONS = ["标记失效"]

    # -- 源样本读取与索引登记 --

    def _read_source_samples(self, point_key: str) -> list[dict[str, Any]]:
        """读取测点的边缘采样，统一成 ``timestamp / raw_value / unit`` 三元组。"""
        path = self._data_dir("edge_samples") / f"{ci.safe_key(point_key)}.jsonl"
        samples: list[dict[str, Any]] = []
        for raw in self._read_jsonl(path):
            value = raw.get("raw_value", raw.get("raw_value_mV"))
            if value is None:
                continue
            samples.append({
                "timestamp": tw.format_time(raw.get("timestamp", "")),
                "raw_value": float(value),
                "unit": str(raw.get("unit") or "mV"),
            })
        samples.sort(key=lambda item: item["timestamp"])
        return samples

    def _effective_window(
        self, calibration: dict[str, Any], samples: list[dict[str, Any]],
    ) -> tuple[str, str]:
        """标定生效区间是闭区间；未显式给出时取该批源样本的首尾时刻。"""
        start = tw.format_time(calibration.get("生效起点", "")) or (
            samples[0]["timestamp"] if samples else "")
        end = tw.format_time(calibration.get("生效终点", "")) or (
            samples[-1]["timestamp"] if samples else "")
        calibration["生效起点"] = start
        calibration["生效终点"] = end
        return start, end

    def build_conversion_index(self, calibrate_id: int) -> dict[str, Any]:
        """按标定记录重建换算出处索引，并落一份不可变的来源快照。"""
        calibration = store.find(self.MODULE, calibrate_id)
        if calibration is None:
            return {"error": f"标定记录 {calibrate_id} 不存在"}
        return self.register_source_row(calibration)

    def register_source_row(self, calibration: dict[str, Any]) -> dict[str, Any]:
        """按已取到的标定条目登记索引；新建流程与按 id 重建共用这一条路径。"""
        point_key = str(calibration["测点编号"])
        samples = self._read_source_samples(point_key)
        start, end = self._effective_window(calibration, samples)
        calibration.setdefault("标定版本", "v1")
        calibration.setdefault("换算出处", calibration.get("标定编号", ""))
        in_window = [
            sample for sample in samples
            if tw.in_closed_period(sample["timestamp"], start, end)
        ]
        entry = ci.build_entry(calibration, in_window)
        ci.save_entry(self._data_dir("."), entry)
        # 来源快照：记录这次换算真正读到的原始样本，后续复验可据此追溯。
        # 同一「标定编号 + 版本」重建时整份覆盖，避免重复登记把行数越叠越多。
        snapshot_path = ci.source_path(
            self._data_dir("."), str(calibration.get("标定编号", "")),
            ci.version_of(calibration),
        )
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with open(snapshot_path, "w", encoding="utf-8") as handle:
            for sample in in_window:
                handle.write(json.dumps({
                    "reference": entry["reference"],
                    "calibrate_id": entry["calibrate_id"],
                    "version": entry["version"],
                    "point": entry["point"],
                    "source": entry["source"],
                    "unit": entry["unit"],
                    "source_unit": entry["source_unit"],
                    "scale": entry["scale"],
                    "offset": entry["offset"],
                    "effective_start": entry["effective_start"],
                    "effective_end": entry["effective_end"],
                    "timestamp": sample["timestamp"],
                    "raw_value": sample["raw_value"],
                    "raw_unit": sample["unit"],
                    "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                }, ensure_ascii=False, default=str) + "\n")
        calibration["样本索引条数"] = str(len(entry["samples"]))
        # 边界交叠：同一时刻被多次标定覆盖的样本，记录每个候选版本。
        overlaps = self.boundary_overlaps(point_key)
        return {
            "index_id": entry["index_id"],
            "reference": entry["reference"],
            "point": point_key,
            "unit": entry["unit"],
            "samples_indexed": len(entry["samples"]),
            "effective_start": entry["effective_start"],
            "effective_end": entry["effective_end"],
            "boundary_overlaps": [item for item in overlaps if item["timestamp"] in {
                sample["timestamp"] for sample in entry["samples"]}],
        }

    def boundary_overlaps(self, point_key: str) -> list[dict[str, Any]]:
        """列出该测点上被多个标定版本同时覆盖的时刻，用于说明边界取值。"""
        entries = ci.load_entries(self._data_dir("."), point_key)
        seen: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            for sample in entry.get("samples", []):
                seen.setdefault(sample["timestamp"], []).append({
                    "reference": entry.get("reference"),
                    "calibrate_id": entry.get("calibrate_id"),
                    "version": entry.get("version"),
                    "source": entry.get("source"),
                    "value": sample.get("value"),
                    "unit": sample.get("unit"),
                })
        return [
            {"timestamp": timestamp, "candidates": candidates}
            for timestamp, candidates in sorted(seen.items()) if len(candidates) > 1
        ]

    # -- 标定执行 --

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记标定前先查重：同一「标定编号 + 标定版本」只能有一条，引用才唯一。"""
        calibrate_id = str(values.get("标定编号") or "").strip()
        version = str(values.get("标定版本") or "v1").strip()
        if calibrate_id:
            duplicate = next(
                (row for row in store.rows(self.MODULE)
                 if str(row.get("标定编号")) == calibrate_id
                 and str(row.get("标定版本") or "v1") == version),
                None,
            )
            if duplicate is not None:
                return None, [f"标定编号 {calibrate_id} 的 {version} 已存在（id={duplicate['id']}）"]
        return super().create_entry(values)

    def calibrate_values(self, calibrate_id: int) -> dict[str, Any]:
        """对标定记录的原始数据进行单位换算计算并写入标定结果归档文件。"""
        cal = store.find(self.MODULE, calibrate_id)
        if cal is None:
            return {"error": f"标定记录 {calibrate_id} 不存在"}
        entry, _ = self.run_action(calibrate_id, "确认标定")
        point_key = entry["测点编号"]
        scale = float(entry.get("换算系数", "1"))
        offset = float(entry.get("零点偏移", "0"))
        # 读取对应测点的边缘采样数据
        samples_file = self._data_dir("edge_samples") / f"{ci.safe_key(point_key)}.jsonl"
        samples = self._read_jsonl(samples_file)
        # 执行单位标定计算：原始值 × 换算系数 + 零点偏移
        calibrated = []
        for s in samples:
            raw = float(s.get("raw_value", s.get("raw_value_mV", 0)))
            cal_val = round(raw * scale + offset, 4)
            calibrated.append({
                "point": s.get("point", point_key),
                "timestamp": s["timestamp"],
                "raw_value_mV": raw,
                "calibrated_value_V": cal_val,
                "scale": scale,
                "offset": offset,
                # 数据转换输出：将标定计算结果写入持久化标定数据JSONL文件存储
                "calibrate_id": entry["标定编号"],
                "calibrate_version": ci.version_of(entry),
                "unit": entry.get("目标单位", "V"),
            })
        # 将标定结果写入持久化标定数据文件
        output_file = self._data_dir("calibrated") / f"{ci.safe_key(point_key)}_calibrated.jsonl"
        written = self._write_jsonl(output_file, calibrated)
        entry["标定时间"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        # 标定生效即登记换算出处样本索引，供血缘与复验按出处定位。
        index_result = self.register_source_row(entry)
        return {"calibrate_id": entry["标定编号"], "point": point_key,
                "records_calibrated": written, "scale": scale, "offset": offset,
                "index": index_result}

    def register_source(self, calibrate_id: int) -> dict[str, Any]:
        """不改标定状态，仅按已保存的标定信息重建换算出处索引。"""
        return self.build_conversion_index(calibrate_id)

    def batch_calibrate(self, point_keys: list[str]) -> dict[str, Any]:
        """批量执行多个测点的单位标定转换，返回标定汇总数据集。"""
        results = []
        for pk in point_keys:
            cals = [r for r in store.rows(self.MODULE)
                    if r.get("测点编号") == pk and r.get("status") in ("待标定", "已标定")]
            for cal in cals:
                r = self.calibrate_values(cal["id"])
                results.append(r)
        # 写入批量标定汇总日志文件
        self._write_jsonl(
            self._data_dir("calibrated") / "batch_calibrate_summary.jsonl",
            [{"action": "batch_calibrate", "points": len(point_keys),
              "executed": len(results), "at": time.time()}]
        )
        return {"total_points": len(point_keys), "executed": len(results), "results": results}

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """登记换算出处动作要真正建索引；其余动作沿用统一状态流转。"""
        if action == "登记换算出处":
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            result = self.register_source(entry_id)
            if "error" in result:
                return None, result["error"]
            return entry, (
                f"已登记换算出处 {result['reference']}，"
                f"索引 {result['samples_indexed']} 条样本"
            )
        return super().run_action(entry_id, action, values)
