"""跑全部测试：python tests/run_all.py

逐个文件跑（每个文件自己退出码非 0 即失败），最后汇总。
不依赖 pytest / 宿主环境，容器内 `/MaiMBot/.venv/bin/python` 可直接跑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["test_text.py", "test_audio.py", "test_config.py"]


def main() -> int:
    failed: list[tuple[str, str]] = []
    for name in SUITES:
        path = HERE / name
        print(f"\n=== {name} ===")
        result = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
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
