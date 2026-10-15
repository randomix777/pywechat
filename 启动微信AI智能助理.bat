@echo off
chcp 65001 >nul
rem 双击运行「微信AI智能助理」。
rem 商户不需要打开命令行、不需要设置 PYTHONPATH、不需要装任何环境变量。
rem 这个脚本只做一件事：找到可用的 Python 并启动软件。

setlocal
set "APPDIR=%~dp0"
cd /d "%APPDIR%"

rem 1) 优先使用随软件附带的 Python
if exist "%APPDIR%runtime\python.exe" (
    "%APPDIR%runtime\python.exe" "%APPDIR%src\pyweixin\launcher.py" %*
    goto :done
)

rem 2) 其次使用 Windows 的 Python 启动器
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%APPDIR%src\pyweixin\launcher.py" %*
    goto :done
)

rem 3) 再次使用 PATH 里的 python
where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python "%APPDIR%src\pyweixin\launcher.py" %*
    goto :done
)

echo.
echo   没有找到可以运行的 Python。
echo.
echo   请重新运行安装程序，选择「完整安装」。
echo   安装完成后请重新安装「Python 3.10 或更高版本」，
echo   安装时务必勾选 "Add Python to PATH"。
echo.
pause

:done
endlocal
