@echo off
rem One-time: auto-start the WSN bridge daemon at logon. Usage: install_bridge_autostart.bat [port]
setlocal EnableExtensions EnableDelayedExpansion
set "PORT=%~1"
if not defined PORT set "PORT=8765"
rem The port is written into a script that runs at every logon, so only a plain
rem number may get there. Delayed expansion keeps "&" and friends inert here.
echo(!PORT!| findstr /r /x "[1-9][0-9]*" >nul || goto :bad_port
if "!PORT:~5!" neq "" goto :bad_port
set /a PORTNUM=!PORT!
if !PORTNUM! lss 1024 goto :bad_port
if !PORTNUM! gtr 65535 goto :bad_port
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\web-search-neo-bridge-!PORTNUM!.bat"
(
echo @echo off
echo call "%~dp0bridge_autostart.bat" !PORTNUM!
) > "!STARTUP!"
echo Installed: !STARTUP!
exit /b 0

:bad_port
echo Invalid port "!PORT!": use a whole number between 1024 and 65535. 1>&2
exit /b 1
