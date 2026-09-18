@echo off
rem WSN bridge daemon launcher. Usage: bridge_autostart.bat [port]
rem Port precedence: argument, then WEB_SEARCH_NEO_BRIDGE_PORT env var, else 8765.
setlocal EnableExtensions
cd /d "%~dp0.."
if not "%~1"=="" set "WEB_SEARCH_NEO_BRIDGE_PORT=%~1"
rem An autostarted bridge must outlive long Chrome-free gaps: disable the idle exit.
set "WEB_SEARCH_NEO_BRIDGE_IDLE_SECONDS=0"
if exist ".venv\Scripts\pythonw.exe" start "" ".venv\Scripts\pythonw.exe" -m web_search_neo.main --bridge
if exist ".venv\Scripts\pythonw.exe" exit /b 0
rem This runs unattended at logon, so the failure goes to a log a person can find.
rem A console interpreter is deliberately not a fallback here: under a logon
rem session with no console it would own a visible window for as long as the
rem daemon lives, which is exactly what autostart must never do.
set "LOGDIR=%LOCALAPPDATA%\WebSearchNeo"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
>>"%LOGDIR%\bridge-autostart.log" echo [%DATE% %TIME%] bridge not started: no .venv\Scripts\pythonw.exe in "%CD%"; create the virtualenv and install the requirements first.
exit /b 1
