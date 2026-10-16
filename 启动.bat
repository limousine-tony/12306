@echo off
rem 12306 抢票控制台 - 双击启动图形界面（无命令行窗口）
cd /d "%~dp0"

if exist ".venv314\Scripts\pythonw.exe" (
    start "" ".venv314\Scripts\pythonw.exe" launcher.py
    exit /b
)
if exist ".venv314\Scripts\python.exe" (
    start "" ".venv314\Scripts\python.exe" launcher.py
    exit /b
)

rem 未创建虚拟环境时用系统 Python
where pythonw >nul 2>nul && (
    start "" pythonw launcher.py
    exit /b
)
where python >nul 2>nul && (
    start "" python launcher.py
    exit /b
)

echo Python 未安装，请先安装 Python 3.10+ 再双击本文件。
pause
