@echo off
rem Remote mode. On first run (or via the gear icon) enter server URL / token / SSH host in the dialog.
rem Settings are saved to %USERPROFILE%\.claude_monitor\config.json. Errors: %USERPROFILE%\.claude_monitor\app.log
cd /d "%~dp0"
start "" pyw claude_monitor.py --remote
