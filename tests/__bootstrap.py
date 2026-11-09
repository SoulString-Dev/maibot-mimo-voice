"""测试公共底座：加载插件模块、伪造宿主上下文、逐条断言。

用法（容器内或本地）：
    python tests/run_all.py

仓库里没有 pytest 依赖，全部用标准库，保证在 MaiBot 容器里直接能跑。
"""
from __future__ import annotations

import contextlib
import importlib.util
import logging
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, List

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_FILE = REPO_ROOT / "plugin.py"


@contextlib.contextmanager
def temp_dir() -> Iterator[Path]:
    """临时目录，替代 `tempfile.TemporaryDirectory`。

    Windows 上 `mkdtemp` 会显式设成 0700 的 DACL，在受限令牌（CI、沙箱）里
    连自己写进去都会被拒；这里用普通 `mkdir`（继承父目录权限），结束后尽力删除。
    """
    path = Path(tempfile.gettempdir()) / f"maibot-test-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


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


class FakeSend:
    """记录命令回执与语音发送，不碰宿主。"""

    def __init__(self) -> None:
        self.texts: List[tuple[str, str]] = []
        self.hybrids: List[tuple[str, List[dict]]] = []
        self.error: Exception | None = None

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        if self.error is not None:
            raise self.error
        self.texts.append((stream_id, text))
        return True

    async def hybrid(self, segments: List[dict], stream_id: str, **kwargs: Any) -> bool:
        if self.error is not None:
            raise self.error
        self.hybrids.append((stream_id, segments))
        return True


class FakeChat:
    """假的会话目录：``groups`` / ``users`` 是 ID → session_id 的映射。"""

    def __init__(self) -> None:
        self.groups: dict[str, str] = {}
        self.users: dict[str, str] = {}
        self.opened: List[tuple[str, str, str]] = []
        self.can_open = True

    async def get_stream_by_group_id(self, group_id: str, platform: str = "qq") -> dict:
        session_id = self.groups.get(group_id)
        if not session_id:
            return {"success": False, "stream": None}
        return {"success": True, "stream": {"session_id": session_id}}

    async def get_stream_by_user_id(self, user_id: str, platform: str = "qq") -> dict:
        session_id = self.users.get(user_id)
        if not session_id:
            return {"success": False, "stream": None}
        return {"success": True, "stream": {"session_id": session_id}}

    async def open_session(
        self,
        platform: str = "qq",
        chat_type: str = "private",
        *,
        user_id: str = "",
        group_id: str = "",
        account_id: str = "",
        scope: str = "",
        **kwargs: Any,
    ) -> dict:
        if not self.can_open:
            return {"success": False, "created": False, "stream": None, "error": "无法新建"}
        key = group_id if chat_type == "group" else user_id
        session_id = f"{chat_type}:{key}"
        self.opened.append((chat_type, key, platform))
        if chat_type == "group":
            self.groups[key] = session_id
        else:
            self.users[key] = session_id
        return {"success": True, "created": True, "stream": {"session_id": session_id}}


class FakeCtx:
    """只实现插件用到的 ctx 能力：logger / paths / send / chat。"""

    def __init__(self, logger: FakeLogger, data_dir: Path | None = None, runtime_dir: Path | None = None) -> None:
        self.logger = logger
        self.paths = type("Paths", (), {"data_dir": data_dir, "runtime_dir": runtime_dir})()
        self.send = FakeSend()
        self.chat = FakeChat()


def load_plugin() -> ModuleType:
    """按文件路径加载 plugin.py（避免依赖包名与 sys.path）。"""
    spec = importlib.util.spec_from_file_location(
        "maibot_mimo_voice_plugin",
        PLUGIN_FILE,
        submodule_search_locations=[str(PLUGIN_FILE.parent)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"无法加载 {PLUGIN_FILE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config_module(module: ModuleType) -> ModuleType:
    """取出插件加载时注册的 ``core.config`` 子模块（配置模型真正的归属）。"""
    return sys.modules[f"{module.__name__}.core.config"]


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
