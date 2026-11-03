@echo off
rem Debug launcher: runs with a console (py) so errors stay visible. Log: %USERPROFILE%\.claude_monitor\app.log
cd /d "%~dp0"
set SERVER_URL=https://REPLACE-WITH-SERVER-HOST:8765
set SSH_HOST=root@claudecode
py claude_monitor.py --server %SERVER_URL% --token-file "%USERPROFILE%\.claude_monitor_token" --ssh-host %SSH_HOST%
pause
