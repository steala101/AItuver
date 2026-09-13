@echo off
rem Register the Remote Launcher to start automatically at Windows logon.
rem This creates a SHORTCUT in the Startup folder that starts the silent
rem VBScript entry through wscript.exe. No console window is created.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Install Neuro Remote Launcher AutoStart

set "TARGET=%~dp0RemoteLauncher.vbs"
set "WORKDIR=%~dp0"

if not exist "%TARGET%" (
  echo [!] RemoteLauncher.vbs not found next to this installer.
  echo     Keep both files in the AItuber folder and run this again.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$sp=[Environment]::GetFolderPath('Startup');" ^
  "$lnk=Join-Path $sp 'NeuroRemoteLauncher.lnk';" ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "$s=$w.CreateShortcut($lnk);" ^
  "$s.TargetPath=Join-Path $env:SystemRoot 'System32\wscript.exe';" ^
  "$s.Arguments='//B //Nologo ' + [char]34 + '%TARGET%' + [char]34;" ^
  "$s.WorkingDirectory='%WORKDIR%';" ^
  "$s.WindowStyle=7;" ^
  "$s.Description='Neuro Remote Launcher (auto start at logon)';" ^
  "$s.Save();" ^
  "Write-Host ('Created shortcut: ' + $lnk)"

if errorlevel 1 (
  echo [!] Failed to create the startup shortcut.
  pause
  exit /b 1
)

echo.
echo [OK] Auto-start registered. The launcher will start (minimized) at next logon.
echo      To start it now without rebooting, run RemoteLauncher.vbs.
echo      To remove auto-start, run UninstallAutoStart.bat.
pause
