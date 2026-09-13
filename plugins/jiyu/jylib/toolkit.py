# -*- coding: utf-8 -*-
"""MythwareToolkit 专属逻辑：退出黑屏、解网络/USB 限制、解禁工具、密码计算、机房助手。

移植自 MythwareToolkit mythware.cpp / bypass.cpp / assistant.cpp / psd.cpp。
"""
import ctypes
import re
from ctypes import wintypes

from . import win32hk as w

# ---------------------------------------------------------------- 退出黑屏
def _find_black_screen():
    """找全屏置顶的疑似黑屏窗口（黑屏安静）。"""
    from ctypes import wintypes as _wt
    result = {"hwnd": None}

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                     wintypes.LPARAM)

    def cb(hwnd, lp):
        if not w.user32.IsWindowVisible(hwnd):
            return True
        ex = w.get_window_exstyle(hwnd)
        if not (ex & w.WS_EX_TOPMOST):
            return True
        rect = _wt.RECT()
        w.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        sw, sh = w.get_screen_size()
        if (rect.right - rect.left) < sw or (rect.bottom - rect.top) < sh:
            return True
        result["hwnd"] = int(hwnd)
        return False

    w.user32.EnumWindows(WNDENUMPROC(cb), 0)
    return result["hwnd"]


def exit_black_screen(max_level=4):
    """多级退出黑屏安静：1隐藏 2最小化 3ESC 4杀进程。"""
    hwnd = _find_black_screen()
    if not hwnd:
        return {"ok": False, "level": 0, "message": "未检测到黑屏窗口"}

    # 第1级：隐藏
    w.ShowWindow(wintypes.HWND(hwnd), w.SW_HIDE)
    ctypes.windll.kernel32.Sleep(200)
    if not w.user32.IsWindowVisible(wintypes.HWND(hwnd)):
        return {"ok": True, "level": 1, "message": "已隐藏黑屏窗口"}

    if max_level < 2:
        return {"ok": False, "level": 1, "message": "隐藏失败"}

    # 第2级：取消置顶 + 最小化
    w.SetWindowPos(wintypes.HWND(hwnd), w.HWND_NOTOPMOST, 0, 0, 0, 0,
                   w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOZORDER |
                   w.SWP_SHOWWINDOW)
    w.ShowWindow(wintypes.HWND(hwnd), w.SW_MINIMIZE)
    ctypes.windll.kernel32.Sleep(200)
    if not w.user32.IsWindowVisible(wintypes.HWND(hwnd)):
        return {"ok": True, "level": 2, "message": "已取消置顶并最小化"}

    if max_level < 3:
        return {"ok": False, "level": 2, "message": "最小化失败"}

    # 第3级：模拟 ESC
    w.user32.SetForegroundWindow(wintypes.HWND(hwnd))
    ctypes.windll.kernel32.Sleep(50)
    keybd = ctypes.WinDLL("user32", use_last_error=True)
    keybd.keybd_event(0x1B, 0, 0, 0)             # VK_ESCAPE down
    keybd.keybd_event(0x1B, 0, 2, 0)             # VK_ESCAPE up
    ctypes.windll.kernel32.Sleep(300)
    if not w.user32.IsWindowVisible(wintypes.HWND(hwnd)):
        return {"ok": True, "level": 3, "message": "已发送 ESC 关闭"}

    if max_level < 4:
        return {"ok": False, "level": 3, "message": "ESC 无效"}

    # 第4级：杀极域进程
    pid, name = find_student_main_safe()
    if pid:
        ok, err = w.terminate_pid(pid)
        if ok:
            return {"ok": True, "level": 4, "message": f"已强杀极域 ({name})"}
    return {"ok": False, "level": 3, "message": "无法常规退出，杀进程也失败"}


def find_student_main_safe():
    from . import jiyu
    return jiyu.find_student_main()


# ---------------------------------------------------------------- 解网络限制
def remove_network_restrictions():
    """解除极域网络限制（TDNetFilter 驱动/服务 + MasterHelper/GATESRV）。

    步骤（同 MythwareToolkit bypass.cpp）：
    1. 向 \\\\.\\TDNetFilter 设备发送停止指令(IOCTL 0x120014)
    2. 杀 MasterHelper.exe / GATESRV.exe
    3. 停止并删除 TDNetFilter 服务
    """
    results = []

    # 1) 设备 IOCTL
    dev = w.create_device(r"\\.\TDNetFilter")
    if dev:
        ok, err = w.device_io_control(dev, 0x120014)
        w.close_handle(dev)
        results.append({"step": "TDNetFilter 设备停止指令",
                        "ok": ok or err in (0, 6, 1168),
                        "error": err})
    else:
        err = w.get_last_error()
        results.append({"step": "打开 TDNetFilter 设备",
                        "ok": err in (0, 2, 6, 1168),
                        "error": err})   # 2/6/1168 = 未找到/无效句柄，说明可能已解除

    # 2) 杀相关进程
    killed = []
    for name in ("MasterHelper.exe", "GATESRV.exe"):
        for pid in w.find_processes(name):
            ok, _ = w.terminate_pid(pid)
            killed.append({"name": name, "pid": pid, "ok": ok})
    results.append({"step": "停止相关进程", "ok": True, "killed": killed})

    # 3) 停止并删除限网驱动服务
    scm = w.open_sc_manager(w.SC_MANAGER_ALL_ACCESS)
    if scm:
        hsvc = w.open_service(scm, "TDNetFilter", w.SERVICE_ALL_ACCESS)
        if hsvc:
            ok, _ = w.control_service(hsvc, 1)   # SERVICE_CONTROL_STOP
            deleted = w.delete_service(hsvc)
            w.close_service_handle(hsvc)
            results.append({"step": "TDNetFilter 服务",
                            "ok": bool(ok or deleted),
                            "stopped": bool(ok), "deleted": bool(deleted)})
        else:
            results.append({"step": "TDNetFilter 服务",
                            "ok": True, "message": "服务不存在"})
        w.close_service_handle(scm)
    else:
        results.append({"step": "OpenSCManager", "ok": False,
                        "error": w.get_last_error()})

    return {"ok": all(r["ok"] for r in results), "steps": results}


# ---------------------------------------------------------------- 解 USB 限制
def remove_usb_restrictions(mode="soft"):
    """解除极域 U 盘限制。

    soft : 连接 TDFileFilterPort 过滤端口，发送停止请求
    hard : 停止并删除 TDFileFilter 驱动服务
    """
    if mode == "soft":
        # FltLib: FilterConnectCommunicationPort + FilterSendMessage
        try:
            fltlib = ctypes.WinDLL("fltlib")
            hPort = wintypes.HANDLE()
            hr = fltlib.FilterConnectCommunicationPort(
                "\\TDFileFilterPort", 0, None, 0, None, ctypes.byref(hPort))
            if hr == 0 and hPort:
                in_buf = (ctypes.c_int * 4)(8, 0, 0, 0)
                out = ctypes.create_string_buffer(64)
                written = wintypes.DWORD()
                hr2 = fltlib.FilterSendMessage(
                    hPort, in_buf, ctypes.sizeof(in_buf), out,
                    ctypes.sizeof(out), ctypes.byref(written))
                w.close_handle(hPort)
                return {"ok": hr2 == 0, "mode": "soft",
                        "message": "已向过滤端口发送停止请求" if hr2 == 0
                        else f"FilterSendMessage 失败: 0x{hr2:08X}"}
            return {"ok": False, "mode": "soft",
                    "message": f"连接端口失败: 0x{hr:08X}"}
        except OSError as e:
            return {"ok": False, "mode": "soft", "message": str(e)}

    # hard：停止删除服务
    scm = w.open_sc_manager(w.SC_MANAGER_ALL_ACCESS)
    if not scm:
        return {"ok": False, "mode": "hard",
                "message": f"OpenSCManager 失败: {w.get_last_error()}"}
    hsvc = w.open_service(scm, "TDFileFilter", w.SERVICE_ALL_ACCESS)
    if not hsvc:
        w.close_service_handle(scm)
        return {"ok": False, "mode": "hard", "message": "TDFileFilter 服务不存在"}
    ok, _ = w.control_service(hsvc, 1)
    deleted = w.delete_service(hsvc)
    w.close_service_handle(hsvc)
    w.close_service_handle(scm)
    return {"ok": bool(deleted), "mode": "hard",
            "message": "已删除 TDFileFilter 驱动服务" if deleted
            else "停止成功但删除失败"}


# ---------------------------------------------------------------- 解禁系统程序
UNLOCK_PATHS = [
    (w.HKEY_CURRENT_USER, "SOFTWARE\\Policies\\Microsoft\\Windows\\System",
     [("DisableCMD", "命令提示符", "v")]),
    (w.HKEY_CURRENT_USER,
     "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System",
     [("DisableRegistryTools", "注册表编辑器", "v"),
      ("DisableTaskMgr", "任务管理器", "v"),
      ("DisableLockWorkstation", "锁定账户", "v"),
      ("DisableChangePassword", "修改密码", "v"),
      ("DisableSwitchUserOption", "切换用户", "v")]),
    (w.HKEY_CURRENT_USER,
     "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer",
     [("NoRun", "Win+R 运行", "v"), ("RestrictRun", "限制程序运行", "v"),
      ("NoLogOff", "注销", "v"), ("StartMenuLogOff", "开始菜单注销", "v"),
      ("NoTrayContextMenu", "托盘右键菜单", "v"),
      ("Hidden", "隐藏文件强制显示", "v"), ("NoFolderOptions", "文件夹选项", "v")]),
    (w.HKEY_LOCAL_MACHINE,
     "SYSTEM\\CurrentControlSet\\Services\\usbstor",
     [("Start", "USB 存储限制(当前控制集)", "dw4")]),
    (w.HKEY_LOCAL_MACHINE, "SYSTEM\\ControlSet001\\Services\\usbstor",
     [("Start", "USB 存储限制(控制集1)", "dw4")]),
    (w.HKEY_LOCAL_MACHINE, "SYSTEM\\ControlSet002\\Services\\usbstor",
     [("Start", "USB 存储限制(控制集2)", "dw4")]),
    (w.HKEY_LOCAL_MACHINE, "SYSTEM\\ControlSet003\\Services\\usbstor",
     [("Start", "USB 存储限制(控制集3)", "dw4")]),
]

UNLOCK_DELETE_PATHS = [
    (w.HKEY_LOCAL_MACHINE,
     "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File "
     "Execution Options\\taskkill.exe", [("debugger", "taskkill.exe 劫持")]),
    (w.HKEY_LOCAL_MACHINE,
     "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File "
     "Execution Options\\ntsd.exe", [("debugger", "ntsd.exe 劫持")]),
    (w.HKEY_LOCAL_MACHINE,
     "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File "
     "Execution Options\\tasklist.exe", [("debugger", "tasklist.exe 劫持")]),
    (w.HKEY_LOCAL_MACHINE,
     "SOFTWARE\\Policies\\Google\\Chrome",
     [("AllowDinosaurEasterEgg", "Chrome 恐龙游戏限制"),
      ("DownloadRestrictions", "Chrome 下载限制"),
      ("SaveAs", "Chrome 另存为"), ("DeveloperToolsAvailability",
                                    "Chrome 开发者工具")]),
    (w.HKEY_LOCAL_MACHINE, "SOFTWARE\\Policies\\Microsoft\\Edge",
     [("AllowSurfGame", "Edge 冲浪游戏"), ("WebWidgetAllowed", "Edge 边栏"),
      ("DownloadRestrictions", "Edge 下载限制"), ("SaveAs", "Edge 另存为"),
      ("DeveloperToolsAvailability", "Edge 开发者工具")]),
    (w.HKEY_LOCAL_MACHINE, "SOFTWARE\\Policies\\Mozilla\\Firefox",
     [("DisableDownloads", "Firefox 下载限制1"),
      ("BlockAboutDownloads", "Firefox 下载限制2")]),
    (w.HKEY_LOCAL_MACHINE,
     "SYSTEM\\CurrentControlSet\\Control\\Keyboard Layout",
     [("Scancode Map", "键盘映射(Tab 键)")]),
]

UNLOCK_IFEO = [
    "taskkill.exe", "ntsd.exe", "tasklist.exe", "sethc.exe",
    "sidebar.exe", "Chess.exe", "FreeCell.exe", "Hearts.exe",
    "Minesweeper.exe", "PurblePlace.exe", "Mahjong.exe",
    "SpiderSolitaire.exe", "bckgzm.exe", "chkrzm.exe", "shvlzm.exe",
    "Solitaire.exe", "winmine.exe", "Magnify.exe", "QQPCTray.exe",
]


def unlock_system_programs():
    """一键解禁系统程序（注册表清除）。返回解禁清单。"""
    done = []

    # 置零 DWORD/删除值
    for root, sub, items in UNLOCK_PATHS:
        h = w.reg_open(root, sub, w.KEY_QUERY_VALUE | w.KEY_SET_VALUE,
                       w.KEY_WOW64_32KEY if root == w.HKEY_LOCAL_MACHINE
                       else 0)
        if not h:
            continue
        for name, desc, mode in items:
            r = w.reg_query_value(h, name)
            if r is None:
                continue
            if mode == "dw4" and r[0] == "dw" and r[1] == 4:
                import struct
                zero = ctypes.create_string_buffer(struct.pack("<I", 3))
                # 3 = SERVICE_DEMAND_START（解除禁用）
                rc = advapi32_set_dword(h, name, 3)
                if rc == 0:
                    done.append(desc)
            elif mode == "v" and r[0] in ("dw", "sz") and r[1] not in (0, ""):
                rc = advapi32_set_dword(h, name, 0)
                if rc == 0:
                    done.append(desc)
        w.reg_close(h)

    # 删除值（IFEO debugger 劫持、策略）
    for root, sub, items in UNLOCK_DELETE_PATHS:
        h = w.reg_open(root, sub, w.KEY_SET_VALUE,
                       w.KEY_WOW64_32KEY if root == w.HKEY_LOCAL_MACHINE
                       else 0)
        if not h:
            continue
        for name, desc in items:
            if w.reg_delete_value(h, name):
                done.append(desc)
        w.reg_close(h)

    # IFEO debugger 劫持批量清除
    for exe in UNLOCK_IFEO:
        sub = ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\"
               "Image File Execution Options\\" + exe)
        h = w.reg_open(w.HKEY_LOCAL_MACHINE, sub, w.KEY_SET_VALUE,
                       w.KEY_WOW64_32KEY)
        if not h:
            continue
        if w.reg_delete_value(h, "debugger"):
            done.append(exe + " (IFEO 劫持)")
        w.reg_close(h)

    return {"ok": True, "unlocked": done, "count": len(done)}


def advapi32_set_dword(hkey, name, value):
    """写 REG_DWORD。返回错误码。"""
    import struct
    buf = ctypes.create_string_buffer(struct.pack("<I", value))
    return w.advapi32.RegSetValueExW(
        hkey, name, 0, w.REG_DWORD, buf, 4)


# ---------------------------------------------------------------- 动态密码计算器
def calc_temp_password(year, month, day, computer_name="X"):
    """学生机房管理助手临时密码（v9.x ~ v12.0）。

    算法（MythwareToolkit psd.cpp / README）：
      - 10.1 前: '8' + 16*(年*91+月*13+日*57)
      - 10.x  : 上面结果 +11
      - 11.0x : 年*789 + 月*123 + 日*456 + 111
      - 11.06~12.0: (月*159 + 日*357 + 计算机名末位ASCII*258) 转 7 进制
    """
    if not computer_name:
        computer_name = "X"
    last_char = computer_name[-1]
    base = 16 * (year * 91 + month * 13 + day * 57)

    def to_base7(n):
        if n == 0:
            return "0"
        digits = []
        while n:
            digits.append(str(n % 7))
            n //= 7
        return "".join(reversed(digits))

    v92 = "8" + str(base)                    # 9.2 ~ 10.0
    v10x = "8" + str(base + 11)              # 10.0 ~ 11.0
    v110x = str(year * 789 + month * 123 + day * 456 + 111)
    v120 = to_base7(month * 159 + day * 357 + ord(last_char) * 258)

    return {
        "9x-10.0": v92,
        "10.x": v10x,
        "11.0x": v110x,
        "11.06-12.0": v120,
    }


# ---------------------------------------------------------------- 机房助手
def kill_student_assistant():
    """杀学生机房管理助手（zmserv + 随机进程名逻辑）。

    随机进程名生成（MythwareToolkit assistant.cpp，VB 随机数模拟）。
    """
    results = []
    # zmserv 服务与进程
    scm = w.open_sc_manager(w.SC_MANAGER_ALL_ACCESS)
    if scm:
        hsvc = w.open_service(scm, "zmserv", w.SERVICE_STOP)
        if hsvc:
            ok, _ = w.control_service(hsvc, 1)
            results.append({"name": "zmserv 服务", "ok": bool(ok)})
            w.close_service_handle(hsvc)
        w.close_service_handle(scm)
    for name in ("zmserv.exe", "prozs.exe", "przs.exe", "jfglzs.exe",
                 "jfglzsp.exe", "jfglzsn.exe", "zmserv.exe"):
        for pid in w.find_processes(name):
            ok, _ = w.terminate_pid(pid)
            results.append({"name": name, "pid": pid, "ok": ok})

    # 读取版本号
    version = ""
    h = w.reg_open(w.HKEY_LOCAL_MACHINE,
                   "SOFTWARE\\WOW6432Node\\ZM软件工作室\\学生机房管理助手",
                   w.KEY_QUERY_VALUE, w.KEY_WOW64_32KEY)
    if h:
        r = w.reg_query_value(h, "Version")
        if r and r[0] == "sz":
            version = r[1]
        w.reg_close(h)

    # 按版本生成随机进程名（简单近似：只覆盖常见后缀命名）
    candidates = ["prozs.exe"]
    if version:
        m = re.match(r"(\d+)\.(\d+)", version)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            import datetime
            now = datetime.datetime.now()

            def vb_rnd(seed):
                # VB6 Rnd() 的一种近似（同 globals.h VBRandomEngine）
                seed = (int(seed) * 1140671485 + 12820163) & 0xFFFFFF
                return seed / 16777216.0

            if major == 9 or (major == 10 and minor < 6):
                # 5 位随机小写字母进程名
                num = int(vb_rnd(now.month * now.day) * 300000 + 1)
                name = "".join(chr(107 + int(str(num)[i])) for i in
                               range(min(5, len(str(num)))))
                candidates.append(name + ".exe")
    for cand in candidates:
        for pid in w.find_processes(cand):
            ok, _ = w.terminate_pid(pid)
            results.append({"name": cand, "pid": pid, "ok": ok})

    return {"ok": True, "version": version, "results": results}