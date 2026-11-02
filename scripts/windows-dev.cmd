@echo off
rem Live development, Windows side: the installed TEST build shows your Mac's dev servers (hot reload).
rem   scripts\windows-dev.cmd 192.168.1.20      (the address dev-mac.sh prints)
rem Only test builds accept --dev-url; the client's build refuses it.
if "%~1"=="" (
  echo Usage: windows-dev.cmd ^<Mac address^>
  exit /b 1
)
set EXE=%LOCALAPPDATA%\CopilotAppTest\current\Copilot.exe
if not exist "%EXE%" (
  echo The Copilot TEST build isn't installed. Install it from the latest test pre-release first.
  exit /b 1
)
start "" "%EXE%" --dev-url http://%~1:5173
