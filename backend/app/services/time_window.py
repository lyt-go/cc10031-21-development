"""标定与复验共用的时间窗工具：解析采样时刻、生成样本桶、合并和切分时段。

复验单需要把"已核时段"和"待续查时段"写成可回看的半开区间 ``[start, end)``，
这样相邻两轮续查在边界上不会重复计算同一条样本，也不会漏掉边界样本。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

# 示例数据按分钟采样；样本时刻代表该样本所在桶的起点，桶在 60 秒后结束。
SAMPLE_STEP = timedelta(seconds=60)
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def parse_time(value: Any) -> datetime | None:
    """把 ISO 时刻解析成 datetime；无法解析时返回 None。"""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_time(value: Any) -> str:
    """统一输出到秒级 ISO 文本，便于落盘和展示。"""
    parsed = parse_time(value)
    return parsed.strftime(_TIME_FORMAT) if parsed else str(value or "")


def bucket_end(value: Any, step: timedelta = SAMPLE_STEP) -> str:
    """样本时刻所在桶的结束时刻（半开区间的右端点）。"""
    parsed = parse_time(value)
    return (parsed + step).strftime(_TIME_FORMAT) if parsed else format_time(value)


def make_period(start: Any, end: Any) -> dict[str, str]:
    return {"start": format_time(start), "end": format_time(end)}


def period_bounds(period: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = parse_time(period.get("start"))
    end = parse_time(period.get("end"))
    if start is None or end is None or end <= start:
        return None
    return start, end


def period_text(period: dict[str, Any] | None) -> str:
    if not period:
        return "无"
    return f"{period.get('start')} ~ {period.get('end')}"


def periods_text(periods: Iterable[dict[str, Any]] | None) -> str:
    items = [period_text(period) for period in (periods or []) if period]
    return "；".join(items) if items else "无"


def in_period(timestamp: Any, period: dict[str, Any]) -> bool:
    moment = parse_time(timestamp)
    bounds = period_bounds(period)
    if moment is None or bounds is None:
        return False
    start, end = bounds
    return start <= moment < end


def in_closed_period(timestamp: Any, start: Any, end: Any) -> bool:
    """标定生效区间是闭区间：起止两个时刻的样本都算被该次标定覆盖。

    窗口边界上的采样点因此可能同时落在两次标定的生效区间里，
    这正是"边界交叠"需要按版本择优而不能简单取并集的原因。
    """
    moment = parse_time(timestamp)
    left = parse_time(start)
    right = parse_time(end)
    if moment is None:
        return False
    if left is not None and moment < left:
        return False
    if right is not None and moment > right:
        return False
    return True


def merge_periods(periods: Iterable[dict[str, Any]] | None) -> list[dict[str, str]]:
    """合并重叠或首尾相接的时段，保证已核范围不重复。"""
    parsed: list[tuple[datetime, datetime]] = []
    for period in periods or []:
        bounds = period_bounds(period)
        if bounds:
            parsed.append(bounds)
    parsed.sort(key=lambda item: item[0])
    merged: list[list[datetime]] = []
    for start, end in parsed:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [make_period(start, end) for start, end in merged]


def subtract_period(periods: Iterable[dict[str, Any]] | None, start: Any, end: Any) -> list[dict[str, str]]:
    """从时段集合中切掉 ``[start, end)``，返回剩余片段。"""
    cut_start = parse_time(start)
    cut_end = parse_time(end)
    if cut_start is None or cut_end is None or cut_end <= cut_start:
        return merge_periods(periods)
    remain: list[dict[str, str]] = []
    for period in merge_periods(periods):
        bounds = period_bounds(period)
        if bounds is None:
            continue
        p_start, p_end = bounds
        if cut_end <= p_start or cut_start >= p_end:
            remain.append(make_period(p_start, p_end))
            continue
        if p_start < cut_start:
            remain.append(make_period(p_start, min(cut_start, p_end)))
        if p_end > cut_end:
            remain.append(make_period(max(cut_end, p_start), p_end))
    return merge_periods(remain)


def intersect_period(period: dict[str, Any], start: Any, end: Any) -> dict[str, str] | None:
    """取时段与给定半开区间的交集。"""
    bounds = period_bounds(period)
    cut_start = parse_time(start)
    cut_end = parse_time(end)
    if bounds is None or cut_start is None or cut_end is None:
        return None
    left = max(bounds[0], cut_start)
    right = min(bounds[1], cut_end)
    return make_period(left, right) if right > left else None


def window_period(start: Any, end: Any) -> dict[str, str] | None:
    parsed_start = parse_time(start)
    parsed_end = parse_time(end)
    if parsed_start is None or parsed_end is None or parsed_end <= parsed_start:
        return None
    return make_period(parsed_start, parsed_end)
