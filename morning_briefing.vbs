' Hidden launcher for morning_briefing.py
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\zoobo\Documents\stock_monitor"
sh.Run """C:\Users\zoobo\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"" morning_briefing.py", 0, False
