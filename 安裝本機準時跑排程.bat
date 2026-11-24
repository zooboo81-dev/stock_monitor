@echo off
rem Create the every-5-minutes Windows task that runs local_runner.py (stock jobs on this PC).
rem ASCII only on purpose (cmd mangles UTF-8 batch files).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_runner_task.ps1"
echo.
pause
