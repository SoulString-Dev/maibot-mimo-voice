"""自适应超时测试：分位数、动态值、失败归类、插件接入。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import Checker, load_plugin, make_plugin

module = load_plugin()
timeout = sys.modules[f"{module.__name__}.core.timeout"]
checker = Checker("自适应超时")

# ---------------------------------------------------------------- 分位数
checker.equal("p80 单样本", timeout.percentile([3.0], 0.8), 3.0)
checker.equal("p80 中位插值", round(timeout.percentile([1.0, 2.0, 3.0], 0.8), 3), 2.6)
checker.equal("p100 取最大", timeout.percentile([1.0, 5.0, 2.0], 1.0), 5.0)
checker.equal("p0 取最小", timeout.percentile([4.0, 1.0, 2.0], 0.0), 1.0)
try:
    timeout.percentile([], 0.8)
except ValueError:
    checker.check("空序列抛 ValueError", True)
else:
    checker.check("空序列抛 ValueError", False, "没有抛")

# ---------------------------------------------------------------- 动态超时
checker.equal("样本不足回退配置值", timeout.effective_timeout([1.0] * 9, config_timeout=20.0), 20.0)
checker.equal("fixed 模式恒为配置值", timeout.effective_timeout([9.0] * 20, config_timeout=18.0, mode="fixed"), 18.0)
checker.equal(
    "样本足够取 p80×1.4",
    timeout.effective_timeout([10.0] * 10, config_timeout=20.0),
    14.0,
)
checker.equal(
    "低于下限时夹到 12s",
    timeout.effective_timeout([1.0] * 10, config_timeout=20.0),
    12.0,
)
checker.equal(
    "高于上限时夹到 25s",
    timeout.effective_timeout([30.0] * 10, config_timeout=20.0),
    25.0,
)
checker.equal(
    "向上取整到 0.5s",
    timeout.effective_timeout([3.33] * 10, config_timeout=20.0),
    12.0,  # 3.33×1.4=4.66 → 夹到下限 12
)
checker.equal(
    "取整后不越上限",
    timeout.effective_timeout([17.9] * 10, config_timeout=20.0),
    25.0,  # 17.9×1.4=25.06 → 25.0
)
checker.equal(
    "取整到 0.5s 粒度",
    timeout.effective_timeout([9.1] * 10, config_timeout=20.0),
    13.0,  # 9.1×1.4=12.74 → 13.0
)

# ---------------------------------------------------------------- 失败归类
checker.equal("超时", timeout.classify_failure(TimeoutError("语音合成超时（20s）")), "timeout")
checker.equal("429", timeout.classify_failure(RuntimeError("MiMo TTS 暂时不可用: 429 Too Many Requests")), "429")
checker.equal("5xx", timeout.classify_failure(RuntimeError("MiMo TTS 暂时不可用: 503 Service Unavailable")), "5xx")
checker.equal("音频校验", timeout.classify_failure(RuntimeError("MiMo TTS 返回音频过短（120 字节）")), "audio")
checker.equal("其他", timeout.classify_failure(RuntimeError("MiMo TTS 请求失败: 400 bad request")), "other")


class FakeClientError(Exception):
    """类名带 Client，模拟 aiohttp.ClientError。"""


checker.equal("网络错误", timeout.classify_failure(FakeClientError("Cannot connect")), "network")

# ---------------------------------------------------------------- 插件接入
plugin, config, logger = make_plugin(module)
timeout_mode_before = plugin._current_timeout()
checker.equal("插件冷启动用配置值", timeout_mode_before, float(config.general.timeout))
plugin._synth_durations.extend([2.0, 2.5, 3.0, 3.0, 3.5, 4.0, 4.0, 4.5, 5.0, 6.0])
auto_value = plugin._current_timeout()
checker.check("插件样本足够后切换动态值", 12.0 <= auto_value <= 25.0, f"{auto_value}")
checker.check("动态值不等于配置值", auto_value != float(config.general.timeout), f"{auto_value}")

fixed_config = module.MiMoVoiceConfig(general={"timeout_mode": "fixed", "timeout": 18.0})
plugin.__dict__["_plugin_config_instance"] = fixed_config
checker.equal("切换到 fixed 后回退 18s", plugin._current_timeout(), 18.0)

# 埋点：耗时入队、日志带 timeout 与第几次
checker.equal("重载后采样队列长度保持上限", len(plugin._synth_durations), 10)

raise SystemExit(checker.report())
