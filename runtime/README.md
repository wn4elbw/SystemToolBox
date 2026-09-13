# runtime/ — 携带的开发运行环境

本目录约定存放随开发目录一起携带的运行时，目的是把整个 `SystemToolBox` 目录拷到
任何一台机器即可开发/构建/运行，无需单独安装工具链。

```
runtime/
  node/       # Node.js 便携版（含 npm）
  python/     # Python 便携版（3.11+，Windows embeddable 或完整安装）
  toolchain/  # cmake + MinGW（或 MSVC 工具链）— 编译 C/C++ 层
```

目录规则：

- 本目录由 `.gitignore` 排除（`runtime/*` 不入库，仅保留本说明）。
- 构建/运行脚本优先探测 `runtime/` 内环境（`scripts/find-runtime.ps1`），
  未找到时回退到系统 `PATH`。
- 当前开机环境示例：

| 组件     | 状态                                        |
|----------|---------------------------------------------|
| Node.js  | 系统 PATH：v24.20.0 ✔                       |
| Python   | 系统 PATH：3.11.3 ✔                        |
| cmake    | 未安装 —— 需要时放入 `runtime/toolchain/` 或装到系统 |
| MinGW    | 未安装 —— 同上                              |

在没有工具链的情况下，C/C++ 层不参与构建，Python 后端通过 `ctypes` 直连 Win32
API 兜底（`python/app/core/bridge.py` 的 fallback 路径），软件依然可以运行。