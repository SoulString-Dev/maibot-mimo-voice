"""跑全部测试：python tests/run_all.py

逐个文件跑（每个文件自己退出码非 0 即失败），最后汇总。
不依赖 pytest / 宿主环境，容器内 `/MaiMBot/.venv/bin/python` 可直接跑。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 子进程强制 UTF-8：Windows 默认 GBK 控制台会让测试里的 emoji 直接 UnicodeEncodeError。
CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
SUITES = ["test_text.py", "test_audio.py", "test_config.py", "test_timeout.py", "test_trigger.py", "test_commands.py", "test_triggers.py", "test_schedule.py", "test_delivery.py", "test_api.py", "test_scheduled.py",
    "test_song_library.py", "test_audio_probe.py", "test_songs.py"]


def main() -> int:
    # GBK 控制台（Windows 默认）碰到测试里的 emoji / 替换字符会 UnicodeEncodeError 崩掉，
    # 这里把不可编码字符降级成 ?，避免「测试全过但汇总脚本自己挂了」。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")

    failed: list[tuple[str, str]] = []
    for name in SUITES:
        path = HERE / name
        print(f"\n=== {name} ===")
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=CHILD_ENV,
        )
        output = (result.stdout or "") + (result.stderr or "")
        print(output.rstrip())
        if result.returncode != 0:
            failed.append((name, output.strip().splitlines()[-1] if output.strip() else f"exit {result.returncode}"))

    print("\n" + "=" * 50)
    if failed:
        print(f"FAILED: {len(failed)}/{len(SUITES)} 个文件未通过")
        for name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    print(f"全部通过：{len(SUITES)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
