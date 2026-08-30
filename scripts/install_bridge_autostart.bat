@echo off
rem One-time: auto-start the WSN bridge daemon at logon. Usage: install_bridge_autostart.bat [port]
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\web-search-neo-bridge-%PORT%.bat"
(
echo @echo off
echo call "%~dp0bridge_autostart.bat" %PORT%
) > "%STARTUP%"
echo Installed: %STARTUP%
