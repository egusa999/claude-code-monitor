@echo off
rem Debug launcher: runs with a console (py) so errors stay visible. Log: %USERPROFILE%\.claude_monitor\app.log
cd /d "%~dp0"
py claude_monitor.py --remote
pause
