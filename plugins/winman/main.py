"""窗口管理插件 — 枚举 / 激活 / 最小化 / 最大化 / 还原 / 关闭窗口、结束窗口进程。

实现：直接调用 Win32 API（ctypes）：
- 枚举：EnumWindows + GetWindowTextW/GetClassNameW/GetWindowThreadProcessId/
  IsWindowVisible/IsIconic/IsZoomed/GetWindowRect/DwmGetWindowAttribute(cloaked)；
- 激活：还原最小化窗口 + SetWindowPos(TOPMOST->NOTOPMOST) 经典置顶技巧 +
  SetForegroundWindow/BringWindowToTop；
- 最小化/最大化/还原：ShowWindow；关闭：PostMessage(WM_CLOSE)；
- 结束进程：OpenProcess + TerminateProcess（保护 pid<=4）。

权限：枚举 readonly；激活/最小化/最大化/还原/关闭/结束进程为 admin 级，
approval 插件首次调用需审批。
"""
import ctypes
import ctypes.wintypes as wt

from app.api.router import ApiError

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ctypes.windll 默认不捕获 last error（get_last_error 恒 0），直接读 GetLastError
kernel32.GetLastError.restype = ctypes.c_ulong


def _last_error() -> int:
    return int(kernel32.GetLastError())

HWND = wt.HWND
DWORD = wt.DWORD
LPARAM = wt.LPARAM
BOOL = ctypes.c_bool
WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", DWORD)]

user32.EnumWindows.argtypes = [WNDENUMPROC, LPARAM]
user32.EnumWindows.restype = BOOL
user32.IsWindow.argtypes = [HWND]
user32.IsWindow.restype = BOOL
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
user32.GetWindowThreadProcessId.restype = DWORD
user32.GetWindowTextW.argtypes = [HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [HWND]
user32.IsWindowVisible.restype = BOOL
user32.IsIconic.argtypes = [HWND]
user32.IsIconic.restype = BOOL
user32.IsZoomed.argtypes = [HWND]
user32.IsZoomed.restype = BOOL
user32.GetWindowLongW.argtypes = [HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = BOOL
user32.GetForegroundWindow.restype = HWND
user32.BringWindowToTop.argtypes = [HWND]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.ShowWindow.restype = BOOL
user32.PostMessageW.argtypes = [HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = BOOL
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = BOOL
user32.SetLayeredWindowAttributes.argtypes = [HWND, DWORD, ctypes.c_ubyte,
                                              DWORD]
user32.SetLayeredWindowAttributes.restype = BOOL
user32.GetLayeredWindowAttributes.argtypes = [
    HWND, ctypes.POINTER(DWORD), ctypes.POINTER(ctypes.c_ubyte),
    ctypes.POINTER(DWORD)]
user32.GetLayeredWindowAttributes.restype = BOOL
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = BOOL
user32.MonitorFromWindow.argtypes = [HWND, DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p,
                                   ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = BOOL
kernel32.OpenProcess.argtypes = [DWORD, ctypes.c_int, DWORD]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WM_CLOSE = 0x0010
SW_HIDE = 0
SW_SHOW = 5
SW_NORMAL = 1
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)
HWND_BOTTOM = ctypes.c_void_p(1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
LWA_COLORKEY = 0x1
LWA_ALPHA = 0x2
MONITOR_DEFAULTTONEAREST = 2
DWMWA_CLOAKED = 14  # 不再用于过滤（新版 DWM 对主窗口一律返 True，见 _is_cloaked）

# 被快捷键隐藏的窗口栈（Win+Alt+Z 入栈，Win+Alt+X 弹出上一个，Win+Alt+S 全部恢复）
_hidden_stack = []


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD),
                ("th32ProcessID", DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", DWORD), ("cntThreads", DWORD),
                ("th32ParentProcessID", DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


# 独立函数原型绑定 toolhelp 快照系列，避免与 procman 等插件在共享的
# kernel32.Process32FirstW 上互相覆盖 argtypes（各自结构体类不同）
_SnapProto = ctypes.WINFUNCTYPE(ctypes.c_void_p, DWORD, DWORD)
_EntryProto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                 ctypes.POINTER(PROCESSENTRY32W))
CreateToolhelp32Snapshot = _SnapProto(("CreateToolhelp32Snapshot", kernel32))
Process32FirstW = _EntryProto(("Process32FirstW", kernel32))
Process32NextW = _EntryProto(("Process32NextW", kernel32))


def _proc_name_map():
    """pid -> 进程名（toolhelp 快照，一次调用建一次）。"""
    snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == 0xFFFFFFFFFFFFFFFF:
        return {}
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        out = {}
        if Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                out[pe.th32ProcessID] = pe.szExeFile
                if not Process32NextW(snap, ctypes.byref(pe)):
                    break
        return out
    finally:
        kernel32.CloseHandle(snap)


def _is_cloaked(hwnd):
    """已停用：新版 Windows DWM 对主窗口一律返回 cloaked=True（实测 90%），
    会导致窗口列表为空；改由 IsWindowVisible + WS_EX_TOOLWINDOW 过滤。"""
    return False


def _enum_windows(include_hidden=False):
    found = []

    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd) and not include_hidden:
            return True
        if _is_cloaked(hwnd):
            return True
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW:
            return True
        found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def _window_opacity(hwnd):
    """读取分层窗口透明度（0-255），非分层/失败返回 None。"""
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if not (ex & WS_EX_LAYERED):
        return None
    alpha = ctypes.c_ubyte(255)   # BYTE 无符号：alpha>127 不得读成负数
    key = DWORD(0)
    flags = DWORD(0)
    ok = user32.GetLayeredWindowAttributes(hwnd, ctypes.byref(key),
                                           ctypes.byref(alpha),
                                           ctypes.byref(flags))
    if ok and (flags.value & LWA_ALPHA):
        return int(alpha.value) & 0xFF
    return None


def _window_info(hwnd, names):
    pid = DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, 512)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    return {
        "hwnd": int(hwnd),
        "title": title.value,
        "cls": cls.value,
        "pid": pid.value,
        "process": names.get(pid.value, ""),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
        "maximized": bool(user32.IsZoomed(hwnd)),
        "topmost": bool(ex & 0x8),           # WS_EX_TOPMOST
        "opacity": _window_opacity(hwnd),    # 0-255 或 None
        "rect": {"x": rect.left, "y": rect.top,
                 "width": rect.right - rect.left,
                 "height": rect.bottom - rect.top},
    }


def _check_hwnd(hwnd):
    hwnd = int(hwnd)
    if not hwnd or not user32.IsWindow(hwnd):
        raise ApiError("not_found", f"窗口句柄无效: {hwnd}", http=404)
    return hwnd


def _kill_owner(hwnd):
    """结束窗口所属进程（保护 pid<=4 与后端自身）。返回 dict。"""
    hwnd = _check_hwnd(hwnd)
    pid = DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value <= 4:
        raise ApiError("denied", "禁止结束系统关键进程 (pid<=4)")
    import os
    if pid.value == os.getpid():
        raise ApiError("denied", "不能结束后端自身进程")
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid.value)
    if not h or h in (0, 0xFFFFFFFFFFFFFFFF):
        raise ApiError("denied", f"无法打开进程 pid={pid.value}")
    try:
        if not kernel32.TerminateProcess(h, 0):
            raise ApiError("io_error", f"结束进程失败 pid={pid.value}，"
                           f"错误码 {ctypes.get_last_error()}")
    finally:
        kernel32.CloseHandle(h)
    return {"hwnd": hwnd, "pid": pid.value, "killed": True}


def _foreground_hwnd():
    """前台（活动）窗口；无则 0。"""
    hwnd = user32.GetForegroundWindow()
    return int(hwnd) if hwnd else 0


def register(api):
    @api.handler("list", permission="readonly",
                 description="枚举顶层窗口（含隐藏/置顶/透明度；hidden=1 时含隐藏窗口）")
    def win_list(args, ctx):
        include_hidden = bool(args.get("hidden"))
        names = _proc_name_map()
        out = []
        for hwnd in _enum_windows(include_hidden):
            info = _window_info(hwnd, names)
            if not info["title"]:
                continue
            out.append(info)
        out.sort(key=lambda w: (w["process"].lower(), w["title"].lower()))
        return {"total": len(out), "windows": out}

    @api.handler("activate", permission="admin",
                 description="激活窗口并置前（需审批）")
    def win_activate(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        # 经典置顶技巧：TOPMOST 再取消，绕过前台锁定
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        return {"hwnd": hwnd, "activated": True}

    @api.handler("minimize", permission="admin",
                 description="最小化窗口（需审批）")
    def win_minimize(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        return {"hwnd": hwnd, "minimized": True}

    @api.handler("maximize", permission="admin",
                 description="最大化窗口（需审批）")
    def win_maximize(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return {"hwnd": hwnd, "maximized": True}

    @api.handler("restore", permission="admin",
                 description="还原窗口（需审批）")
    def win_restore(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.ShowWindow(hwnd, SW_RESTORE)
        return {"hwnd": hwnd, "restored": True}

    @api.handler("hide", permission="admin",
                 description="隐藏窗口（需审批）")
    def win_hide(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.ShowWindow(hwnd, SW_HIDE)
        return {"hwnd": hwnd, "hidden": True}

    @api.handler("show", permission="admin",
                 description="显示（恢复）窗口（需审批）")
    def win_show(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.ShowWindow(hwnd, SW_RESTORE)  # 还原最小化并显示
        return {"hwnd": hwnd, "shown": True}

    @api.handler("opacity", permission="admin",
                 description="设置窗口透明度（0-100，需审批）")
    def win_opacity(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        try:
            alpha = int(args.get("alpha") or 100)
        except (TypeError, ValueError):
            raise ApiError("bad_args", "透明度必须是 0-100 的整数")
        alpha = max(0, min(100, alpha))
        if alpha == 100:
            # 恢复完全不透明：清除 LAYERED 扩展样式
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
            return {"hwnd": hwnd, "alpha": alpha}
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (ex & WS_EX_LAYERED):
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        # alpha: 0-255，界面传 0-100 -> 换算 0-255
        lwa = max(1, round(alpha * 2.55))
        user32.SetLayeredWindowAttributes(hwnd, 0, lwa, LWA_ALPHA)
        return {"hwnd": hwnd, "alpha": alpha}

    @api.handler("topmost", permission="admin",
                 description="窗口置顶（需审批）")
    def win_topmost(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        return {"hwnd": hwnd, "topmost": True}

    @api.handler("untopmost", permission="admin",
                 description="取消窗口置顶（需审批）")
    def win_untopmost(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE)
        return {"hwnd": hwnd, "topmost": False}

    @api.handler("bottom", permission="admin",
                 description="窗口置底（需审批）")
    def win_bottom(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return {"hwnd": hwnd, "bottom": True}

    @api.handler("move", permission="admin",
                 description="移动窗口到 (x,y)（需审批）")
    def win_move(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        try:
            x = int(args.get("x"))
            y = int(args.get("y"))
        except (TypeError, ValueError):
            raise ApiError("bad_args", "x/y 必须是整数")
        user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return {"hwnd": hwnd, "x": x, "y": y}

    @api.handler("resize", permission="admin",
                 description="调整窗口大小到 width×height（需审批）")
    def win_resize(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        try:
            w = int(args.get("width"))
            h = int(args.get("height"))
        except (TypeError, ValueError):
            raise ApiError("bad_args", "width/height 必须是整数")
        user32.SetWindowPos(hwnd, 0, 0, 0, w, h,
                            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
        return {"hwnd": hwnd, "width": w, "height": h}

    @api.handler("center", permission="admin",
                 description="窗口居中于所在屏幕工作区（需审批）")
    def win_center(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            raise ApiError("io_error", "无法获取显示器信息")
        x = mi.rcWork.left + (mi.rcWork.right - mi.rcWork.left - w) // 2
        y = mi.rcWork.top + (mi.rcWork.bottom - mi.rcWork.top - h) // 2
        user32.SetWindowPos(hwnd, 0, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return {"hwnd": hwnd, "x": x, "y": y, "centered": True}

    @api.handler("close", permission="admin",
                 description="向窗口发送关闭消息（需审批）")
    def win_close(args, ctx):
        hwnd = _check_hwnd(args.get("hwnd"))
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return {"hwnd": hwnd, "closeSent": True}

    @api.handler("killOwner", permission="admin",
                 description="结束窗口所属进程（需审批；保护 pid<=4）")
    def win_kill_owner(args, ctx):
        return _kill_owner(args.get("hwnd"))

    # ---------------- 快捷键（默认在快捷键管理中启用后生效） ----------------

    @api.shortcut("kill-fg", "Alt+C", "结束前台窗口所属进程")
    def sc_kill_fg(evt, ctx):
        hwnd = _foreground_hwnd()
        if not hwnd:
            return {"ok": False, "message": "无前台窗口"}
        return _kill_owner(hwnd)

    @api.shortcut("hide-fg", "Super+Alt+Z", "隐藏前台窗口（记入隐藏栈）")
    def sc_hide_fg(evt, ctx):
        hwnd = _foreground_hwnd()
        if not hwnd:
            return {"ok": False, "message": "无前台窗口"}
        hwnd = _check_hwnd(hwnd)
        if hwnd not in _hidden_stack:
            _hidden_stack.append(hwnd)
        user32.ShowWindow(hwnd, SW_HIDE)
        return {"hwnd": hwnd, "hidden": True, "stack": len(_hidden_stack)}

    @api.shortcut("show-last-hidden", "Super+Alt+X",
                  "显示上一个被隐藏的窗口")
    def sc_show_last(evt, ctx):
        if not _hidden_stack:
            return {"ok": False, "message": "隐藏栈为空"}
        hwnd = _hidden_stack.pop()
        if user32.IsWindow(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            return {"hwnd": hwnd, "shown": True, "left": len(_hidden_stack)}
        return {"ok": False, "message": f"窗口已失效: {hwnd}",
                "left": len(_hidden_stack)}

    @api.shortcut("show-all-hidden", "Super+Alt+S", "恢复所有隐藏窗口")
    def sc_show_all(evt, ctx):
        restored = []
        while _hidden_stack:
            hwnd = _hidden_stack.pop()
            if user32.IsWindow(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                restored.append(hwnd)
        return {"ok": True, "restored": restored,
                "count": len(restored)}

    api.log.info("winman 插件已注册: list/activate/minimize/maximize/"
                 "restore/hide/show/opacity/topmost/untopmost/bottom/"
                 "move/resize/center/close/killOwner + 快捷键 "
                 "Alt+C/Super+Alt+Z/Super+Alt+X/Super+Alt+S")
