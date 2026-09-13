@echo off
setlocal enabledelayedexpansion
title SystemToolBox 启动器

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "ELECTRON_EXE=%ROOT%\electron\node_modules\electron\dist\electron.exe"

rem ---------------- 参数解析 ----------------
set "DO_BUILD="
set "DO_INSTALL="
set "DEBUG_FLAG="
for %%a in (%*) do (
    if /i "%%~a"=="-build"    set "DO_BUILD=1"
    if /i "%%~a"=="-install"  set "DO_INSTALL=1"
    if /i "%%~a"=="-debug"    set "DEBUG_FLAG=1"
    if /i "%%~a"=="-h"        goto :help
    if /i "%%~a"=="/?"        goto :help
    if /i "%%~a"=="-help"     goto :help
)

rem ---------------- 初始化/安装 ----------------
if defined DO_INSTALL (
    echo [信息] 重新初始化环境:scripts\bootstrap.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\bootstrap.ps1"
    if errorlevel 1 goto :end
)

if not exist "%ELECTRON_EXE%" (
    echo [信息] Electron 未安装,自动执行初始化:scripts\bootstrap.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\bootstrap.ps1"
    if not exist "%ELECTRON_EXE%" (
        echo [错误] Electron 安装失败。
        echo        若网络下载 Electron 二进制失败,可设置镜像后重试 start.bat -install
        echo        set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
        goto :end
    )
)

rem ---------------- 编译原生层(可选) ----------------
if defined DO_BUILD (
    echo [信息] 编译 C/C++ 原生层:scripts\build.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\build.ps1"
    if errorlevel 1 (
        echo [错误] 原生层构建失败,请安装 cmake + MinGW 或 MSVC 后重试。
        goto :end
    )
)

rem ---------------- 环境变量 ----------------
if defined DEBUG_FLAG (
    set "STB_DEBUG=1"
    echo [信息] 调试模式:后端日志将输出到本控制台。
)

rem ---------------- 启动 Electron ----------------
echo [信息] 启动 SystemToolBox ...
pushd "%ROOT%\electron"
"%ELECTRON_EXE%" .
set "RC=%ERRORLEVEL%"
popd
if defined DEBUG_FLAG set "STB_DEBUG="

echo [信息] 应用已退出:退出码 %RC%
if not "%RC%"=="0" echo [提示] 非零退出码通常表示启动失败,可加 -debug 查看后端日志。
goto :end

rem ---------------- 帮助 ----------------
:help
echo.
echo SystemToolBox 启动脚本
echo.
echo 用法: start.bat [选项]
echo.
echo   ^(无参数^)  直接启动应用
echo   -build      启动前编译 C/C++ 原生层:需要 cmake + MinGW 或 MSVC
echo   -install    重新初始化环境:检查运行时并安装 Electron 依赖
echo   -debug      调试模式启动:控制台实时打印 Python 后端日志
echo   -h / -help  显示本帮助
echo.
echo 说明: 未编译原生层也可运行,系统信息走 ctypes 兜底,
echo       驱动/服务管理接口会提示需要 native_core.dll。
echo.

:end
endlocal
exit /b 0