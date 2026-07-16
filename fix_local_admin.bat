@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [fix] 停止卡死注册会话，恢复本地后台...
python fix_local_admin.py %*
if errorlevel 1 pause
