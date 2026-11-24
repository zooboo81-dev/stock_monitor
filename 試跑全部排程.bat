@echo off
rem Dry-run every stock job in a sandbox copy with odd test rows (no phone pushes) to catch crashes early.
rem ASCII only on purpose (cmd mangles UTF-8 batch files).
chcp 65001 >nul
cd /d "%~dp0"
py -3 -X utf8 job_selftest.py
echo.
pause
