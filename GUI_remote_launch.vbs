' Launches the remote GUI monitor with no console window at all (a .bat always flashes one).
' Double-click this file, or put a shortcut to it in shell:startup. Errors: %USERPROFILE%\.claude_monitor\app.log
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "pyw claude_monitor.py --remote", 0, False
