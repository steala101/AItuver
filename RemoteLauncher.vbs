Option Explicit

' Neuro Remote Launcher - Windows silent entry point.
' WScript.Shell.Run with window style 0 keeps both pythonw.exe and the
' python.exe fallback completely hidden.  Runtime diagnostics remain in
' logs\remote_launcher.log and the tray status window.

Dim shell, fso, baseDir, pythonExe, launcherScript, args, command, i
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcherScript = fso.BuildPath(baseDir, "remote_launcher.py")

pythonExe = fso.BuildPath(baseDir, ".venv\Scripts\pythonw.exe")
If Not fso.FileExists(pythonExe) Then
    pythonExe = fso.BuildPath(baseDir, ".venv\Scripts\python.exe")
End If
If Not fso.FileExists(pythonExe) Then
    pythonExe = "pythonw.exe"
End If

args = ""
For i = 0 To WScript.Arguments.Count - 1
    args = args & " " & QuoteArgument(WScript.Arguments(i))
Next

shell.CurrentDirectory = baseDir
command = QuoteArgument(pythonExe) & " " & QuoteArgument(launcherScript) & args
shell.Run command, 0, False

Function QuoteArgument(value)
    QuoteArgument = Chr(34) & Replace(CStr(value), Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
