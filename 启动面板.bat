@echo off
rem ============================================================
rem  WorkBuddy multi-account signin panel - launcher
rem  Finds a usable Python on this machine and starts panel.pyw.
rem  Pure ASCII + CRLF. Do not add non-ASCII characters here.
rem ============================================================
setlocal EnableExtensions
set "HERE=%~dp0"
set "PANEL=%HERE%panel.pyw"

if not exist "%PANEL%" (
  echo [ERROR] panel.pyw not found next to this launcher.
  pause
  exit /b 1
)

rem 1) portable layout: pythonw.exe shipped next to this script
if exist "%HERE%pythonw.exe" (
  start "" "%HERE%pythonw.exe" "%PANEL%" %*
  exit /b 0
)

rem 2) Windows Python launcher (windowless pyw.exe)
if exist "%SystemRoot%\pyw.exe" (
  start "" "%SystemRoot%\pyw.exe" "%PANEL%" %*
  exit /b 0
)

rem 3) pythonw.exe on PATH (skip Microsoft Store placeholder)
for /f "delims=" %%I in ('where pythonw 2^>nul') do (
  echo %%I | findstr /i /c:"WindowsApps" >nul
  if errorlevel 1 (
    start "" "%%I" "%PANEL%" %*
    exit /b 0
  )
)

rem 4) common install locations (newest first)
for %%V in (314 313 312 311 310) do (
  if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" "%PANEL%" %*
    exit /b 0
  )
  if exist "C:\Python%%V\pythonw.exe" (
    start "" "C:\Python%%V\pythonw.exe" "%PANEL%" %*
    exit /b 0
  )
)

echo [ERROR] Python 3 not found.
echo Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
pause
exit /b 1
