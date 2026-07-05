# Streamlit Watchdog - check every 5 min, restart if dead
$url = 'http://localhost:8501/_stcore/health'
$logFile = 'C:\Users\zoobo\Documents\stock_monitor\watchdog.log'
$vbs = 'C:\Users\zoobo\Documents\stock_monitor\start_hidden.vbs'

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# 3 health checks; only declare dead if all fail
$alive = $false
for ($i = 1; $i -le 3; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $alive = $true; break }
    } catch { Start-Sleep -Seconds 2 }
}

if ($alive) { exit 0 }

Write-Log "Streamlit not responding, preparing restart"

# Kill old streamlit processes
try { Get-Process -Name streamlit -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}

# Kill stray pythonw that runs streamlit
try {
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        if ($p.CommandLine -like '*streamlit*') {
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
} catch {}

Start-Sleep -Seconds 3

# Restart via VBS (hidden launcher)
$restartOk = $true
try {
    & wscript.exe $vbs
    Write-Log "Restarted via VBS"
} catch {
    Write-Log "Restart failed: $_"
    $restartOk = $false
}

if (-not $restartOk) { exit 1 }

# Verify after 20 seconds
Start-Sleep -Seconds 20
$verified = $false
try {
    $r = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    if ($r.StatusCode -eq 200) { $verified = $true }
} catch {}

if ($verified) {
    Write-Log "Restart success"
} else {
    Write-Log "Streamlit still unhealthy after restart (may need more time)"
}

# Trim log to last 200 lines
try {
    if (Test-Path $logFile) {
        $lines = Get-Content $logFile -Encoding UTF8
        if ($lines.Count -gt 200) {
            $lines | Select-Object -Last 200 | Set-Content -Path $logFile -Encoding UTF8
        }
    }
} catch {}
