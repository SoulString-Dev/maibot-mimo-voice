"""自适应超时与失败归类的纯函数实现（0.5.3）。

设计约束：

* 只用标准库，方便单测与容器内直接跑；
* 不持有状态——采样队列由插件实例维护，传进来即可；
* 样本不足（< ``_TIMEOUT_MIN_SAMPLES``）时退回 ``general.timeout``，冷启动不会比 0.5.2 更激进；
* ``timeout_mode = "fixed"`` 时与 0.5.2 完全一致，出问题可以一键回退。
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from .constants import (
    _TIMEOUT_FACTOR,
    _TIMEOUT_MAX_SECONDS,
    _TIMEOUT_MIN_SAMPLES,
    _TIMEOUT_MIN_SECONDS,
    _TIMEOUT_STEP_SECONDS,
)


def percentile(samples: Sequence[float], ratio: float) -> float:
    """线性插值分位数：``ratio=0.8`` 即 p80。空序列调用方自己兜底。"""
    if not samples:
        raise ValueError("samples 不能为空")
    ordered = sorted(float(item) for item in samples)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, float(ratio))) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _round_up_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.ceil(value / step) * step


def effective_timeout(
    samples: Iterable[float],
    *,
    config_timeout: float,
    mode: str = "auto",
    min_samples: int = _TIMEOUT_MIN_SAMPLES,
    factor: float = _TIMEOUT_FACTOR,
    minimum: float = _TIMEOUT_MIN_SECONDS,
    maximum: float = _TIMEOUT_MAX_SECONDS,
    step: float = _TIMEOUT_STEP_SECONDS,
) -> float:
    """算出这次该用的超时（秒）。

    * ``mode != "auto"`` 或样本不足 → 原样返回 ``config_timeout``；
    * 否则 ``clamp(p80 × factor, minimum, maximum)``，再向上取整到 ``step``。
    """
    observed = [float(item) for item in samples if float(item) > 0.0]
    if mode != "auto" or len(observed) < max(1, int(min_samples)):
        return float(config_timeout)
    target = percentile(observed, 0.8) * float(factor)
    clamped = min(max(target, float(minimum)), float(maximum))
    rounded = _round_up_to_step(clamped, float(step))
    # 取整后仍要守住硬上限，避免 step 把值顶出范围。
    return float(min(max(rounded, float(minimum)), float(maximum)))


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
