"""插件状态与授权持久化（data/state.json、data/grants.json）。

- state.json: 插件级覆盖 {pluginId: {"enabled": bool, "permission": str}}
- grants.json: API 级授权 {pluginId: {apiName: "allowed"|"denied"}}
  （"once" 授权的会话级记录保存在内存，不落盘，重启后作废。）
"""
import json
import os
import threading
import time
from pathlib import Path

from .. import config
from ..log import error, info


class Store:
    def __init__(self, state_file: Path = None, grants_file: Path = None):
        self._state_file = state_file or config.STATE_FILE
        self._grants_file = grants_file or config.GRANTS_FILE
        self._lock = threading.RLock()   # 可重入：持锁内保存文件
        self._state: dict = {}
        self._grants: dict = {}
        # 会话级授权（"允许一次"），内存态
        self._session_grants: dict = {}
        self.load()

    # ---------- 读写 ----------

    def load(self):
        with self._lock:
            self._state = self._read_json(self._state_file)
            self._grants = self._read_json(self._grants_file)
        info(f"状态已加载: {len(self._state)} 个插件覆盖, "
             f"{sum(len(v) for v in self._grants.values())} 条授权")

    @staticmethod
    def _read_json(p: Path) -> dict:
        try:
            if p.is_file():
                # utf-8-sig：兼容被外部工具（记事本/PowerShell 等）写成带 BOM 的
                # state.json，否则 json.loads 会因 BOM 抛 JSONDecodeError 导致
                # 全部设置回退默认（表现为标题/主题等改动重启后丢失）。
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def save_state(self):
        with self._lock:
            payload = json.dumps(self._state, ensure_ascii=False, indent=2) + "\n"
        self._commit(self._state_file, payload)

    def save_grants(self):
        with self._lock:
            payload = json.dumps(self._grants, ensure_ascii=False, indent=2) + "\n"
        self._commit(self._grants_file, payload)

    def _commit(self, p: Path, payload: str):
        """锁外落盘：临时文件 + 原子替换 + 快速失败重试。

        本机曾观察到 state.json 被搜索索引/杀软/同步服务短暂独占（秒级~数十秒），
        直接 open("w") 会长时间阻塞 HTTP 请求线程；改为写唯一临时文件后
        os.replace 原子替换——目标被占用时立即失败（不阻塞），短暂重试后放弃，
        数据完整性不受影响（旧文件保留），下一次保存再补写。
        """
        last = None
        for attempt in range(6):
            tmp = Path(str(p) + f".{os.getpid()}.tmp")
            try:
                tmp.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                os.replace(tmp, p)
                return
            except OSError as e:
                last = e
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                time.sleep(0.15 * (attempt + 1))
        error(f"落盘失败 {p.name}: {last!r}（稍后保存会重试）")

    # ---------- 插件级覆盖 ----------

    def plugin_state(self, plugin_id: str) -> dict:
        with self._lock:
            return dict(self._state.get(plugin_id) or {})

    def set_plugin_enabled(self, plugin_id: str, enabled: bool):
        with self._lock:
            st = self._state.setdefault(plugin_id, {})
            st["enabled"] = bool(enabled)
        self.save_state()

    def set_plugin_permission(self, plugin_id: str, permission: str):
        if permission not in config.PERM_LEVELS:
            raise ValueError(f"非法权限级别: {permission}")
        with self._lock:
            st = self._state.setdefault(plugin_id, {})
            st["permission"] = permission
        self.save_state()

    # ---------- API 授权 ----------

    def get_api_grant(self, plugin_id: str, api_name: str):
        """返回 allowed / denied / None。"""
        with self._lock:
            g = self._grants.get(plugin_id, {}).get(api_name)
            if g:
                return g
            s = self._session_grants.get(plugin_id, {}).get(api_name)
            return s

    def set_api_grant(self, plugin_id: str, api_name: str, grant: str,
                      persistent: bool):
        if grant not in ("allowed", "denied"):
            raise ValueError(f"非法授权: {grant}")
        with self._lock:
            if persistent:
                self._grants.setdefault(plugin_id, {})[api_name] = grant
                self.save_grants()
            else:
                # 会话级（允许一次）
                self._session_grants.setdefault(plugin_id, {})[api_name] = grant

    def reset_api_grant(self, plugin_id: str, api_name: str):
        with self._lock:
            self._grants.get(plugin_id, {}).pop(api_name, None)
            self._session_grants.get(plugin_id, {}).pop(api_name, None)
            self.save_grants()

    def api_grants_view(self, plugin_id: str) -> dict:
        with self._lock:
            persistent = self._grants.get(plugin_id, {})
            session = self._session_grants.get(plugin_id, {})
            merged = {}
            for k, v in persistent.items():
                merged[k] = {"grant": v, "persistent": True}
            for k, v in session.items():
                merged[k] = {"grant": v, "persistent": False}
            return merged

    def reset_all_api_grants(self) -> int:
        """清除全部插件授权（持久+会话），返回清除条数；之后插件需重新审批。"""
        with self._lock:
            n = sum(len(v) for v in self._grants.values()) \
                + sum(len(v) for v in self._session_grants.values())
            self._grants = {}
            self._session_grants = {}
            self.save_grants()
            return n

    # ---------- 宿主级设置（存储在 state.json 的保留键 __settings__ 下） ----------

    _HOST_KEY = "__settings__"

    def get_host_setting(self, key: str, default=None):
        with self._lock:
            return self._state.get(self._HOST_KEY, {}).get(key, default)

    def set_host_setting(self, key: str, value):
        with self._lock:
            self._state.setdefault(self._HOST_KEY, {})[key] = value
            self.save_state()

    def host_settings_view(self) -> dict:
        with self._lock:
            return dict(self._state.get(self._HOST_KEY) or {})