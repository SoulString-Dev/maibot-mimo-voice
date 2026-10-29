"""测试公共底座：加载插件模块、伪造宿主上下文、逐条断言。

用法（容器内或本地）：
    python tests/run_all.py

仓库里没有 pytest 依赖，全部用标准库，保证在 MaiBot 容器里直接能跑。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_FILE = REPO_ROOT / "plugin.py"


class FakeLogger:
    """记录日志，供断言"有没有打出某条告警"。"""

    def __init__(self) -> None:
        self.records: List[tuple[str, str]] = []

    def _push(self, level: str, message: str, *args: Any) -> None:
        text = message % args if args else message
        self.records.append((level, text))

    def debug(self, message: str, *args: Any) -> None:
        self._push("debug", message, *args)

    def info(self, message: str, *args: Any) -> None:
        self._push("info", message, *args)

    def warning(self, message: str, *args: Any) -> None:
        self._push("warning", message, *args)

    def error(self, message: str, *args: Any) -> None:
        self._push("error", message, *args)

    def messages(self, level: str | None = None) -> List[str]:
        return [text for lvl, text in self.records if level is None or lvl == level]


class FakeCtx:
    """只实现插件用到的 ctx 能力：logger / paths。"""

    def __init__(self, logger: FakeLogger, data_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
        self.logger = logger
        self.paths = type("Paths", (), {"data_dir": data_dir, "runtime_dir": runtime_dir})()


def load_plugin() -> ModuleType:
    """按文件路径加载 plugin.py（避免依赖包名与 sys.path）。"""
    spec = importlib.util.spec_from_file_location("maibot_mimo_voice_plugin", PLUGIN_FILE)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"无法加载 {PLUGIN_FILE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # `from __future__ import annotations` 会让 pydantic 拿到字符串注解，需要重建一次
    namespaces = vars(module)
    for name in (
        "PluginSectionConfig",
        "GeneralConfig",
        "TriggerConfig",
        "FilterConfig",
        "VoiceConfig",
        "MiMoConfig",
        "DeliveryConfig",
        "MiMoVoiceConfig",
    ):
        getattr(module, name).model_rebuild(_types_namespace=namespaces)
    return module


def make_plugin(module: ModuleType, **overrides: Any):
    """构造一个不经过宿主 on_load 的插件实例，配置用默认值。"""
    plugin = module.MiMoVoicePlugin()
    config = module.MiMoVoiceConfig(**overrides)
    plugin.__dict__["_plugin_config_instance"] = config
    logger = FakeLogger()
    plugin.__dict__["_ctx"] = FakeCtx(logger)
    plugin.__dict__["_warned"] = set()
    return plugin, config, logger


class Checker:
    """极简断言器：失败就记下来，最后统一汇报，便于一次看全。"""

    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failures: List[str] = []
        print(f"\n[{title}]")

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  ok   {name}" + (f"  ({detail})" if detail else ""))
        else:
            self.failures.append(name + (f"  [{detail}]" if detail else ""))
            print(f"  FAIL {name}" + (f"  ({detail})" if detail else ""))

    def equal(self, name: str, actual: Any, expected: Any) -> None:
        self.check(name, actual == expected, f"{actual!r} != {expected!r}" if actual != expected else "")

    def report(self) -> int:
        total = self.passed + len(self.failures)
        print(f"  → {self.title}: {self.passed}/{total} 通过")
        for item in self.failures:
            print(f"     失败：{item}")
        return len(self.failures)


def silence_logging() -> None:
    logging.getLogger("maibot.plugin.maibot-mimo-voice").addHandler(logging.NullHandler())
