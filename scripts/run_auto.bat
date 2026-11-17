@echo off
chcp 65001 >nul
rem DNSHE 自动续期（供计划任务/手动双击调用），附加参数会原样传给 dnshe.py
cd /d "%~dp0.."
python dnshe.py auto %*
exit /b %ERRORLEVEL%
