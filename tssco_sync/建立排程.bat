@echo off
rem Create the daily 21:30 task for Taishin bill sync. ASCII only on purpose (cmd mangles UTF-8 batch files).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\create_task.ps1"
echo.
pause
