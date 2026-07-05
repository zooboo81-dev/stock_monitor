@echo off
chcp 65001 >nul
echo ========================================
echo  開放手機連線股票儀表板 (port 8501)
echo ========================================
echo.
netsh advfirewall firewall delete rule name="StockMonitor 8501" >nul 2>&1
netsh advfirewall firewall add rule name="StockMonitor 8501" dir=in action=allow protocol=TCP localport=8501 profile=any
echo.
if %errorlevel%==0 (
  echo [成功] 防火牆已開放 port 8501
  echo.
  echo  家裡同 Wi-Fi： http://192.168.1.107:8501
  echo  出門 Tailscale：http://[電腦的 100.x.x.x]:8501
) else (
  echo [失敗] 請以「系統管理員」身分執行此檔
)
echo.
pause
