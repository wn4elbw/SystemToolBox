# SystemToolBox 系统工具箱

Electron + Python + C/C++ 构建的系统工具箱。**软件本体只含最基本的「系统信息预览」**，
其余功能全部由插件提供；插件带三级权限（只读 / 审批 / 全权）。

架构与协议细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 技术分层

| 层 | 目录 | 职责 |
|----|------|------|
| C（最底层） | `core/` | 直接 Win32 API 采集系统信息（CPU/内存/磁盘/系统/开机时长），纯 C |
| C++（兼容层） | `native/` | 底层交互封装、**驱动/服务管理**（SCM）、**权限控制**（提权/令牌），导出 C ABI → `native_core.dll` |
| C（内核驱动） | `native/driver/` | **STBDriver 内核驱动**（WDK）：进程/内存/文件/注册表/组策略/命令执行/令牌提权，全部在 SYSTEM 系统线程执行 |
| Python（界面/API/插件） | `python/` | HTTP 后端、三级权限裁决、插件扫描/加载/管理、审批流、内置系统信息页、驱动 ctypes 客户端与直调路径 |
| Electron（壳） | `electron/` | 无边框窗口、自绘标题栏、左栏+顶栏布局、插件管理界面、审批弹窗、启动自动提权 |

## 快速开始

```powershell
# 1) 初始化（检查运行时；安装 Electron）
.\scripts\bootstrap.ps1

# 2) 编译 C/C++ 原生层（可选：无编译环境也能跑，Python 有 ctypes 兜底）
.\scripts\build.ps1          # 需要 cmake + MinGW或MSVC

# 3) 启动
.\scripts\run.ps1            # 加 -Debug 查看后端日志
```

**最简单：双击根目录 `start.bat`**（自动检测依赖，缺 Electron 时先装后启）：

```bat
start.bat                :: 直接启动
start.bat -build         :: 启动前编译 C/C++ 原生层
start.bat -install       :: 重新初始化环境（安装 Electron 等）
start.bat -debug         :: 调试模式（控制台打印后端日志）
```

- 后端无独立依赖（纯标准库）；插件也不需要 pip 包。
- 原生层未编译时：系统信息正常（ctypes 兜底）；内核驱动未编译/未加载时，「高级选项」相关操作走直调路径
  （仅需管理员，不需要 WDK/驱动）。
- **启动自动提权**：应用启动时检测是否管理员，否则 UAC 提权重启（`runas`）；
  拒绝提权则以普通权限继续（高级选项不可用）。开发/测试可设 `STB_NO_ELEVATE=1` 跳过提权。

## 布局与窗口

- **无系统标题栏**：`frame:false`，自绘标题栏（可拖动；右侧 最小化/最大化/关闭）。
- **左栏**：侧栏分组（插件按 `sidebar` 归组，同组内多个插件可共享一个侧栏）。
- **右区顶栏**：当前插件声明唯一的 `topbar`；下方内容区为插件页面（iframe）。
- 默认内容：内置「系统信息预览」（只读，由 C 层采集）。

## 插件机制

插件放在 `plugins/` 下，**树状搜索**：从根目录深度优先遍历，遇到 `plugin.json` 即认为该目录是一个插件，
**不再向该目录的子目录继续搜索**（插件自持子树）。

插件配置文件 `plugin.json`：

```json
{
  "id": "sysmon",            // 全局唯一（模块名/URL 前缀）
  "name": "资源监控",
  "version": "1.0.0",
  "description": "…",
  "entry": "main.py",        // 插件 Python 入口
  "permission": "readonly",  // 申请级别: readonly | approval | full
  "sidebar": "系统",          // 侧栏 id，不同插件可相同（共享/追加）
  "topbar": "sysmon",        // 顶栏 id，全局唯一
  "icon": "",
  "enabled": true,           // 可在插件管理中覆盖
  "requires": []             // 前置插件 id 列表：缺失/未启用 => 本插件自动禁用
}
```

启动规则：启用插件按发现顺序注册 ——
`sidebar` 已存在→追加，不存在→新建；`topbar` 已存在→**启动报错弹窗**（该插件标记 conflict 不加载），不存在→新建。
`requires` 任一前置插件缺失/未启用 → 本插件整体禁用（原因见插件管理）。

示例插件已删除（`plugins/` 默认从本仓库自带的以下插件开始，也可自行按「写一个插件」添加）：

| 插件 | 侧栏 | 权限 | 功能 |
|------|------|------|------|
| `ftp-server` | 网络 | approval | 纯标准库 FTP 服务器：账户/匿名、PASV·EPSV·PORT·EPRT 主动/被动、上传下载断点续传(REST)、STOU/STAT/MLSD/中文目录；数据默认存于插件文件夹 `ftp-server/Data/`（可指定其它根目录） |
| `ftp-client` | 网络 | approval | 标准库 ftplib 客户端：连接/浏览/上传/下载/新建目录/删除/重命名，自动识别 MLSD/LIST 列表 |
| `cmd-console` | 工具 | approval | 交互式 CMD 终端：持久 cmd.exe 会话、轮询输出、Ctrl+Alt+T 聚焦、一键终止/重启 |
| `procman` | 系统 | approval | 进程管理：枚举（路径/内存/父进程）、结束/挂起/恢复/启动（保护系统关键进程与本软件） |
| `winman` | 系统 | approval | 窗口管理：枚举所有顶层窗口、激活/最小化/最大化/还原/关闭、结束窗口所属进程 |
| `jiyu` | 电子教室 | approval | 极域电子教室工具集（合并自 极域对抗器 jytrainer + 极域工具包 mythwaretoolkit）：广播窗口化/全屏化、密码读取、进程控制、驱动加载、DLL 注入、UDP 命令、SYSTEM 提权、退出黑屏、解网络/USB 限制、一键解禁、动态密码计算器、防杀/防截屏、机房助手处理 |
| `antiseewo` | 电子教室 | approval | 希沃易课堂反制（基于 `学习/反编译` 静态报告移植）：黑屏退出、广播窗口化/全屏化、解除键鼠/网络/USB/HTTPS 审计限制、HTTPS 证书清理（ZoroEx CA）、恢复安全模式引导项、防火墙规则清理、剪贴板监控停用、进程树全停、一键全解、防杀/防截屏、持久化清理、卸载密码读取 |

> 插件之间共享 `kernel32.*` 等 ctypes 函数对象时，**必须用独立函数原型绑定**
> （`ctypes.WINFUNCTYPE(...)(("函数名", windll.kernel32))`）避免 `argtypes` 互相覆盖（结构体类不同会报错），
> 详见 `procman` / `winman` 中对 toolhelp 快照系列的处理方式；插件系统其余特性（前置插件 requires、
> 快捷键申请、共享总线、树状扫描等）均保留。

冒烟测试：`python scripts/plugin_smoke.py`（不启动应用，直接驱动各插件 `register()` 注册的接口，
覆盖 FTP 上传/下载/列表/越界防护、客户端对接本地 FTP、CMD 会话、进程/窗口枚举）。

软件本体内置 API（`python/app/api/system.py`，调用方为插件时按该插件权限级别裁决）：

| API | 权限 | 说明 |
|-----|------|------|
| `summary` / `disks` / `extended` | readonly | 系统信息（扩展含 BIOS/主板/GPU/电池/网络/机器GUID） |
| `infoapi` | readonly | 信息API组合：扩展信息 + 共享总线摘要 |
| `share.list` / `share.get` | readonly | 共享总线（所有插件/系统的信息可共享） |
| `file.read` / `file.write` / `file.list` / `file.delete` / `file.mkdir` | admin | 文件读取/操作（readonly 插件 403；approval 需审批；full 直接放行） |
| `shortcuts` | readonly | 全局快捷键一览 |
| `plugins` | readonly | 插件清单/统计 |

## 权限模型

- **readonly**：只能调 readonly 接口（系统信息查询等）。
- **approval**：首次调用管理接口时宿主弹窗申请；「允许一次」(会话)/「总是允许」(持久)/「拒绝」(持久)；
  之后可在**插件管理 → 权限明细**中开放/取消/重置。
- **full**：管理接口直接放行（与系统 UAC 提权正交；真实改驱动仍需管理员）。
- 裁决发生在 Python 后端 API 路由层（`python/app/api/router.py`），授权持久化于 `python/data/grants.json`，
  插件级覆盖持久化于 `python/data/state.json`。

## 高级选项与内核驱动

「设置 → 更改设置 → **高级选项**」是宿主级总开关（`state.json` 的 `__settings__.advanced`），
开启后以下 `adv.*` 管理接口才对插件开放（且仍受三级权限裁决；调用方为只读/审批插件时按原规则拦截）：

| 接口 | 说明 |
|------|------|
| `adv.proc.list/kill/suspend/resume/start` | 进程枚举/结束/挂起/恢复/启动 |
| `adv.mem.read/write` | 跨进程内存读写 |
| `adv.file.read/write/delete/mkdir` | 系统级文件操作 |
| `adv.reg.read/write/delete/list` | 注册表读写/枚举（HKLM/HKCU/HKCR/HKU） |
| `adv.gp.set` | 本地组策略（写 `HKLM\SOFTWARE\Policies`） |
| `adv.cmd.exec` | 执行命令/程序（不经 cmd.exe） |
| `adv.token.elevate` | 复制 winlogon 的 SYSTEM 令牌到本进程（管理员→系统级） |

**双路径执行**（`python/app/adv.py`）：

- 驱动路径：驱动已加载时（`adv.*` 中可用操作）优先走 `stbdrv.sys` —— 操作在内核的
  SYSTEM 系统线程内执行，可绕过普通管理员受限的 ACL；进程结束/挂起/启动、命令执行均
  为内核原生实现，不调用 `cmd.exe` / `taskkill`。
- 直调路径：驱动不可用时回退 Python ctypes/Win32 直调（`TerminateProcess`、`NtSuspendProcess`、
  `ReadProcessMemory`、`winreg`、`CreateProcessW` 等），仅需管理员权限。

**驱动加载**：设置页显示驱动状态（服务/版本/测试签名/错误），一键「加载驱动」「卸载驱动」。
开发机加载需启用测试签名（`bcdedit /set testsigning on` + 重启）。构建、签名与 ABI 说明见
[`native/driver/README.md`](native/driver/README.md)。驱动已交叉编译为
`native/driver/bin/stbdrv.sys`（无需 VS/WDK，运行 `native\driver\build-driver.ps1`
即可用 NuGet 头 + 便携 llvm-mingw 重建）。未加载驱动时一切功能仍可用（直调路径）。

## 写一个插件

1. 在 `plugins/` 任意层级建目录（父目录不含 `plugin.json`），放入 `plugin.json` + `main.py` + `web/index.html`。
2. `main.py`：

```python
def register(api):
    @api.handler("do_thing", permission="admin",
                 description="干一件事（需要审批/全权）")
    def do_thing(args, ctx):
        data = ctx.bridge.sysinfo()   # 访问 C/C++ 桥接 / ctypes 兜底
        return {"ok": True, **data}
    # permission="readonly" 为只读接口

    # 申请全局快捷键（默认禁用；在「快捷键管理」弹窗中开启/改键/恢复默认）
    @api.shortcut("show", "Ctrl+Alt+S", "触发后做什么")
    def on_show(evt, ctx):
        return {"ok": True}

    # 发布信息到共享总线（任何插件/主页可读）
    api.bus.publish(api.plugin_id, "myplugin.data", {...})
```

3. 页面 `web/index.html` 引入 `<script src="/ui/stbox.js"></script>`，
   用 `stbox.rpc(插件id, 接口, 参数)` 调用；管理接口用 `stbox.rpcWithApproval(...)` 自动等待审批并重试；
   监听快捷键触发事件：`window.addEventListener("message", e => { if (e.data?.type==="stb-shortcut") ... })`。

## 插件管理（应用内）

左栏底部四行：`[日志][退出]` / `[快捷键管理]` / `[插件管理]` / `[设置]`。
「插件管理」：**启用全部 / 禁用全部 / 重新扫描**；列表不显示权限选项，选中插件打开**详情**后才有
权限级别修改、API 授权（开放/取消按当前授权状态切换文案、重置）；快捷键区**只读**展示名称与功能
（开启/改键请到「快捷键管理」，默认禁用）；
权限为只读或全权时授权列表**灰色半透明覆盖、不可编辑**（改为审批权限后恢复）。
「设置」自带侧栏：**系统信息**（应用/原生层/目录）、**更改设置**（**外观主题**：深色默认 / 浅色 / 深蓝 / 纯黑 OLED
预置一键切换，**不提供自定义配色**，主页与全部插件页自动跟随；
**高级选项**开关 + 驱动状态与「加载/卸载驱动」；清除所有插件授权——之后需重新审批、禁用/启用全部插件）、
**关于**（软件本体版本、所有 API 名字+版本、环境版本 Python/Node/Electron/Chromium/原生层）。
「日志」实时查看后端日志；「快捷键管理」：全部快捷键**默认禁用**，每行可**开启/关闭**、**更改键位**
（按下新组合键捕获，Esc 取消）、**恢复默认**（还原默认键位并禁用），顶部「恢复全部默认」一键清空配置。

## 安全（访问令牌）

后端 HTTP 服务（`127.0.0.1:<随机端口>`）**要求每个请求携带访问令牌**：
- 令牌每次启动由宿主(Electron)生成，经 `STB_READY` 交给外壳；请求头 `X-STB-Token`、页面 URL `?token=`，
  或**首次通过校验后种下的 `stb_token` Cookie**（页面内的 `<script>/<link>` 等子资源请求自动携带）均可。
- 未携带/错误令牌的请求一律 `403`（无论页面还是 `/api/*`）——**浏览器直接打开端口无法浏览页面或调用接口**；
  CORS 预检(OPTIONS) 放行但 `Allow-Headers` 仅 `Content-Type, X-STB-Token`。
- 页面输出时后端会把 `?token=` 注入 `<script src="/ui/stbox.js">` 标签：外壳以 `file://` 页面的 iframe 加载页面
  （相对顶层是跨站上下文，SameSite Cookie 不随子资源发送），stbox.js 凭自身 URL 里的令牌即可加载并给所有请求带头。
- 独立运行后端（`python -m app.main`）时自动生成令牌并打印在 `STB_READY` 行，可用 `--token` 指定。
- 插件页面由外壳以 `?token=` 加载，`stbox.js` 自动把令牌附到所有请求，插件无需自行处理。

## 开发目录约定（runtime/）

`runtime/` 随开发目录携带运行环境（不入库）：

```
runtime/node/       便携 Node.js
runtime/python/     便携 Python 3.11+
runtime/toolchain/  cmake + MinGW64（或本机 MSVC）
```

脚本优先使用 `runtime/` 内环境，未找到时回退系统 PATH。拷贝整个开发目录到干净机器即可开发/构建/运行。

## 常用脚本

| 脚本 | 作用 |
|------|------|
| `scripts/bootstrap.ps1` | 检查运行时 + `npm install`（Electron） |
| `scripts/build.ps1` | CMake 构建 `core` + `native_core.dll`（自动选 MinGW/MSVC） |
| `scripts/run.ps1 [-Debug]` | 启动应用（自动跳过构建；无 DLL 也能跑） |
| `scripts/plugin_smoke.py` | 插件冒烟测试：直接驱动各插件的 `register()` 接口（不启动应用） |

## 目录总览

```
core/          C 最底层（sysinfo）
native/        C++ 兼容层（native_core.dll 源码）
native/driver/ C 内核驱动（STBDriver，WDK，含构建/加载文档）
python/        后端（app/ 服务与插件系统；web/ 内置页面；data/ 运行数据；native/ DLL 落点）
plugins/       插件（树状搜索根）
electron/      桌面壳（main/preload/renderer）
scripts/       bootstrap / build / run / find-runtime
docs/          架构设计
runtime/       携带的开发运行环境（占位说明）
```