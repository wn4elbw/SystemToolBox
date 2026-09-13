"""应用上下文：把存储、扫描、布局、加载、审批、共享总线、快捷键串起来。"""
import threading
import time

from . import config
from .api.approvals import ApprovalRegistry
from .core.bridge import bridge
from .log import info
from .plugin.loader import PluginLoader
from .plugin.scanner import build_layout, scan_plugins
from .plugin.store import Store
from .share import SharedBus
from .shortcuts import ShortcutRegistry


class EventQueue:
    """轻量事件队列：宿主/渲染层轮询 /api/events 取走。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []

    def push(self, evt: dict):
        with self._lock:
            self._items.append(evt)
            if len(self._items) > 200:      # 防堆积
                del self._items[:100]

    def drain(self):
        with self._lock:
            items, self._items = self._items, []
            return items


class AppContext:
    def __init__(self):
        self.store = Store()
        self.bridge = bridge
        self.approvals = ApprovalRegistry(self.store)
        self.bus = SharedBus()
        self.shortcuts = ShortcutRegistry(self.store)
        self.events = EventQueue()

        self.manifests: dict = {}        # id -> PluginManifest
        self.sidebars: dict = {}         # sidebar id -> SidebarEntry
        self.topbars: dict = {}          # topbar id -> PluginManifest
        self.conflicts: list = []        # 顶栏冲突列表
        self.scan_warnings: list = []
        self.registry: dict = {}         # plugin id -> {api: Handler}
        self.loader: PluginLoader = None

    def rescan(self):
        """重新从磁盘扫描插件（树状搜索）+ 应用覆盖状态 + 重建布局/加载。"""
        self.manifests, self.scan_warnings = scan_plugins()
        for m in self.manifests.values():
            st = self.store.plugin_state(m.id)
            if "enabled" in st:
                m.enabled = st["enabled"]
            if "permission" in st:
                m.permission = st["permission"]
        self.rebuild()
        info(f"扫描完成: {len(self.manifests)} 个插件, "
             f"{len(self.sidebars)} 个侧栏, {len(self.topbars)} 个顶栏, "
             f"{len(self.conflicts)} 个冲突, "
             f"快捷键 {len(self.shortcuts.list_active(self._perm_lookup))} 个生效")

    def rebuild(self):
        """按当前清单重新布局（侧栏/顶栏规则）并加载插件。

        步骤：
        1. 卸载旧注册（快捷键/共享信息）；
        2. build_layout 计算侧栏/顶栏/冲突；
        3. 专门的加载器解析前置依赖(requires)与拓扑顺序，逐个导入；
        4. 内置 system API 常驻 registry，并发布系统信息到共享总线。
        """
        if self.loader:
            self.loader.unload_all()
        else:
            # 首次：清理任何残留
            self.shortcuts.clear()
            self.bus.clear_owner("*")

        priority = [p.id for p in self.topbars.values()
                    if p.enabled and p.id in self.manifests]
        self.sidebars, self.topbars, self.conflicts = \
            build_layout(self.manifests, priority_ids=priority)

        self.loader = PluginLoader(self)
        self.loader.resolve_status()          # requires 缺失 -> 禁用
        self.registry = self.loader.load_all()

        # 内置 system API 常驻 registry
        from .api import system as _sys
        _sys.register(self)

        # 系统信息发布到共享总线（所有插件可读取）
        self._publish_system_info()

    def _publish_system_info(self):
        try:
            s = self.bridge.sysinfo()
            self.bus.publish(config.BUILTIN_PLUGIN_ID, "system.info", s)
            self.bus.publish(config.BUILTIN_PLUGIN_ID, "system.summary", {
                "osName": s.get("osName"), "osVersion": s.get("osVersion"),
                "cpuName": s.get("cpuName"), "cores": s.get("cores"),
                "logicalCores": s.get("logicalCores"),
                "cpuUsage": s.get("cpuUsage"),
                "memoryTotal": s.get("memoryTotal"),
                "memoryFree": s.get("memoryFree"),
                "uptimeSec": s.get("uptimeSec"),
                "machineName": s.get("machineName"),
                "userName": s.get("userName"),
                "processCount": s.get("processCount"),
            })
            self.bus.publish(config.BUILTIN_PLUGIN_ID, "system.extended",
                             self.bridge.extended())
        except Exception as e:  # noqa: BLE001
            info(f"系统信息发布到共享总线失败: {e}")

    def _perm_lookup(self, pid: str) -> str:
        """插件当前有效权限（供快捷键注册查询）；不存在的插件返回 'none'。"""
        m = self.manifests.get(pid)
        if m is None or m.status != "ok":
            return "none"
        return m.effective_permission

    def plugin_handlers(self, plugin_id: str) -> dict:
        return self.registry.get(plugin_id) or {}
