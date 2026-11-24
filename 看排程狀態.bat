@echo off
rem Show when each stock job last ran on this PC and when it runs next. ASCII only on purpose.
chcp 65001 >nul
cd /d "%~dp0"
py -3 -X utf8 local_runner.py status
echo.
pause
