' Starts the Berta server hidden (no console window) for background PC use.
' Double-click this file to start; run stop_phone.cmd to stop.
' Output is written to phone_server.log next to main.py.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
py = """" & base & "\.venv\Scripts\python.exe"" main.py --host=0.0.0.0 --no-browser"
logPath = """" & base & "\phone_server.log"""
run = "cmd /c " & py & " >> " & logPath & " 2>&1"
' 0 = hidden window, False = do not wait for completion
sh.Run run, 0, False
