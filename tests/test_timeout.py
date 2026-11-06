"""超时与失败归类测试（0.5.5：只保留固定超时 + 归类）。

0.5.3 的 p80 自适应超时、0.5.4 的"超时后补发"都在 0.5.5 删除，这里额外加了
"这两个 API 不该再存在"的守卫断言，避免以后被误加回来。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, load_plugin, make_plugin

module = load_plugin()
timeout = sys.modules[f"{module.__name__}.core.timeout"]
checker = Checker("超时与失败归类")

# ---------------------------------------------------------------- 已删除的 API
checker.check(
    "不再提供 p80 自适应超时",
    not hasattr(timeout, "effective_timeout"),
    "effective_timeout 仍然存在",
)
checker.check(
    "不再提供超时后补发",
    not hasattr(timeout, "retry_timeout"),
    "retry_timeout 仍然存在",
)
defaults = module.MiMoVoiceConfig()
checker.check(
    "配置里不再有 timeout_mode",
    "timeout_mode" not in type(defaults.general).model_fields,
    "timeout_mode 仍然存在",
)
checker.check(
    "配置里不再有 retry_on_timeout",
    "retry_on_timeout" not in type(defaults.mimo).model_fields,
    "retry_on_timeout 仍然存在",
)

# ---------------------------------------------------------------- 固定超时
checker.equal("默认超时 25 秒", module.MiMoVoiceConfig().general.timeout, 25.0)
checker.equal(
    "可改成 30 秒",
    module.MiMoVoiceConfig(general={"timeout": 30.0}).general.timeout,
    30.0,
)

# ---------------------------------------------------------------- 失败归类
checker.equal("超时", timeout.classify_failure(TimeoutError("语音合成超时（25s）")), "timeout")
checker.equal("429", timeout.classify_failure(RuntimeError("MiMo TTS 暂时不可用: 429 Too Many Requests")), "429")
checker.equal("5xx", timeout.classify_failure(RuntimeError("MiMo TTS 暂时不可用: 503 Service Unavailable")), "5xx")
checker.equal("音频校验", timeout.classify_failure(RuntimeError("MiMo TTS 返回音频过短（120 字节）")), "audio")
checker.equal("其他", timeout.classify_failure(RuntimeError("MiMo TTS 请求失败: 400 bad request")), "other")


class FakeClientError(Exception):
    """类名带 Client，模拟 aiohttp.ClientError。"""


checker.equal("网络错误", timeout.classify_failure(FakeClientError("Cannot connect")), "network")

# ---------------------------------------------------------------- 插件接入
plugin, config, logger = make_plugin(module)
checker.equal("插件读到的超时就是配置值", float(plugin.config.general.timeout), 25.0)

captured = {}


async def _fake_once(text: str, timeout: float, attempt: int = 1) -> bytes:
    captured["timeout"] = timeout
    captured["attempt"] = attempt
    captured["text"] = text
    return b"payload"


async def _run_guarded() -> bytes:
    plugin.__dict__["_synthesize_once"] = _fake_once
    try:
        return await plugin._synthesize_guarded("测试文本")
    finally:
        plugin.__dict__.pop("_synthesize_once", None)


result = asyncio.run(_run_guarded())
checker.equal("守卫函数把 25 秒传下去", captured.get("timeout"), 25.0)
checker.equal("默认只请求一次", captured.get("attempt"), 1)
checker.equal("返回合成结果", result, b"payload")

raise SystemExit(checker.report())
