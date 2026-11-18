# 建立 Windows 排程「台新交割憑單同步」：每天 21:30 背景跑 sync_bills.py（無黑窗）
# 由 建立排程.bat 呼叫；檔案要存 UTF-8 with BOM（PowerShell 5.1 讀中文才不會亂碼）
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 排程環境找不到 WindowsApps 的 pyw.exe 別名（錯誤 0x80070002），一定要用 pythonw.exe 完整路徑；Python 升級後重跑這支即可
$pyw = Join-Path (Split-Path -Parent (& py -3 -c 'import sys; print(sys.executable)')) 'pythonw.exe'
if (-not (Test-Path $pyw)) { throw "找不到 $pyw" }
$act = New-ScheduledTaskAction -Execute $pyw -Argument '-X utf8 sync_bills.py' -WorkingDirectory $dir
$trg = New-ScheduledTaskTrigger -Daily -At 21:30
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '台新交割憑單同步' -Action $act -Trigger $trg -Settings $set -Force | Out-Null
$t = Get-ScheduledTask -TaskName '台新交割憑單同步'
$i = $t | Get-ScheduledTaskInfo
Write-Host ("完成：{0}　狀態 {1}　下次執行 {2}" -f $t.TaskName, $t.State, $i.NextRunTime)
Write-Host '若要取消：schtasks /Delete /TN "台新交割憑單同步" /F'
