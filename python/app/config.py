"""路径与全局配置。"""
from pathlib import Path

# python/app/config.py -> python/app -> python -> 仓库根
ROOT = Path(__file__).resolve().parents[2]

PYTHON_DIR = ROOT / "python"
APP_DIR = PYTHON_DIR / "app"
DATA_DIR = PYTHON_DIR / "data"
NATIVE_DIR = PYTHON_DIR / "native"
WEB_DIR = PYTHON_DIR / "web"

# 插件目录（树状搜索根）
PLUGINS_DIR = ROOT / "plugins"

# 插件配置文件（扫描依据：找到即视为插件，不再下钻其子目录）
PLUGIN_CONFIG_NAME = "plugin.json"

# 状态持久化
STATE_FILE = DATA_DIR / "state.json"      # 插件启用/权限覆盖
GRANTS_FILE = DATA_DIR / "grants.json"    # API 级授权
LOG_FILE = DATA_DIR / "backend.log"

# 内置系统 API 的虚拟插件 id
BUILTIN_PLUGIN_ID = "system"

APP_NAME = "SystemToolBox"
APP_VERSION = "0.1.0"

# 权限级别
PERM_READONLY = "readonly"
PERM_APPROVAL = "approval"
PERM_FULL = "full"
PERM_LEVELS = (PERM_READONLY, PERM_APPROVAL, PERM_FULL)

# API 权限要求
API_READONLY = "readonly"
API_ADMIN = "admin"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 0  # 0 = 随机可用端口

# 访问令牌（安全）：后端 HTTP 服务要求每个请求携带该令牌
# （X-STB-Token 请求头、URL ?token=，或首次通过校验后自动种下的
# stb_token Cookie），否则 403 —— 浏览器直接打开
# http://127.0.0.1:<port> 无法浏览页面/调用接口。
# 令牌由宿主(Electron)生成并经 STB_READY 传递；独立运行后端时自动生成。
TOKEN_HEADER = "X-STB-Token"
TOKEN_QUERY = "token"
TOKEN_COOKIE = "stb_token"

# 静态资源 MIME 表
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}
