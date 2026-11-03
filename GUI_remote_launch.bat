@echo off
rem Remote mode: shows the server sessions and opens a local terminal via ssh on the "term" button.
rem 1) Replace SERVER_URL below with your Web monitor URL (e.g. https://your-host.example.ts.net:8765)
rem 2) Put the token (contents of /etc/claude-monitor/token on the server) in %USERPROFILE%\.claude_monitor_token
rem 3) ssh must reach SSH_HOST without a password prompt (ssh config / Tailscale SSH)
cd /d "%~dp0"
set SERVER_URL=https://REPLACE-WITH-SERVER-HOST:8765
set SSH_HOST=root@claudecode
start "" pyw claude_monitor.py --server %SERVER_URL% --token-file "%USERPROFILE%\.claude_monitor_token" --ssh-host %SSH_HOST%
