@echo off
rem Run the Cathay US-stock statement sync once and keep the window open.
rem ASCII only on purpose (cmd mangles UTF-8 batch files); all Chinese text comes from the Python script.
cd /d "%~dp0"
py -3 -X utf8 sync_cathay.py %*
echo.
pause
