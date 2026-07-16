@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "config.auto_refill.json" (
  echo [info] 生成默认配置 config.auto_refill.json
  python -c "import auto_refill_sub2api as c; c.save_json(c.DEFAULT_CONFIG, c.default_config()); print(c.DEFAULT_CONFIG)"
)

echo.
echo  号池控制面板启动中…
echo  浏览器打开: http://127.0.0.1:8787
echo  按 Ctrl+C 停止
echo.
python panel_server.py
if errorlevel 1 pause
