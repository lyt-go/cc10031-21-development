"""标定复验链路：换算出处索引、窗口续查、按提交记录重开与单点复验。

场景对齐真实值班过程：

* 同一窗口里两次标定各覆盖半段，并在第三点（10:02）以闭区间交叠；
* 旧版复验单只核完前两点（10:00、10:01），边界第三点按旧版为 1.2 V，
  留在待续查范围；
* 复验单进入续查、交给质量复验之前，同一第三点已获准采用的更正版为 2.4 V，
  第四点（10:03）也补齐了；
* 让同一张单执行续查，核对它实际读到的第三点与规则；
* 再按提交记录重开前两点，并另发起第四点的复验，确认各次实际计算的范围与量纲。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


POINT = "SENS-RV1"
# 四分钟窗口，逐分钟采样；源数据单位是 mV。
SAMPLES = {
    "2026-09-01T10:00:00": 980.0,    # 第一点：前两点已复验
    "2026-09-01T10:01:00": 1100.0,   # 第二点：前两点已复验
    "2026-09-01T10:02:00": 1200.0,   # 边界第三点：旧版 0.001 → 1.2 V，更正版 0.002 → 2.4 V
    "2026-09-01T10:03:00": 1560.0,   # 第四点：第二段补完的窗口尾段
}
OLD = "UNIT-RV-OLD@v1"
NEW = "UNIT-RV-NEW@v2"


@pytest.fixture()
def data_dir(tmp_path: Path):
    with patch("app.services.base.DATA_ROOT", tmp_path):
        for name in [
            "conversion_index", "unit_calibrate", "reverify_session",
            "series_lineage", "time_window",
        ]:
            module = __import__(f"app.services.{name}", fromlist=["DATA_ROOT"])
            if hasattr(module, "DATA_ROOT"):
                module.DATA_ROOT = tmp_path
        yield tmp_path


@pytest.fixture()
def rig(data_dir):
    """两次标定：旧版覆盖前三点，更正版覆盖边界三点与尾段第四点。"""
    from app.services.unit_calibrate import UnitCalibrateService
    from app.store import store

    edge_dir = data_dir / "edge_samples"
    edge_dir.mkdir(parents=True, exist_ok=True)
    with open(edge_dir / f"{POINT}.jsonl", "w", encoding="utf-8") as handle:
        for timestamp, raw in SAMPLES.items():
            handle.write(json.dumps({
                "point": POINT, "timestamp": timestamp,
                "raw_value": raw, "unit": "mV",
            }, ensure_ascii=False) + "\n")

    rows = store.rows("unit_calibrate")
    rows[:] = [row for row in rows if row.get("测点编号") != POINT]
    base_id = max((int(row.get("id", 0)) for row in rows), default=0)
    first = {
        "id": base_id + 1,
        "标定编号": "UNIT-RV-OLD", "测点编号": POINT,
        "原始单位": "mV", "目标单位": "V", "换算系数": "0.001", "零点偏移": "0",
        "标定方式": "线性换算", "标定版本": "v1", "换算出处": "台账-旧版换算",
        # 旧版覆盖前三点，第三点是与更正版交叠的边界。
        "生效起点": "2026-09-01T10:00:00", "生效终点": "2026-09-01T10:02:00",
        "标定时间": "2026-09-01T09:00:00", "status": "已标定",
        "pending": False, "abnormal": False,
    }
    second = {
        "id": base_id + 2,
        "标定编号": "UNIT-RV-NEW", "测点编号": POINT,
        "原始单位": "mV", "目标单位": "V", "换算系数": "0.002", "零点偏移": "0",
        "标定方式": "线性换算", "标定版本": "v2", "换算出处": "复检-更正换算",
        # 更正版从边界第三点起覆盖到尾段第四点。
        "生效起点": "2026-09-01T10:02:00", "生效终点": "2026-09-01T10:03:00",
        "标定时间": "2026-09-01T11:30:00", "status": "已标定",
        "pending": False, "abnormal": False,
    }
    rows.extend([first, second])

    service = UnitCalibrateService()
    return {
        "old": first, "new": second,
        "old_index": service.build_conversion_index(first["id"]),
        "new_index": service.build_conversion_index(second["id"]),
    }


def _samples_of(svc, session_id: int) -> list[dict]:
    return svc.runs(session_id)[-1]["samples"]


def _value(samples: list[dict], moment: str) -> dict:
    for sample in samples:
        if sample["timestamp"] == moment:
            return sample
    raise AssertionError(f"{moment} 不在本轮结果里：{samples}")


def test_index_registers_each_version_with_units_and_reference(rig):
    assert rig["old_index"]["reference"] == OLD
    assert rig["new_index"]["reference"] == NEW
    # 旧版三点、更正版两点，边界第三点两边各记一次。
    assert rig["old_index"]["samples_indexed"] == 3
    assert rig["new_index"]["samples_indexed"] == 2
    assert rig["new_index"]["unit"] == "V"
    assert rig["old_index"]["effective_end"] == "2026-09-01T10:02:00"


def test_locate_filters_by_point_unit_window_and_rule(rig):
    from app.services.series_lineage import SeriesLineageService

    svc = SeriesLineageService()
    by_point = svc.locate_series(point=POINT)
    assert by_point["total"] == 2
    assert {item["reference"] for item in by_point["items"]} == {OLD, NEW}

    # 尾段 [10:02, 10:03] 里，更正版命中两点；旧版只命中边界第三点，
    # 因为边界第三点同时落在两次标定的闭区间上。
    tail = svc.locate_series(point=POINT, start="2026-09-01T10:02:00",
                             end="2026-09-01T10:03:00")
    assert tail["total"] == 2
    counts = {item["reference"]: item["matched_count"] for item in tail["items"]}
    assert counts == {OLD: 1, NEW: 2}
    new_hit = next(item for item in tail["items"] if item["reference"] == NEW)
    assert {sample["timestamp"] for sample in new_hit["samples"]} == {
        "2026-09-01T10:02:00", "2026-09-01T10:03:00"}

    by_rule = svc.locate_series(point=POINT, rule="UNIT-RV-OLD")
    assert by_rule["total"] == 1
    assert by_rule["items"][0]["version"] == "v1"

    by_unit = svc.locate_series(point=POINT, unit="V")
    assert by_unit["total"] == 2
    assert by_unit["items"][0]["artifact"].endswith("_calibrated.jsonl")


def test_session_reviews_front_two_points_and_leaves_boundary_pending(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    session, message = svc.open_session({
        "测点编号": POINT,
        "复验规则": "标定引用",
        "标定引用": OLD,
        "起始时刻": "2026-09-01T10:00:00",
        "截止时刻": "2026-09-01T10:02:00",
        "计量单位": "V",
    })
    assert session is not None, message
    assert session["采用范围"] == "2026-09-01T10:00:00 ~ 2026-09-01T10:03:00"

    # 只提交前两点：10:00、10:01。
    submitted, message = svc.run_action(
        session["id"], "提交复验",
        {"起始时刻": "2026-09-01T10:00:00", "截止时刻": "2026-09-01T10:01:00"},
    )
    assert submitted is not None, message
    run = svc.runs(session["id"])[-1]
    assert run["scope"] == {"start": "2026-09-01T10:00:00", "end": "2026-09-01T10:02:00"}
    assert _value(run["samples"], "2026-09-01T10:00:00")["value"] == pytest.approx(0.98)
    assert _value(run["samples"], "2026-09-01T10:01:00")["value"] == pytest.approx(1.1)
    assert submitted["已核时段"] == "2026-09-01T10:00:00 ~ 2026-09-01T10:02:00"
    # 边界第三点仍在待续查范围内。
    assert submitted["待续查时段"] == "2026-09-01T10:02:00 ~ 2026-09-01T10:03:00"
    assert submitted["status"] == "待续查"

    blocked, message = svc.run_action(session["id"], "标记完成", {})
    assert blocked is None
    assert "待续查" in message


def test_continue_reads_newly_approved_boundary_value_and_rule(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    session, _ = svc.open_session({
        "测点编号": POINT, "复验规则": "标定引用", "标定引用": OLD,
        "起始时刻": "2026-09-01T10:00:00", "截止时刻": "2026-09-01T10:02:00",
        "计量单位": "V",
    })
    svc.run_action(session["id"], "提交复验",
                   {"起始时刻": "2026-09-01T10:00:00", "截止时刻": "2026-09-01T10:01:00"})

    # 续查同一张单：边界第三点已获准采用的更正版胜出 → 2.4 V。
    continued, message = svc.run_action(
        session["id"], "续查", {"样本时刻": "2026-09-01T10:02:00"})
    assert continued is not None, message
    run = svc.runs(session["id"])[-1]
    assert run["action"] == "续查"
    assert run["scope"] == {"start": "2026-09-01T10:02:00", "end": "2026-09-01T10:03:00"}
    boundary = _value(run["samples"], "2026-09-01T10:02:00")
    assert boundary["value"] == pytest.approx(2.4)
    assert boundary["unit"] == "V"
    assert boundary["source_unit"] == "mV"
    assert boundary["reference"] == NEW
    assert boundary["scale"] == pytest.approx(0.002)
    # 运行快照保留边界两个候选，说明为什么取更正版。
    overlap = run["overlaps"][0]
    assert overlap["chosen"] == NEW
    assert {item["reference"] for item in overlap["candidates"]} == {OLD, NEW}
    assert continued["status"] == "已复验"
    assert continued["待续查时段"] == "无"
    # 第三点只算了一次：续查范围不重算前两点。
    assert run["sample_count"] == 1


def test_reopen_from_run_restores_old_reading(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    session, _ = svc.open_session({
        "测点编号": POINT, "复验规则": "标定引用", "标定引用": OLD,
        "起始时刻": "2026-09-01T10:00:00", "截止时刻": "2026-09-01T10:02:00",
        "计量单位": "V",
    })
    svc.run_action(session["id"], "提交复验",
                   {"起始时刻": "2026-09-01T10:00:00", "截止时刻": "2026-09-01T10:01:00"})
    svc.run_action(session["id"], "续查", {"样本时刻": "2026-09-01T10:02:00"})
    assert session["status"] == "已复验"
    assert len(svc.runs(session["id"])) == 2

    # 按 RUN-1 重开前两点：引用仍是旧版，范围与单位按当时的提交记录。
    reopened, message = svc.run_action(
        session["id"], "按提交记录重开", {"提交记录": 1})
    assert reopened is not None, message
    assert reopened["标定引用"] == OLD
    assert reopened["采用范围"] == "2026-09-01T10:00:00 ~ 2026-09-01T10:02:00"
    assert reopened["计量单位"] == "V"
    assert reopened["重开自"] == f"{session['复验编号']}#RUN-1"
    assert reopened["父复验编号"] == session["复验编号"]

    rerun, message = svc.run_action(reopened["id"], "提交复验", {})
    assert rerun is not None, message
    samples = _samples_of(svc, reopened["id"])
    assert {sample["timestamp"] for sample in samples} == {
        "2026-09-01T10:00:00", "2026-09-01T10:01:00"}
    assert _value(samples, "2026-09-01T10:00:00")["value"] == pytest.approx(0.98)
    assert _value(samples, "2026-09-01T10:01:00")["value"] == pytest.approx(1.1)
    assert all(sample["reference"] == OLD for sample in samples)

    # 按 RUN-2 重开边界点：钉住原引用后回到旧版 1.2 V，而不是新版的 2.4 V。
    boundary_child, message = svc.run_action(
        session["id"], "按提交记录重开", {"提交记录": 2})
    assert boundary_child is not None, message
    assert boundary_child["采用范围"] == "2026-09-01T10:02:00 ~ 2026-09-01T10:03:00"
    svc.run_action(boundary_child["id"], "提交复验", {})
    boundary = _value(_samples_of(svc, boundary_child["id"]), "2026-09-01T10:02:00")
    assert boundary["value"] == pytest.approx(1.2)
    assert boundary["reference"] == OLD
    assert boundary["scale"] == pytest.approx(0.001)


def test_reverify_single_point_for_new_tail_sample(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    session, _ = svc.open_session({
        "测点编号": POINT, "复验规则": "标定引用", "标定引用": OLD,
        "起始时刻": "2026-09-01T10:00:00", "截止时刻": "2026-09-01T10:02:00",
        "计量单位": "V",
    })
    child, message = svc.run_action(
        session["id"], "单点复验",
        {"样本时刻": "2026-09-01T10:03:00", "标定引用": NEW, "计量单位": "V"})
    assert child is not None, message
    assert child["采用范围"] == "2026-09-01T10:03:00 ~ 2026-09-01T10:04:00"
    assert child["单点时刻"] == "2026-09-01T10:03:00"
    assert child["父复验编号"] == session["复验编号"]

    result, message = svc.run_action(child["id"], "提交复验", {})
    assert result is not None, message
    run = svc.runs(child["id"])[-1]
    assert run["scope"] == {"start": "2026-09-01T10:03:00", "end": "2026-09-01T10:04:00"}
    assert run["sample_count"] == 1
    sample = run["samples"][0]
    assert sample["timestamp"] == "2026-09-01T10:03:00"
    assert sample["value"] == pytest.approx(3.12)   # 1560 mV × 0.002
    assert sample["unit"] == "V"
    assert sample["source_unit"] == "mV"
    assert sample["reference"] == NEW
    assert result["status"] == "已复验"


def test_completed_session_refuses_further_continue(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    session, _ = svc.open_session({
        "测点编号": POINT, "复验规则": "标定引用", "标定引用": NEW,
        "起始时刻": "2026-09-01T10:02:00", "截止时刻": "2026-09-01T10:03:00",
        "计量单位": "V",
    })
    # 两段两次处理到完成。
    done, message = svc.run_action(session["id"], "提交复验",
                                   {"样本时刻": "2026-09-01T10:02:00"})
    assert done is not None, message
    assert done["status"] == "待续查"
    done, message = svc.run_action(session["id"], "续查",
                                   {"样本时刻": "2026-09-01T10:03:00"})
    assert done is not None, message
    assert done["status"] == "已复验"

    refused, message = svc.run_action(session["id"], "续查", {})
    assert refused is None
    assert "另起复验单" in message


def test_lineage_records_reference_and_can_launch_reverify(rig):
    from app.services.series_lineage import SeriesLineageService
    from app.services.reverify_session import ReverifySessionService
    from app.store import store

    lineage_svc = SeriesLineageService()
    built = lineage_svc.build_lineage(POINT)
    assert built["calibrate"] == NEW

    row = next(item for item in store.rows("series_lineage")
               if item.get("链路编号") == built["lineage_id"])
    entry, message = lineage_svc.run_action(
        row["id"], "发起复验",
        {"复验规则": NEW, "起始时刻": "2026-09-01T10:02:00",
         "截止时刻": "2026-09-01T10:03:00", "计量单位": "V"},
    )
    assert entry is not None, message
    assert entry["最近复验单"]

    session = next(item for item in store.rows("reverify_session")
                   if item.get("复验编号") == entry["最近复验单"])
    assert session["标定引用"] == NEW
    assert session["来源血缘"] == built["lineage_id"]
    assert session["采用范围"] == "2026-09-01T10:02:00 ~ 2026-09-01T10:04:00"
    assert ReverifySessionService().runs(session["id"]) == []


def test_calibrate_values_keeps_each_version_indexed(rig, data_dir):
    from app.services import conversion_index as ci
    from app.services.unit_calibrate import UnitCalibrateService
    from app.store import store

    rows = store.rows("unit_calibrate")
    third = {
        "id": max(int(row.get("id", 0)) for row in rows) + 1,
        "标定编号": "UNIT-RV-THIRD", "测点编号": POINT,
        "原始单位": "mV", "目标单位": "V", "换算系数": "0.0015", "零点偏移": "0",
        "标定方式": "线性换算", "标定版本": "v3", "换算出处": "夜班-补充换算",
        "生效起点": "2026-09-01T10:03:00", "生效终点": "2026-09-01T10:03:00",
        "标定时间": "2026-09-01T12:00:00", "status": "待标定",
        "pending": True, "abnormal": False,
    }
    rows.append(third)
    result = UnitCalibrateService().calibrate_values(third["id"])
    assert result["records_calibrated"] == len(SAMPLES)

    entries = ci.load_entries(data_dir, POINT)
    assert {entry["version"] for entry in entries} == {"v1", "v2", "v3"}
    # 旧版按原引用仍可回看，数值不随后来的版本漂移。
    old = next(entry for entry in entries if entry["version"] == "v1")
    assert old["reference"] == OLD
    assert old["samples"][0]["value"] == pytest.approx(0.98)
    # 血缘没有被"最新即唯一"覆盖：旧引用仍在索引里可定位。
    located = ci.locate(data_dir, point=POINT, rule="UNIT-RV-OLD")
    assert [item["reference"] for item in located] == [OLD]
