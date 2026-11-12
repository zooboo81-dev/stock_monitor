@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 建立 Windows 排程「台新交割憑單同步」：每天 21:30 背景執行（錯過會補跑）
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir = '%~dp0'.TrimEnd('\');" ^
  "$act = New-ScheduledTaskAction -Execute 'pyw.exe' -Argument '-3 -X utf8 sync_bills.py' -WorkingDirectory $dir;" ^
  "$trg = New-ScheduledTaskTrigger -Daily -At 21:30;" ^
  "$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew;" ^
  "Register-ScheduledTask -TaskName '台新交割憑單同步' -Action $act -Trigger $trg -Settings $set -Force | Out-Null;" ^
  "Write-Host '完成：' (Get-ScheduledTask -TaskName '台新交割憑單同步').State"
echo.
echo 若要取消排程，在命令提示字元執行：schtasks /Delete /TN "台新交割憑單同步" /F
pause
