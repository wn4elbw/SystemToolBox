"""HTTP 服务：/api/* 路由 + /ui/* 内置页面 + /plugin/<id>/* 插件页面。

安全：除 OPTIONS（CORS 预检）外，所有请求都必须携带访问令牌
（X-STB-Token 请求头、URL ?token=，或首次通过校验后种下的
stb_token Cookie），否则返回 403。令牌由宿主生成，浏览器直接访问
127.0.0.1:<port> 将无法浏览页面或调用接口。

页面经 /ui/* 或 /plugin/* 输出时，会把 ?token= 注入 <script src=
"/ui/stbox.js"> 标签——外壳以 file:// 页面的 iframe 加载页面，iframe
相对顶层是跨站上下文，SameSite Cookie 不会随子资源请求发送，必须让
stbox.js 自身携带令牌才能加载（否则页面报 “stbox is not defined”）。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config, themes
from .log import debug, error, info


class BackendServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, ctx, token: str = ""):
        self.ctx = ctx
        self.token = token or ""
        super().__init__(addr, BackendHandler)


def _mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return config.MIME.get(ext, "application/octet-stream")


class BackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SystemToolBox/0.1"

    # ---------------- 基础 ---------------- 

    def log_message(self, fmt, *args):  # 静默访问日志，用 debug 级别
        debug("http %s" % (fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, " + config.TOKEN_HEADER)

    def _send(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self._cors()
        self._set_auth_cookie()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------------- 访问令牌校验 ----------------

    def _set_auth_cookie(self):
        """首次以 头/查询参数 通过校验的响应种下 Cookie，后续子资源请求自动携带。"""
        if getattr(self, "_want_cookie", False):
            token = getattr(self.server, "token", "") or ""
            if token:
                self.send_header(
                    "Set-Cookie",
                    f"{config.TOKEN_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax")

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == config.TOKEN_COOKIE:
                return v.strip()
        return ""

    def _token_ok(self) -> bool:
        """请求头 X-STB-Token / URL ?token= / stb_token Cookie 与服务器令牌一致才放行。"""
        token = getattr(self.server, "token", "") or ""
        if not token:
            return True          # 未启用令牌（兼容旧调用方）
        if self.headers.get(config.TOKEN_HEADER) == token:
            return True
        query = parse_qs(urlparse(self.path).query)
        if (query.get(config.TOKEN_QUERY) or [""])[0] == token:
            self._want_cookie = True     # 页面首载：回 Set-Cookie 供子资源使用
            return True
        return self._cookie_token() == token

    def _deny(self):
        self._send_json(403, {"ok": False, "error": {
            "code": "forbidden",
            "message": "未授权访问（缺少有效访问令牌）"}})

    # ---------------- 路由 ---------------- 

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        self._handle("GET", path, {})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        self._handle("POST", path, self._read_body())

    def _handle(self, method: str, path: str, body: dict):
        ctx = self.server.ctx
        self._want_cookie = False     # 每次请求重置（HTTP/1.1 连接可复用）
        try:
            if not self._token_ok():
                self._deny()
                return
            if path.startswith("/api/"):
                status, payload = ctx.router.handle(method, path, body)
                self._send_json(status, payload)
                return
            if path == "/" or path == "":
                self.send_response(302)
                self._set_auth_cookie()
                self.send_header("Location", "/ui/home")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/ui/"):
                self._serve_file(config.WEB_DIR, path[len("/ui/"):])
                return
            if path.startswith("/plugin/"):
                self._serve_plugin(path[len("/plugin/"):])
                return
            self._send_json(404, {"ok": False,
                                  "error": {"code": "not_found",
                                            "message": path}})
        except Exception as e:  # noqa: BLE001
            error(f"处理 {method} {path} 异常: {e!r}")
            self._send_json(500, {"ok": False,
                                  "error": {"code": "internal_error",
                                            "message": str(e)}})

    # ---------------- 静态文件 ---------------- 

    def _safe_resolve(self, root: Path, rel: str):
        """把相对路径安全解析到 root 内，越界返回 None。"""
        target = (root / rel).resolve()
        root_resolved = root.resolve()
        if root_resolved != target and root_resolved not in target.parents:
            return None
        return target

    def _serve_file(self, root: Path, rel: str):
        rel = rel.replace("\\", "/")
        if not rel or rel.endswith("/"):
            rel += "index.html"
        target = self._safe_resolve(root, rel)
        # 形如 /ui/home 的地址：先找同名文件，不存在则尝试 .html
        if target is None or not target.is_file():
            if "." not in rel.rsplit("/", 1)[-1]:
                alt = self._safe_resolve(root, rel + ".html")
                if alt is not None and alt.is_file():
                    target = alt
        if target is None or not target.is_file():
            self._send_json(404, {"ok": False,
                                  "error": {"code": "not_found",
                                            "message": rel}})
            return
        data = target.read_bytes()
        # 令牌注入：外壳以 file:// 页面的 iframe 加载本页，相对顶层是“跨站”，
        # SameSite Cookie 不会随子资源请求发送；因此把 ?token= 直接注入
        # <script src="/ui/stbox.js">，保证 stbox.js 在任何上下文都能带令牌加载。
        if getattr(self.server, "token", ""):
            data = self._inject_assets(data, _mime(str(target)))
        self._send(200, _mime(str(target)), data)

    _STBOX_TAG = '<script src="/ui/stbox.js"></script>'

    def _inject_assets(self, data: bytes, content_type: str) -> bytes:
        """注入访问令牌与当前主题到 HTML（页面/插件页统一生效）。"""
        if not content_type.startswith("text/html"):
            return data
        token = getattr(self.server, "token", "") or ""
        html = data.decode("utf-8", "replace")
        if token:
            tagged = f'<script src="/ui/stbox.js?token={token}"></script>'
            if self._STBOX_TAG in html:
                html = html.replace(self._STBOX_TAG, tagged)
        # 主题：把 :root{...} 覆盖块注入到 </head> 前（晚于页面自身 <style>，
        # 同名 CSS 变量按后定义覆盖，页面无需任何改动即随主题切换）
        tid = themes.current_id(self.server.ctx.store)
        style = (f'<style id="stb-theme" data-theme="{tid}">'
                 f'{themes.css_block(tid)}</style>')
        html = html.replace("</head>", style + "</head>", 1)
        return html.encode("utf-8")

    def _serve_plugin(self, rest: str):
        parts = rest.split("/", 1)
        pid = parts[0]
        m = self.server.ctx.manifests.get(pid)
        if m is None:
            self._send_json(404, {"ok": False,
                                  "error": {"code": "plugin_not_found",
                                            "message": pid}})
            return
        if m.status != "ok":
            self._send_json(403, {"ok": False,
                                  "error": {"code": "plugin_unavailable",
                                            "message": m.status}})
            return
        rel = parts[1] if len(parts) > 1 else ""
        self._serve_file(m.web_dir, rel)


def start_server(ctx, host: str = config.DEFAULT_HOST,
                 port: int = config.DEFAULT_PORT,
                 token: str = "") -> BackendServer:
    server = BackendServer((host, port), ctx, token=token)
    info(f"后端服务已启动: http://{host}:{server.server_address[1]}"
         + ("（访问令牌校验已启用）" if token else ""))
    return server