# bootstrap.ps1 — 初始化开发环境：检查运行时 / 安装 Electron 依赖
param(
  [switch]$SkipNpm
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "find-runtime.ps1")

Write-Host "== SystemToolBox bootstrap ==" -ForegroundColor Cyan
Write-Host ("Root:     " + $R.Root)
Write-Host ("Node:     " + $(if ($R.Node) { $R.Node } else { "[未找到]" }))
Write-Host ("Python:   " + $(if ($R.Python) { $R.Python } else { "[未找到]" }))
Write-Host ("CMake:    " + $(if ($R.Cmake) { $R.Cmake } else { "[未找到]" }))
Write-Host ("C 编译器: " + $(if ($R.CCompiler) { $R.CCompiler } else { "[未找到]" }))

if (-not $R.Node) {
  Write-Warning "未找到 Node.js：请安装，或放入 runtime/node/ 便携版。"
}
if (-not $R.Python) {
  Write-Warning "未找到 Python：请安装 3.11+，或放入 runtime/python/ 便携版。"
}
if (-not $R.CCompiler -or -not $R.Cmake) {
  Write-Warning "未找到 CMake/C 编译器：C/C++ 原生层将跳过构建（Python 兜底可运行）。"
  Write-Warning "  可放入 runtime/toolchain/（cmake + MinGW64），或用 MSVC 开发者命令行。"
}

if (-not $SkipNpm -and $R.Node) {
  Write-Host "`n== npm install (electron) ==" -ForegroundColor Cyan

  # Electron 二进制默认从 github 下载；不可达时自动切到 npmmirror
  $githubOk = $false
  try {
    $probe = Invoke-WebRequest "https://github.com" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    $githubOk = $true
  } catch { $githubOk = $false }
  if (-not $githubOk -and -not $env:ELECTRON_MIRROR) {
    Write-Warning "github 不可达，使用镜像 npmmirror 下载 Electron 二进制"
    $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
  }
  if ($env:ELECTRON_MIRROR) {
    Write-Host "ELECTRON_MIRROR = $env:ELECTRON_MIRROR"
  }

  Push-Location (Join-Path $R.Root "electron")
  try {
    & npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm install 失败" }

    # npm 11 的 allowScripts 策略可能阻止 electron postinstall 下载二进制：
    # 手动执行下载脚本兜底
    $exe = "node_modules\electron\dist\electron.exe"
    if (-not (Test-Path $exe)) {
      Write-Host "electron 二进制未就绪，手动执行下载脚本…"
      Push-Location "node_modules\electron"
      try {
        & node install.js
        if ($LASTEXITCODE -ne 0) { throw "electron install.js 失败" }
      } finally { Pop-Location }
    }
    if (-not (Test-Path $exe)) {
      Write-Warning "Electron 二进制可能未下载成功；可手动设置 ELECTRON_MIRROR 后重试 .\scripts\bootstrap.ps1"
    } else {
      Write-Host ("Electron 二进制就绪: " + (Resolve-Path $exe))
    }
  } finally { Pop-Location }
}

Write-Host "`n== 完成 ==" -ForegroundColor Green
Write-Host "下一步： .\scripts\build.ps1   （编译 C/C++ 原生层，可选）"
Write-Host "        .\scripts\run.ps1     （启动应用）"