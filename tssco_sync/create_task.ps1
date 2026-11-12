# 建立 Windows 排程「台新交割憑單同步」：每天 21:30 背景跑 sync_bills.py（無黑窗）
# 由 建立排程.bat 呼叫；檔案要存 UTF-8 with BOM（PowerShell 5.1 讀中文才不會亂碼）
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$act = New-ScheduledTaskAction -Execute 'pyw.exe' -Argument '-3 -X utf8 sync_bills.py' -WorkingDirectory $dir
$trg = New-ScheduledTaskTrigger -Daily -At 21:30
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '台新交割憑單同步' -Action $act -Trigger $trg -Settings $set -Force | Out-Null
$t = Get-ScheduledTask -TaskName '台新交割憑單同步'
$i = $t | Get-ScheduledTaskInfo
Write-Host ("完成：{0}　狀態 {1}　下次執行 {2}" -f $t.TaskName, $t.State, $i.NextRunTime)
Write-Host '若要取消：schtasks /Delete /TN "台新交割憑單同步" /F'
