@echo off
rem WSN bridge daemon launcher. Usage: bridge_autostart.bat [port]
rem Port precedence: argument, then WEB_SEARCH_NEO_BRIDGE_PORT env var, else 8765.
cd /d "%~dp0.."
if not "%~1"=="" set "WEB_SEARCH_NEO_BRIDGE_PORT=%~1"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m web_search_neo.main --bridge
) else (
    start "WSN Bridge" /min cmd /c ".venv\Scripts\python.exe -m web_search_neo.main --bridge"
)
