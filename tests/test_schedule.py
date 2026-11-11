"""定时语音的时间/星期解析测试：纯函数，不碰宿主与网络。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from __bootstrap import Checker, load_plugin


def test_schedule(module) -> int:
    check = Checker("定时：时间 / 星期 / 命中 / 摘要")
    schedule = __import__(f"{module.__name__}.core.schedule", fromlist=["x"])

    check.equal("标准时间", schedule.parse_time("08:30"), (8, 30))
    check.equal("个位数小时", schedule.parse_time("8:5"), (8, 5))
    check.equal("带空格与全角冒号", schedule.parse_time(" 23：59 "), (23, 59))
    check.equal("24 点非法", schedule.parse_time("24:00"), None)
    check.equal("60 分非法", schedule.parse_time("08:60"), None)
    check.equal("空串非法", schedule.parse_time(""), None)
    check.equal("中文写法非法", schedule.parse_time("8点30"), None)

    check.equal("daily", schedule.parse_days("daily"), frozenset(range(7)))
    check.equal("中文每天", schedule.parse_days("每天"), frozenset(range(7)))
    check.equal("工作日", schedule.parse_days("工作日"), frozenset(range(5)))
    check.equal("mon-fri", schedule.parse_days("mon-fri"), frozenset(range(5)))
    check.equal("逗号列表大小写", schedule.parse_days("MON,WED,FRI"), frozenset({0, 2, 4}))
    check.equal("数字区间", schedule.parse_days("1-5"), frozenset(range(5)))
    check.equal("数字列表", schedule.parse_days("1,3,5"), frozenset({0, 2, 4}))
    check.equal("周末", schedule.parse_days("周末"), frozenset({5, 6}))
    check.equal("7 表示周日", schedule.parse_days("7"), frozenset({6}))
    check.equal("混入非法名字", schedule.parse_days("mon,8"), None)
    check.equal("空串非法", schedule.parse_days(""), None)

    wednesday = datetime(2026, 11, 11, 8, 30)
    check.check("每天命中", schedule.entry_due("08:30", "daily", wednesday))
    check.check("差一分钟不命中", not schedule.entry_due("08:31", "daily", wednesday))
    check.check("工作日命中周三", schedule.entry_due("08:30", "mon-fri", wednesday))
    check.check("周末不命中周三", not schedule.entry_due("08:30", "周末", wednesday))
    check.check("23:59 命中", schedule.entry_due("23:59", "daily", datetime(2026, 11, 11, 23, 59)))

    entries = [
        SimpleNamespace(time="08:30", days="daily"),
        SimpleNamespace(time="20:00", days="mon-fri"),
    ]
    check.equal("摘要两条", schedule.summarize_entries(entries), "2 条（08:30 每天；20:00 工作日）")
    check.equal("摘要为空", schedule.summarize_entries([]), "0 条")
    many = [SimpleNamespace(time=f"0{i}:00", days="daily") for i in range(1, 5)]
    check.check("摘要最多列三条", "等）" in schedule.summarize_entries(many))
    return check.report()


def run(module) -> int:
    return test_schedule(module)


if __name__ == "__main__":
    raise SystemExit(run(load_plugin()))
