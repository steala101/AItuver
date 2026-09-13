@echo off
rem Neuro Voice AI - Remote Launcher (system tray / notification area)
rem Compatibility entry point. The resident process is launched by VBScript
rem with window style 0, so even a python.exe fallback cannot leave a black
rem command-prompt window open.
cd /d "%~dp0"
"%SystemRoot%\System32\wscript.exe" //B //Nologo "%~dp0RemoteLauncher.vbs" %*
exit /b 0
