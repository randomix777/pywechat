' 双击运行「微信AI智能助理」，不弹出黑色命令行窗口。
' 商户看到的只有一个软件窗口。
Option Explicit

Dim shell, fso, appDir, pythonExe, cmd

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)

' 1) 随软件附带的 Python
pythonExe = appDir & "\runtime\python.exe"
If fso.FileExists(pythonExe) Then
    cmd = """" & pythonExe & """ """ & appDir & "\src\pyweixin\launcher.py"""
' 2) Windows 的 Python 启动器
Else
    cmd = "py -3 """ & appDir & "\src\pyweixin\launcher.py"""
End If

' 隐藏窗口运行；启动器自己会在出错时弹出中文说明。
shell.Run cmd, 0, False
