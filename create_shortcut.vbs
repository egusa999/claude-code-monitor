' Creates "Claude Code Monitor.lnk" (with the app icon) next to this file and on the Desktop.
' Double-click once on the Windows PC. The shortcut runs GUI_remote_launch.vbs (no console window).
' Re-run it if you move this folder. Pin the shortcut to the taskbar / Start, or copy it to shell:startup.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = dir & "\GUI_remote_launch.vbs"
icon = dir & "\claude_monitor.ico"
If Not fso.FileExists(launcher) Or Not fso.FileExists(icon) Then
    MsgBox "GUI_remote_launch.vbs or claude_monitor.ico was not found next to this file." & vbCrLf & dir, vbCritical, "Claude Code Monitor"
    WScript.Quit 1
End If
For Each place In Array(dir, sh.SpecialFolders("Desktop"))
    Set lnk = sh.CreateShortcut(place & "\Claude Code Monitor.lnk")
    lnk.TargetPath = sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\wscript.exe"
    lnk.Arguments = """" & launcher & """"
    lnk.WorkingDirectory = dir
    lnk.IconLocation = icon & ",0"
    lnk.Description = "Claude Code Monitor"
    lnk.WindowStyle = 7
    lnk.Save
Next
MsgBox "Created: Claude Code Monitor.lnk (this folder and Desktop)", vbInformation, "Claude Code Monitor"
