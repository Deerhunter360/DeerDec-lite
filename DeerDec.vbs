Option Explicit
Dim sh, fso, here, ok, cmds, c, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
ok = False
' Ask cmd.exe whether each Python command works (exit code 0), then start the first one that does.
' Window style 0 = hidden, so no console shows.
cmds = Array("py", "python", "pyw", "pythonw")
On Error Resume Next
For Each c In cmds
  If Not ok Then
    rc = 1
    rc = sh.Run("cmd /c " & c & " --version >nul 2>nul", 0, True)
    If rc = 0 Then
      sh.Run "cmd /c cd /d """ & here & """ && " & c & " app.py", 0, False
      ok = True
    End If
  End If
Next
If Not ok Then
  MsgBox "Could not find Python (tried py, python, pyw, pythonw)." & vbCrLf & _
         "Run DeerDec-debug.bat to see the error, or run setup.bat.", 48, "DeerDec"
End If
