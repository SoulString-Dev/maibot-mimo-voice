#!/usr/bin/env python3
"""插件配置兼容性小工具（开发/排查用，不参与运行时）。

0.5.5 起命令层相关配置放在 ``[commands]`` 段落。这个脚本把老配置里"写错位置"
的项挪回去，避免宿主把空的 ``[commands]`` 段当成"用户已经配过"而不再生成默认值。

用法（容器内，插件目录下）::

    python3 tools/config_compat.py check                    # 只检查
    python3 tools/config_compat.py fix config.toml           # 就地修（先备份）
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# 老版本里被写进 [plugin] 或别的段落的命令配置
_COMMAND_KEYS = ("enabled", "admins", "test_max_chars")
_COMMAND_SECTION = "commands"

PREFERRED_BY_KEY = {
    "enabled": "plugin",
    "admins": "commands",
    "test_max_chars": "commands",
}


def _parse(text: str):
    sections = {}  # name -> {key: raw value string}
    order = []
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped.strip("[]").strip()
            sections.setdefault(current, {})
            order.append(current)
            continue
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        sections.setdefault(current, {})[key.strip()] = raw.strip()
    return sections, order


def check(path: Path) -> list[str]:
    """返回问题列表；空列表表示没问题。"""
    sections, _ = _parse(path.read_text(encoding="utf-8"))
    problems = []
    cmd = sections.get(_COMMAND_SECTION)
    other = sections.get("plugin", {})
    if cmd is not None and not any(key in cmd for key in _COMMAND_KEYS):
        problems.append(
            f"[{_COMMAND_SECTION}] 段存在但没有任何命令配置（宿主可能因此不生成默认项）"
        )
    for key in _COMMAND_KEYS:
        want = PREFERRED_BY_KEY[key]
        if key in other and want != "plugin":
            problems.append(f"plugin.{key} 应放在 [{want}] 段")
    return problems


def fix(path: Path) -> None:
    """把错位的命令配置搬到 [commands]：只改文本，不动注释与其它段。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    moved: list[str] = []
    out: list[str] = []
    in_plugin = False
    has_commands = any(line.strip() == f"[{_COMMAND_SECTION}]" for line in lines)
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_plugin = stripped.strip("[]").strip() == "plugin"
            out.append(line)
            continue
        if in_plugin and "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if PREFERRED_BY_KEY.get(key) == "commands":
                moved.append(f"{key} = {stripped.split('=', 1)[1].strip()}")
                continue
        out.append(line)
    if not moved:
        print("无需修改")
        return
    if not has_commands:
        out.extend(["", f"[{_COMMAND_SECTION}]", "# 由 config_compat.py 迁移"])
    insert_at = next(
        (i for i, line in enumerate(out) if line.strip() == f"[{_COMMAND_SECTION}]"),
        len(out),
    )
    out[insert_at + 1 : insert_at + 1] = moved
    backup = path.with_suffix(path.suffix + ".bak-compat")
    shutil.copyfile(path, backup)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"已迁移 {len(moved)} 项到 [{_COMMAND_SECTION}]，原文件备份为 {backup.name}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"check", "fix"}:
        print(__doc__)
        return 2
    path = Path(argv[2] if len(argv) > 2 else "config.toml")
    if not path.exists():
        print(f"找不到 {path}")
        return 1
    if argv[1] == "check":
        problems = check(path)
        if not problems:
            print("配置结构没问题")
            return 0
        for item in problems:
            print(f"- {item}")
        return 1
    fix(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
