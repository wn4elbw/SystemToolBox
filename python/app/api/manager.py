"""插件管理接口：列表 / 启用停用 / 权限级别 / API 授权 / 重新扫描。"""
from .. import config
from ..log import info
from ..plugin.model import manifest_to_dict
from .router import ApiError, api_ok


def _plugin(ctx, pid):
    m = ctx.manifests.get(pid)
    if m is None:
        raise ApiError("plugin_not_found", f"插件不存在: {pid}", http=404)
    return m


def register(ctx, router):
    store = ctx.store

    @router.route("GET", "/api/manager/list")
    def _list(c, m, body):
        return api_ok({
            "plugins": [manifest_to_dict(p) for p in c.manifests.values()],
            "conflicts": c.conflicts,
            "scanWarnings": c.scan_warnings,
            "native": {
                "available": c.bridge.available,
                "path": c.bridge.path,
                "version": c.bridge.version(),
            },
        })

    @router.route("POST", r"/api/manager/([A-Za-z0-9_-]+)/enable")
    def _enable(c, m, body):
        p = _plugin(c, m.group(1))
        store.set_plugin_enabled(p.id, True)
        p.enabled = True
        c.rebuild()
        info(f"插件管理: 启用 {p.id}")
        return api_ok(manifest_to_dict(p))

    @router.route("POST", r"/api/manager/([A-Za-z0-9_-]+)/disable")
    def _disable(c, m, body):
        p = _plugin(c, m.group(1))
        store.set_plugin_enabled(p.id, False)
        p.enabled = False
        c.rebuild()
        info(f"插件管理: 停用 {p.id}")
        return api_ok(manifest_to_dict(p))

    @router.route("POST", r"/api/manager/([A-Za-z0-9_-]+)/permission")
    def _permission(c, m, body):
        p = _plugin(c, m.group(1))
        perm = (body or {}).get("permission")
        if perm not in config.PERM_LEVELS:
            raise ApiError("bad_args", f"非法权限级别: {perm}")
        store.set_plugin_permission(p.id, perm)
        p.permission = perm
        info(f"插件管理: {p.id} 权限级别 -> {perm}")
        return api_ok(manifest_to_dict(p))

    # ---------------- 批量启用/停用 ----------------

    @router.route("POST", "/api/manager/enable-all")
    def _enable_all(c, m, body):
        ids = []
        for p in c.manifests.values():
            store.set_plugin_enabled(p.id, True)
            p.enabled = True
            ids.append(p.id)
        c.rebuild()
        info(f"插件管理: 启用全部 {len(ids)} 个插件")
        return api_ok({"enabledAll": len(ids)})

    @router.route("POST", "/api/manager/disable-all")
    def _disable_all(c, m, body):
        ids = []
        for p in c.manifests.values():
            store.set_plugin_enabled(p.id, False)
            p.enabled = False
            ids.append(p.id)
        c.rebuild()
        info(f"插件管理: 停用全部 {len(ids)} 个插件")
        return api_ok({"disabledAll": len(ids)})

    # ---------------- 插件详情（权限/API 授权/快捷键授权） ----------------

    @router.route("GET", r"/api/manager/([A-Za-z0-9_-]+)/grants")
    def _grants(c, m, body):
        p = _plugin(c, m.group(1))
        handlers = c.registry.get(p.id) or {}
        grants = c.store.api_grants_view(p.id)
        apis = [
            {
                "api": name,
                "permission": h.permission,
                "description": h.description,
                "grant": (grants.get(name) or {}).get("grant"),
                "persistent": (grants.get(name) or {}).get("persistent"),
            }
            for name, h in sorted(handlers.items())
        ]
        shortcuts = []
        items = c.shortcuts._items.get(p.id) or {}
        for sid, it in sorted(items.items()):
            shortcuts.append({
                "id": sid,
                "accelerator": it.get("accelerator"),
                "description": it.get("description"),
                "enabled": c.shortcuts.enabled(p.id, sid),
                "effective": c.shortcuts.effective(
                    p.id, sid, p.effective_permission),
            })
        return api_ok({
            "plugin": manifest_to_dict(p),
            "apis": apis,
            "shortcuts": shortcuts,
            "effectivePermission": p.effective_permission,
        })

    @router.route("POST", r"/api/manager/([A-Za-z0-9_-]+)/grants/([A-Za-z0-9_.-]+)")
    def _grant(c, m, body):
        p = _plugin(c, m.group(1))
        api_name = m.group(2)
        action = (body or {}).get("action")
        if action == "allow":
            c.store.set_api_grant(p.id, api_name, "allowed", persistent=True)
        elif action == "deny":
            c.store.set_api_grant(p.id, api_name, "denied", persistent=True)
        elif action == "reset":
            c.store.reset_api_grant(p.id, api_name)
        else:
            raise ApiError("bad_args", f"非法操作: {action}")
        info(f"插件管理: {p.id}.{api_name} grant -> {action}")
        return api_ok({"pluginId": p.id, "api": api_name, "action": action})

    @router.route("POST",
                  r"/api/manager/([A-Za-z0-9_-]+)/shortcuts/([A-Za-z0-9_.-]+)")
    def _shortcut_grant(c, m, body):
        p = _plugin(c, m.group(1))
        sid = m.group(2)
        action = (body or {}).get("action")
        if action in ("allow", "enable", "deny", "disable"):
            msg = c.shortcuts.set_grant(p.id, sid, action)
            info(f"快捷键管理: {p.id}.{sid} -> {action}")
            return api_ok({"pluginId": p.id, "shortcut": sid,
                           "action": action, "message": msg})
        if action == "reset":
            c.shortcuts.reset(p.id, sid)
            info(f"快捷键管理: {p.id}.{sid} 恢复默认")
            return api_ok({"pluginId": p.id, "shortcut": sid,
                           "action": "reset", "message": "已恢复默认键位与禁用状态"})
        if action == "bind":
            acc = (body or {}).get("accelerator", "")
            try:
                c.shortcuts.set_accelerator(p.id, sid, acc)
            except ValueError as e:
                raise ApiError("bad_args", str(e))
            info(f"快捷键管理: {p.id}.{sid} 改键 -> {acc}")
            return api_ok({"pluginId": p.id, "shortcut": sid,
                           "action": "bind", "accelerator": acc,
                           "message": f"已更改为 {acc}"})
        raise ApiError("bad_args", f"非法操作: {action}")

    @router.route("POST", r"/api/manager/shortcuts/reset-all")
    def _shortcut_reset_all(c, m, body):
        n = c.shortcuts.reset_all()
        info(f"快捷键管理: 全部恢复默认（{n} 条配置已清除）")
        return api_ok({"reset": n, "message": f"已清除 {n} 条快捷键配置"})

    @router.route("POST", "/api/manager/rescan")
    def _rescan(c, m, body):
        c.rescan()
        info("插件管理: 已重新扫描")
        return api_ok({
            "plugins": [manifest_to_dict(p) for p in c.manifests.values()],
            "conflicts": c.conflicts,
            "scanWarnings": c.scan_warnings,
        })