"""复验单边界条件：越界范围、单位不符、重复提交与已完成单的隔离。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

POINT = "SENS-RV2"
OLD = "UNIT-RV2-OLD@v1"
NEW = "UNIT-RV2-NEW@v2"
SAMPLES = {
    "2026-09-02T09:00:00": 1000.0,
    "2026-09-02T09:01:00": 1200.0,
    "2026-09-02T09:02:00": 1400.0,
}


@pytest.fixture()
def rig(tmp_path: Path):
    with patch("app.services.base.DATA_ROOT", tmp_path):
        for name in [
            "conversion_index", "unit_calibrate", "reverify_session",
            "series_lineage", "time_window",
        ]:
            module = __import__(f"app.services.{name}", fromlist=["DATA_ROOT"])
            if hasattr(module, "DATA_ROOT"):
                module.DATA_ROOT = tmp_path
        edge_dir = tmp_path / "edge_samples"
        edge_dir.mkdir(parents=True, exist_ok=True)
        with open(edge_dir / f"{POINT}.jsonl", "w", encoding="utf-8") as handle:
            for timestamp, raw in SAMPLES.items():
                handle.write(json.dumps({
                    "point": POINT, "timestamp": timestamp,
                    "raw_value": raw, "unit": "mV",
                }, ensure_ascii=False) + "\n")

        from app.services.unit_calibrate import UnitCalibrateService
        from app.store import store

        rows = store.rows("unit_calibrate")
        rows[:] = [row for row in rows if row.get("测点编号") != POINT]
        base = max((int(row.get("id", 0)) for row in rows), default=0)
        first = {
            "id": base + 1, "标定编号": "UNIT-RV2-OLD", "测点编号": POINT,
            "原始单位": "mV", "目标单位": "V", "换算系数": "0.001", "零点偏移": "0",
            "标定方式": "线性换算", "标定版本": "v1", "换算出处": "台账-旧版",
            "生效起点": "2026-09-02T09:00:00", "生效终点": "2026-09-02T09:01:00",
            "标定时间": "2026-09-02T08:00:00", "status": "已标定",
            "pending": False, "abnormal": False,
        }
        second = {
            "id": base + 2, "标定编号": "UNIT-RV2-NEW", "测点编号": POINT,
            "原始单位": "mV", "目标单位": "V", "换算系数": "0.002", "零点偏移": "0",
            "标定方式": "线性换算", "标定版本": "v2", "换算出处": "复检-更正",
            "生效起点": "2026-09-02T09:01:00", "生效终点": "2026-09-02T09:02:00",
            "标定时间": "2026-09-02T10:00:00", "status": "已标定",
            "pending": False, "abnormal": False,
        }
        rows.extend([first, second])
        service = UnitCalibrateService()
        service.build_conversion_index(first["id"])
        service.build_conversion_index(second["id"])
        yield


def _open(svc, **overrides):
    values = {
        "测点编号": POINT, "复验规则": "标定引用", "标定引用": OLD,
        "起始时刻": "2026-09-02T09:00:00", "截止时刻": "2026-09-02T09:02:00",
        "计量单位": "V",
    }
    values.update(overrides)
    session, message = svc.open_session(values)
    assert session is not None, message
    return session


def test_scope_outside_pending_is_rejected(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    session = _open(svc)
    svc.run_action(session["id"], "提交复验",
                   {"起始时刻": "2026-09-02T09:00:00", "截止时刻": "2026-09-02T09:00:00"})
    assert session["待续查时段"] == "2026-09-02T09:01:00 ~ 2026-09-02T09:02:00"
    # 已核的前段不能再核；越界到窗口外也不行。
    for values in (
        {"起始时刻": "2026-09-02T09:00:00", "截止时刻": "2026-09-02T09:00:00"},
        {"起始时刻": "2026-09-02T08:00:00", "截止时刻": "2026-09-02T08:30:00"},
    ):
        blocked, message = svc.run_action(session["id"], "续查", values)
        assert blocked is None
        assert "不在待续查时段" in message
    # 部分重叠的请求会被裁剪到待续查内，不会重算已核时段。
    partial, message = svc.run_action(
        session["id"], "续查",
        {"起始时刻": "2026-09-02T09:00:00", "截止时刻": "2026-09-02T09:01:00"})
    assert partial is not None, message
    assert svc.runs(session["id"])[-1]["scope"] == {
        "start": "2026-09-02T09:01:00", "end": "2026-09-02T09:02:00"}


def test_moment_outside_pending_is_rejected(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    session = _open(svc)
    blocked, message = svc.run_action(
        session["id"], "续查", {"样本时刻": "2026-09-02T23:59:00"})
    assert blocked is None
    assert "不在待续查时段" in message


def test_unit_mismatch_is_reported_as_difference(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    # 复验单声明 mV，但索引里的实际单位是 V：结论应为存在差异并说明单位不符。
    session = _open(svc, 计量单位="mV")
    result, message = svc.run_action(
        session["id"], "提交复验",
        {"起始时刻": "2026-09-02T09:00:00", "截止时刻": "2026-09-02T09:00:00"})
    assert result is not None, message
    assert result["复验结论"] == "存在差异"
    assert result["abnormal"] is True
    assert "mV" in result["边界交叠"] or "mV" in message
    run = svc.runs(session["id"])[-1]
    assert any("实际单位" in problem for problem in run["problems"])
    # 实际单位仍按样本自己带的值记录，不被声明单位覆盖。
    assert run["samples"][0]["unit"] == "V"
    assert run["units_seen"] == ["V"]


def test_empty_window_is_marked_difference(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    session = _open(svc)
    # 窗口内没有任何样本时，提交也要落快照并给出可解释结论。
    session["pending_ranges"] = [{"start": "2026-09-02T07:00:00",
                                  "end": "2026-09-02T07:01:00"}]
    result, message = svc.run_action(session["id"], "提交复验", {})
    assert result is not None, message
    assert result["复验结论"] == "存在差异"
    assert result["样本数"] == "0"


def test_repeated_submit_without_scope_covers_each_sample_once(rig):
    from app.services.reverify_session import ReverifySessionService

    svc = ReverifySessionService()
    # 不钉具体引用时，按换算规则取两个版本的包络，覆盖整段三分钟。
    session = _open(svc, 标定引用="", 复验规则="UNIT-RV2")
    assert session["采用范围"] == "2026-09-02T09:00:00 ~ 2026-09-02T09:03:00"

    # 先核中间一点，待续查被切成前后两段；后续不指定范围时取第一段。
    middle, message = svc.run_action(
        session["id"], "提交复验", {"样本时刻": "2026-09-02T09:01:00"})
    assert middle is not None, message
    assert middle["待续查时段"] == (
        "2026-09-02T09:00:00 ~ 2026-09-02T09:01:00；"
        "2026-09-02T09:02:00 ~ 2026-09-02T09:03:00"
    )
    first, message = svc.run_action(session["id"], "提交复验", {})
    assert first is not None, message
    assert first["待续查时段"] == "2026-09-02T09:02:00 ~ 2026-09-02T09:03:00"
    assert first["status"] == "待续查"
    second, message = svc.run_action(session["id"], "提交复验", {})
    assert second is not None, message
    assert second["status"] == "已复验"
    assert second["待续查时段"] == "无"
    # 完成后再提交不会再算一遍，也不会悄悄换个版本重来。
    again, message = svc.run_action(session["id"], "提交复验", {})
    assert again is None
    assert "另起复验单" in message

    stamps = [
        sample["timestamp"]
        for run in svc.runs(session["id"])
        for sample in run["samples"]
    ]
    assert sorted(stamps) == sorted(SAMPLES)
    assert len(stamps) == len(set(stamps))
    # 第三次样本由更正版读出，前两点仍来自旧版自己的索引。
    versions = {
        sample["timestamp"]: sample["reference"]
        for run in svc.runs(session["id"])
        for sample in run["samples"]
    }
    assert versions["2026-09-02T09:00:00"] == OLD
    assert versions["2026-09-02T09:02:00"] == NEW
