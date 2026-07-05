Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\zoobo\Documents\stock_monitor"
sh.Run """C:\Users\zoobo\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"" -m streamlit run app.py --server.headless true --browser.gatherUsageStats false --server.port 8501", 0, False
