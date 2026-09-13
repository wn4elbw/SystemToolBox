# build-native.ps1 - build C/C++ native layer with bundled llvm-mingw toolchain
#
# No cmake / VS required: uses native/driver/toolchain/llvm-mingw
#   clang(gcc driver, x86_64-w64-windows-gnu) to compile core + native,
#   output: python/native/native_core.dll
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\build-native.ps1
param(
  [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$llvmBin = Join-Path $Root "native\driver\toolchain\llvm-mingw\bin"
$gcc = Join-Path $llvmBin "x86_64-w64-mingw32-gcc.exe"
$gxx = Join-Path $llvmBin "x86_64-w64-mingw32-g++.exe"
if (-not (Test-Path $gcc) -or -not (Test-Path $gxx)) {
  # some distributions only ship gcc/g++ names
  if (-not (Test-Path $gcc)) { $gcc = Join-Path $llvmBin "gcc.exe" }
  if (-not (Test-Path $gxx)) { $gxx = Join-Path $llvmBin "g++.exe" }
}
if (-not (Test-Path $gcc)) { throw "llvm-mingw gcc not found: $llvmBin" }

$build = Join-Path $Root "build"
if ($Clean -and (Test-Path $build)) { Remove-Item $build -Recurse -Force }
New-Item -ItemType Directory -Force -Path $build | Out-Null

$coreSrc = Join-Path $Root "core\src\sysinfo.c"
$nSrc = Join-Path $Root "native\src"
$incCore = Join-Path $Root "core\include"
$incNat = Join-Path $Root "native\include"

$coreObj = Join-Path $build "sysinfo.o"
Write-Host "== compile core (sysinfo.c) ==" -ForegroundColor Cyan
& $gcc -c $coreSrc -o $coreObj `
  -std=c11 -O2 -Wall -Wextra -Wno-unused-parameter `
  -DWIN32_LEAN_AND_MEAN -D_WIN32 "-I$incCore"
if ($LASTEXITCODE -ne 0) { throw "core compile failed" }

Write-Host "== compile native_core.dll (C++) ==" -ForegroundColor Cyan
$objs = @($coreObj)
foreach ($cpp in @("native.cpp", "service.cpp", "elevate.cpp")) {
  $obj = Join-Path $build ($cpp -replace "\.cpp$", ".o")
  Write-Host "  $cpp"
  & $gxx -c (Join-Path $nSrc $cpp) -o $obj `
    -std=c++17 -O2 -Wall -Wextra -Wno-unused-parameter `
    -DNATIVE_CORE_EXPORTS -D_WIN32 -D_WIN32_WINNT=0x0A00 -DWINVER=0x0A00 `
    "-I$incNat" "-I$incCore" "-I$nSrc"
  if ($LASTEXITCODE -ne 0) { throw "$cpp compile failed" }
  $objs += $obj
}

$dllOut = Join-Path $build "native_core.dll"
Write-Host "== link $dllOut ==" -ForegroundColor Cyan
& $gxx -shared -o $dllOut @objs `
  -ladvapi32 -lshell32 -lpsapi -static -static-libgcc -static-libstdc++
if ($LASTEXITCODE -ne 0) { throw "link failed" }

# copy to the location the python backend expects
$target = Join-Path $Root "python\native\native_core.dll"
Copy-Item $dllOut $target -Force
Write-Host ("`noutput: {0} ({1:N0} KB)" -f $target, ((Get-Item $target).Length / 1KB)) -ForegroundColor Green
Write-Host "build done OK"