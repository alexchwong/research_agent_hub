@echo off
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0Install.ps1" %*
set "RAH_INSTALL_EXIT=%errorlevel%"
if "%RAH_INSTALL_EXIT%"=="130" exit /b 130
if not "%RAH_INSTALL_EXIT%"=="0" if "%~1"=="" pause
exit /b %RAH_INSTALL_EXIT%
