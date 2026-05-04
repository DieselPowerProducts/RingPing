@echo off
cd /d "%~dp0"
powershell.exe -ExecutionPolicy Bypass -File "%~dp0install-ringping-scheduled-task.ps1"
set EXITCODE=%ERRORLEVEL%
echo.
echo Installer exit code: %EXITCODE%
pause
exit /b %EXITCODE%
