# SystemToolBox 系统工具箱 — 架构设计

## 1. 总体架构（分层）

```
┌────────────────────────────────────────────────────────────┐
│ Electron Shell（无边框窗口 / 自绘标题栏 / 左栏 / 顶栏）      │
│   renderer: HTML/CSS/JS   preload: contextBridge  main:     │
│   BrowserWindow(无边框) + 启动自动提权(UAC runas) + 后端进程  │
│   (HTTP 127.0.0.1:随机端口)                                 │
├────────────────────────────────────────────────────────────┤
│ Python 后端（界面内容 / API / 插件 / 权限裁决）              │
│   http.server(线程化) + API 路由 + 插件扫描/加载/管理        │
│   内置「系统信息预览」只读 API + 插件页面静态托管             │
│   adv.* 双路径分发：驱动可用→IOCTL；不可用→ctypes Win32 直调  │
├────────────────────────────────────────────────────────────┤
│ C++ 兼容层 native_core.dll（驱动管理 / 服务控制 / 提权 /    │
│   底层交互封装），导出 C ABI，供 Python ctypes 加载          │
├────────────────────────────────────────────────────────────┤
│ C 内核驱动 STBDriver（stbdrv.sys, WDK）                     │
│   进程/内存/文件/注册表/组策略/命令执行/令牌提权             │
│   全部操作在 SYSTEM 系统线程执行（绕过管理员 ACL）           │
├────────────────────────────────────────────────────────────┤
│ C 最底层 core（系统信息采集：CPU/内存/磁盘/系统/开机时长）    │
│   直接调用 Win32 API，纯 C 编写                              │
└────────────────────────────────────────────────────────────┘
```

- **C（core）**：最底层交互。只做系统信息等纯 Win32 采集，无业务逻辑，输出结构化数据。
- **C（内核驱动）**：`native/driver/`，WDK 驱动 `stbdrv.sys`（测试签名加载）。
  每个 IOCTL 请求由驱动派发到 **SYSTEM 系统线程**（`PsCreateSystemThread`）执行，
  从而绕过管理员级 ACL；进程结束/启动/挂起与命令执行均为内核原生实现，**不调用 cmd/taskkill**。
- **C++（native）**：兼容层 + 底层交互 + 驱动管理 + 权限控制。
  以 `extern "C"` ABI 编译为 `native_core.dll`，供 Python `ctypes` 调用；
  也负责服务/驱动（SCM）操作与管理员提权（elevation）。未编译时可运行（ctypes 兜底）。
- **Python**：界面内容、API 服务、插件系统、权限裁决；通过 ctypes 桥接 C/C++ 层，
  通过 `driver.py` 以 `DeviceIoControl` 调用内核驱动（无驱动时 `adv.py` 直调 Win32）。
- **Electron**：桌面壳（窗口、自绘标题栏、左栏/顶栏布局、插件管理界面、审批弹窗、
  启动自动提权）。

进程/通信：
- Electron 主进程启动 Python 后端子进程（`--port`），解析 `STB_READY host/port/token` 就绪后加载界面。
- **访问令牌**：后端 HTTP 服务要求每个请求携带令牌（请求头 `X-STB-Token`、URL `?token=`，
  或首次通过校验后种下的 `stb_token` Cookie），否则一律 `403`；令牌每次启动由宿主生成并经 `STB_READY` 下发，
  **浏览器直接打开端口无法浏览页面或调用接口**。页面输出时后端把 `?token=` 注入 `stbox.js` 标签，
  兼容外壳 `file://` 页面 iframe（跨站）加载插件页的子资源场景。
- 渲染进程与插件页通过 HTTP 访问 Python API；返回头带 `Access-Control-Allow-Origin: *`
  且 `Access-Control-Allow-Headers` 仅放行 `Content-Type, X-STB-Token`。
- 插件页面由 Python 静态托管于 `/plugin/<id>/`，与 API 同源。

## 2. 功能归属

- 软件本体**只含**「系统信息预览」（只读，内置），位于 Python 内置 API + 内置首页。
- 其余一切功能由插件提供。

## 3. 插件协议

### 3.1 插件配置文件 `plugin.json`（扫描依据）

```json
{
  "id": "sysmon",
  "name": "性能监控",
  "version": "1.0.0",
  "description": "实时查看 CPU / 内存 / 磁盘",
  "entry": "main.py",
  "permission": "readonly",
  "sidebar": "系统",
  "topbar": "sysmon",
  "icon": "icon.svg",
  "enabled": true,
  "requires": ["other-plugin"]
}
```

字段：
- `id`：全局唯一标识（同时作为插件模块名与 URL 前缀）。
- `entry`：插件 Python 入口（相对插件目录）。
- `permission`：申请的权限级别 `readonly | approval | full`（可在插件管理里改，见 §5）。
- `sidebar`：侧栏分组 id。**不同插件可以相同**（同一分组内追加入口）。
- `topbar`：顶栏 id。**全局唯一**，冲突即启动报错。
- `enabled`：是否启用；停用的插件启动时不加载、不参与冲突检查，但在插件管理中可见。
- `requires`：前置插件 id 列表。任一前置插件**缺失或未启用/加载失败**，本插件整体**禁用**
  （`status=disabled`，原因写入 `loadError`），不做部分加载。

### 3.4 前置插件（requires）

- 依赖解析在专门的插件加载器（`python/app/plugin/loader.py` 的 `PluginLoader`）中完成：
  1. `resolve_status()`：按 `requires` 检查，缺失/不可用 ⇒ 本插件 `status=disabled`（传递性：被连带禁用的插件也检查其 `requires`）；
  2. 拓扑排序加载：按 `requires` 决定导入顺序，成环 ⇒ 环上插件 `status=error`（“依赖成环”）。
- 示例：插件 A 声明 `requires: ["B"]` —— 停用 B 后 A 立即变“停用”，原因“前置插件未启用: B”。
  （示例插件已随测试插件一并删除，特性保留。）

### 3.5 快捷键申请（插件加载时登记）

插件入口内用 `@api.shortcut(sid, accelerator, description)` 申请全局快捷键：

```python
@api.shortcut("show", "Ctrl+Alt+N", "显示本机网络信息")
def on_show(evt, ctx):
    return {...}          # 结果经事件队列推给插件页面
```

- 快捷键**默认禁用**（与插件权限级别无关，见 §4.3）：仅在「快捷键管理」弹窗中开启/关闭、更改键位、恢复默认；
  配置持久化于 `state.json` 的 `__settings__.shortcuts`（`{"<pluginId>:<sid>": {"enabled": bool, "acc": "自定义键位"}}`）；
  旧版 grants.json 中 `shortcut:<id>=allowed` 的授权在首次读取时**惰性迁移**为「已启用」，保留历史显式授权。
- 生效列表由宿主轮询 `/api/shortcuts` 后经 Electron `globalShortcut` 注册；按下时 `POST /api/shortcuts/trigger`，后端执行插件回调并把结果写入事件队列；渲染层轮询 `/api/events` 后 `postMessage` 转发给插件页面（页面监听 `{type:"stb-shortcut"}`）。

### 3.6 信息共享总线（share）

- 系统与所有插件的公开信息统一发布到共享总线（`python/app/share.py`），实现“信息均可共享”：
  - 系统内置发布 `system.info` / `system.summary` / `system.extended`（BIOS/主板/GPU/电池/网络适配器/机器GUID）。
  - 插件用 `api.bus.publish(plugin_id, "ns", data)` 发布，任何插件经 `system.share.get {ns}` 或 `api.bus.get(ns)` 读取。
  - 只读 API `share.list` / `share.get` 对所有权限级别开放（信息本身是共享公开的）。

### 3.7 软件本体内置 API（供插件按自身权限调用）

- 软件本体内置 API（`python/app/api/system.py`）：系统信息（summary/disks/extended）、信息API（infoapi）、
  共享总线（share.*）、**文件读取/操作**（file.read/write/list/delete/mkdir，admin 级）、插件清单、快捷键清单。
- 插件以**自身 id** 调用内置 API 时（如 `stbox.rpc(pluginId, "file.read", …)`），路由**回退到内置注册表**
  并**按调用方插件级别裁决**（readonly→403、approval→审批流、full→放行），而不是按“system 完全信任”放行，
  避免文件类能力被低权限插件越权使用。

### 3.2 树状搜索（首个配置即终止）

- 搜索根：`plugins/`。
- 递归（深度优先），每进入一个目录先检查是否存在 `plugin.json`：
  - 存在 ⇒ 该目录为一个插件，注册之，**不再下钻其子目录**（插件目录自持子树）。
  - 不存在 ⇒ 继续向下递归。
- 同一插件 id 重复出现：以先找到者为准，后出现的记入启动告警（防止重复注册）。

### 3.3 启动排查规则（侧栏 / 顶栏）

启动时对**启用**插件按发现顺序逐个注册：

| 属性 | 规则 |
|------|------|
| `sidebar` | 已存在该侧栏 ⇒ 把插件加入该侧栏；不存在 ⇒ 新建侧栏 |
| `topbar`  | 已存在该顶栏 ⇒ **弹出报错**（该插件加载失败，启动日记警告）；不存在 ⇒ 新建顶栏 |

侧栏可共享、顶栏不可共享。冲突插件会被标记 `status: conflict`，不加载其页面与 API。

## 4. 权限模型（readonly / approval / full）

- **readonly（只读）**：只能调用标记为只读的 API（系统信息查询等）。
- **approval（审批）**：调用非只读 API 时，**第一次**需要权限 → 弹出申请（应用内模态弹窗，展示插件与 API 说明），用户可"允许 / 拒绝 / 总是允许"；选择结果持久化到 `python/data/grants.json`，之后同 API 直接放行；可在插件管理中**取消**或**开放**权限。
- **full（全权）**：可调用全部 API（含驱动管理、服务控制等提权操作，操作时仍需系统 UAC 提权弹窗——这是操作系统级权限，与插件权限正交）。

实现要点：
- API 处理器注册时声明 `permission`（`readonly` 或 `admin`）。
- 服务器按「插件生效权限级别」+「API 要求」+「grant 记录」三级裁决：

| 插件级别 | readonly API | admin API |
|----------|--------------|-----------|
| readonly | ✅ | ❌ 403 |
| approval | ✅ | 已 grant ✅ / 未 grant → 审批流 ⏳ |
| full     | ✅ | ✅（不再问插件级权限；系统 UAC 另算） |

- 审批流：插件调用 → 服务器返回 `403 {code:"approval_required", request_id}` → 宿主弹窗 → 用户裁决 → 写 grants → 插件重试（由宿主注入的 `stbox` API 提供自动重试辅助）。

### 4.3 快捷键生效（默认禁用，仅快捷键管理可操作）

- 快捷键**默认禁用**（含 full 权限插件，不再自动放行）。生效条件 = **插件已启用 且 快捷键已开启**：

| 插件级别 | 快捷键默认 |
|----------|-----------|
| full / approval / readonly | 🔒 一律默认停用 |

- **唯一操作入口是「快捷键管理」弹窗**（左栏底部）：每行提供启用开关、更改键位（按下新组合键捕获）、
  恢复默认（还原默认键位并禁用）；顶部「恢复全部默认」清空全部配置。插件详情**只读展示**快捷键的名称与功能，
  不提供开关/允许/拒绝按钮。
- 管理接口（`python/app/api/manager.py`）：`POST /api/manager/<pid>/shortcuts/<sid>`
  `{action: enable|disable|bind, accelerator|reset}` 与 `POST /api/manager/shortcuts/reset-all`；
  键位字符串由 `python/app/shortcuts.py` 校验（`^[A-Za-z0-9+]{1,64}$`，非法返回 `bad_args`）。
- 配置持久化在 `state.json` 的 `__settings__.shortcuts`；旧版 grants.json `shortcut:<id>=allowed`
  首次读取时惰性迁移为「已启用」（保留用户历史显式授权）。
- 宿主侧二次校验：`POST /api/shortcuts/trigger` 会再次检查插件状态与快捷键生效状态，防止越权触发。

### 4.4 高级选项与内核驱动（adv.*）

- 「高级选项」是**宿主级**总开关（`state.json` 的 `__settings__.advanced`，`GET/POST /api/settings`、
  `/api/settings/advanced` 维护），**非按插件**；关闭时所有 `adv.*` 返回 `403 advanced_disabled`。
- `adv.*`（proc/mem/file/reg/gp/cmd/token）注册为 admin 级内置 API（`python/app/api/system.py`），
  插件调用时**仍走三级权限裁决**（系统自身调用为完全信任）；每个操作还有必需参数校验。
- **双路径分发**（`python/app/adv.py` 的 `_driver_or_direct`）：
  驱动已加载 → 封装请求结构，经 `driver.py` `DeviceIoControl` 走内核（操作在 SYSTEM 线程内执行，
  真正绕过普通管理员受限 ACL）；驱动不可用/IOError → 回退 ctypes/Win32 直调
  （`TerminateProcess`、`NtSuspendProcess`、`ReadProcessMemory`、`winreg`、`CreateProcessW`——同样不经 cmd/taskkill）。
- 直调路径要求进程为管理员（`_need_admin`，否则 `403 need_admin`）；软件启动自动提权保证其在正式运行中满足。
- **令牌提权**：`adv.token.elevate` 驱动路径复制 winlogon 的 SYSTEM 主令牌并注入调用进程
  （管理员→系统级）；直调路径退化为仅启用 `SeDebugPrivilege`（报告 `system:False`）。
- 驱动 ABI 约定见 [`native/driver/README.md`](../native/driver/README.md)（IOCTL 号、请求结构、magic、
  尺寸上限、REG_LIST 序列化须与 `stbdrv.h` / `driver.py` / `adv.py` 三处保持一致）。
- 安全边界：驱动拒绝结束 pid≤4 的进程；进程启动为**越权通道**，由「高级选项总开关 +
  admin 门控 + 插件权限模型」三道闸把关；驱动缺失时全部能力仍可用（直调路径）。

## 5. 插件管理（应用内）

- 列表页**不显示权限更改选项**（权限属于插件详情）：插件 id / 名称 / 状态（正常、停用、冲突、加载失败、缺前置）/ 侧栏 / 顶栏 / 操作（启用、停用、详情）。
- 工具栏：**启用全部 / 禁用全部 / 重新扫描**。
- 选择具体插件后打开**详情**，才提供：
  - 权限级别（readonly / approval / full，即时生效）；
  - API 授权表：**开放/取消合并为单个按钮**，按当前授权状态更换文案（未授权→"开放"允许；已授权→"取消"拒绝），另有"重置"；
  - 快捷键信息区（该插件申请的快捷键：**只读展示名称 + 功能**；开启/改键请到「快捷键管理」操作）。
- **只读/全权覆盖层**：插件权限为 readonly 或 full 时，下方授权列表被**灰色半透明覆盖层**遮罩、不可编辑
  （readonly 提示"管理接口均不可调用"、full 提示"全部放行"）；改为审批权限后恢复编辑。
- 状态持久化在 `python/data/state.json`（插件级覆盖：enabled 覆盖、permission 覆盖），grant 在 `python/data/grants.json`。

## 6. 窗口与布局

- **无系统标题栏**：`BrowserWindow({ frame: false, titleBarStyle: 'hidden' })`。
- **自绘标题栏**：上层一行，`-webkit-app-region: drag`，右侧最小化/最大化/关闭按钮（`no-drag`）。
- **左栏**：
  - 顶部：**主页**入口（软件本体主页：左侧计算机账户 + 基础信息，右侧磁贴实时显示系统占用 CPU/内存/磁盘）。
  - 中部：侧栏分组列表（可折叠），每项为该侧栏下的插件入口；
  - 底部四行：`[日志][退出]` / `[快捷键管理]` / `[插件管理]` / `[设置]`（日志与退出同一行）。
- **右区顶栏**：当前插件所声明的顶栏（含面包屑、插件操作），下方为内容区（iframe 加载插件页 / 内置首页）。
- **设置弹窗自带侧栏**（三个分栏）：
  - **系统信息**：应用 / 原生层 / 目录 / 计数；
  - **更改设置**：**外观主题**（预置主题一键切换：深色默认 / 浅色 / 深蓝 / 纯黑 OLED；仅切换固定预置，**不提供自定义配色**——后端 `themes.py` 持有预置调色板，`GET/POST /api/theme`、`GET /api/theme/list` 管理当前主题并持久化到宿主设置，服务端输出任何页面 HTML 时在 `</head>` 前注入 `<style id="stb-theme">:root{...}</style>` 覆盖块，外壳在启动与切换时把主题变量应用到自身文档并重载 iframe，主页/插件页经 `var(--*)` 自动跟随）、**高级选项**（总开关 + 驱动状态行 + 加载/卸载驱动，见 §4.4）、**清除所有插件授权**（之后需重新审批）、禁用 / 启用全部插件；
  - **关于**：软件本体版本、**所有 API 的名字 + 版本**（内置 system 与各插件，含权限级别）、**环境版本**（Python / Node / Electron / Chromium / 原生层）。
- 日志：实时查看后端日志（可刷新、清空）；快捷键管理：全部快捷键的**启用开关 / 更改键位 / 恢复默认**（默认禁用，含「恢复全部默认」；改键通过按下新组合键捕获，Esc 取消）；退出：关闭窗口结束应用。

## 7. 开发目录约定（runtime/）

`runtime/` 存放随开发目录携带的运行环境（不入库，见 .gitignore）：

```
runtime/
  node/      # Node.js 便携版
  python/    # Python 便携版
  toolchain/ # cmake 或 MinGW / MSVC 工具链
```

- 脚本（`scripts/*.ps1`）优先使用 `runtime/` 内环境，其次回退到系统 PATH。
- 目标：把整个开发目录拷走即可在一台干净机器上开发/构建/运行。

## 8. 目录总览

```
SystemToolBox/
├── core/       C 最底层（sysinfo 采集）
├── native/     C++ 兼容层（native_core.dll：驱动/服务/提权/封装）
├── native/driver/  C 内核驱动（STBDriver / stbdrv.sys：WDK 源码 + 构建/加载文档）
├── python/     后端（app/ 服务与插件系统，web/ 内置页面）
├── plugins/    插件目录（树状搜索根）
├── electron/   桌面壳（main/preload/renderer，含启动自动提权）
├── scripts/    bootstrap / build / run
├── docs/       设计文档
└── runtime/    携带的开发运行环境（node/python/工具链）
```