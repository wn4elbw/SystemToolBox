"""系统信息叠加层插件 — 近 20s 实时采样 + 叠加层状态管理。

职责：
- 后台线程每 1s 采样 CPU% / 内存% / 系统盘使用率% / 网络收发速率，环形保留最近 20 个点；
- 维护叠加层配置（开关 / 透明度 / 尺寸 / 位置 / 鼠标穿透），持久化到 python/data/overlay.json；
- 提供 API：state（状态，供 Electron 主进程轮询创建叠加层窗口）、history（折线图数据）、
  set（改配置）；全局快捷键 Win+Alt+O 开关、Win+Alt+P 切换穿透。

实现说明：
- 全部使用 ctypes 直连 Win32 API（GetSystemTimes / GlobalMemoryStatusEx /
  GetDiskFreeSpaceExW / GetIfTable），零第三方依赖；
- 叠加层窗口由 Electron 主进程创建并应用配置（透明置顶窗口、setBounds、
  setIgnoreMouseEvents），本插件只负责采样与状态。
"""
import ctypes
import ctypes.wintypes as wt
import json
import threading
import time
from collections import deque
from pathlib import Path

from app.api.router import ApiError

# ---------------- Win32 ----------------

kernel32 = ctypes.windll.kernel32
iphlpapi = ctypes.windll.iphlpapi

DWORD = wt.DWORD
FILETIME = wt.FILETIME

kernel32.GetLastError.restype = ctypes.c_ulong

WINDOWS_DIR = ctypes.create_unicode_buffer(320)


def _system_drive() -> str:
    """系统盘根（如 C:\\）。"""
    n = kernel32.GetSystemDirectoryW(WINDOWS_DIR, len(WINDOWS_DIR))
    if n and n < len(WINDOWS_DIR):
        return WINDOWS_DIR.value[:3]
    return "C:\\"


SYSTEM_DRIVE = _system_drive()


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", DWORD), ("dwMemoryLoad", DWORD),
        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _MIB_IFROW(ctypes.Structure):
    """iphlpapi MIB_IFROW（GetIfTable，v1 表）。"""
    _fields_ = [
        ("wszName", ctypes.c_wchar * 256),
        ("dwIndex", DWORD), ("dwType", DWORD), ("dwMtu", DWORD),
        ("dwSpeed", DWORD), ("dwPhysAddrLen", DWORD),
        ("bPhysAddr", ctypes.c_ubyte * 8),
        ("dwAdminStatus", DWORD), ("dwOperStatus", DWORD),
        ("dwLastChange", DWORD),
        ("dwInOctets", DWORD), ("dwInUcastPkts", DWORD),
        ("dwInNUcastPkts", DWORD), ("dwInDiscards", DWORD),
        ("dwInErrors", DWORD), ("dwInUnknownProtos", DWORD),
        ("dwOutOctets", DWORD), ("dwOutUcastPkts", DWORD),
        ("dwOutNUcastPkts", DWORD), ("dwOutDiscards", DWORD),
        ("dwOutErrors", DWORD), ("dwOutQLen", DWORD),
        ("dwDescrLen", DWORD), ("bDescr", ctypes.c_ubyte * 256),
    ]


_IFROW_SIZE = ctypes.sizeof(_MIB_IFROW)
ERROR_INSUFFICIENT_BUFFER = 122
MIB_IF_OPER_STATUS_UP = 1

# ---------------- 采样 ----------------

_cpu_prev = None          # (idle, kern, user) 上一采样
_net_prev = None          # (inOctets, outOctets, t)
_lock = threading.RLock()
# 环形缓冲：刷新率低至 200ms 时 20s ≈ 100 点，取上限 200 点覆盖
_samples = deque(maxlen=200)   # [{t, cpu, mem, disk, netRx, netTx}]
_started_at = time.time()
_sampler = None
_sampling = True


def _cpu_pct() -> float:
    global _cpu_prev
    idle, kern, user = FILETIME(), FILETIME(), FILETIME()
    if not kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
        return 0.0

    def tot(ft):
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    cur = (tot(idle), tot(kern), tot(user))
    prev = _cpu_prev
    _cpu_prev = cur
    if not prev:
        return 0.0
    i_d = cur[0] - prev[0]
    k_d = cur[1] - prev[1]
    u_d = cur[2] - prev[2]
    busy = (k_d + u_d) - i_d
    total = busy + i_d
    if total <= 0:
        return 0.0
    return round(min(100.0, max(0.0, busy / total * 100.0)), 1)


def _mem_pct() -> float:
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return round(float(m.dwMemoryLoad), 1)
    return 0.0


def _disk_pct() -> float:
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    if kernel32.GetDiskFreeSpaceExW(
            SYSTEM_DRIVE, ctypes.byref(free), ctypes.byref(total), None) \
            and total.value:
        return round((total.value - free.value) / total.value * 100.0, 1)
    return 0.0


def _net_octets():
    """所有 up 接口的收发字节合计 (in, out)；失败返回 (0,0)。"""
    size = DWORD(0)
    if iphlpapi.GetIfTable(None, ctypes.byref(size), False) \
            != ERROR_INSUFFICIENT_BUFFER:
        return 0, 0
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetIfTable(buf, ctypes.byref(size), False) != 0:
        return 0, 0
    num = DWORD.from_buffer(buf).value
    base = ctypes.addressof(buf) + 4
    total_in = total_out = 0
    for i in range(num):
        row = ctypes.cast(base + i * _IFROW_SIZE,
                          ctypes.POINTER(_MIB_IFROW)).contents
        if row.dwOperStatus == MIB_IF_OPER_STATUS_UP:
            total_in += row.dwInOctets
            total_out += row.dwOutOctets
    return total_in, total_out


def _net_rate():
    """近 1s 网络收发速率（KB/s）。"""
    global _net_prev
    now = time.time()
    try:
        cur = _net_octets()
    except Exception:  # noqa: BLE001
        return 0.0, 0.0
    prev = _net_prev
    _net_prev = (cur[0], cur[1], now)
    if not prev:
        return 0.0, 0.0
    dt = now - prev[2]
    if dt <= 0:
        return 0.0, 0.0
    rx = max(0, cur[0] - prev[0]) / dt / 1024.0
    tx = max(0, cur[1] - prev[1]) / dt / 1024.0
    return round(rx, 1), round(tx, 1)


def _sample_once():
    try:
        cpu = _cpu_pct()
        mem = _mem_pct()
        disk = _disk_pct()
        rx, tx = _net_rate()
    except Exception:  # noqa: BLE001
        return
    with _lock:
        _samples.append({
            "t": time.time(),
            "cpu": cpu, "mem": mem, "disk": disk,
            "netRx": rx, "netTx": tx,
        })


def _sampler_loop():
    while _sampling:
        _sample_once()
        with _lock:
            delay = float(_cfg.get("refreshMs") or 1000) / 1000.0
        time.sleep(max(0.05, min(10.0, delay)))


# ---------------- 状态与持久化 ----------------

_CFG_PATH = Path(__file__).resolve().parents[2] / "python" / "data" / "overlay.json"

_DEFAULTS = {
    "enabled": False,
    "opacity": 0.78,
    "x": None,          # None = 交给 Electron 默认（右上角）
    "y": None,
    "w": 460,
    "h": 260,
    "refreshMs": 1000,   # 采样/刷新率（200-5000ms，持久化）
    "passthrough": False,
}


def _load_cfg():
    cfg = dict(_DEFAULTS)
    try:
        if _CFG_PATH.is_file():
            # utf-8-sig：容忍 BOM（Windows 记事本 / PowerShell Set-Content 会写 BOM）
            cfg.update(json.loads(_CFG_PATH.read_text("utf-8-sig")))
    except Exception:  # noqa: BLE001
        pass
    return cfg


_cfg = _load_cfg()


def _save_cfg():
    try:
        _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CFG_PATH.write_text(
            json.dumps(_cfg, ensure_ascii=False, indent=2), "utf-8")
    except Exception:  # noqa: BLE001
        pass


def _state_dict():
    with _lock:
        last = _samples[-1] if _samples else None
        return {
            "enabled": bool(_cfg["enabled"]),
            "opacity": float(_cfg["opacity"]),
            "x": _cfg["x"], "y": _cfg["y"],
            "w": int(_cfg["w"]), "h": int(_cfg["h"]),
            "refreshMs": int(_cfg.get("refreshMs") or 1000),
            "passthrough": bool(_cfg["passthrough"]),
            "sampling": len(_samples),
            "last": last,
        }


def _clamp(v, lo, hi):
    try:
        return float(max(lo, min(hi, float(v))))
    except (TypeError, ValueError):
        return lo


def _apply_set(args):
    with _lock:
        if args.get("enabled") is not None:
            _cfg["enabled"] = bool(args["enabled"])
        if args.get("passthrough") is not None:
            _cfg["passthrough"] = bool(args["passthrough"])
        if args.get("opacity") is not None:
            _cfg["opacity"] = _clamp(args["opacity"], 0.08, 1.0)
        if args.get("w") is not None:
            _cfg["w"] = int(_clamp(args["w"], 180, 1600))
        if args.get("h") is not None:
            _cfg["h"] = int(_clamp(args["h"], 90, 1000))
        if args.get("refreshMs") is not None:
            _cfg["refreshMs"] = int(_clamp(args["refreshMs"], 200, 5000))
        # 位置显式置 None 表示自动（右上角）
        if "x" in args:
            _cfg["x"] = None if args["x"] is None else int(args["x"])
        if "y" in args:
            _cfg["y"] = None if args["y"] is None else int(args["y"])
        _save_cfg()
    return _state_dict()


# ---------------- 注册 ----------------

def register(api):
    @api.handler("state", permission="readonly",
                 description="叠加层状态与最近一次采样（Electron 主进程轮询用）")
    def state(args, ctx):
        return _state_dict()

    @api.handler("history", permission="readonly",
                 description="近 20s 采样序列（折线图数据）：cpu/mem/disk/netRx/netTx")
    def history(args, ctx):
        with _lock:
            return {"samples": list(_samples),
                    "since": _started_at}

    @api.handler("set", permission="admin",
                 description="更新叠加层配置：开关/透明度/尺寸/位置/鼠标穿透")
    def set_cfg(args, ctx):
        if not isinstance(args, dict):
            raise ApiError("bad_args", "参数必须是 JSON 对象")
        return _apply_set(args)

    # 全局快捷键：Win+Alt+O 开关叠加层；Win+Alt+P 切换鼠标穿透
    @api.shortcut("toggle", "Win+Alt+O", "开/关系统信息叠加层")
    def on_toggle(evt, ctx):
        with _lock:
            _cfg["enabled"] = not _cfg["enabled"]
            _save_cfg()
        return _state_dict()

    @api.shortcut("passthrough", "Win+Alt+P", "切换叠加层鼠标穿透")
    def on_passthrough(evt, ctx):
        with _lock:
            _cfg["passthrough"] = not _cfg["passthrough"]
            _save_cfg()
        return _state_dict()

    _sample_once()          # 首采样（CPU 差值基准）
    global _sampler
    with _lock:
        if _sampler is None or not _sampler.is_alive():
            _sampler = threading.Thread(target=_sampler_loop, daemon=True)
            _sampler.start()

    api.log.info("overlay 插件已注册: state/history/set + 快捷键 "
                 "Win+Alt+O(开关) / Win+Alt+P(穿透)")