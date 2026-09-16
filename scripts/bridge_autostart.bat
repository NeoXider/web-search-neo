@echo off
rem WSN bridge daemon launcher. Usage: bridge_autostart.bat [port]
rem Port precedence: argument, then WEB_SEARCH_NEO_BRIDGE_PORT env var, else 8765.
setlocal EnableExtensions
cd /d "%~dp0.."
if not "%~1"=="" set "WEB_SEARCH_NEO_BRIDGE_PORT=%~1"
rem An autostarted bridge must outlive long Chrome-free gaps: disable the idle exit.
set "WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS=0"
if exist ".venv\Scripts\pythonw.exe" goto :windowless
if exist ".venv\Scripts\python.exe" goto :console
goto :no_venv

:windowless
start "" ".venv\Scripts\pythonw.exe" -m web_search_neo.main --bridge
exit /b 0

:console
start "WSN Bridge" /min cmd /c ".venv\Scripts\python.exe -m web_search_neo.main --bridge"
exit /b 0

:no_venv
rem This runs unattended at logon, so the failure goes to a log a person can find.
set "LOGDIR=%LOCALAPPDATA%\WebSearchNeo"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
>>"%LOGDIR%\bridge-autostart.log" echo [%DATE% %TIME%] bridge not started: no .venv\Scripts\python.exe in "%CD%"; create the virtualenv and install the requirements first.
exit /b 1
