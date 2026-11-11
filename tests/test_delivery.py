"""并存测试：语音之外再发一条文字时的段顺序与内容。"""

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
    return asyncio.run(
        plugin.convert_outbound_text(message=message(text), stream_id="s1", set_reply=False)
    )


def test_delivery(module) -> int:
    check = Checker("并存：语音 + 文字")

    plugin, _config, _logger = make_ready(module)
    stub_synthesis(plugin, wav_bytes())
    result = convert(plugin, "你好呀😀")
    segments = result["modified_kwargs"]["message"]["raw_message"]
    check.equal("默认只有语音一段", len(segments), 1)
    check.equal("默认段类型", segments[0]["type"], "voice")

    plugin, _config, _logger = make_ready(module, delivery={"keep_text": True})
    stub_synthesis(plugin, wav_bytes())
    result = convert(plugin, "你好呀😀")
    segments = result["modified_kwargs"]["message"]["raw_message"]
    check.equal("并存两段", len(segments), 2)
    check.equal("文字先", [segment["type"] for segment in segments], ["text", "voice"])
    check.equal("文字是原始回复", segments[0]["data"], "你好呀😀")

    plugin, _config, _logger = make_ready(module, delivery={"keep_text": True, "text_first": False})
    stub_synthesis(plugin, wav_bytes())
    result = convert(plugin, "你好呀😀")
    segments = result["modified_kwargs"]["message"]["raw_message"]
    check.equal("语音先", [segment["type"] for segment in segments], ["voice", "text"])

    plugin, _config, _logger = make_ready(module, delivery={"keep_text": True})
    stub_failure(plugin)
    check.equal("失败时保持原文（不额外发文字）", convert(plugin, "你好呀😀"), None)

    plugin, _config, _logger = make_ready(
        module, delivery={"keep_text": True, "fallback_to_text": False}
    )
    stub_failure(plugin)
    check.equal("失败且不回退时拦截", convert(plugin, "你好呀😀"), {"action": "abort"})
    return check.report()


def run(module) -> int:
    return test_delivery(module)


if __name__ == "__main__":
    raise SystemExit(run(load_plugin()))
