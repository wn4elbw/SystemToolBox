"""极简日志（写文件 + 可选控制台）。"""
import sys
import threading
from datetime import datetime

from . import config

_lock = threading.Lock()


def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(level: str, msg: str):
    line = f"[{_ts()}] [{level}] {msg}"
    with _lock:
        try:
            config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(config.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print(line, file=sys.stderr)
        sys.stderr.flush()


def info(msg: str):
    log("INFO", msg)


def warn(msg: str):
    log("WARN", msg)


def error(msg: str):
    log("ERROR", msg)


def debug(msg: str):
    if _debug_enabled:
        log("DEBUG", msg)


_debug_enabled = False


def set_debug(enabled: bool):
    global _debug_enabled
    _debug_enabled = enabled
