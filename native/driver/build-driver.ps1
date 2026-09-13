# build-driver.ps1 — 轻量交叉构建 STBDriver 内核驱动（stbdrv.sys）
#
# 无需安装 VS/WDK：使用
#   1. 官方 Windows SDK C++ x64 NuGet（Microsoft.Windows.SDK.CPP，提供 shared/um 头，含 sal.h）
#   2. 官方 WDK x64 NuGet（Microsoft.Windows.WDK.x64，提供 km 头 ntddk.h + 内核导入库 ntoskrnl.lib 等）
#   3. 便携 LLVM（llvm-mingw ucrt-x86_64：clang.exe + ld.lld，用 MSVC 目标交叉编译）
# 全部解包到 native/driver/toolchain/ 下（不入库）。
#
# 说明：新版 llvm-mingw 不再提供 clang-cl.exe / lld-link.exe，因此本脚本用
#   clang.exe --target=x86_64-pc-windows-msvc（GNU 驱动、MSVC ABI）编译，
#   用 ld.lld -flavor link（COFF 模式）链接。
#
# 产物：native/driver/bin/stbdrv.sys（python/app/driver.py 的默认路径）
#
# 用法：powershell -ExecutionPolicy Bypass -File native\driver\build-driver.ps1
# 首次运行需联网下载约 460MB（WDK+SDK+llvm-mingw，缓存于 toolchain\dl\）。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # native/
$tc   = Join-Path $root "driver\toolchain"
$dl   = Join-Path $tc "dl"
$build = Join-Path $tc "build"
New-Item -ItemType Directory -Force -Path $tc, $dl, $build | Out-Null

# ---------- 1) 准备 SDK / WDK 头与库（缺失则自动下载） ----------
$ver = "10.0.26100.0"
function Ensure-NuGet($pkgId, $pkgVer, $nupkg, $dest) {
  if (Test-Path $dest) { return }
  $file = Join-Path $dl $nupkg
  if (-not (Test-Path $file)) {
    $url = "https://api.nuget.org/v3-flatcontainer/$pkgId/$pkgVer/$nupkg"
    Write-Host "下载 $url ..."
    Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
  }
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  tar -xf $file -C $dest
  Write-Host "就绪: $dest"
}

$sdkRoot = Join-Path $tc "sdkbase"   # Microsoft.Windows.SDK.CPP（含 shared/um 头）
$wdkRoot = Join-Path $tc "wdk"
Ensure-NuGet "microsoft.windows.sdk.cpp" "10.0.26100.6584" `
  "microsoft.windows.sdk.cpp.10.0.26100.6584.nupkg" $sdkRoot
Ensure-NuGet "microsoft.windows.wdk.x64" "10.0.26100.6584" `
  "microsoft.windows.wdk.x64.10.0.26100.6584.nupkg" $wdkRoot

# SDK 解包后的实际布局探测（SDK: <dest>/c/Include/<ver>/..., WDK: <dest>/c/Include/<ver>/...）
$sdkInc = Join-Path $sdkRoot "c\Include\$ver"
$wdkInc = Join-Path $wdkRoot "c\Include\$ver"
if (-not (Test-Path $sdkInc)) { $sdkInc = Join-Path $sdkRoot "Include\$ver" }
if (-not (Test-Path $wdkInc)) { $wdkInc = Join-Path $wdkRoot "Include\$ver" }
$kmLib  = Join-Path $wdkRoot "c\Lib\$ver\km\x64"
if (-not (Test-Path $kmLib)) { $kmLib = Join-Path $wdkRoot "Lib\$ver\km\x64" }

# ---------- 2) 准备 clang.exe / ld.lld（llvm-mingw） ----------
$llvmBin = Join-Path $tc "llvm-mingw\bin"
if (-not (Test-Path (Join-Path $llvmBin "clang.exe"))) {
  $zip = Join-Path $dl "llvm-mingw.zip"
  if (-not (Test-Path $zip)) {
    throw "缺少 llvm-mingw.zip，请先从 https://github.com/mstorsjo/llvm-mingw/releases 下载 ucrt-x86_64 版（如 llvm-mingw-20260908-ucrt-x86_64.zip）放到 $dl"
  }
  tar -xf $zip -C $tc
  # 解包目录带版本前缀，把其中的 bin 挪到 toolchain\llvm-mingw\bin
  $dirs = Get-ChildItem $tc -Directory | Where-Object {
    $_.Name -like "llvm-mingw-*" -and (Test-Path (Join-Path $_.FullName "bin\clang.exe"))
  }
  if ($dirs) {
    $real = Join-Path $dirs[0].FullName "bin"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $llvmBin) | Out-Null
    Move-Item $real $llvmBin -Force
    Get-ChildItem $tc -Directory | Where-Object {
      $_.Name -like "llvm-mingw-*" -and $_.FullName -ne (Split-Path -Parent $llvmBin)
    } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
  }
}
$clang = Join-Path $llvmBin "clang.exe"
$ldlld = Join-Path $llvmBin "ld.lld.exe"
if (-not (Test-Path $clang) -or -not (Test-Path $ldlld)) {
  throw "未找到 clang.exe / ld.lld.exe（llvm-mingw 解包失败）"
}

# ---------- 3) 编译 ----------
# 顺序关键：km\crt 在最前（excpt.h 等 CRT 头），其后 km / shared / um
$includeDirs = @(
  (Join-Path $wdkInc "km\crt"),
  (Join-Path $wdkInc "km"),
  (Join-Path $sdkInc "shared"),
  (Join-Path $sdkInc "um"),
  (Join-Path $wdkInc "shared"),
  (Join-Path $wdkInc "um")
) | Where-Object { Test-Path $_ }

$src = Join-Path (Split-Path -Parent $PSScriptRoot) "driver\stbdrv.c"
$obj = Join-Path $build "stbdrv.obj"

$target   = "--target=x86_64-pc-windows-msvc"
$fmsv     = "-fms-compatibility-version=19.40"
$ccArgs = @(
  $target, "-c", $src, "-o", $obj,
  "-fms-extensions", "-fms-compatibility", $fmsv,
  "-ffreestanding", "-fno-stack-protector", "-fno-exceptions",
  "-fno-rtti", "-fno-unwind-tables",
  "-O1", "-w",
  "-D_WIN32", "-D_WIN64", "-D_AMD64_", "-DAMD64", "-DWIN32"
)
foreach ($d in $includeDirs) { $ccArgs += "-I$d" }

Write-Host "== 编译 stbdrv.c =="
& $clang @ccArgs 2>&1 | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) { throw "编译失败 (clang exit $LASTEXITCODE)" }

# ---------- 4) 链接（ld.lld COFF 模式） ----------
$outDir = Join-Path (Split-Path -Parent $PSScriptRoot) "driver\bin"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$sysOut = Join-Path $outDir "stbdrv.sys"

Write-Host "== 链接 stbdrv.sys =="
$ldArgs = @(
  "-flavor", "link", "/nologo", "/machine:x64", "/entry:DriverEntry",
  "/subsystem:native", "/driver", "/nodefaultlib", "/opt:ref",
  "/libpath:$kmLib",
  $obj,
  "ntoskrnl.lib", "hal.lib", "wdmsec.lib", "ntstrsafe.lib",
  "bufferoverflowfastfailk.lib",
  "/out:$sysOut", "/map:$build\stbdrv.map"
)
& $ldlld @ldArgs 2>&1 | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) { throw "链接失败 (ld.lld exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "构建完成: $sysOut ($([math]::Round((Get-Item $sysOut).Length/1KB,1)) KB)"
