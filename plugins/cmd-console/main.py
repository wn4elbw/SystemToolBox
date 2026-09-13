"""CMD命令行插件 — 持久 cmd.exe 会话 + 轮询输出。

实现要点：
- 后端进程内维护一个常驻 cmd.exe（无窗口，管道输入输出），
  前端通过 write() 发送命令、read() 轮询增量输出，模拟交互式终端；
- 会话启动时执行 chcp65001 切换 UTF-8 代码页，输出按 UTF-8 解码；
- 会话意外退出（如输入 exit）后前端可一键重启；
- 全局快捷键 Ctrl+Alt+T：触发后页面聚焦输入框。

权限：start/write/kill 为 admin 级（approval 插件首次调用需审批）。
"""
import os
import subprocess
import threading
import time

from app.api.router import ApiError

CREATE_NO_WINDOW = 0x08000000
KEEP_LINES = 2500          # 缓冲超限时保留的最近行数
MAX_CHUNK_LINES = 4000


class TermSession:
    def __init__(self):
        self.proc = None
        self.buf = []
        self.lines = 0
        self.lock = threading.RLock()
        self.reader = None
        self.started_at = None

    # ---------------- 生命周期 ----------------

    @property
    def running(self):
        p = self.proc
        return p is not None and p.poll() is None

    def start(self):
        if self.running:
            return self
        env = dict(os.environ)
        env["PROMPT"] = "$P$G"
        env["PYTHONIOENCODING"] = "utf-8"
        # /Q 关闭回显；/K 保持会话且抑制启动横幅；管道环境下 chcp 无效，
        # 输出按 UTF-8->GBK 启发式解码（见 _decode）
        self.proc = subprocess.Popen(
            ["cmd.exe", "/Q", "/K"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, bufsize=0, env=env)
        with self.lock:
            self.buf = []
            self.lines = 0
            self.started_at = time.time()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        return self

    @staticmethod
    def _decode(data: bytes) -> str:
        """启发式解码：优先 UTF-8；失败按系统 ANSI 代码页(GBK)解码。"""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("gbk", "replace")

    def _read_loop(self):
        fd = self.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 8192)
            except OSError:
                break
            if not data:
                break
            text = self._decode(data)
            with self.lock:
                self.buf.append(text)
                self.lines += text.count("\n") + 1
                if self.lines > MAX_CHUNK_LINES:
                    self._trim()

    def _trim(self):
        blob = "".join(self.buf)
        lines = blob.splitlines(keepends=True)
        if len(lines) > KEEP_LINES:
            blob = "".join(lines[-KEEP_LINES:])
        self.buf = [blob]
        self.lines = blob.count("\n") + 1

    def kill(self):
        p = self.proc
        self.proc = None
        if p is None:
            return True
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        return True

    # ---------------- 交互 ----------------

    def write(self, line):
        p = self.proc
        if p is None or p.poll() is not None:
            raise ApiError("not_running", "命令会话未运行，请先启动/重启")
        # 不在此回显：cmd 会在每次命令前自行输出提示符（如 I:\>），
        # 输入行由前端在本地回显，避免出现双重提示符
        raw = (line + "\r\n").encode("utf-8", "replace")
        try:
            p.stdin.write(raw)
            p.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise ApiError("write_failed", f"写入失败(会话可能已退出): {e}")

    def drain(self):
        p = self.proc
        with self.lock:
            out = "".join(self.buf)
            self.buf = []
            self.lines = 0
        running = p is not None and p.poll() is None
        code = None if running else (p.poll() if p is not None else -1)
        return {"output": out, "running": running, "exitCode": code,
                "startedAt": self.started_at}


_session = TermSession()
_session_lock = threading.RLock()


def _status():
    with _session_lock:
        p = _session.proc
        running = _session.running
        return {
            "running": running,
            "startedAt": _session.started_at,
            "exitCode": None if running else (p.poll() if p is not None else None),
            "pid": p.pid if p is not None and running else None,
        }


def register(api):
    @api.handler("status", permission="readonly",
                 description="命令会话运行状态")
    def status(args, ctx):
        return _status()

    @api.handler("start", permission="admin",
                 description="启动命令会话（需审批）")
    def start(args, ctx):
        with _session_lock:
            _session.start()
        return _status()

    @api.handler("write", permission="admin",
                 description="向命令会话发送一行命令（需审批）")
    def write(args, ctx):
        line = str(args.get("line") or "")
        if not line:
            raise ApiError("bad_args", "命令不能为空")
        with _session_lock:
            _session.write(line)
        return {"sent": line}

    @api.handler("read", permission="readonly",
                 description="取走命令会话的增量输出（轮询用）")
    def read(args, ctx):
        with _session_lock:
            return _session.drain()

    @api.handler("kill", permission="admin",
                 description="终止当前命令会话（需审批）")
    def kill(args, ctx):
        with _session_lock:
            _session.kill()
        return {"killed": True}

    @api.handler("restart", permission="admin",
                 description="重启命令会话（需审批）")
    def restart(args, ctx):
        with _session_lock:
            _session.kill()
            _session.start()
        return _status()

    # 全局快捷键：Ctrl+Alt+T 聚焦命令行（页面监听 stb-shortcut 事件）
    @api.shortcut("focus", "Ctrl+Alt+T", "聚焦 CMD 命令行输入框")
    def on_focus(evt, ctx):
        return {"action": "focus"}

    api.log.info("cmd-console 插件已注册: status/start/write/read/kill/restart"
                 " + 快捷键 Ctrl+Alt+T")
