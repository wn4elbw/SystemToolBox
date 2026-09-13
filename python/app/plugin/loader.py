"""专门插件加载器 PluginLoader：前置依赖解析、拓扑顺序加载、快捷键注册。

插件入口约定（plugin.json 的 entry 字段，默认 main.py）：
    模块内定义 `def register(api)`，其中 api 是 PluginApi 实例：
        @api.handler("do_thing", permission="admin", description="说明")
        def do_thing(args, ctx): ... return {...}

        @api.shortcut("refresh", "Ctrl+Alt+R", "刷新数据")
        def on_shortcut(evt, ctx): ...   # 申请全局快捷键（是否生效由权限管理决定）

前置插件（plugin.json 的 requires 字段）：
    ["sysmon", ...] —— 任一缺失或未启用(disabled/conflict/error)，
    则本插件整体禁用(status="disabled")，并给出原因。

加载顺序按 requires 做拓扑排序；环依赖视为加载失败。
"""
import importlib.util
import sys
import traceback
from pathlib import Path

from .. import config
from ..log import error, info
from ..api.router import Handler


class PluginApi:
    """插件可见的注册 API 门面。"""

    def __init__(self, ctx, manifest):
        self._ctx = ctx
        self._manifest = manifest
        self._handlers = {}
        self._shortcuts = {}

    def handler(self, name=None, permission=config.API_READONLY,
                description=""):
        """注册一个接口。permission: readonly | admin。"""
        def deco(fn):
            api_name = name or fn.__name__
            if permission not in (config.API_READONLY, config.API_ADMIN):
                raise ValueError(f"非法接口权限: {permission}")
            self._handlers[api_name] = Handler(api_name, fn, permission,
                                               description)
            return fn
        return deco

    def shortcut(self, sid, accelerator, description=""):
        """申请一个全局快捷键（通过插件加载方式登记）。

        是否生效属于权限管理：full 默认启用；approval/readonly 需在
        插件管理中授权（grants.json 持久化）。加速键格式遵循 Electron
        globalShortcut，如 "Ctrl+Alt+S"、"F8"。
        """
        def deco(fn):
            self._shortcuts[sid] = {
                "accelerator": accelerator,
                "description": description,
                "handler": fn,
            }
            return fn
        return deco

    # ---- 便捷工具 ----

    @property
    def native(self):
        """访问 C/C++ 桥接（native_core.dll 或 Win32 ctypes 兜底）。"""
        return self._ctx.bridge

    @property
    def log(self):
        from .. import log as logger
        return logger

    @property
    def bus(self):
        """信息共享总线：publish(owner, ns, data) / get(ns) / list()。"""
        return self._ctx.bus

    @property
    def data_dir(self) -> Path:
        """插件私有数据目录（自动创建）。"""
        d = config.DATA_DIR / "plugins" / self._manifest.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def plugin_id(self) -> str:
        return self._manifest.id

    def get_handlers(self):
        return self._handlers

    def get_shortcuts(self):
        return self._shortcuts


class PluginLoader:
    """专门的插件加载器：解析依赖 -> 拓扑排序 -> 逐个导入注册。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.registry = {}        # pid -> {api: Handler}
        self.shortcuts = {}       # pid -> {sid: {...}}（注册在 ShortcutRegistry 后不再保留）

    # ---------------- 依赖解析 ----------------

    def resolve_status(self):
        """根据 requires 与各插件当前启用状态，计算最终 status。

        规则：
        - 用户/清单停用(enabled=False) 或顶栏冲突 -> 保持 disabled/conflict；
        - requires 中任一插件缺失或不可用(非 ok) -> 本插件 disabled(缺前置)；
        - 其余保持 ok。
        返回 None（就地修改 manifests）。
        """
        ms = self.ctx.manifests
        ok_ids = {pid for pid, m in ms.items() if m.status == "ok"}

        for pid, m in ms.items():
            if m.status != "ok":
                continue
            for req in m.requires:
                rm = ms.get(req)
                if rm is None:
                    m.status = "disabled"
                    m.load_error = f"缺少前置插件: {req}"
                    info(f"插件 {pid} 禁用: {m.load_error}")
                    break
                if req not in ok_ids:
                    m.status = "disabled"
                    m.load_error = f"前置插件未启用: {req}({rm.status})"
                    info(f"插件 {pid} 禁用: {m.load_error}")
                    break
        # 依赖被连带禁用的插件需要再检查一轮（传递性）
        changed = True
        while changed:
            changed = False
            ok_ids = {pid for pid, m in ms.items() if m.status == "ok"}
            for pid, m in ms.items():
                if m.status != "ok":
                    continue
                for req in m.requires:
                    if req not in ok_ids:
                        m.status = "disabled"
                        m.load_error = f"前置插件未启用: {req}"
                        changed = True
                        break

    # ---------------- 拓扑排序 ----------------

    def _topo_order(self, ids):
        """对需要加载的插件按 requires 拓扑排序；环依赖返回 (None, 环成员)。"""
        ms = self.ctx.manifests
        state = {}   # 0=未访问 1=访问中 2=完成
        order = []
        cycle = []

        def visit(pid, stack):
            if state.get(pid) == 2:
                return True
            if state.get(pid) == 1:
                # 找到环：从当前栈里截取
                i = stack.index(pid)
                cycle.extend(stack[i:] + [pid])
                return False
            state[pid] = 1
            stack.append(pid)
            for req in ms[pid].requires:
                if req in ids and not visit(req, stack):
                    return False
            stack.pop()
            state[pid] = 2
            order.append(pid)
            return True

        for pid in ids:
            if state.get(pid) != 2 and not visit(pid, []):
                return None, cycle
        return order, None

    # ---------------- 加载 ----------------

    def load_all(self):
        """加载所有 status=='ok' 的插件（依赖拓扑序），返回 registry。"""
        self.registry = {}
        ids = [pid for pid, m in self.ctx.manifests.items()
               if m.status == "ok" and pid != config.BUILTIN_PLUGIN_ID]
        order, cycle = self._topo_order(ids)
        if cycle:
            error(f"插件依赖成环，跳过加载: {' -> '.join(cycle)}")
            for pid in cycle:
                m = self.ctx.manifests.get(pid)
                if m:
                    m.status = "error"
                    m.load_error = f"依赖成环: {' -> '.join(cycle)}"
        for pid in (order or []):
            m = self.ctx.manifests.get(pid)
            try:
                handlers, shortcuts = self._load_one(m)
                self.registry[pid] = handlers
                if shortcuts:
                    self.ctx.shortcuts.register(pid, m.name, shortcuts)
                    info(f"插件 {pid} 申请快捷键: {list(shortcuts)}")
                m.loaded = True
                info(f"插件 {pid} 已加载，注册接口 {sorted(handlers.keys())}"
                     + (f"，快捷键 {list(shortcuts)}" if shortcuts else ""))
            except Exception as e:  # noqa: BLE001
                m.status = "error"
                m.load_error = f"{type(e).__name__}: {e}"
                error(f"插件 {pid} 加载失败:\n{traceback.format_exc()}")
        return self.registry

    def unload_all(self):
        """卸载（在重新扫描前清理旧注册）。"""
        self.ctx.shortcuts.clear()
        self.ctx.bus.clear_owner("*")   # 共享信息在重载后由各插件重新发布

    def _load_one(self, m):
        entry = m.entry_path
        if not entry.is_file():
            raise FileNotFoundError(f"入口文件不存在: {entry}")

        api = PluginApi(self.ctx, m)
        module_name = f"stb_plugin_{m.id}"

        # 允许插件 import 同目录模块；并确保后端包 app 可被导入
        sys.path.insert(0, str(m.dir))
        sys.path.insert(0, str(config.PYTHON_DIR))
        try:
            spec = importlib.util.spec_from_file_location(module_name, entry)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法加载入口: {entry}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "register"):
                module.register(api)
        finally:
            sys.path.remove(str(m.dir))
            try:
                sys.path.remove(str(config.PYTHON_DIR))
            except ValueError:
                pass

        return api.get_handlers(), api.get_shortcuts()


def load_plugins(ctx):
    """兼容入口：使用专门的 PluginLoader 加载。"""
    ctx.loader = PluginLoader(ctx)
    ctx.registry = ctx.loader.load_all()
