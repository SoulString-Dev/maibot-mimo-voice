# Windows 侧跑 lint + 测试（等价于 WSL 里的三件套）
#
# 用法：pwsh -File tools/run_checks.ps1
#
# 与 WSL 不同的两点：
#   * PYTHONIOENCODING 必须是 utf-8，否则测试里的 emoji 打印在 GBK 控制台会抛 UnicodeEncodeError
#   * ruff 加 --no-cache：工作区目录带 Deny Delete 的 ACL，缓存清理会报 os error 5
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "== ruff =="
python -m ruff check --no-cache .
Write-Host "== mypy =="
python -m mypy plugin.py core
Write-Host "== tests =="
python tests/run_all.py
