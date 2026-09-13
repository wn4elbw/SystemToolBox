"""插件清单模型：plugin.json 的解析与校验。"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..log import warn


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    description: str = ""
    entry: str = "main.py"
    permission: str = config.PERM_READONLY
    sidebar: str = "默认"
    topbar: str = ""
    icon: str = ""
    enabled: bool = True
    requires: list = field(default_factory=list)   # 前置插件：缺失或未启用 -> 本插件禁用

    # 运行时附加
    dir: Path = None
    config_path: Path = None
    status: str = "ok"          # ok | conflict | disabled | error
    load_error: str = ""        # 禁用/失败原因（如“缺少前置插件 xxx”）
    loaded: bool = False        # 是否真正加载（模块导入成功）

    @property
    def web_dir(self) -> Path:
        return self.dir / "web"

    @property
    def entry_path(self) -> Path:
        return self.dir / self.entry

    @property
    def effective_permission(self) -> str:
        return self.permission


def parse_manifest(config_path: Path):
    """解析 plugin.json；失败返回 (None, 错误信息)。"""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"配置解析失败: {e}"

    if not isinstance(raw, dict):
        return None, "配置必须是 JSON 对象"

    pid = str(raw.get("id", "")).strip()
    if not pid:
        return None, "缺少必填字段 id"
    # id 只允许安全字符（同时用作模块名与 URL 前缀）
    if not all(c.isalnum() or c in "-_" for c in pid):
        return None, f"id 含非法字符: {pid}"

    name = str(raw.get("name") or pid)
    version = str(raw.get("version") or "0.0.0")
    entry = str(raw.get("entry") or "main.py")

    permission = str(raw.get("permission") or config.PERM_READONLY)
    if permission not in config.PERM_LEVELS:
        warn(f"插件 {pid} 权限级别非法({permission})，回退为 readonly")
        permission = config.PERM_READONLY

    m = PluginManifest(
        id=pid,
        name=name,
        version=version,
        description=str(raw.get("description") or ""),
        entry=entry,
        permission=permission,
        sidebar=str(raw.get("sidebar") or "默认"),
        topbar=str(raw.get("topbar") or ""),
        icon=str(raw.get("icon") or ""),
        enabled=bool(raw.get("enabled", True)),
        requires=[str(x) for x in (raw.get("requires") or raw.get("depends") or [])],
        dir=config_path.parent,
        config_path=config_path,
    )
    return m, None


def manifest_to_dict(m: PluginManifest) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "version": m.version,
        "description": m.description,
        "permission": m.permission,
        "sidebar": m.sidebar,
        "topbar": m.topbar,
        "icon": m.icon,
        "enabled": m.enabled,
        "requires": list(m.requires),
        "status": m.status,
        "loadError": m.load_error or None,
        "dir": str(m.dir) if m.dir else None,
        "url": f"/plugin/{m.id}/" if m.status in ("ok", "disabled") else None,
    }