# find-runtime.ps1 — 定位运行时（优先 runtime/ 内置，回退系统 PATH）
# 用法: . .\scripts\find-runtime.ps1   （dot-source，导出 $RuntimeEnv）
param()

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$R = @{
  Root     = $Root
  Node     = $null
  Python   = $null
  Cmake    = $null
  CCompiler = $null
  Notes    = @()
}

# --- Node ---
foreach ($c in @((Join-Path $Root "runtime\node\node.exe"),
                 (Join-Path $Root "runtime\nodejs\node.exe"))) {
  if (Test-Path $c) { $R.Node = $c; break }
}
if (-not $R.Node) {
  $cmd = Get-Command node -ErrorAction SilentlyContinue
  if ($cmd) { $R.Node = $cmd.Source }
}

# --- Python ---
foreach ($c in @((Join-Path $Root "runtime\python\python.exe"),
                 (Join-Path $Root "runtime\python\pythonw.exe"))) {
  if (Test-Path $c) { $R.Python = $c; break }
}
if (-not $R.Python) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { $R.Python = $cmd.Source }
}
if (-not $R.Python) {
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { $R.Python = $py.Source }
}

# --- CMake ---
foreach ($c in @((Join-Path $Root "runtime\toolchain\cmake\bin\cmake.exe"),
                 (Join-Path $Root "runtime\cmake\bin\cmake.exe"))) {
  if (Test-Path $c) { $R.Cmake = $c; break }
}
if (-not $R.Cmake) {
  $cmd = Get-Command cmake -ErrorAction SilentlyContinue
  if ($cmd) { $R.Cmake = $cmd.Source }
}

# --- C 编译器（MinGW gcc / MSVC(cl)） ---
foreach ($c in @((Join-Path $Root "runtime\toolchain\mingw64\bin\gcc.exe"),
                 (Join-Path $Root "runtime\mingw64\bin\gcc.exe"))) {
  if (Test-Path $c) { $R.CCompiler = $c; break }
}
if (-not $R.CCompiler) {
  $cmd = Get-Command gcc -ErrorAction SilentlyContinue
  if ($cmd) { $R.CCompiler = $cmd.Source }
}
if (-not $R.CCompiler) {
  $cmd = Get-Command cl -ErrorAction SilentlyContinue
  if ($cmd) { $R.CCompiler = $cmd.Source }
}

# 完成；调用方 dot-source 后使用 $R（如 . .\scripts\find-runtime.ps1）