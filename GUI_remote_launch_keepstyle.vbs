' TEST launcher: the Keep-style title bar (min/max/close drawn by the app) on Windows.
' The normal launcher (GUI_remote_launch.vbs) uses the standard Windows title bar.
' If the screen ever stops responding: Ctrl+Alt+Del -> Task Manager -> end "pythonw". Log: %USERPROFILE%\.claude_monitor\app.log
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Environment("PROCESS")("CLAUDE_MONITOR_KEEP_STYLE") = "1"
sh.Run "pyw claude_monitor.py --remote", 0, False
