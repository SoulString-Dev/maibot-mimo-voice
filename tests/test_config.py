"""配置侧测试：默认值、api_base_url 守卫、告警去重、版本一致性。"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from __bootstrap import REPO_ROOT, Checker, config_module, load_plugin, make_plugin

module = load_plugin()
cfg = config_module(module)
checker = Checker("配置侧")

# ---------------------------------------------------------------- 默认值
plugin, config, _ = make_plugin(module)
defaults = {
    "config_version": config.plugin.config_version,
    "mode": config.trigger.mode,
    "probability": config.trigger.probability,
    "english_policy": config.trigger.english_policy,
    "random_requires_chinese": config.trigger.random_requires_chinese,
    "decision_ttl": config.trigger.decision_ttl,
    "max_text_length": config.general.max_text_length,
    "timeout": config.general.timeout,
    "temperature": config.mimo.temperature,
    "retry_on_timeout": config.mimo.retry_on_timeout,
    "timbre_match": config.delivery.timbre_match,
    "trim_tail_silence_ms": config.delivery.trim_tail_silence_ms,
    "tail_pad_ms": config.delivery.tail_pad_ms,
    "peak_normalize": config.delivery.peak_normalize,
    "api_base_url": config.mimo.api_base_url,
    "allow_private_base_url": config.mimo.allow_private_base_url,
}
expected = {
    "mode": "random",
    "probability": 0.15,
    "english_policy": "strip_tics",
    "random_requires_chinese": True,
    "decision_ttl": 10.0,
    "max_text_length": 300,
    "timeout": 20.0,
    "temperature": 0.15,
    "retry_on_timeout": True,
    "timbre_match": True,
    "trim_tail_silence_ms": 250,
    "tail_pad_ms": 120,
    "peak_normalize": True,
    "api_base_url": "https://api.xiaomimimo.com/v1",
    "allow_private_base_url": False,
}
for key, want in expected.items():
    checker.equal(f"默认 {key}", defaults[key], want)

# ---------------------------------------------------------------- 音色别名
checker.equal("默认音色收敛成 id", cfg.MiMoVoiceConfig().voice.preset, "bingtang")
for raw, want in (
    ("冰糖", "bingtang"),
    ("茉莉", "jasmine"),
    ("理塘王", "litang"),
    ("自定义", "custom"),
    ("bingtang", "bingtang"),
    (" Litang ", "litang"),
):
    checker.equal(f"音色别名 {raw!r}", cfg.MiMoVoiceConfig(voice={"preset": raw}).voice.preset, want)
builtin_files = {path.name for path in REPO_ROOT.glob("reference-*.wav")}
checker.check("内置音色文件齐全",
              {"reference-bingtang.wav", "reference-jasmine.wav", "reference-litang.wav"} <= builtin_files,
              f"{sorted(builtin_files)}")

# ---------------------------------------------------------------- 地址守卫
checker.equal("官方地址放行", cfg._api_base_url_problem("https://api.xiaomimimo.com/v1", allow_private=False), "")
checker.equal("http 被拒", cfg._api_base_url_problem("http://api.xiaomimimo.com/v1", allow_private=False), "不是 https")
for bad in ("https://localhost:8000", "https://127.0.0.1/v1", "https://192.168.1.10/v1", "https://169.254.169.254/latest", "https://[::1]/v1"):
    checker.equal(f"内网被拒 {bad}", cfg._api_base_url_problem(bad, allow_private=False), "指向本机或内网地址")
checker.equal("允许私有后放行", cfg._api_base_url_problem("http://192.168.1.10:8000/v1", allow_private=True), "")

# 非法地址在构造配置时就回退，并留下告警
bad_config = cfg.MiMoVoiceConfig(mimo={"api_base_url": "http://127.0.0.1:8000"})
checker.equal("非法配置回退官方地址", bad_config.mimo.api_base_url, "https://api.xiaomimimo.com/v1")
warnings = list(cfg._CONFIG_WARNINGS)
checker.check("回退时写入配置告警", any("api_base_url" in item for item in warnings), f"{warnings[:2]}")
cfg._CONFIG_WARNINGS.clear()

# ---------------------------------------------------------------- 告警去重
inst, _, logger = make_plugin(module)
inst._warn_once("demo:path", "第一次 %s", "a")
inst._warn_once("demo:path", "第二次 %s", "b")
inst._warn_once("demo:other", "另一类")
checker.equal("同一 key 只打一次，不同 key 各打一次", logger.messages("warning"), ["第一次 a", "另一类"])

# 配置告警按桶去重：同类只展开一条
cfg._CONFIG_WARNINGS.extend(
    [
        "mimo.api_base_url: 第 1 条",
        "mimo.api_base_url: 第 2 条",
        "general.timeout: 第 3 条",
        "general.max_text_length: 第 4 条",
    ]
)
inst2, _, logger2 = make_plugin(module)
inst2._flush_config_warnings()
checker.equal(
    "配置告警按节去重",
    logger2.messages("warning"),
    ["mimo.api_base_url: 第 1 条", "general.timeout: 第 3 条", "general.max_text_length: 第 4 条"],
)
checker.equal("清理后桶为空", cfg._CONFIG_WARNINGS, [])

# ---------------------------------------------------------------- 版本一致性
manifest = json.loads((REPO_ROOT / "_manifest.json").read_text(encoding="utf-8"))
template = tomllib.loads((REPO_ROOT / "config.example.toml").read_text(encoding="utf-8"))
checker.equal("manifest 版本", manifest["version"], config.plugin.config_version)
checker.equal("模板 config_version", template["plugin"]["config_version"], config.plugin.config_version)
checker.equal("sdk 上限不含 99 后缀", manifest["sdk"]["max_version"], "2.12.99")
checker.check("模板键集合包含模型全部字段", set(template.keys()) == {
    "plugin", "general", "trigger", "filters", "voice", "mimo", "delivery"
}, f"{sorted(template.keys())}")

sys.exit(checker.report())
