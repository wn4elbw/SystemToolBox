"""快捷键注册表：插件通过加载(PluginApi.shortcut)申请全局快捷键。

新语义（用户可配置，持久化于 state.json 的 __settings__.shortcuts）：
- **默认禁用**：所有插件申请的快捷键默认不生效；用户需在「快捷键管理」中手动开启。
- 每项可配置：{"<pid>:<sid>": {"enabled": true, "acc": "Ctrl+Alt+K"}}
  - enabled 缺省 False（默认禁用）；acc 缺省即插件申请时的默认键位。
- 兼容旧数据：早期 grants.json 中 shortcut:<id> = allowed 视为已启用（惰性读取）。
- 已生效的快捷键由宿主(Electron 主进程)经 /api/shortcuts 拉取后注册到系统全局，
  每 3 秒轮询同步，因此改键/开关在数秒内自动生效。
"""

import re
import threading

# 持久化键名（state.json __settings__ 下）
_HOST_KEY = "shortcuts"
# Electron accelerator 粗略校验：仅字母/数字/加号（组合键 "Ctrl+Alt+T"、"F8"…）
_ACC_RE = re.compile(r"^[A-Za-z0-9+]{1,64}$")


class ShortcutRegistry:
    def __init__(self, store):
        self._store = store
        self._lock = threading.RLock()
        self._items = {}          # pid -> {id: {"accelerator","description","callback","name"}}

    def clear(self):
        with self._lock:
            self._items.clear()

    def register(self, plugin_id: str, plugin_name: str, shortcuts: dict):
        """shortcuts: {id: {"accelerator","description","handler"}}"""
        with self._lock:
            self._items[plugin_id] = {
                sid: {
                    "id": sid,
                    "accelerator": s.get("accelerator", ""),
                    "description": s.get("description", ""),
                    "handler": s.get("handler"),
                    "plugin": plugin_id,
                    "pluginName": plugin_name,
                }
                for sid, s in shortcuts.items()
            }

    def unregister(self, plugin_id: str):
        with self._lock:
            self._items.pop(plugin_id, None)

    # ---------------- 持久化配置 ----------------

    def _conf(self, plugin_id: str, sid: str) -> dict:
        table = self._store.get_host_setting(_HOST_KEY, {}) or {}
        return dict(table.get(f"{plugin_id}:{sid}") or {})

    def _write_conf(self, plugin_id: str, sid: str, conf: dict):
        key = f"{plugin_id}:{sid}"
        table = dict(self._store.get_host_setting(_HOST_KEY, {}) or {})
        if conf:
            table[key] = conf
        else:
            table.pop(key, None)
        self._store.set_host_setting(_HOST_KEY, table)

    def _legacy_allowed(self, plugin_id: str, sid: str) -> bool:
        """旧 grants.json 里 shortcut:<id>=allowed 视为已启用（兼容迁移）。"""
        return self._store.get_api_grant(plugin_id, f"shortcut:{sid}") == "allowed"

    def enabled(self, plugin_id: str, sid: str) -> bool:
        """该快捷键是否被用户启用（默认禁用）。"""
        conf = self._conf(plugin_id, sid)
        if "enabled" in conf:
            return bool(conf["enabled"])
        return self._legacy_allowed(plugin_id, sid)

    def set_enabled(self, plugin_id: str, sid: str, on: bool):
        conf = self._conf(plugin_id, sid)
        conf["enabled"] = bool(on)
        self._write_conf(plugin_id, sid, conf)

    def accelerator(self, plugin_id: str, sid: str) -> str:
        """当前生效键位（用户改过的优先，否则插件默认）。"""
        conf = self._conf(plugin_id, sid)
        acc = conf.get("acc")
        if acc:
            return acc
        it = self.get(plugin_id, sid)
        return it["accelerator"] if it else ""

    def set_accelerator(self, plugin_id: str, sid: str, acc: str):
        acc = (acc or "").strip()
        if not _ACC_RE.match(acc):
            raise ValueError(
                f"非法快捷键格式: {acc!r}（应为 Ctrl+Alt+T / F8 之类）")
        conf = self._conf(plugin_id, sid)
        conf["acc"] = acc
        self._write_conf(plugin_id, sid, conf)

    def reset(self, plugin_id: str, sid: str):
        """单条恢复默认：清除配置（回默认键位 + 默认禁用）。"""
        self._write_conf(plugin_id, sid, {})

    def reset_all(self) -> int:
        """全部恢复默认：清除所有快捷键配置，返回清掉的条数。"""
        table = self._store.get_host_setting(_HOST_KEY, {}) or {}
        n = len(table)
        if table:
            self._store.set_host_setting(_HOST_KEY, {})
        return n

    # ---------------- 判定 ----------------

    def effective(self, plugin_id: str, sid: str, perm: str) -> bool:
        """结合权限级别与用户开关，判断该快捷键当前是否生效。"""
        if perm == "none":
            return False
        return self.enabled(plugin_id, sid)

    def set_grant(self, plugin_id: str, sid: str, action: str) -> str:
        """兼容旧接口：allow/deny 映射为开关，reset 恢复默认。"""
        if action == "reset":
            self.reset(plugin_id, sid)
            return "已恢复默认"
        self.set_enabled(plugin_id, sid, action in ("allow", "enable"))
        return "已启用" if action in ("allow", "enable") else "已禁用"

    # ---------------- 列表 ----------------

    def list_all(self, lookup_perm=None) -> list:
        """全量清单（含未启用的），供快捷键管理界面展示。"""
        with self._lock:
            out = []
            for pid, items in self._items.items():
                perm = lookup_perm(pid) if lookup_perm else "full"
                for it in items.values():
                    if not it["accelerator"]:
                        continue
                    out.append({
                        "pluginId": pid,
                        "pluginName": it["pluginName"],
                        "id": it["id"],
                        "accelerator": self.accelerator(pid, it["id"]),
                        "defaultAccelerator": it["accelerator"],
                        "description": it["description"],
                        "enabled": self.enabled(pid, it["id"]),
                        "effective": self.effective(pid, it["id"], perm),
                    })
            return out

    def list_active(self, lookup_perm) -> list:
        """返回当前生效的快捷键（供 Electron 注册）。lookup_perm(pid)->str。"""
        with self._lock:
            out = []
            for pid, items in self._items.items():
                perm = lookup_perm(pid)
                if perm == "none":
                    continue
                for it in items.values():
                    if not it["accelerator"]:
                        continue
                    if self.effective(pid, it["id"], perm):
                        out.append({
                            "pluginId": pid,
                            "pluginName": it["pluginName"],
                            "id": it["id"],
                            "accelerator": self.accelerator(pid, it["id"]),
                            "description": it["description"],
                        })
            return out

    def get(self, plugin_id: str, sid: str):
        """取快捷键定义（用于触发）。"""
        with self._lock:
            items = self._items.get(plugin_id)
            if not items:
                return None
            return items.get(sid)

    def trigger(self, plugin_id: str, sid: str, ctx, event_data: dict) -> dict:
        """由 Electron 触发后的后端回调：执行插件申明的 handler。"""
        it = self.get(plugin_id, sid)
        if not it or not it.get("handler"):
            return {"ok": False, "error": "shortcut_not_found"}
        try:
            from .api.router import ApiCallCtx
            call_ctx = ApiCallCtx(ctx.bridge, plugin_id, "__shortcut__", None)
            result = it["handler"]({"type": "shortcut", "pluginId": plugin_id, "id": sid}, call_ctx)
        except Exception as e:
            ctx.events.push({"type": "shortcut-error", "pluginId": plugin_id, "id": sid,
                             "error": str(e)})
            return {"ok": False, "error": str(e)}
        ctx.events.push({"type": "shortcut", "pluginId": plugin_id, "id": sid, "data": result})
        return {"ok": True}
