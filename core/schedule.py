"""定时语音的时间与星期解析：纯函数、无 IO，便于单测与复用。

``days`` 约定 0=周一、6=周日，与 ``datetime.weekday()`` 一致；用户写法可以是
``daily`` / ``工作日`` / ``mon-fri`` / ``mon,wed,fri`` / ``1-5`` / ``6,7``。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, FrozenSet, Optional, Sequence, Tuple

_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_ALIASES = {
    "daily": "1-7",
    "everyday": "1-7",
    "every day": "1-7",
    "每天": "1-7",
    "weekday": "1-5",
    "weekdays": "1-5",
    "工作日": "1-5",
    "weekend": "6-7",
    "weekends": "6-7",
    "周末": "6-7",
}
_TIME_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$")
_SPLIT_RE = re.compile(r"[,，、\s]+")


def parse_time(text: str) -> Optional[Tuple[int, int]]:
    """``"08:30"`` → ``(8, 30)``；非法返回 None。"""
    match = _TIME_RE.match((text or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _day_number(token: str) -> Optional[int]:
    token = token.strip()
    if token in _DAY_NAMES:
        return _DAY_NAMES[token]
    if token.isdigit():
        value = int(token)
        if 1 <= value <= 7:
            return value - 1
    return None


def parse_days(text: str) -> Optional[FrozenSet[int]]:
    """解析星期表达式，返回 0–6 的集合；非法返回 None。"""
    raw = (text or "").strip().casefold()
    if not raw:
        return None
    raw = _ALIASES.get(raw, raw)
    days: set[int] = set()
    for token in _SPLIT_RE.split(raw):
        if not token:
            continue
        if "-" in token:
            left, _, right = token.partition("-")
            start, end = _day_number(left), _day_number(right)
            if start is None or end is None or start > end:
                return None
            days.update(range(start, end + 1))
            continue
        number = _day_number(token)
        if number is None:
            return None
        days.add(number)
    return frozenset(days) if days else None


def entry_due(time_text: str, days_text: str, moment: datetime) -> bool:
    """该条目在 ``moment`` 这一分钟是否该执行。"""
    clock = parse_time(time_text)
    days = parse_days(days_text)
    if clock is None or days is None:
        return False
    return moment.weekday() in days and (moment.hour, moment.minute) == clock


def _days_label(days: Optional[FrozenSet[int]]) -> str:
    if days is None:
        return "星期无效"
    if len(days) == 7:
        return "每天"
    if days == frozenset(range(5)):
        return "工作日"
    if days == frozenset({5, 6}):
        return "周末"
    return "、".join(_DAY_LABELS[index] for index in sorted(days))


def summarize_entries(entries: Sequence[Any]) -> str:
    """给启动日志用的一行摘要，最多列三条。"""
    if not entries:
        return "0 条"
    parts = []
    for entry in entries[:3]:
        time_text = str(getattr(entry, "time", "") or "").strip()
        days = parse_days(str(getattr(entry, "days", "") or ""))
        parts.append(f"{time_text} {_days_label(days)}".strip())
    text = "；".join(parts)
    if len(entries) > 3:
        text += " 等"
    return f"{len(entries)} 条（{text}）"
