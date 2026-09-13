"""插件树状扫描与启动布局（侧栏 / 顶栏规则）。

搜索规则（规格原文）：
  启动时排查插件；插件在插件文件夹树状搜索，
  以搜索到插件配置文件为准，不再继续搜索。
实现：深度优先遍历 plugins/；某目录存在 plugin.json 即视为一个插件，
      注册后**不再下钻该目录的子目录**（插件自持子树），继续遍历兄弟分支。

布局规则：
  - sidebar：已存在该侧栏 => 插件加入该侧栏；不存在 => 新建侧栏（可共享）。
  - topbar ：已存在该顶栏 => 冲突报错（该插件置为 conflict）；不存在 => 新建（全局唯一）。
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..log import info, warn
from .model import PluginManifest, parse_manifest


@dataclass
class SidebarEntry:
    id: str
    title: str
    plugins: list = field(default_factory=list)   # PluginManifest 列表（发现顺序）


@dataclass
class ScanResult:
    manifests: dict = field(default_factory=dict)     # id -> PluginManifest
    scan_warnings: list = field(default_factory=list) # 重复 id / 解析失败等
    sidebars: dict = field(default_factory=dict)      # sidebar id -> SidebarEntry
    topbars: dict = field(default_factory=dict)       # topbar id -> PluginManifest
    conflicts: list = field(default_factory=list)     # {pluginId, topbar, existingPluginId}


def _walk(root: Path, out: list, warnings: list):
    """深度优先树状扫描；找到插件配置即注册该目录，不下钻其子目录。"""
    entries = sorted(os.scandir(root), key=lambda e: e.name)
    for e in entries:
        if not e.is_dir(follow_symlinks=False):
            continue
        d = Path(e.path)
        cfg = d / config.PLUGIN_CONFIG_NAME
        if cfg.is_file():
            out.append(d)
            # 关键：找到配置即停止继续向该目录下方搜索
            continue
        try:
            _walk(d, out, warnings)
        except OSError as ex:
            warnings.append(f"扫描 {d} 失败: {ex}")


def scan_plugins(plugins_root: Path = None):
    """扫描插件文件夹，返回 {id: PluginManifest} 与扫描告警。"""
    plugins_root = plugins_root or config.PLUGINS_DIR
    found_dirs: list = []
    warnings: list = []
    if plugins_root.is_dir():
        _walk(plugins_root, found_dirs, warnings)

    manifests: dict = {}
    for d in found_dirs:
        m, err = parse_manifest(d / config.PLUGIN_CONFIG_NAME)
        if err:
            warnings.append(f"[{d}] {err}")
            continue
        if m.id in manifests:
            warnings.append(
                f"插件 id 重复: {m.id}（{manifests[m.id].dir} 已注册，"
                f"忽略 {d}）"
            )
            continue
        manifests[m.id] = m
        info(f"发现插件 {m.id} v{m.version} @ {d}")

    return manifests, warnings


def _iterate_ordered(manifests: dict, priority_ids):
    """迭代顺序：先处理既有的顶栏占用者（保持其占用权），再按发现顺序处理其余。

    规则语义（规格）：「顶栏如已有 -> 弹出报错」——后注册者才应当报错。
    - 启动（无既有占用者）：纯发现顺序，先发现者占用顶栏，后到者报错。
    - 运行时启用/扫描：既有占用者优先保留，后启用的插件若顶栏被占 -> 冲突。
    """
    prio = set(priority_ids or [])
    ordered = [manifests[pid] for pid in priority_ids if pid in manifests]
    ordered += [m for m in manifests.values() if m.id not in prio]
    return ordered


def build_layout(manifests: dict, priority_ids=None):
    """按启动规则组装侧栏/顶栏，产出版本冲突列表。

    只对「启用」的插件做布局（停用插件不占位置，不参与冲突检查）。
    - sidebar 冲突不报错：同侧栏追加。
    - topbar 冲突报错：后注册者 status='conflict'。
    返回 (sidebars, topbars, conflicts)。
    """
    sidebars: dict = {}
    topbars: dict = {}
    conflicts: list = []

    for m in _iterate_ordered(manifests, priority_ids):
        if not m.enabled:
            m.status = "disabled"
            continue
        m.status = "ok"

        if m.sidebar in sidebars:
            sidebars[m.sidebar].plugins.append(m)
        else:
            sidebars[m.sidebar] = SidebarEntry(id=m.sidebar, title=m.sidebar,
                                               plugins=[m])

        if m.topbar:
            if m.topbar in topbars:
                conflicts.append({
                    "pluginId": m.id,
                    "pluginName": m.name,
                    "topbar": m.topbar,
                    "existingPluginId": topbars[m.topbar].id,
                })
                m.status = "conflict"
                warn(f"顶栏冲突: 插件 {m.id} 的顶栏 [{m.topbar}] 已被 "
                     f"{topbars[m.topbar].id} 占用")
            else:
                topbars[m.topbar] = m

    for c in conflicts:
        info(f"启动报错: {c['pluginName']} 顶栏冲突 -> {c['existingPluginId']}")

    return sidebars, topbars, conflicts


def layout_to_dict(sidebars: dict, topbars: dict,
                   conflicts: list, manifests: dict) -> dict:
    """序列化布局供 /api/meta 与宿主界面使用。"""
    return {
        "sidebars": [
            {
                "id": s.id,
                "title": s.title,
                "plugins": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "icon": p.icon,
                        "topbar": p.topbar,
                        "status": p.status,
                        "permission": p.effective_permission,
                    }
                    for p in s.plugins
                ],
            }
            for s in sidebars.values()
        ],
        "topbars": [{"id": t, "pluginId": p.id} for t, p in topbars.items()],
        "conflicts": conflicts,
        "plugins": [_manifest_view(m) for m in manifests.values()],
    }


def _manifest_view(m: PluginManifest) -> dict:
    from .model import manifest_to_dict
    return manifest_to_dict(m)