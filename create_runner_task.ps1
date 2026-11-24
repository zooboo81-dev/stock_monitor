# 建立 Windows 排程「\理財\台股準時跑」：每 5 分鐘背景叫一次 local_runner.py tick（無黑窗）
# local_runner.py 會自己看時刻表（data\local_schedule.json），到時間的工作才跑，跑完自動推上 GitHub。
# 由「安裝本機準時跑排程.bat」呼叫；檔案要存 UTF-8 with BOM（PowerShell 5.1 讀中文才不會亂碼）
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 排程環境找不到 WindowsApps 的別名，一定要用 pythonw.exe 完整路徑；Python 升級後重跑這支即可
$pyw = Join-Path (Split-Path -Parent (& py -3 -c 'import sys; print(sys.executable)')) 'pythonw.exe'
if (-not (Test-Path $pyw)) { throw "找不到 $pyw" }
$name = '台股準時跑'
$path = '\理財\'
$act = New-ScheduledTaskAction -Execute $pyw -Argument '-X utf8 local_runner.py tick' -WorkingDirectory $dir
# 從今天 00:00:20 起每 5 分鐘一次、不設期限（每個 :00:20、:05:20 …，對得上 09:00、14:10、21:35 這些時刻）
$trg = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date.AddSeconds(20)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 50) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -TaskPath $path -Action $act -Trigger $trg -Settings $set -Force | Out-Null
$t = Get-ScheduledTask -TaskName $name -TaskPath $path
$i = $t | Get-ScheduledTaskInfo
Write-Host ("完成：{0}{1}　狀態 {2}　下次執行 {3}" -f $t.TaskPath, $t.TaskName, $t.State, $i.NextRunTime)
Write-Host '要暫停：工作排程器 → 理財 → 台股準時跑 → 停用（或 schtasks /Change /TN "\理財\台股準時跑" /DISABLE）'
Write-Host '只想關掉其中一項：改 data\local_schedule.json 的 enabled'
