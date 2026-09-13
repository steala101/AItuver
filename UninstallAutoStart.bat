@echo off
rem Remove the Remote Launcher auto-start shortcut from the Startup folder.
setlocal
chcp 65001 >nul
title Uninstall Neuro Remote Launcher AutoStart

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$lnk=Join-Path ([Environment]::GetFolderPath('Startup')) 'NeuroRemoteLauncher.lnk';" ^
  "if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host ('Removed: ' + $lnk) } else { Write-Host 'No auto-start shortcut found.' }"

echo.
echo [OK] Done. The launcher will no longer start automatically at logon.
pause
