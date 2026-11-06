"""失败归类的纯函数实现（0.5.5 起只保留这一个）。

历史：0.5.3 这里还有「p80 × 1.4 自适应超时」和「超时后预算内补发」，两项都在 0.5.5 删除——
11-06 的名额实验（``docs/analysis-dual-draft.md``）证明 MiMo 每账号 TTS 并发名额只有 1，
超时后重发一律 429，动态超时也只是在猜"自己愿意等多久"。现在超时固定、失败即回退文字。
"""

from __future__ import annotations


def classify_failure(exc: BaseException) -> str:
    """把合成异常归成日志里的类型：timeout / 429 / 5xx / network / audio / other。

    只按消息和类名判断，不 import aiohttp，容器里没有网络库也能单测。
    """
    if isinstance(exc, TimeoutError):
        return "timeout"
    message = str(exc)
    if "429" in message:
        return "429"
    for code in ("500", "502", "503", "504", "5xx"):
        if code in message:
            return "5xx"
    name = type(exc).__name__
    if "Client" in name or "Connection" in name or "ServerDisconnected" in name:
        return "network"
    lowered = message.lower()
    if any(token in lowered for token in ("音频", "base64", "采样", "wav", "静音", "过短", "过长", "几乎无声")):
        return "audio"
    return "other"
