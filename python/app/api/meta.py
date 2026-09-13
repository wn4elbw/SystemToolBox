"""元信息接口：健康检查 / 启动布局（侧栏、顶栏、冲突）。"""
from .. import config, themes
from ..plugin.scanner import layout_to_dict
from .router import api_ok


def register(ctx, router):
    @router.route("GET", "/api/health")
    def _health(c, m, body):
        return api_ok({
            "ok": True,
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "native": {
                "available": c.bridge.available,
                "path": c.bridge.path,
                "version": c.bridge.version(),
            },
        })

    @router.route("GET", "/api/meta")
    def _meta(c, m, body):
        layout = layout_to_dict(c.sidebars, c.topbars, c.conflicts,
                                c.manifests)
        layout["app"] = {
            "name": config.APP_NAME,
            "version": config.APP_VERSION,
        }
        layout["scanWarnings"] = c.scan_warnings
        layout["native"] = {
            "available": c.bridge.available,
            "path": c.bridge.path,
            "version": c.bridge.version(),
        }
        layout["shortcuts"] = c.shortcuts.list_all(c._perm_lookup)
        layout["pluginCount"] = {
            "total": len(c.manifests),
            "ok": sum(1 for m in c.manifests.values() if m.status == "ok"),
            "disabled": sum(1 for m in c.manifests.values()
                            if m.status == "disabled"),
        }
        layout["theme"] = {
            "current": themes.current_id(c.store),
            "list": themes.list_presets(),
        }
        return api_ok(layout)