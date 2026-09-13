@echo off
setlocal
title SystemToolBox

rem ============================================================
rem  SystemToolBox launcher (self-contained)
rem  - NO permission checks: starts directly regardless of admin.
rem  - Browser core (Electron) ships with repo: dist files are
rem    committed directly, but the 180MB electron.exe is stored
rem    compressed at electron\vendor\electron.exe.zip (GitHub
rem    100MB single-file limit) and auto-extracted on first run.
rem  - Portable python/node under runtime\ are used by backend.
rem  - The app itself attempts elevation on startup (see Electron
rem    main process ensureElevated / UAC relaunch).
rem  All paths below are relative to this script (%~dp0), so the
rem  project can be launched from anywhere / any drive.
rem ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "ELECTRON_DIR=%ROOT%\electron\node_modules\electron\dist"
set "ELECTRON_EXE=%ELECTRON_DIR%\electron.exe"
set "EXE_ZIP=%ROOT%\electron\vendor\electron.exe.zip"

rem ---------------- args ----------------
set "DEBUG_FLAG="
for %%a in (%*) do (
    if /i "%%~a"=="-debug"    set "DEBUG_FLAG=1"
    if /i "%%~a"=="-h"        goto :help
    if /i "%%~a"=="/?"        goto :help
    if /i "%%~a"=="-help"     goto :help
)

rem ---------------- first-run: extract electron.exe if missing ----------------
if not exist "%ELECTRON_EXE%" (
    if exist "%EXE_ZIP%" (
        echo [info] Extracting browser core first run: electron.exe ...
        powershell -NoProfile -ExecutionPolicy Bypass -Command ^
            "Expand-Archive -LiteralPath '%EXE_ZIP%' -DestinationPath '%ELECTRON_DIR%' -Force"
        if errorlevel 1 (
            echo [error] Failed to extract electron.exe from vendor zip.
            goto :end
        )
    ) else (
        echo [error] Browser core missing: "%ELECTRON_EXE%"
        echo         and vendor zip not found: "%EXE_ZIP%"
        goto :end
    )
)
if not exist "%ELECTRON_EXE%" (
    echo [error] Electron still missing after extraction: "%ELECTRON_EXE%"
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
echo SystemToolBox launcher (self-contained)
echo.
echo Usage: start.bat [options]
echo.
echo   ^(no args^)  start the app directly
echo   -debug       debug mode: print python backend logs to console
echo   -h / -help   show this help
echo.
echo Note: no permission check is performed here. The app itself
echo       attempts to elevate to administrator on startup.
echo       Browser core auto-extracts from electron\vendor on first run.
echo.

:end
endlocal
exit /b %RC%