' Запускает сервер Berta СКРЫТО (без окна консоли) — фоновая работа на ПК.
' Двойной клик по этому файлу, либо автозапуск через Планировщик задач.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
run = """" & base & "\.venv\Scripts\pythonw.exe"" main.py --host=0.0.0.0 --no-browser"
' 0 = скрытое окно, False = не ждать завершения
sh.Run run, 0, False
