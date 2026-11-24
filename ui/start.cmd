@echo off
rem LiveType desktop shell from source (development). Options:
rem   --mock    simulated engine (ui\dev\mock-engine.js) instead of Python
rem   --hidden  start in the tray without opening the control panel
rem Electron: ui\node_modules (run "npm install" in ui\) or the LIVETYPE_ELECTRON variable.
setlocal
set "ELECTRON=%LIVETYPE_ELECTRON%"
if "%ELECTRON%"=="" set "ELECTRON=%~dp0node_modules\electron\dist\electron.exe"
if not exist "%ELECTRON%" (
  echo Electron not found: %ELECTRON%
  echo Run "npm install" in the ui folder, or set LIVETYPE_ELECTRON to electron.exe
  pause
  exit /b 1
)
start "" "%ELECTRON%" "%~dp0." %*
