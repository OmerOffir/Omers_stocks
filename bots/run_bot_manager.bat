@echo off
setlocal

REM go to your repo root
cd /d "C:\Users\user\PycharmProjects\Omers_stocks"

REM ensure this folder is on PYTHONPATH so 'bots' imports work
set "PYTHONPATH=%CD%"
chcp 65001 >nul

REM pick the Python you use in VS Code
set "PYTHON_EXE=C:\Users\user\AppData\Local\Microsoft\WindowsApps\python.exe"

REM run as module from the repo root
"%PYTHON_EXE%" -u -m bots.bot_meneger

pause
