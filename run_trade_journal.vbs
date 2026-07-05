Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\zoobo\Documents\stock_monitor"
sh.Run """C:\Users\zoobo\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"" trade_journal.py", 0, False
