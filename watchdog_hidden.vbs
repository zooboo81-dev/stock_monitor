' Invisible launcher for watchdog.ps1 (no black flash)
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\zoobo\Documents\stock_monitor\watchdog.ps1""", 0, False
