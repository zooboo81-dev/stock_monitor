@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 台新交割憑單同步 ===
py -3 -X utf8 sync_bills.py %*
echo.
pause
