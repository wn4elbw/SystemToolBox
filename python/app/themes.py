"""主题预置（固定配色，不提供自定义）：外壳与全部页面可切换的主题集合。

需求：提供主题切换，但**不提供更改主题配色**——配色只能在下列预置集中选择，
没有保存/编辑颜色接口。每个预置是完整的 CSS 变量调色板（与外壳 styles.css
的 :root 变量一一对应）；后端输出页面 HTML 时注入 :root 覆盖块，外壳与
各插件页面通过 var(--*) 自动跟随当前主题。
"""
import re
import threading

from .api.router import ApiError
from .log import info

_LOCK = threading.RLock()

THEME_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

DEFAULT_THEME = "dark"

# ---------------- 预置主题（唯一配色来源） ----------------

DARK = {
    "bg": "#202124", "bg2": "#2a2c31", "card": "#26282d", "border": "#000000",
    "text": "#ffffff", "dim": "#9aa0ab", "accent": "#4f8cff",
    "green": "#43d17e", "amber": "#f0b34b", "red": "#ef5a5a",
    "hover1": "#2c2f34", "btn": "#32353c", "btn-hover": "#3a3e46",
    "btn2": "#2a2d33", "btn2-hover": "#33373e", "item-hover": "#2b2e34",
    "code-bg": "#15161a", "item-bg": "#23252a", "log-text": "#d4d7de",
    "pill-bg": "rgba(0,0,0,.78)", "pill-text": "#ffffff",
}

LIGHT = {
    "bg": "#f4f5f7", "bg2": "#ffffff", "card": "#ffffff", "border": "#d6d9de",
    "text": "#1f2329", "dim": "#6b7280", "accent": "#2563eb",
    "green": "#15803d", "amber": "#b45309", "red": "#dc2626",
    "hover1": "#e8eaee", "btn": "#e2e5ea", "btn-hover": "#d3d7de",
    "btn2": "#e9ebef", "btn2-hover": "#dde0e6", "item-hover": "#e9ebf0",
    "code-bg": "#fafbfc", "item-bg": "#f8f9fb", "log-text": "#374151",
    "pill-bg": "rgba(0,0,0,.08)", "pill-text": "#1f2329",
}

NAVY = {
    "bg": "#10141f", "bg2": "#1a2030", "card": "#171d2b", "border": "#000000",
    "text": "#e8ebf2", "dim": "#8b94a7", "accent": "#5b8cff",
    "green": "#46d489", "amber": "#f0b34b", "red": "#ef5a6a",
    "hover1": "#222a3c", "btn": "#262f45", "btn-hover": "#2e3a55",
    "btn2": "#1e2636", "btn2-hover": "#273041", "item-hover": "#202838",
    "code-bg": "#0c1017", "item-bg": "#131927", "log-text": "#c7cedd",
    "pill-bg": "rgba(0,0,0,.6)", "pill-text": "#e8ebf2",
}

OLED = {
    "bg": "#000000", "bg2": "#0a0a0a", "card": "#0d0d0d", "border": "#1c1c1c",
    "text": "#f2f2f2", "dim": "#9a9a9a", "accent": "#4f8cff",
    "green": "#43d17e", "amber": "#f0b34b", "red": "#ef5a5a",
    "hover1": "#141414", "btn": "#1e1e1e", "btn-hover": "#2a2a2a",
    "btn2": "#121212", "btn2-hover": "#1c1c1c", "item-hover": "#161616",
    "code-bg": "#070707", "item-bg": "#0a0a0a", "log-text": "#d9d9d9",
    "pill-bg": "rgba(255,255,255,.06)", "pill-text": "#f2f2f2",
}

# ---- Claude 配色（Anthropic：奶油底 #faf9f5 / 陶土橙 #d97757） ----

CLAUDE_DARK = {
    "bg": "#1f1e1b", "bg2": "#262521", "card": "#2b2a25", "border": "#3a3833",
    "text": "#f0eee6", "dim": "#a29d92", "accent": "#d97757",
    "green": "#7fb069", "amber": "#d9a441", "red": "#e06c5f",
    "hover1": "#33312b", "btn": "#3a3830", "btn-hover": "#45423a",
    "btn2": "#302e28", "btn2-hover": "#3a3830", "item-hover": "#302e28",
    "code-bg": "#181713", "item-bg": "#262521", "log-text": "#ddd9cf",
    "pill-bg": "rgba(0,0,0,.55)", "pill-text": "#f0eee6",
}

CLAUDE_LIGHT = {
    "bg": "#faf9f5", "bg2": "#f0eee6", "card": "#ffffff", "border": "#e3dfd3",
    "text": "#1f1e1b", "dim": "#767065", "accent": "#c96442",
    "green": "#3f7d3f", "amber": "#9a6a1a", "red": "#bf4b3f",
    "hover1": "#efece2", "btn": "#e8e4d9", "btn-hover": "#ded9cb",
    "btn2": "#f0eee6", "btn2-hover": "#e6e2d6", "item-hover": "#efece2",
    "code-bg": "#f6f4ee", "item-bg": "#f7f5ef", "log-text": "#3b382f",
    "pill-bg": "rgba(0,0,0,.06)", "pill-text": "#1f1e1b",
}

# ---- GPT 配色（OpenAI：深绿 #10a37f / 墨绿底 #0f1a17） ----

GPT_DARK = {
    "bg": "#0f1a17", "bg2": "#14211d", "card": "#182722", "border": "#22352e",
    "text": "#e8f2ee", "dim": "#8aa79d", "accent": "#10a37f",
    "green": "#3ddc97", "amber": "#e0b341", "red": "#ef5a5a",
    "hover1": "#1d2f29", "btn": "#233830", "btn-hover": "#2b443a",
    "btn2": "#1a2b25", "btn2-hover": "#233830", "item-hover": "#1d2f29",
    "code-bg": "#0a1310", "item-bg": "#14211d", "log-text": "#cfe3dc",
    "pill-bg": "rgba(0,0,0,.55)", "pill-text": "#e8f2ee",
}

GPT_LIGHT = {
    "bg": "#f7faf9", "bg2": "#eef4f1", "card": "#ffffff", "border": "#d5e3dd",
    "text": "#14211d", "dim": "#5f7a72", "accent": "#0d8a6a",
    "green": "#0f7a4f", "amber": "#9a6a1a", "red": "#c0392b",
    "hover1": "#e8f1ed", "btn": "#dfeae5", "btn-hover": "#d1e0d9",
    "btn2": "#eef4f1", "btn2-hover": "#e2ece7", "item-hover": "#e8f1ed",
    "code-bg": "#f4f9f7", "item-bg": "#f2f8f5", "log-text": "#28453c",
    "pill-bg": "rgba(0,0,0,.06)", "pill-text": "#14211d",
}

PRESETS = {
    "dark": {"name": "深色（默认）", "colors": DARK},
    "light": {"name": "浅色", "colors": LIGHT},
    "navy": {"name": "深蓝", "colors": NAVY},
    "oled": {"name": "纯黑 OLED", "colors": OLED},
    "claude-dark": {"name": "Claude 深色", "colors": CLAUDE_DARK},
    "claude-light": {"name": "Claude 浅色", "colors": CLAUDE_LIGHT},
    "gpt-dark": {"name": "GPT 深绿", "colors": GPT_DARK},
    "gpt-light": {"name": "GPT 浅绿", "colors": GPT_LIGHT},
}


def list_presets():
    """返回 [{id, name, accent, bg, preview}] —— 只读预置列表，无自定义项。

    accent/bg 供外壳顶栏渲染主题色点预览；preview 是两色渐变（accent→bg）。
    """
    out = []
    for k, v in PRESETS.items():
        c = v["colors"]
        out.append({
            "id": k, "name": v["name"],
            "accent": c["accent"], "bg": c["bg"], "card": c["card"],
            "preview": f'linear-gradient(135deg,{c["accent"]} 0 52%,{c["bg"]} 52% 100%)',
        })
    return out


def get_theme(theme_id: str):
    """返回 {id, name, colors} 或 None（未知主题）。"""
    p = PRESETS.get(theme_id)
    if p is None:
        return None
    return {"id": theme_id, "name": p["name"], "colors": dict(p["colors"])}


def current_id(store) -> str:
    """当前生效的主题 id（持久化于宿主设置，未知则回落默认）。"""
    tid = store.get_host_setting("theme", DEFAULT_THEME)
    return tid if tid in PRESETS else DEFAULT_THEME


def set_current(store, theme_id: str):
    """切换并持久化当前主题。非法 id 抛 ApiError。"""
    with _LOCK:
        if not theme_id or not THEME_ID_RE.match(theme_id) \
                or theme_id not in PRESETS:
            raise ApiError("bad_args", f"未知主题: {theme_id!r}")
        store.set_host_setting("theme", theme_id)
        info(f"主题切换: {theme_id} ({PRESETS[theme_id]['name']})")
        return get_theme(theme_id)


def css_block(theme_id: str = DEFAULT_THEME) -> str:
    """生成 :root{...} CSS 覆盖块（注入到页面 </head> 前，晚于页面自身样式）。"""
    p = PRESETS.get(theme_id) or PRESETS[DEFAULT_THEME]
    body = ";".join(f"--{k}:{v}" for k, v in p["colors"].items())
    return f":root{{{body}}}"