@echo off
setlocal
cd /d "%~dp0"
title Turb GPT Free Register

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo WebUI stopped with error code %EXIT_CODE%.
    echo Review the message above, then press any key to close this window.
    pause >nul
)

exit /b %EXIT_CODE%
