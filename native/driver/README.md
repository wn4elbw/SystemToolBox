# STBDriver — SystemToolBox 内核驱动（C / WDK）

提供"高级选项"开启后的内核级执行通道。**所有操作在 SYSTEM 系统线程内执行**，
可绕过管理员级 ACL；进程操作不调用 taskkill/cmd。

## 能力一览

| IOCTL | 实现 | 说明 |
|-------|------|------|
| PROC_KILL | `ZwOpenProcess` + `ZwTerminateProcess` | 结束进程（pid≤4 受保护） |
| PROC_SUSPEND / RESUME | `ZwOpenProcess` + `NtSuspendProcess` / `NtResumeProcess` | 整进程挂起/恢复（pid≤4 受保护） |
| PROC_START / CMD_EXEC | `ZwCreateFile`→`ZwCreateSection`(SEC_IMAGE)→`ZwCreateProcessEx`→`ZwCreateThreadEx(LdrInitializeThunk)` | 内核直接创建进程，不经 cmd.exe |
| MEM_READ / WRITE | `PsLookupProcessByProcessId` + `MmCopyVirtualMemory` | 跨进程读写（PreviousMode=KernelMode） |
| FILE_* | `ZwCreateFile`/`ZwReadFile`/`ZwWriteFile`/`ZwDeleteFile`（OBJ_KERNEL_HANDLE） | 系统级文件读写/删除/建目录 |
| REG_* | `Zw` 注册表 API（`ZwCreateKey`/`ZwSetValueKey`/`ZwEnumerateValueKey`…） | HKLM/HKCU/HKCR/HKU |
| POLICY_SET | 写 `HKLM\SOFTWARE\Policies\...` | 本地组策略（注册表承载） |
| TOKEN_ELEVATE | 复制 winlogon 的 SYSTEM 令牌（`ZwDuplicateToken` 主令牌）→ `ZwDuplicateObject` 注入客户端进程 | 管理员 → 系统级 |

## 构建

### 方式一：轻量交叉编译（推荐，无需 VS/WDK）

本仓库自带 `build-driver.ps1`：用官方 WDK/SDK 的 **NuGet 头与导入库** + 便携
llvm-mingw（`clang.exe` + `ld.lld`）在**未安装 Visual Studio 的机器**上交叉编译：

```
powershell -ExecutionPolicy Bypass -File native\driver\build-driver.ps1
```

- 首次运行联网下载约 460 MB（`Microsoft.Windows.WDK.x64`、`Microsoft.Windows.SDK.CPP`、
  llvm-mingw ucrt-x86_64），缓存于 `native\driver\toolchain\dl\`（不入库）。
- 产物：`native/driver/bin/stbdrv.sys`（python/app/driver.py 的默认路径）。
- 新版 llvm-mingw 不再提供 clang-cl/lld-link，脚本改用
  `clang.exe --target=x86_64-pc-windows-msvc` 编译、`ld.lld -flavor link`（COFF）链接，
  编译标志含 `-ffreestanding -fno-stack-protector -fno-exceptions -fno-rtti`，
  链接标志含 `/entry:DriverEntry /subsystem:native /driver /nodefaultlib`。

### 方式二：官方 WDK 构建

安装 Windows Driver Kit (WDK) + Visual Studio（含 C++ 桌面负载），在
**"开发人员命令提示符 (WDK)"** 中 cd 到本目录执行：

```
build -Z
```

（现代 WDK 建议直接用 VS 新建 "Kernel Mode Driver, Empty" 工程加入 stbdrv.c，
或将该目录作为源文件目录引入；输出 stbdrv.sys 放到 `bin\stbdrv.sys`。）

## 动态解析说明

WDK 导入库刻意隐藏了部分内核导出（`NtWriteVirtualMemory`、`NtCreateThreadEx`、
`NtSuspendProcess`/`NtResumeProcess` 等）。驱动在 `DriverEntry` 用
`MmGetSystemRoutineAddress` 按名解析这些服务（ntoskrnl 运行时有导出）；
解析失败时相关 op 返回 `STATUS_NOT_IMPLEMENTED`，驱动本身照常加载。

## 签名与加载（开发机）

1. 管理员执行 `bcdedit /set testsigning on` 并**重启**（UEFI 需关闭 Secure Boot）。
2. 运行软件 → 设置 → 更改设置 → 高级选项 → 开启 → 「加载驱动」。
   （等效手动：`sc create STBDriver type= kernel binPath= C:\...\stbdrv.sys` +
   `sc start STBDriver`；驱动日志见 `sc query STBDriver` / 事件查看器。）

## ABI 约定

- IOCTL 全部为 `METHOD_BUFFERED`，请求结构首字段必须为 `STB_HDR{magic,op,status}`。
- `hdr.status` 为驱动回填的 NTSTATUS；IRP 恒返回成功以便客户端读取。
- ctypes 镜像：`python/app/driver.py`；操作分发：`python/app/adv.py`。
- 尺寸上限：路径 260 WCHAR、值名 64 WCHAR、数据 4096 字节。

## 安全边界

- pid≤4 与"系统关键进程"保护：驱动拒绝结束 pid≤4 的进程。
- 进程启动不做 ACL 放行：**这是越权通道**，必须由宿主"高级选项"总开关 +
  API 层 admin 门控 + 插件权限模型三道闸把关（参见 docs/ARCHITECTURE.md）。
- 卸载 `sc stop STBDriver`（服务保留，下次即刻加载）。