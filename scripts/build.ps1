# build.ps1 — 编译 C/C++ 原生层（core + native_core.dll）
# 自动选择：runtime 工具链 -> PATH(gcc) -> MSVC(vswhere)
param(
  [switch]$Clean
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "find-runtime.ps1")

$Root = $R.Root
$BuildDir = Join-Path $Root "build"

Write-Host "== 构建 C/C++ 原生层 ==" -ForegroundColor Cyan

if (-not $R.Cmake) {
  Write-Error "未找到 cmake。请安装，或放入 runtime/toolchain/cmake/bin/。"
}
if (-not $R.CCompiler) {
  Write-Warning "PATH 上未找到 gcc/cl。尝试用 vswhere 定位 MSVC…"
}

# --- 选择生成器与工具链 ---
$Generator = $null
$ToolchainArgs = @()
$Env_Init = $null   # MSVC 环境初始化脚本

if ($R.CCompiler -and $R.CCompiler -match "gcc(\.exe)?$") {
  # MinGW
  $Generator = "MinGW Makefiles"
  $mingwBin = Split-Path $R.CCompiler -Parent
  # 让 cmake 能找到 make/gcc/g++
  $env:PATH = "$mingwBin;$env:PATH"
  Write-Host "生成器: MinGW Makefiles ($R.CCompiler)"
} elseif ($R.CCompiler -and $R.CCompiler -match "cl(\.exe)?$") {
  # MSVC 已就绪（开发者命令行内）
  $Generator = "Visual Studio 17 2022"
  Write-Host "生成器: Visual Studio ($R.CCompiler)"
} else {
  # 用 vswhere 找 MSVC
  $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
  if (Test-Path $vswhere) {
    $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    $vsPath = ($vsPath | Select-Object -First 1)
    if ($vsPath) {
      $vcvars = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"
      if (Test-Path $vcvars) {
        $Generator = "Visual Studio 17 2022"
        $Env_Init = $vcvars
        Write-Host "使用 MSVC: $vcvars"
      }
    }
  }
  if (-not $Generator) {
    Write-Error "未找到可用 C 编译器。请安装 MinGW-w64 并加入 PATH，或放入 runtime/toolchain/mingw64/bin/。"
  }
}

if ($Clean -and (Test-Path $BuildDir)) {
  Remove-Item $BuildDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

# --- configure ---
$configureArgs = @("-S", $Root, "-B", $BuildDir)
if ($Generator) { $configureArgs += @("-G", $Generator) }
if ($ToolchainArgs) { $configureArgs += $ToolchainArgs }
$configureArgs += @("-DCMAKE_BUILD_TYPE=Release")

if ($Env_Init) {
  # MSVC：在 cmd 中先初始化环境再执行 cmake
  $cmd = "`"$Env_Init`" >nul && cmake $($configureArgs -join ' ') && cmake --build $BuildDir --config Release"
  Write-Host "执行: $cmd"
  cmd /c $cmd
  if ($LASTEXITCODE -ne 0) { Write-Error "MSVC 构建失败 (exit=$LASTEXITCODE)" }
} else {
  Write-Host "cmake configure: $($configureArgs -join ' ')"
  & $R.Cmake @configureArgs
  if ($LASTEXITCODE -ne 0) { Write-Error "cmake configure 失败" }
  Write-Host "cmake build…"
  & $R.Cmake --build $BuildDir --config Release
  if ($LASTEXITCODE -ne 0) { Write-Error "cmake build 失败" }
}

$dll = Join-Path $BuildDir "bin\native_core.dll"
if (Test-Path $dll) {
  Write-Host "`n== 产物 ==" -ForegroundColor Green
  Write-Host $dll
  Write-Host "(已由 POST_BUILD 自动拷贝到 python/native/native_core.dll)"
} else {
  Write-Warning "未找到预期产物 $dll"
}