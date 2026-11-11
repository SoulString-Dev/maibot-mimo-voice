"""对外 API 测试：开关、口令、长度、限流、只合成 / 发送。"""

from __future__ import annotations

import asyncio
import base64
import io
import time
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
    plugin, config, logger = make_plugin(module, **overrides)
    return plugin, config, logger


def stub_synthesis(plugin, audio: bytes) -> None:
    async def fake(text: str) -> bytes:
        return audio

    plugin.__dict__["_synthesize_guarded"] = fake
    plugin.__dict__["_postprocess"] = lambda data: data


def call(plugin, **kwargs):
    return asyncio.run(plugin.handle_voice_say(**kwargs))


def test_api(module) -> int:
    check = Checker("对外 API：voice_say")

    disabled, _config, _logger = make_ready(module, api={"enabled": False})
    check.check("关闭时拒绝", "已关闭" in call(disabled, text="你好")["error"])

    token_plugin, _config, _logger = make_ready(module, api={"token": "s3cret"})
    stub_synthesis(token_plugin, wav_bytes())
    check.check("口令不对被拒", "token" in call(token_plugin, text="你好", token="wrong")["error"])
    check.check("口令正确放行", call(token_plugin, text="你好", token="s3cret")["success"])

    empty, _config, _logger = make_ready(module)
    stub_synthesis(empty, wav_bytes())
    check.check("空文本被拒", not call(empty, text="   ")["success"])

    long_plugin, _config, _logger = make_ready(module, general={"max_text_length": 20})
    stub_synthesis(long_plugin, wav_bytes())
    check.check("超长被拒", "太长" in call(long_plugin, text="字" * 21)["error"])

    rate, _config, _logger = make_ready(module, api={"rate_limit_seconds": 30.0})
    stub_synthesis(rate, wav_bytes())
    rate.__dict__["_api_last_call"] = time.monotonic()
    check.check("限流生效", not call(rate, text="你好")["success"])
    rate.__dict__["_api_last_call"] = 0.0
    check.check("间隔足够放行", call(rate, text="你好")["success"])

    audio_plugin, _config, _logger = make_ready(module)
    stub_synthesis(audio_plugin, wav_bytes(2.0))
    result = call(audio_plugin, text="你好", return_audio=True)
    check.check("只合成成功", result["success"])
    check.equal("返回时长", round(result["seconds"], 1), 2.0)
    payload = base64.b64decode(result["audio_base64"])
    check.check("返回的是 WAV", payload[:4] == b"RIFF")
    check.equal("只合成不发送", len(audio_plugin.ctx.send.hybrids), 0)

    send_plugin, _config, _logger = make_ready(module)
    stub_synthesis(send_plugin, wav_bytes())
    result = call(send_plugin, text="你好", stream_id="s1")
    check.check("发送成功", result["success"])
    check.equal("发到指定会话", send_plugin.ctx.send.hybrids[-1][0], "s1")
    check.check("发送时不返回 base64", "audio_base64" not in result)

    busy, _config, _logger = make_ready(module)

    async def busy_synth(text: str) -> bytes:
        raise module._SynthesisBusy("已有语音正在合成")

    busy.__dict__["_synthesize_guarded"] = busy_synth
    check.check("忙时返回失败", not call(busy, text="你好")["success"])

    failing, _config, _logger = make_ready(module)

    async def boom(text: str) -> bytes:
        raise RuntimeError("上游炸了")

    failing.__dict__["_synthesize_guarded"] = boom
    check.check("异常转成失败结果", "上游炸了" in call(failing, text="你好")["error"])
    return check.report()


def run(module) -> int:
    return test_api(module)


if __name__ == "__main__":
    raise SystemExit(run(load_plugin()))
