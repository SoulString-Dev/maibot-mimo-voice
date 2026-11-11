"""定时语音测试：到期判定、去重、开关 / 忙跳过、发送。"""

from __future__ import annotations

import asyncio
import io
import sys
import wave
from datetime import datetime

from __bootstrap import Checker, load_plugin, make_plugin


def wav_bytes(seconds: float = 1.0, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def entry(**overrides):
    data = {
        "enabled": True,
        "time": "08:30",
        "days": "每天",
        "target": "g123",
        "text": "早呀",
    }
    data.update(overrides)
    return data


def stub_synthesis(plugin, audio: bytes) -> None:
    async def fake(text: str) -> bytes:
        return audio

    plugin.__dict__["_synthesize_guarded"] = fake
    plugin.__dict__["_postprocess"] = lambda data: data


def ready(module, **overrides):
    plugin, config, logger = make_plugin(module, **overrides)
    plugin.ctx.chat.groups["123"] = "group:123"
    return plugin, config, logger


def test_due(module) -> None:
    check = Checker("定时：到期与去重")
    plugin, _config, _logger = ready(
        module,
        scheduled={
            "enabled": True,
            "entries": [
                entry(),
                entry(time="20:00", days="工作日", target="u456"),
                entry(enabled=False),
            ],
        },
    )
    wednesday = datetime(2026, 11, 11, 8, 30)
    due = plugin._due_entries(wednesday)
    check.equal("每天 08:30 命中一条", len(due), 1)
    check.equal("命中下标", due[0][0], 0)
    check.equal("同一分钟第二次为空", len(plugin._due_entries(wednesday)), 0)
    check.equal("08:31 不命中", len(plugin._due_entries(datetime(2026, 11, 11, 8, 31))), 0)
    check.equal(
        "工作日 20:00 命中",
        len(plugin._due_entries(datetime(2026, 11, 11, 20, 0))),
        1,
    )
    check.equal(
        "周日 20:00 不命中",
        len(plugin._due_entries(datetime(2026, 11, 15, 20, 0))),
        0,
    )
    check.equal("关闭的条目永不命中", len(plugin._due_entries(datetime(2026, 11, 16, 8, 30))), 1)
    check.check("调度未启用时为空", ready(module)[0]._due_entries(wednesday) == [])
    return check.report()


def test_run(module) -> None:
    check = Checker("定时：发送与跳过")
    plugin, config, logger = ready(module, scheduled={"enabled": True, "entries": [entry()]})
    stub_synthesis(plugin, wav_bytes())
    scheduled_entry = config.scheduled.entries[0]
    asyncio.run(plugin._run_scheduled(0, scheduled_entry))
    check.equal("发到目标会话", plugin.ctx.send.hybrids[-1][0], "group:123")
    check.check("记录发送日志", any("定时语音已发送" in line for line in logger.messages("info")))

    closed, closed_config, _logger = ready(
        module, scheduled={"enabled": True, "entries": [entry()]}
    )
    stub_synthesis(closed, wav_bytes())
    closed.__dict__["_session_switch"] = {"group:123": False}
    asyncio.run(closed._run_scheduled(0, closed_config.scheduled.entries[0]))
    check.equal("会话关闭时跳过", len(closed.ctx.send.hybrids), 0)

    busy, busy_config, busy_logger = ready(
        module, scheduled={"enabled": True, "entries": [entry()]}
    )
    stub_synthesis(busy, wav_bytes())

    async def run_busy() -> None:
        async with busy._synthesis_slots:
            await busy._run_scheduled(0, busy_config.scheduled.entries[0])

    asyncio.run(run_busy())
    check.equal("正在合成时跳过", len(busy.ctx.send.hybrids), 0)
    check.check("跳过有日志", any("正在合成" in line for line in busy_logger.messages("info")))

    both, both_config, _logger = ready(
        module,
        scheduled={"enabled": True, "entries": [entry()]},
        delivery={"keep_text": True},
    )
    stub_synthesis(both, wav_bytes())
    asyncio.run(both._run_scheduled(0, both_config.scheduled.entries[0]))
    check.equal("并存时先发文字", both.ctx.send.texts[-1][1], "早呀")
    check.equal("并存时再发语音", len(both.ctx.send.hybrids), 1)

    missing, missing_config, missing_logger = ready(
        module, scheduled={"enabled": True, "entries": [entry(target="g999")]}
    )
    missing.ctx.chat.can_open = False
    stub_synthesis(missing, wav_bytes())
    asyncio.run(missing._run_scheduled(0, missing_config.scheduled.entries[0]))
    check.equal("目标解析失败不发", len(missing.ctx.send.hybrids), 0)
    check.check("目标解析失败有告警", bool(missing_logger.messages("warning")))

    unsupported, _unsupported_config, unsupported_logger = ready(
        module, scheduled={"enabled": True, "entries": [entry()]}
    )
    stub_synthesis(unsupported, wav_bytes())
    cfg_module = sys.modules[f"{module.__name__}.core.config"]
    llm_entry = cfg_module.ScheduleEntry(
        enabled=True, time="08:30", days="每天", target="g123", text="早呀", content="llm"
    )
    asyncio.run(unsupported._run_scheduled(0, llm_entry))
    check.equal("content=llm 暂不支持", len(unsupported.ctx.send.hybrids), 0)
    check.check("不支持时有告警", bool(unsupported_logger.messages("warning")))
    return check.report()


def run(module) -> int:
    return test_due(module) + test_run(module)


if __name__ == "__main__":
    raise SystemExit(run(load_plugin()))
