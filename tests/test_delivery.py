"""并存测试：语音单独一条，文字另发一条（不混在同一条消息里）。"""

from __future__ import annotations

import asyncio
import io
import wave

from __bootstrap import Checker, load_plugin, make_plugin


def wav_bytes(seconds: float = 1.0, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def make_ready(module, **overrides):
    overrides.setdefault("trigger", {"mode": "voice_only"})
    plugin, config, logger = make_plugin(module, **overrides)
    return plugin, config, logger


def stub_synthesis(plugin, audio: bytes) -> None:
    async def fake(text: str) -> bytes:
        return audio

    plugin.__dict__["_synthesize_guarded"] = fake
    plugin.__dict__["_postprocess"] = lambda data: data


def stub_failure(plugin) -> None:
    async def fake(text: str) -> bytes:
        raise RuntimeError("合成失败")

    plugin.__dict__["_synthesize_guarded"] = fake


def message(text: str) -> dict:
    return {
        "raw_message": [{"type": "text", "data": text}],
        "session_id": "s1",
        "scope": "private",
    }


def convert(plugin, text: str = "你好呀😀"):
    """转换并等所有延迟任务跑完（测试里把延迟改成 0）。"""

    async def run():
        result = await plugin.convert_outbound_text(
            message=message(text), stream_id="s1", set_reply=False
        )
        pending = list(plugin._pending_text_tasks)
        if pending:
            await asyncio.gather(*pending)
        return result

    return asyncio.run(run())


def test_delivery(module) -> int:
    check = Checker("并存：语音 + 文字")

    module._KEEP_TEXT_DELAY_SECONDS = 0.0

    plugin, _config, _logger = make_ready(module)
    stub_synthesis(plugin, wav_bytes())
    result = convert(plugin, "你好呀😀")
    segments = result["modified_kwargs"]["message"]["raw_message"]
    check.equal("默认只有语音一段", len(segments), 1)
    check.equal("默认段类型", segments[0]["type"], "voice")
    check.equal("默认不额外发文字", plugin.ctx.send.texts, [])

    plugin, _config, _logger = make_ready(module, delivery={"keep_text": True})
    stub_synthesis(plugin, wav_bytes())
    result = convert(plugin, "你好呀😀")
    segments = result["modified_kwargs"]["message"]["raw_message"]
    check.equal("文字先：消息里仍只有语音", [segment["type"] for segment in segments], ["voice"])
    check.equal("文字先：文字单独发一条", plugin.ctx.send.texts, [("s1", "你好呀😀")])

    plugin, _config, _logger = make_ready(
        module, delivery={"keep_text": True, "text_first": False}
    )
    stub_synthesis(plugin, wav_bytes())
    result = convert(plugin, "你好呀😀")
    segments = result["modified_kwargs"]["message"]["raw_message"]
    check.equal("语音先：消息里只有语音", [segment["type"] for segment in segments], ["voice"])
    check.equal("语音先：文字随后补发", plugin.ctx.send.texts, [("s1", "你好呀😀")])

    plugin, _config, _logger = make_ready(module, delivery={"keep_text": True})
    stub_failure(plugin)
    check.equal("失败时保持原文（不额外发文字）", convert(plugin, "你好呀😀"), None)
    check.equal("失败时不补发文字", plugin.ctx.send.texts, [])

    plugin, _config, _logger = make_ready(
        module, delivery={"keep_text": True, "fallback_to_text": False}
    )
    stub_failure(plugin)
    check.equal("失败且不回退时拦截", convert(plugin, "你好呀😀"), {"action": "abort"})
    return check.report()


def test_self_sent(module) -> int:
    """自己发的文字不能再被转成语音（否则一条回复会连出 2–3 条语音）。"""

    check = Checker("并存：自己发的文字不再转换")

    plugin, _config, _logger = make_ready(module, delivery={"keep_text": True})
    stub_synthesis(plugin, wav_bytes())

    asyncio.run(plugin._send_text("s1", "已发送到群 123"))
    check.equal("回执原样发出", plugin.ctx.send.texts, [("s1", "已发送到群 123")])
    check.equal("同一段文字再进钩子时放行", convert(plugin, "已发送到群 123"), None)
    check.equal(
        "别段文字照常转换",
        [s["type"] for s in convert(plugin, "另一句话")["modified_kwargs"]["message"]["raw_message"]],
        ["voice"],
    )

    # 标记是一次性的：同一条文字第二次进来（真的是模型回复）仍要转换
    asyncio.run(plugin._send_text("s1", "再来一次"))
    convert(plugin, "再来一次")
    again = convert(plugin, "再来一次")
    check.equal("标记用后即删", again is not None, True)

    # /voice test 在 keep_text 打开时也发文字
    plugin, _config, _logger = make_ready(module, delivery={"keep_text": True})
    stub_synthesis(plugin, wav_bytes())
    asyncio.run(plugin._command_test("s1", "你好呀"))
    check.equal("/voice test 发文字", plugin.ctx.send.texts, [("s1", "你好呀")])
    check.equal("/voice test 发语音", len(plugin.ctx.send.hybrids), 1)

    # /voice say 在 keep_text 打开时把文字发到目标会话
    plugin, _config, _logger = make_ready(module, delivery={"keep_text": True})
    stub_synthesis(plugin, wav_bytes())

    async def fake_target(kind, ident, platform):
        return "group:123", "group", ""

    plugin.__dict__["_resolve_command_target"] = fake_target
    asyncio.run(plugin._command_say("s1", "g123 你好呀", "10000002", "qq"))
    check.equal(
        "say 的文字发到目标",
        [item for item in plugin.ctx.send.texts if item[0] == "group:123"],
        [("group:123", "你好呀")],
    )
    check.equal("say 的语音发到目标", plugin.ctx.send.hybrids[0][0], "group:123")
    return check.report()


def run(module) -> int:
    return test_delivery(module) + test_self_sent(module)


if __name__ == "__main__":
    raise SystemExit(run(load_plugin()))
