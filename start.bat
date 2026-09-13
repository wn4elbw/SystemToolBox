@echo off
setlocal
title SystemToolBox

rem ============================================================
rem  SystemToolBox launcher
rem  - NO permission checks: starts directly regardless of admin.
rem  - The app itself attempts elevation on startup (see Electron
rem    main process ensureElevated / UAC relaunch).
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "ELECTRON_EXE=%ROOT%\electron\node_modules\electron\dist\electron.exe"

rem ---------------- args ----------------
set "DEBUG_FLAG="
for %%a in (%*) do (
    if /i "%%~a"=="-debug"    set "DEBUG_FLAG=1"
    if /i "%%~a"=="-h"        goto :help
    if /i "%%~a"=="/?"        goto :help
    if /i "%%~a"=="-help"     goto :help
)

if not exist "%ELECTRON_EXE%" (
    echo [error] Electron not found: "%ELECTRON_EXE%"
    echo         Run scripts\bootstrap.ps1 once to install dependencies.
    goto :end
)

if defined DEBUG_FLAG (
    set "STB_DEBUG=1"
    echo [info] debug mode: python backend logs print to this console.
)

rem ---------------- start (no permission checks) ----------------
echo [info] Starting SystemToolBox ...
pushd "%ROOT%\electron"
"%ELECTRON_EXE%" .
set "RC=%ERRORLEVEL%"
popd
if defined DEBUG_FLAG set "STB_DEBUG="

echo [info] App exited with code %RC%
if not "%RC%"=="0" echo [hint] Non-zero exit usually means startup failure; add -debug to see backend logs.
goto :end

rem ---------------- help ----------------
:help
echo.
echo SystemToolBox launcher
echo.
echo Usage: start.bat [options]
echo.
echo   ^(no args^)  start the app directly
echo   -debug       debug mode: print python backend logs to console
echo   -h / -help   show this help
echo.
echo Note: no permission check is performed here. The app itself
echo       attempts to elevate to administrator on startup.
echo.

:end
endlocal
exit /b %RC%