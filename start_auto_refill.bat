@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "config.auto_refill.json" (
  echo [info] 首次运行：生成 config.auto_refill.json
  python auto_refill_sub2api.py --init-config -c config.auto_refill.json
  echo.
  echo 请编辑 config.auto_refill.json 填入密码 / 分组名后，再重新运行本脚本。
  pause
  exit /b 1
)

echo [start] auto refill sub2api
python auto_refill_sub2api.py -c config.auto_refill.json %*
if errorlevel 1 pause
