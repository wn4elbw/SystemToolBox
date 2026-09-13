# -*- coding: utf-8 -*-
"""极域(StudentMain)相关逻辑：进程定位、窗口识别、广播窗口化/全屏化、密码解密。

源自 JiYuTrainer TrainerWorker.cpp 与 MythwareToolkit mythware.cpp 的移植。
"""
import ctypes
from ctypes import wintypes

from . import win32hk as w

STUDENT_MAIN = "StudentMain.exe"
ALT_NAMES = ["StudentMain64.exe", "StudentM.exe", "Student.exe",
             "MasterHelper.exe"]
GB_TEXT = ("屏幕广播", "演示", "共享")          # 广播窗口标题关键词
GB_TEXT_FULL = "屏幕演播室窗口"

# 极域注册表路径
REG_KEY_LEGACY = r"SOFTWARE\TopDomain\e-Learning Class\Student"
REG_KEY_NEW = r"SOFTWARE\TopDomain\e-Learning Class Standard\1.00"
REG_VER_KEY = r"SOFTWARE\TopDomain\e-Learning Class Standard\1.00"


# ---------------------------------------------------------------- 进程定位
def find_student_main():
    """返回 StudentMain pid，找不到则尝试候选进程名。返回 (pid, 实际进程名)。"""
    pid = w.find_process(STUDENT_MAIN)
    if pid:
        return pid, STUDENT_MAIN
    for name in ALT_NAMES:
        pid = w.find_process(name)
        if pid:
            return pid, name
    return 0, None


def student_main_info():
    """极域进程状态 + 版本号。"""
    pid, name = find_student_main()
    info = {
        "running": pid > 0,
        "pid": pid,
        "processName": name,
        "version": read_version(),
        "suspended": False,
        "broadcastWindow": None,
    }
    if pid:
        bwin = find_broadcast_window(pid)
        if bwin:
            info["broadcastWindow"] = bwin
    return info


def read_version():
    h = w.reg_open(w.HKEY_LOCAL_MACHINE, REG_VER_KEY,
                   w.KEY_QUERY_VALUE, w.KEY_WOW64_32KEY)
    if not h:
        return ""
    r = w.reg_query_value(h, "Version")
    w.reg_close(h)
    return r[1] if r and r[0] in ("sz", "dw") else ""


# ---------------------------------------------------------------- 广播窗口识别
def is_gb_window_text(text):
    """判断窗口标题是否为广播窗口。"""
    if not text:
        return False
    if text == GB_TEXT_FULL:
        return True
    for kw in GB_TEXT:
        if kw in text:
            return True
    return False


def find_broadcast_window(pid=None):
    """枚举窗口找极域广播窗口，返回描述 dict 或 None。

    匹配规则（同原项目）：进程为极域 + 类名 Afx: + 标题含 广播/演示/共享。
    """
    pid = pid or find_student_main()[0]
    if not pid:
        # 无极域进程时也尝试全窗口找
        pid = None
    for hwnd in w.find_window_windows():
        if pid is not None and hwnd["pid"] != pid:
            continue
        cls = hwnd["class"]
        if not (cls and cls.startswith("Afx")):
            continue
        if is_gb_window_text(hwnd["text"]):
            return {
                "hwnd": hwnd["hwnd"], "text": hwnd["text"],
                "pid": hwnd["pid"], "visible": hwnd["visible"],
            }
    return None


def list_broadcast_windows():
    """列出所有疑似广播窗口（供用户选择）。"""
    out = []
    for hwnd in w.find_window_windows():
        cls = hwnd["class"]
        if not (cls and cls.startswith("Afx")):
            continue
        if is_gb_window_text(hwnd["text"]):
            out.append(hwnd)
    return out


# ---------------------------------------------------------------- 窗口化/全屏化
def toggle_broadcast_window(hwnd=None):
    """再广播窗口上模拟点击「窗口化/全屏化」按钮。

    原 MythwareToolkit 用 PostMessage(WM_COMMAND, 1004, BM_CLICK)；
    JiYuTrainer 直接改窗口样式。这里两者结合：优先发命令，并校验样式。
    """
    if hwnd is None:
        bw = find_broadcast_window()
        if not bw:
            return {"ok": False, "message": "未找到广播窗口"}
        hwnd = bw["hwnd"]
    style = w.get_window_style(hwnd)
    was_full = not (style & w.WS_CAPTION)
    w.user32.PostMessageW(wintypes.HWND(hwnd), w.WM_COMMAND,
                          wintypes.WPARAM(1004), wintypes.LPARAM(0))
    return {"ok": True, "hwnd": int(hwnd), "wasFull": bool(was_full),
            "message": "已发送切换命令"}


def set_broadcast_windowed(hwnd=None, width=0, height=0):
    """强制将广播窗口改为普通窗口外观（居中 3/4 比例）。"""
    if hwnd is None:
        bw = find_broadcast_window()
        if not bw:
            return {"ok": False, "message": "未找到广播窗口"}
        hwnd = bw["hwnd"]
    sw, sh = w.get_screen_size()
    if not width:
        width = int(sw * 3 / 4)
    if not height:
        height = int(sh * 4 / 5)
    style = w.get_window_style(hwnd)
    # 保留基本边框（WS_CAPTION|WS_SIZEBOX），去掉全屏特征
    new_style = style | w.WS_CAPTION | w.WS_SIZEBOX | w.WS_SYSMENU
    w.SetWindowLong(hwnd, w.GWL_STYLE, new_style)
    # 去掉置顶
    ex = w.get_window_exstyle(hwnd)
    w.SetWindowLong(hwnd, w.GWL_EXSTYLE, ex & ~w.WS_EX_TOPMOST)
    w.SetWindowPos(hwnd, w.HWND_NOTOPMOST,
                   (sw - width) // 2, (sh - height) // 2,
                   width, height,
                   w.SWP_NOZORDER | w.SWP_SHOWWINDOW)
    w.user32.PostMessageW(wintypes.HWND(hwnd), w.WM_SIZE, 0,
                          wintypes.LPARAM(w.make_wparam_rect(width, height)))
    return {"ok": True, "hwnd": int(hwnd), "size": [width, height]}


def set_broadcast_fullscreen(hwnd=None):
    """强制全屏（置顶 + 移除边框 + 铺满屏幕）。"""
    if hwnd is None:
        bw = find_broadcast_window()
        if not bw:
            return {"ok": False, "message": "未找到广播窗口"}
        hwnd = bw["hwnd"]
    sw, sh = w.get_screen_size()
    style = w.get_window_style(hwnd)
    style &= ~(w.WS_CAPTION | w.WS_SIZEBOX | w.WS_BORDER)
    w.SetWindowLong(hwnd, w.GWL_STYLE, style)
    ex = w.get_window_exstyle(hwnd)
    w.SetWindowLong(hwnd, w.GWL_EXSTYLE, ex | w.WS_EX_TOPMOST)
    w.SetWindowPos(hwnd, w.HWND_TOPMOST, 0, 0, sw, sh,
                   w.SWP_SHOWWINDOW)
    w.user32.PostMessageW(wintypes.HWND(hwnd), w.WM_SIZE, 0,
                          wintypes.LPARAM(w.make_wparam_rect(sw, sh)))
    return {"ok": True, "hwnd": int(hwnd), "size": [sw, sh]}


# ---------------------------------------------------------------- 密码解密
def get_password(force_knock=False):
    """读取极域解锁/卸载密码（注册表 knock1 异或解密）。

    算法（源自 MythwareToolkit mythware.cpp）：
        每 4 字节依次 XOR 0x50^0x45、0x43^0x4c、0x4c^0x43、0x45^0x50
        然后取每两字节中的第一个非零字节拼接。
    """
    h = w.reg_open(w.HKEY_LOCAL_MACHINE, REG_KEY_LEGACY,
                   w.KEY_QUERY_VALUE, w.KEY_WOW64_32KEY)
    if not h:
        return None, "注册表路径不存在（可能未安装极域）"
    r = w.reg_query_value(h, "knock1")
    w.reg_close(h)
    if not r or r[0] not in ("sz", "raw"):
        return None, "未找到 knock1 字段"
    raw = r[1]
    if isinstance(raw, str):
        raw = raw.encode("utf-16-le", errors="ignore")
    data = bytearray(raw)
    xk = (0x50 ^ 0x45, 0x43 ^ 0x4c, 0x4c ^ 0x43, 0x45 ^ 0x50)
    for i in range(0, len(data) - 3, 4):
        data[i] ^= xk[0]
        data[i + 1] ^= xk[1]
        data[i + 2] ^= xk[2]
        data[i + 3] ^= xk[3]
    out = bytearray()
    for i in range(0, len(data) - 1, 2):
        if data[i + 1] == 0:
            out.append(data[i])
            if data[i] == 0:
                break
    try:
        pwd = out.decode("gbk", errors="ignore").rstrip("\x00")
    except Exception:  # noqa: BLE001
        pwd = out.decode("utf-8", errors="ignore").rstrip("\x00")
    if not pwd:
        return None, "解密结果为空"
    return pwd, None


# ---------------------------------------------------------------- 进程控制
def kill_student_main(force=True):
    """强杀极域（先找主进程）。"""
    pid, name = find_student_main()
    if not pid:
        return {"ok": False, "message": "极域未运行"}
    ok, err = w.terminate_pid(pid)
    return {"ok": ok, "pid": pid, "processName": name,
            "error": err if not ok else None}


def suspend_student_main():
    pid, name = find_student_main()
    if not pid:
        return {"ok": False, "message": "极域未运行"}
    ok, err = w.suspend_process(pid)
    return {"ok": ok, "pid": pid, "error": err if not ok else None}


def resume_student_main():
    pid, name = find_student_main()
    if not pid:
        return {"ok": False, "message": "极域未运行"}
    ok, err = w.resume_process(pid)
    return {"ok": ok, "pid": pid, "error": err if not ok else None}