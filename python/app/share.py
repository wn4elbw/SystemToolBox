"""信息共享总线：系统与所有插件的公开信息统一发布/读取，实现“信息均可共享”。

约定：
- 命名空间按发布者习惯取名，如 "system.info"、"system.summary"。
- 发布者(系统或插件)通过 PluginApi.bus.publish(...) 发布；
  任何插件与内置 API 都可以通过 rpc("system.share.get") 读取。
- 读取不限权限（信息是公开共享的），发布由各插件自身代码控制。
"""
import threading
import time


class SharedBus:
    def __init__(self):
        self._lock = threading.RLock()
        self._items = {}          # ns -> {"data":..., "owner": pid, "updated": ts}

    def publish(self, owner: str, ns: str, data) -> None:
        with self._lock:
            self._items[ns] = {"data": data, "owner": owner, "updated": time.time()}

    def get(self, ns: str):
        with self._lock:
            it = self._items.get(ns)
            if not it:
                return None
            return {"ns": ns, "owner": it["owner"], "updated": it["updated"], "data": it["data"]}

    def list(self):
        """按命名空间字典序返回所有共享项（不含 data 主体，只含元信息+摘要）。"""
        with self._lock:
            out = []
            for ns in sorted(self._items):
                it = self._items[ns]
                data = it["data"]
                out.append({
                    "ns": ns,
                    "owner": it["owner"],
                    "updated": it["updated"],
                    "type": type(data).__name__,
                })
            return out

    def remove(self, owner: str, ns: str) -> None:
        with self._lock:
            it = self._items.get(ns)
            if it and it["owner"] == owner:
                del self._items[ns]

    def clear_owner(self, owner: str) -> None:
        """清空某发布者的信息；owner='*' 清空全部。"""
        with self._lock:
            for ns in [k for k, v in self._items.items()
                       if owner == "*" or v["owner"] == owner]:
                del self._items[ns]
