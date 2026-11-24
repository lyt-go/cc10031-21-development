"""换算出处样本索引：把每次标定实际采用的样本、计量单位与采集区间登记成可检索的条目。

索引条目按 ``测点 + 标定记录 + 标定版本`` 唯一，条目内的样本保留原始单位、
换算后的计量单位、换算规则与来源单据。血缘查询按测点、实际单位、采集区间
和换算规则定位时序产物时，直接消费这份索引，而不是再次扫描原始文件。

索引是派生数据，可以按标定记录重建；真正不可变的是复验单每次提交时落盘的
运行快照，历史核对以那份快照里的引用为准。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from app.services import time_window as tw

INDEX_DIR = "sample_index"
SOURCE_DIR = "conversion_sources"
_OPEN_START = "0001-01-01T00:00:00"
_OPEN_END = "9999-12-31T23:59:59"


def safe_key(value: Any) -> str:
    """把编号里的路径分隔符等字符收拢，避免索引文件名逃出数据目录。"""
    return re.sub(r"[^0-9A-Za-z_.\-一-鿿]+", "_", str(value or "")).strip("_") or "unnamed"


def index_path(data_dir: Path, point: str) -> Path:
    return data_dir / INDEX_DIR / f"{safe_key(point)}_conversion_index.jsonl"


def source_path(data_dir: Path, calibrate_id: str, version: str) -> Path:
    return data_dir / SOURCE_DIR / f"{safe_key(calibrate_id)}_{safe_key(version)}.jsonl"


def index_id(point: str, calibrate_id: str, version: str) -> str:
    return f"{point}:{calibrate_id}:{version}"


def version_of(calibration: dict[str, Any]) -> str:
    return str(calibration.get("标定版本") or "v1")


def source_of(calibration: dict[str, Any]) -> str:
    return str(calibration.get("换算出处") or calibration.get("标定编号") or "")


def reference_of(calibration: dict[str, Any]) -> str:
    """血缘里保存的原引用：标定编号 + 版本，两者一起才能定位到唯一换算资料。"""
    return f"{calibration.get('标定编号', '')}@{version_of(calibration)}"


def display_reference(entry: dict[str, Any]) -> str:
    return f"{entry.get('calibrate_id', '')}@{entry.get('version', '')}"


def load_entries(data_dir: Path, point: str | None = None) -> list[dict[str, Any]]:
    """读取测点索引；point 为空时扫描全部测点索引文件。"""
    if point:
        files = [index_path(data_dir, point)]
    else:
        directory = data_dir / INDEX_DIR
        files = sorted(directory.glob("*_conversion_index.jsonl")) if directory.exists() else []
    entries: list[dict[str, Any]] = []
    for path in files:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_entry(data_dir: Path, entry: dict[str, Any]) -> Path:
    """按 index_id 覆盖写入单条索引，保留同一测点的其他版本。"""
    path = index_path(data_dir, entry["point"])
    existing = [
        item for item in load_entries(data_dir, entry["point"])
        if item.get("index_id") != entry.get("index_id")
    ]
    existing.append(entry)
    existing.sort(key=lambda item: (str(item.get("effective_start", "")), str(item.get("version", ""))))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for item in existing:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    return path


def _sample(
    raw: dict[str, Any],
    *,
    point: str,
    unit: str,
    scale: float,
    offset: float,
    calibrate_id: str,
    version: str,
    source: str,
) -> dict[str, Any]:
    # 索引里只存原始采样值；换算后的值单独放 value，避免 mV 与 V 混淆。
    raw_value = float(raw.get("raw_value", 0) or 0)
    converted = round(raw_value * scale + offset, 6)
    timestamp = tw.format_time(raw.get("timestamp", ""))
    return {
        "timestamp": timestamp,
        "point": point,
        "raw_value": raw_value,
        "source_unit": str(raw.get("unit") or "mV"),
        "value": converted,
        "unit": unit,
        "scale": scale,
        "offset": offset,
        "rule": reference_of({"标定编号": calibrate_id, "标定版本": version}),
        "calibrate_id": calibrate_id,
        "version": version,
        "source": source,
    }


def build_entry(calibration: dict[str, Any], source_samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """由标定记录和已筛过的原始样本生成一条不可混淆的换算出处索引条目。"""
    point = str(calibration.get("测点编号", ""))
    calibrate_id = str(calibration.get("标定编号", ""))
    version = version_of(calibration)
    unit = str(calibration.get("目标单位") or "V")
    scale = float(calibration.get("换算系数") or 1)
    offset = float(calibration.get("零点偏移") or 0)
    source = source_of(calibration)
    samples = [
        _sample(raw, point=point, unit=unit, scale=scale, offset=offset,
                calibrate_id=calibrate_id, version=version, source=source)
        for raw in source_samples
    ]
    samples.sort(key=lambda item: item["timestamp"])
    return {
        "index_id": index_id(point, calibrate_id, version),
        "point": point,
        "unit": unit,
        "source_unit": samples[0]["source_unit"] if samples else "mV",
        "calibrate_id": calibrate_id,
        "version": version,
        "source": source,
        "reference": reference_of(calibration),
        "scale": scale,
        "offset": offset,
        "effective_start": tw.format_time(calibration.get("生效起点", "")),
        "effective_end": tw.format_time(calibration.get("生效终点", "")),
        "start": samples[0]["timestamp"] if samples else "",
        "end": samples[-1]["timestamp"] if samples else "",
        "sample_count": len(samples),
        "samples": samples,
    }


def _window(start: Any, end: Any) -> dict[str, str]:
    return {
        "start": tw.format_time(start) if start else _OPEN_START,
        "end": tw.format_time(end) if end else _OPEN_END,
    }


def matches_rule(entry: dict[str, Any], rule: str) -> bool:
    needle = str(rule or "").strip().casefold()
    if not needle:
        return True
    haystacks = [
        entry.get("calibrate_id", ""),
        entry.get("version", ""),
        entry.get("reference", ""),
        entry.get("source", ""),
    ]
    return any(needle in str(value).casefold() for value in haystacks)


def locate(
    data_dir: Path,
    *,
    point: str | None = None,
    unit: str | None = None,
    start: Any = None,
    end: Any = None,
    rule: str | None = None,
) -> list[dict[str, Any]]:
    """按测点、实际单位、采集区间与换算规则定位时序产物。

    区间按半开 ``[start, end)`` 过滤样本；只给出单边时另一边保持开放。
    命中的条目会带上裁剪后的样本、命中区间与产物路径，便于血缘查询直接展示。
    返回项一律带 ``artifact`` 与 ``reference``，可直接跳到下次复验。
    """
    window = _window(start, end)
    located: list[dict[str, Any]] = []
    for entry in load_entries(data_dir, point):
        if point and entry.get("point") != point:
            continue
        if unit and str(entry.get("unit")) != str(unit):
            continue
        if not matches_rule(entry, rule or ""):
            continue
        matched = [
            sample for sample in entry.get("samples", [])
            if tw.in_period(sample.get("timestamp"), window)
        ]
        if (start or end) and not matched:
            continue
        hit = dict(entry)
        hit["samples"] = matched
        hit["matched_count"] = len(matched)
        hit["window"] = {"start": tw.format_time(start) if start else "",
                         "end": tw.format_time(end) if end else ""}
        hit["artifact"] = f"calibrated/{entry.get('point')}_calibrated.jsonl"
        located.append(hit)
    located.sort(key=lambda item: (str(item.get("effective_start", "")), str(item.get("version", ""))))
    return located


def rank(entry: dict[str, Any]) -> tuple[str, str, int]:
    """择优排序：生效起点更晚、版本更晚、编号更大的换算资料优先。"""
    tail = str(entry.get("calibrate_id", "")).rsplit("-", 1)[-1]
    sequence = int(tail) if tail.isdigit() else 0
    return (str(entry.get("effective_start", "")), str(entry.get("version", "")), sequence)


def collect_samples(
    entries: Iterable[dict[str, Any]],
    period: dict[str, Any],
    *,
    pin_reference: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """收集时段内的样本，同一时刻按 ``rank`` 择优并记录边界交叠候选。

    返回 ``(样本列表, 交叠记录)``。默认按 ``effective_start → version → 编号``
    择优，因此后来获准的更正版会在边界处胜出；``pin_reference`` 用于"按提交记录
    重开"：钉住某个原引用时，只用该引用自己的样本，等价于回到旧版当时读数。
    交叠记录始终保留全部候选，复验单据此说明为什么取了这个值。
    """
    by_timestamp: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        for sample in entry.get("samples", []):
            if not tw.in_period(sample.get("timestamp"), period):
                continue
            record = dict(sample)
            record["reference"] = entry.get("reference") or display_reference(entry)
            record["_rank"] = rank(entry)
            by_timestamp.setdefault(record["timestamp"], []).append(record)
    chosen: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    for timestamp in sorted(by_timestamp):
        candidates = sorted(by_timestamp[timestamp], key=lambda item: item["_rank"])
        pool = candidates
        if pin_reference:
            pinned = [item for item in candidates if item["reference"] == pin_reference]
            if pinned:
                pool = pinned
        winner = dict(pool[-1])
        winner.pop("_rank", None)
        chosen.append(winner)
        if len(candidates) > 1:
            overlaps.append({
                "timestamp": timestamp,
                "chosen": winner["reference"],
                "chosen_value": winner["value"],
                "unit": winner["unit"],
                "pinned": bool(pin_reference),
                "candidates": [
                    {
                        "reference": item["reference"],
                        "value": item["value"],
                        "unit": item["unit"],
                        "source": item.get("source"),
                        "calibrate_id": item.get("calibrate_id"),
                        "version": item.get("version"),
                    }
                    for item in candidates
                ],
            })
    return chosen, overlaps
