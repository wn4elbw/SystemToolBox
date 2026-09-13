"""宿主(Host)辅助接口：全局快捷键注册表与触发、事件队列、后端日志、设置信息。"""
from .. import config, themes
from ..log import error as log_error, info
from .router import ApiError, api_ok


def register(ctx, router):
    # ---------------- 快捷键 ----------------

    @router.route("GET", "/api/shortcuts")
    def _shortcuts(c, m, body):
        """当前生效的全局快捷键（供 Electron 主进程注册）。"""
        return api_ok({
            "shortcuts": c.shortcuts.list_active(c._perm_lookup),
        })

    @router.route("POST", "/api/shortcuts/trigger")
    def _trigger(c, m, body):
        """Electron 全局快捷键被按下 -> 触发插件回调并推事件。"""
        body = body or {}
        pid = body.get("pluginId")
        sid = body.get("id")
        if not pid or not sid:
            raise ApiError("bad_args", "缺少 pluginId / id")
        # 校验该快捷键当前确实生效（防止越权触发）
        perm = c._perm_lookup(pid)
        if perm == "none":
            raise ApiError("shortcut_inactive", f"插件未加载: {pid}", http=403)
        items = c.shortcuts._items.get(pid) or {}
        it = items.get(sid)
        if not it or not c.shortcuts.effective(pid, sid, perm):
            raise ApiError("shortcut_inactive",
                           f"快捷键 {sid} 未生效（未授权或插件停用）", http=403)
        return api_ok(c.shortcuts.trigger(pid, sid, c, body))

    # ---------------- 事件队列 ----------------

    @router.route("GET", "/api/events")
    def _events(c, m, body):
        """取走并清空事件队列（渲染层轮询，转发给插件页面）。"""
        return api_ok({"events": c.events.drain()})

    # ---------------- 日志 ----------------

    @router.route("GET", "/api/logs")
    def _logs(c, m, body):
        lines = 200
        try:
            lines = max(10, min(int((body or {}).get("lines", 200)), 2000))
        except (TypeError, ValueError):
            pass
        try:
            data = config.LOG_FILE.read_text(encoding="utf-8",
                                             errors="replace").splitlines()
        except OSError:
            data = []
        return api_ok({
            "lines": data[-lines:],
            "path": str(config.LOG_FILE),
        })

    @router.route("POST", "/api/logs/clear")
    def _logs_clear(c, m, body):
        try:
            config.LOG_FILE.write_text("", encoding="utf-8")
        except OSError as e:
            log_error(f"清空日志失败: {e}")
            raise ApiError("io_error", f"清空日志失败: {e}")
        return api_ok({"cleared": True})

    # ---------------- 设置 ----------------

    @router.route("GET", "/api/settings")
    def _settings(c, m, body):
        import sys as _sys
        # 所有 API 的名字与版本（内置 system 版本=软件版本；插件=清单版本）
        apis = []
        from ..plugin.model import manifest_to_dict
        reg = c.registry
        for pid, mf in sorted(c.manifests.items()):
            handlers = reg.get(pid) or {}
            apis.append({
                "pluginId": pid,
                "pluginName": mf.name,
                "version": mf.version,
                "permission": mf.permission,
                "status": mf.status,
                "apis": sorted([
                    {"name": h.name, "permission": h.permission,
                     "description": h.description}
                    for h in handlers.values()],
                    key=lambda x: x["name"]),
            })
        sys_handlers = reg.get(config.BUILTIN_PLUGIN_ID) or {}
        apis.insert(0, {
            "pluginId": config.BUILTIN_PLUGIN_ID,
            "pluginName": "软件本体(内置)",
            "version": config.APP_VERSION,
            "permission": "full",
            "status": "ok",
            "apis": sorted([
                {"name": h.name, "permission": h.permission,
                 "description": h.description}
                for h in sys_handlers.values()],
                key=lambda x: x["name"]),
        })
        return api_ok({
            "app": {"name": config.APP_NAME, "version": config.APP_VERSION},
            "native": {
                "available": c.bridge.available,
                "path": c.bridge.path,
                "version": c.bridge.version(),
            },
            "dirs": {
                "root": str(config.ROOT),
                "pythonDir": str(config.PYTHON_DIR),
                "dataDir": str(config.DATA_DIR),
                "pluginsDir": str(config.PLUGINS_DIR),
                "logFile": str(config.LOG_FILE),
            },
            "counts": {
                "plugins": len(c.manifests),
                "shortcutsActive": len(c.shortcuts.list_active(c._perm_lookup)),
                "grants": sum(len(v) for v in c.store._grants.values()),
            },
            "advanced": {
                "enabled": bool(c.store.get_host_setting("advanced", False)),
                "driverState": driver_status_locked(c)["state"],
                "driverLoaded": c.store.get_host_setting("advanced", False)
                and driver_status_locked(c)["state"] == "ready",
                "elevated": bool(c.bridge.is_elevated() == 1),
            },
            "theme": {
                "current": themes.current_id(c.store),
                "list": themes.list_presets(),
            },
            "env": {
                "python": ".".join(map(str, _sys.version_info[:3])),
                "pythonFull": _sys.version.split("\n")[0],
            },
            "apis": apis,
        })

    @router.route("POST", "/api/settings/grants/clear")
    def _grants_clear(c, m, body):
        n = c.store.reset_all_api_grants()
        info(f"设置: 已清除全部插件授权 {n} 条，插件需重新审批")
        return api_ok({"cleared": n})

    # ---------------- 窗口外观（标题模板 / 图标文件） ----------------
    # 标题为模板字符串，支持 %%time%%/%%date%%/%%perm%%/%%rand6%%/%%ver%%/%%name%%
    # 等占位符，由渲染层解析后显示；图标为用户自选的图片文件，
    # 经 Electron 主进程缩放为 128x128 custom.ico 存放（默认图标不变）。

    _TITLE_MAX = 120
    # 支持的占位符说明（供设置面板提示按钮展示）
    TITLE_TOKENS = [
        ("%%time%%", "当前时间 HH:MM:SS（每秒刷新）"),
        ("%%date%%", "当前日期 YYYY-MM-DD（每秒刷新）"),
        ("%%perm%%", "权限（管理员 / 普通）"),
        ("%%rand6%%", "随机 6 位（大小写字母+数字）"),
        ("%%ver%%", "软件版本"),
        ("%%name%%", "软件名称"),
    ]

    @router.route("GET", "/api/settings/window")
    def _window_get(c, m, body):
        return api_ok({
            "title": str(c.store.get_host_setting("windowTitle",
                                                  config.APP_NAME)),
            # 模板解析所需上下文（供渲染层替换 %%perm%%/%%ver%%/%%name%%）
            "perm": ("管理员" if (c.bridge.is_elevated() == 1) else "普通"),
            "version": config.APP_VERSION,
            "name": config.APP_NAME,
            "tokens": [{"token": tk, "hint": ht}
                       for tk, ht in TITLE_TOKENS],
        })

    @router.route("POST", "/api/settings/window")
    def _window_set(c, m, body):
        body = body or {}
        title = str(body.get("title") or "").strip()
        if title:
            if len(title) > _TITLE_MAX:
                raise ApiError("bad_args", f"窗口标题模板过长（最多 {_TITLE_MAX} 字）")
            c.store.set_host_setting("windowTitle", title)
            info(f"设置: 窗口标题模板 -> {title!r}")
        return api_ok({
            "title": str(c.store.get_host_setting("windowTitle",
                                                  config.APP_NAME)),
        })

    @router.route("POST", "/api/settings/advanced")
    def _advanced_set(c, m, body):
        """高级选项开关：开启后 adv.* 优先走内核驱动，否则直调。"""
        body = body or {}
        enabled = bool(body.get("enabled"))
        c.store.set_host_setting("advanced", enabled)
        if enabled:
            status = driver_status_locked(c)
            info(f"设置: 高级选项已开启，驱动状态={status['state']}")
        else:
            info("设置: 高级选项已关闭（高级操作将拒绝执行）")
        return api_ok({"enabled": enabled})

    # ---------------- 主题预置（只读列表 + 切换，无配色自定义） ----------------

    @router.route("GET", "/api/theme/list")
    def _theme_list(c, m, body):
        return api_ok({
            "themes": themes.list_presets(),
            "current": themes.current_id(c.store),
        })

    @router.route("GET", "/api/theme")
    def _theme_get(c, m, body):
        return api_ok(themes.get_theme(themes.current_id(c.store)))

    @router.route("POST", "/api/theme")
    def _theme_set(c, m, body):
        body = body or {}
        return api_ok(themes.set_current(c.store, str(body.get("id") or "")))

    # ---------------- 内核驱动（高级选项的底层执行器） ----------------

    @router.route("GET", "/api/driver/status")
    def _driver_status(c, m, body):
        return api_ok(driver_status_locked(c))

    @router.route("POST", "/api/driver/load")
    def _driver_load(c, m, body):
        from ..driver import manager
        r = manager.load()
        return api_ok(r)

    @router.route("POST", "/api/driver/unload")
    def _driver_unload(c, m, body):
        from ..driver import manager
        return api_ok(manager.unload())

    def driver_status_locked(c):
        from ..driver import manager
        st = manager.status()
        st["advanced"] = bool(c.store.get_host_setting("advanced", False))
        return st
