# run.ps1 — 启动 SystemToolBox（Electron + Python 后端）
param(
  [switch]$Debug
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "find-runtime.ps1")

Write-Host "== 启动 SystemToolBox ==" -ForegroundColor Cyan
Write-Host ("Root:   " + $R.Root)
Write-Host ("Python: " + $(if ($R.Python) { $R.Python } else { "[未找到]" }))

if (-not $R.Node) {
  Write-Error "未找到 Node.js。"
}
if (-not $R.Python) {
  Write-Error "未找到 Python。"
}

$electronDir = Join-Path $R.Root "electron"
$electronBin = Join-Path $electronDir "node_modules\.bin\electron.cmd"
if (-not (Test-Path $electronBin)) {
  Write-Host "Electron 未安装，先执行 bootstrap…"
  & (Join-Path $PSScriptRoot "bootstrap.ps1")
  if (-not (Test-Path $electronBin)) {
    Write-Error "Electron 安装失败，请检查网络后重试： .\scripts\bootstrap.ps1"
  }
}

# 让后端能找到便携版 python；--debug 通过环境变量传递（electron 命令行不接受 --debug）
if ($R.Python) {
  $env:STB_PYTHON = $R.Python
}
if ($Debug) {
  $env:STB_DEBUG = "1"
}

Push-Location $electronDir
try {
  & $electronBin .
  exit $LASTEXITCODE
} finally {
  Pop-Location
  Remove-Item Env:STB_DEBUG -ErrorAction SilentlyContinue
}