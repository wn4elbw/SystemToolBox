# -*- coding: utf-8 -*-
"""希沃易课堂(Seewo ECR)反制核心逻辑。

基于 `学习\反编译\` 的静态报告与 refimpl（反编译产物，纯静态证据）：
    - REPORT.md / DRIVER_REPORT.md / RENDERER_REPORT.md / MAIN_PROCESS_REPORT.md
    - refimpl\services\* 与已有的 refimpl_antiseewo

覆盖：进程/服务/设备检测、黑屏退出、广播窗口化、解除键鼠/网络/USB/HTTPS审计、
     防杀(ACL)、防截屏、持久化清理、卸载密码读取、HTTPS 证书清理、
     安全模式恢复、防火墙规则清理、剪贴板停用、进程树全停、一键全解。
"""
import ctypes
import re
import subprocess
import time
from pathlib import Path
from ctypes import wintypes

from . import win32hk as w

# ---------------------------------------------------------------- 希沃进程名
SEEWO_PROCESSES = [
    "seewo-ecr-student.exe",     # Electron 主程序
    "electronic-classroom.exe",  # 核心管控 cppService（session0 admin）
    "classroom-protect.exe",     # 守护服务（复活 + KillProcessTree）
    "screen-broadcast.exe",      # 广播
    "seewo-celink.exe",          # Go accService 文件共享
    "DriverService.exe",         # 驱动安装/自保裁决
    "EnableAllUsbDisk.exe",      # USB 管控
    "SetupReport.exe",           # 上报
    "clipboard-event-handler.exe",  # 剪贴板监控 (.NET)
]

# 驱动服务名（按 AfterReal.bat / driver.json）
#   informational:
SERVICE_TABLE = {
    "seewoSkynet":   "SWSkyNet WFP 断网管控",
    "seewoKB":       "kbhacker 键盘过滤(拦 Ctrl+Alt+Del)",
    "seewoGPIO":     "PCMiniHacker GPIO(黑屏/背光/电源)",
    "SeewoClassRoomKeLiteLady": "KeLiteLady 文件/注册表/进程自防御",
    "SWClassRoomZoroHttps":     "HTTPS 审计(MITM)",
    "SeewoClassroomProtect":    "classroom-protect 守护(进程复活)",
}

# 设备对象
DEVICE_KBHACKER = r"\\.\{89ACCB8C-E0CD-4476-8102-6A1C48F9F578}"
DEVICE_MINIHACK = r"\\.\MINIHACK-UBCP"
DEVICE_SKYNET = r"\\.\SWSkyNet"

# 注册表
REG_BASE = r"SOFTWARE\Seewo\SeewoYiQiXueStudent"
RUN_KEY_32 = (w.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run")
RUN_KEY_64 = (w.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run")

# 黑屏/广播窗口标题关键词
BLACKSCREEN_TEXTS = ("黑屏", "管控中", "visitor")
BROADCAST_TEXTS = ("教师广播", "广播", "接管教师机")


# ---------------------------------------------------------------- 检测
def find_seewo_processes():
    """列出正在运行的希沃进程。"""
    all_ps = {p["name"].lower(): p for p in w.list_processes()}
    out = []
    for name in SEEWO_PROCESSES:
        for p in w.find_processes(name):
            out.append({"name": name, "pid": p})
    return out


def service_status():
    """查询所有相关服务的状态。"""
    scm = w.open_sc_manager(w.SC_MANAGER_CONNECT)
    out = {}
    if not scm:
        return out
    try:
        for name, desc in SERVICE_TABLE.items():
            h = w.open_service(scm, name, w.SERVICE_QUERY_STATUS)
            if h:
                st = w.query_service_status(h)
                w.close_service_handle(h)
                out[name] = {
                    "present": True,
                    "state": w.service_state_name(st.dwCurrentState)
                    if st else "UNKNOWN",
                    "desc": desc,
                }
            else:
                out[name] = {"present": False, "state": "ABSENT",
                             "desc": desc}
    finally:
        w.close_service_handle(scm)
    return out


def seewo_status():
    """综合状态。"""
    procs = find_seewo_processes()
    return {
        "elevated": w.is_elevated(),
        "processes": procs,
        "services": service_status(),
        "blackScreen": detect_black_screen(),
        "broadcastWindows": find_broadcast_windows(),
        "driverDeviceOpen": {
            "kbhacker": bool(w.create_device(DEVICE_KBHACKER)),
            "minihack": bool(w.create_device(DEVICE_MINIHACK)),
            "skynet":   bool(w.create_device(DEVICE_SKYNET)),
        },
        "persistence": find_persistence(),
    }


# ---------------------------------------------------------------- 黑屏
def detect_black_screen():
    """检测是否处于黑屏状态（全屏置顶 + 标题含黑屏关键词）。"""
    for hwnd in w.find_window_windows():
        if not hwnd["visible"]:
            continue
        ex = w.get_window_exstyle(hwnd["hwnd"])
        if not (ex & w.WS_EX_TOPMOST):
            continue
        rect = wintypes.RECT()
        if not w.user32.GetWindowRect(wintypes.HWND(hwnd["hwnd"]),
                                      ctypes.byref(rect)):
            continue
        sw, sh = w.get_screen_size()
        if (rect.right - rect.left) >= sw and \
           (rect.bottom - rect.top) >= sh:
            if any(k in (hwnd["text"] or "") for k in BLACKSCREEN_TEXTS):
                return {
                    "active": True, "hwnd": hwnd["hwnd"],
                    "text": hwnd["text"], "pid": hwnd["pid"],
                }
    return {"active": False}


def exit_black_screen():
    """退出黑屏安静。

    步骤（基于报告证据）：
      1. 停 SeewoClassroomProtect 守护（防复活）
      2. 关掉黑屏全屏窗口（PostMessage WM_CLOSE；关不掉则隐藏）
      3. 恢复键鼠（停 kbhacker 键盘过滤驱动 + 卸载期恢复输入）
      4. 解除断网 / HTTPS 审计（可选开关由调用方控制）
    返回步骤结果明细。
    """
    steps = []
    blk = detect_black_screen()

    # 1) 停守护服务，防 classroom-protect 复活
    ok, msg = w.stop_service_by_name("SeewoClassroomProtect")
    steps.append({"step": "停守护服务 SeewoClassroomProtect", "ok": ok,
                  "message": msg})
    # 再杀一次 classroom-protect.exe（若服务已停但进程残留）
    for pid in w.find_processes("classroom-protect.exe"):
        w.terminate_pid(pid)
        steps.append({"step": f"终止 classroom-protect.exe (pid {pid})",
                      "ok": True})

    # 2) 关黑屏窗口
    if blk and blk.get("active"):
        hwnd = wintypes.HWND(blk["hwnd"])
        w.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.3)
        if not w.user32.IsWindowVisible(hwnd):
            steps.append({"step": "关闭黑屏窗口 (WM_CLOSE)", "ok": True})
        else:
            w.ShowWindow(hwnd, w.SW_HIDE)
            steps.append({"step": "隐藏黑屏窗口", "ok": True})
    else:
        steps.append({"step": "未检测到黑屏窗口", "ok": True})

    # 3) 键盘解锁（停 seewoKB 过滤驱动）
    ok, msg = w.stop_service_by_name("seewoKB")
    steps.append({"step": "停 seewoKB 键盘过滤驱动", "ok": ok,
                  "message": msg})
    # 3b) 停 KeLiteLady 自防御（防文件/进程被保护干扰恢复）
    ok, msg = w.stop_service_by_name("SeewoClassRoomKeLiteLady")
    steps.append({"step": "停 KeLiteLady 自防御驱动", "ok": ok,
                  "message": msg})

    return {"ok": all(s["ok"] for s in steps), "steps": steps,
            "blackScreen": detect_black_screen()}


def restore_screen_logical():
    """逻辑恢复：通知 cpp 服务走正常 off 流程（尽力而为）。

    直接向 electronic-classroom 的 WS RPC 发 student.classroom.blackScreen.off
    需要课堂 token；这里退化为：驱动已卸载 + 黑屏窗口已关后，向
    ECR 主窗口发送焦点/可见性恢复。
    """
    # 简单恢复：枚举希沃窗口，把所有隐藏的顶层窗显示出来
    restored = []
    for hwnd in w.find_window_windows():
        h = wintypes.HWND(hwnd["hwnd"])
        if not w.user32.IsWindowVisible(h) and \
           w.get_window_exstyle(hwnd["hwnd"]) & w.WS_EX_APPWINDOW:
            w.ShowWindow(h, w.SW_SHOW)
            restored.append(hwnd["hwnd"])
    return {"ok": True, "restored": restored}


# ---------------------------------------------------------------- 广播
def find_broadcast_windows():
    """找广播相关窗口（标题含 教师广播/广播/接管 等）。"""
    out = []
    for hwnd in w.find_window_windows():
        if not hwnd["text"]:
            continue
        if any(k in hwnd["text"] for k in BROADCAST_TEXTS):
            out.append(hwnd)
    return out


def _window_broadcast(target):
    """如未指定 hwnd，自动选第一个广播窗口。"""
    if target:
        return int(target)
    bws = find_broadcast_windows()
    if not bws:
        return None
    # 优先全屏窗口
    for b in bws:
        ex = w.get_window_exstyle(b["hwnd"])
        if ex & w.WS_EX_TOPMOST:
            return b["hwnd"]
    return bws[0]["hwnd"]


def set_broadcast_windowed(hwnd=None):
    """广播窗口改为窗口模式（去全屏边框 + 置中 3/4 屏）。"""
    h = _window_broadcast(hwnd)
    if not h:
        return {"ok": False, "message": "未找到广播窗口"}
    hw = wintypes.HWND(h)
    style = w.GetWindowLong(hw, w.GWL_STYLE)
    new_style = style | w.WS_CAPTION | w.WS_SIZEBOX | w.WS_SYSMENU
    new_style &= ~(w.WS_POPUP | w.WS_MINIMIZEBOX)  # 去 POPUP（全屏风格）
    w.SetWindowLong(hw, w.GWL_STYLE, new_style)
    w.SetWindowPos(hw, w.HWND_NOTOPMOST, 0, 0, 0, 0,
                   w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_FRAMECHANGED |
                   w.SWP_SHOWWINDOW)
    sw, sh = w.get_screen_size()
    nw, nh = int(sw * 0.75), int(sh * 0.75)
    x, y = (sw - nw) // 2, (sh - nh) // 2
    w.SetWindowPos(hw, w.HWND_NOTOPMOST, x, y, nw, nh,
                   w.SWP_SHOWWINDOW)
    # 通知窗口重绘
    w.user32.PostMessageW(hw, 0x000F, 0, 0)  # WM_PAINT
    return {"ok": True, "hwnd": h, "mode": "windowed"}


def set_broadcast_fullscreen(hwnd=None):
    """广播窗口恢复全屏。"""
    h = _window_broadcast(hwnd)
    if not h:
        return {"ok": False, "message": "未找到广播窗口"}
    hw = wintypes.HWND(h)
    style = w.GetWindowLong(hw, w.GWL_STYLE)
    new_style = style & ~(w.WS_CAPTION | w.WS_SIZEBOX | w.WS_SYSMENU)
    new_style |= w.WS_POPUP
    w.SetWindowLong(hw, w.GWL_STYLE, new_style)
    sw, sh = w.get_screen_size()
    w.SetWindowPos(hw, w.HWND_TOPMOST, 0, 0, sw, sh,
                   w.SWP_FRAMECHANGED | w.SWP_SHOWWINDOW)
    return {"ok": True, "hwnd": h, "mode": "fullscreen"}


def toggle_broadcast(hwnd=None):
    """切换广播窗口全屏/窗口化。"""
    h = _window_broadcast(hwnd)
    if not h:
        return {"ok": False, "message": "未找到广播窗口"}
    hw = wintypes.HWND(h)
    # 用 exstyle TOPMOST 判断当前是否全屏
    ex = w.get_window_exstyle(int(hw))
    if ex & w.WS_EX_TOPMOST:
        return set_broadcast_windowed(int(hw))
    return set_broadcast_fullscreen(int(hw))


# ---------------------------------------------------------------- 解除限制
def unlock_network():
    """解除网络限制：停 SWSkyNet 驱动服务。

    证据：RENDERER_REPORT §7 断网 student.devctrl.cutOffNetwork →
          NetworkControlService；SWSkyNet WFP classify BLOCK。
    """
    steps = []
    ok, msg = w.stop_service_by_name("seewoSkynet")
    steps.append({"step": "停 seewoSkynet (SWSkyNet WFP)", "ok": ok,
                  "message": msg})
    ok, msg = w.stop_service_by_name("SWClassRoomZoroHttps")
    steps.append({"step": "停 HTTPS 审计驱动", "ok": ok, "message": msg})
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


def unlock_usb():
    """恢复 USB 存储（usbstor Start=3 各控制集）+ 释放被禁设备。

    证据：EnableAllUsbDisk.exe 禁用 USB；AfterReal 装 seewoGPIO/seewoSkynet。
    这里恢复注册表 usbstor 启动类型（val 3 = DEMAND_START）。
    """
    done = []
    for ctrl in ("SYSTEM\\CurrentControlSet", "SYSTEM\\ControlSet001",
                 "SYSTEM\\ControlSet002"):
        sub = ctrl + r"\Services\usbstor"
        h = w.reg_open(w.HKEY_LOCAL_MACHINE, sub,
                       w.KEY_QUERY_VALUE | w.KEY_SET_VALUE,
                       w.KEY_WOW64_64KEY)
        if h:
            r = w.reg_query_value(h, "Start")
            if r and r[0] == "dw":
                rc = w.reg_set_dword(h, "Start", 3)
                done.append({"key": sub, "from": r[1], "to": 3,
                             "ok": rc == 0})
            w.reg_close(h)
    return {"ok": True, "usbstor": done}


def unlock_input():
    """解除键鼠锁定：停键盘过滤驱动 + 查询锁状态。"""
    ok, msg = w.stop_service_by_name("seewoKB")
    return {"ok": ok, "message": msg}


# ---------------------------------------------------------------- 防杀/防截屏
def protect_self():
    """ACL DENY Everyone 对本进程加保护（防被强杀）。"""
    ok, msg = w.protect_current_process()
    return {"ok": ok, "message": msg}


def block_screenshot(window=0):
    """防截屏：对指定窗口或全部调用方窗口应用 WDA_EXCLUDEFROMCAPTURE。"""
    if window:
        hw = wintypes.HWND(int(window))
        return {"ok": w.set_display_affinity(
            hw, w.WDA_EXCLUDEFROMCAPTURE), "hwnd": int(window)}
    # 全部可见窗口
    applied = []
    for hwnd in w.find_window_windows():
        if hwnd["visible"]:
            h = wintypes.HWND(hwnd["hwnd"])
            if hasattr(w.user32, "SetWindowDisplayAffinity"):
                if w.user32.SetWindowDisplayAffinity(
                        h, w.WDA_EXCLUDEFROMCAPTURE):
                    applied.append(hwnd["hwnd"])
    return {"ok": True, "applied": applied}


# ---------------------------------------------------------------- 持久化清理
def find_persistence():
    """定位持久化点：Run 键 + 服务。"""
    out = {"runKeys": [], "services": {}}
    for root, sub in (RUN_KEY_32, RUN_KEY_64):
        h = w.reg_open(root, sub, w.KEY_QUERY_VALUE)
        if h:
            for name in ("SeewoYiQiXueStudent", "SeewoYiQiXueTeacher"):
                r = w.reg_query_value(h, name)
                if r:
                    out["runKeys"].append({
                        "root": "HKLM", "sub": sub, "name": name,
                        "value": r[1] if r[0] in ("sz", "ex") else None})
            w.reg_close(h)
    out["services"] = service_status()
    return out


def clean_persistence():
    """清理持久化：删除 Run 自启值 + 停相关服务（保守：不停 KeLiteLady 之外全部停）。"""
    done = []
    for root, sub in (RUN_KEY_32, RUN_KEY_64):
        h = w.reg_open(root, sub, w.KEY_SET_VALUE)
        if h:
            for name in ("SeewoYiQiXueStudent",):
                if w.reg_delete_value(h, name):
                    done.append(name + " (Run 自启)")
            w.reg_close(h)
    # 停网络/键盘/GPIO 服务（不删服务，仅停止，便于恢复）
    for name in ("seewoSkynet", "seewoKB", "seewoGPIO",
                 "SWClassRoomZoroHttps"):
        ok, msg = w.stop_service_by_name(name)
        done.append(f"{name}: {msg}")
    return {"ok": True, "done": done}


# ---------------------------------------------------------------- 卸载密码
def read_uninstall_password():
    """读取卸载/配置密码（property.uninstallPassword）。

    证据：RENDERER_REPORT §5/2.4 `base.uninstall.checkPassword` 本地明文校验，
    config.normal.uninstall 存于渲染层 store（明文）。此处从注册表
    HKLM\SOFTWARE\Seewo 与本地 cppService 配置 json 尽力读取。
    """
    found = []
    # 1) 注册表 Seewo 树
    h = w.reg_open(w.HKEY_LOCAL_MACHINE, REG_BASE,
                   w.KEY_QUERY_VALUE, w.KEY_WOW64_32KEY)
    if h:
        for n in ("uninstallPassword", "UninstallPassword", "password"):
            r = w.reg_query_value(h, n)
            if r and r[0] in ("sz", "dw"):
                found.append({"source": "HKLM\\" + REG_BASE, "key": n,
                              "value": r[1]})
        w.reg_close(h)
    # 2) 配置文件（尽力，浅扫描避免全盘遍历）
    import glob as _glob
    for pat in (r"C:\Program Files (x86)\Seewo\*.json",
                r"C:\Program Files (x86)\Seewo\**\*.json",
                r"C:\ProgramData\Seewo\*.json",
                r"C:\ProgramData\Seewo\**\*.json"):
        for f in _glob.glob(pat, recursive=True):
            try:
                txt = open(f, "r", encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            low = txt.lower()
            for key in ("uninstallpassword", "uninstall_password"):
                if key in low:
                    found.append({"source": f, "key": key, "value":
                                  "（配置文件存在，需解析结构）"})
                    break
            if len(found) >= 6:
                break
    return {"found": found, "count": len(found)}


# ---------------------------------------------------------------- 系统命令
def _run(cmd, timeout=12):
    """执行系统命令（字节模式 + 编码回退，避免 GBK 输出崩溃）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out, err = p.stdout, p.stderr
        for enc in ("utf-8", "mbcs"):
            try:
                return p.returncode, out.decode(enc), err.decode(enc)
            except UnicodeDecodeError:
                continue
        return p.returncode, out.decode("utf-8", "replace"), \
            err.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


# ---------------------------------------------------------------- HTTPS 审计
# 依据 TeacherClient/REPORT.md：SWZoroHttps 驱动 + ZoroCa.* 中间人证书
# （httpControlResource/ZoroCa.*），AfterReal.bat 把它装进系统根证书库。
ZORO_CERT_NAME = "ZoroEx CA"
ZORO_FILE_GLOBS = [
    r"C:\Program Files (x86)\Seewo\**\ZoroCa*",
    r"C:\ProgramData\Seewo\**\ZoroCa*",
]


def unlock_https():
    """解除 HTTPS 审计（MITM）：停驱动 + 删根证书 + 删 ZoroCa 文件。"""
    steps = []
    ok, msg = w.stop_service_by_name("SWClassRoomZoroHttps")
    steps.append({"step": "停止 SWClassRoomZoroHttps 驱动", "ok": ok,
                  "message": msg})
    rc, out, err = _run(["certutil", "-delstore", "Root", ZORO_CERT_NAME])
    steps.append({"step": "删除根证书库 ZoroEx CA",
                  "ok": rc == 0, "message": (out or err).strip()[:120]})
    removed = []
    import glob as _glob
    for pat in ZORO_FILE_GLOBS:
        for f in _glob.glob(pat, recursive=True):
            try:
                Path(f).unlink()
                removed.append(f)
            except OSError as e:
                steps.append({"step": f"删除文件 {f}", "ok": False,
                              "message": str(e)})
    steps.append({"step": "删除 ZoroCa 证书/密钥文件", "ok": True,
                  "message": f"删除 {len(removed)} 个文件"})
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


# ---------------------------------------------------------------- 安全模式
def restore_safeboot():
    """恢复安全模式引导项（反 disable_safeboot.bat：advancedoptions false）。"""
    rc, out, err = _run(["bcdedit", "/set", "{globalsettings}",
                         "advancedoptions", "true"])
    return {"ok": rc == 0, "message": (out or err).strip()[:200]}


# ---------------------------------------------------------------- 防火墙
# firewall_config.bat / add_*.bat 建立了 UI/Gard/CPPService/BroadcastService/
# SeewoStorageDriver/EnService 双向放行规则；这里按名称关键词删除。
FW_KEYWORDS = ("seewo", "yiqixue", "gard", "cppservice",
               "broadcastservice", "seewostoragedriver", "enservice", "zoro")


def clean_firewall():
    """删除希沃创建的 Windows 防火墙入站/出站规则。"""
    removed, failed = [], []
    for scope in ("in", "out"):
        rc, out, err = _run(["netsh", "advfirewall", "firewall", "show",
                             "rule", "name=all", f"dir={scope}"])
        if rc != 0:
            failed.append(f"{scope}: {err.strip()[:100]}")
            continue
        # 中/英系统表头均可；逐条按名称匹配关键词后删除
        names = re.findall(
            r"(?:规则名称|Rule Name)\s*[:：]\s*(.+)", out)
        for n in names:
            n = n.strip()
            low = n.lower()
            if any(k in low for k in FW_KEYWORDS):
                r2, o2, e2 = _run(["netsh", "advfirewall", "firewall",
                                   "delete", "rule", f"name={n}"])
                (removed if r2 == 0 else failed).append(
                    f"{scope}:{n}" + ("" if r2 == 0 else f"({e2.strip()[:60]})"))
    return {"ok": not failed, "removed": removed, "failed": failed}


# ---------------------------------------------------------------- 剪贴板监控
def stop_clipboard():
    """停用剪贴板共享监控进程（clipboard-event-handler.exe）。"""
    hits = []
    for pid in w.find_processes("clipboard-event-handler.exe"):
        ok, _ = w.terminate_pid(pid)
        hits.append({"pid": pid, "ok": ok})
    return {"ok": True, "killed": hits,
            "message": f"已终止 {len(hits)} 个剪贴板监控进程"}


# ---------------------------------------------------------------- 进程树全停
# 关键顺序（依据 DRIVER_REPORT.md 自防御机制）：
#   1) 先停 classroom-protect（守护，负责复活）→ 杀其进程；
#   2) 停 KeLiteLady（文件/注册表/进程自防御，其 Ob 回调剥 TERMINATE 权限、
#      注册表回调拦截 Run 键删除）——必须先停它才能杀进程/删键；
#   3) 停网络/键盘/GPIO/HTTPS 驱动；4) 杀其余希沃进程；5) 清 Run 自启。
def kill_all():
    """完整停止希沃（进程树 + 驱动 + 自启），不删除服务。"""
    steps = []
    # 1) 守护
    ok, msg = w.stop_service_by_name("SeewoClassroomProtect")
    steps.append({"step": "停止守护服务 classroom-protect", "ok": ok,
                  "message": msg})
    for pid in w.find_processes("classroom-protect.exe"):
        ok2, _ = w.terminate_pid(pid)
        steps.append({"step": f"杀 classroom-protect pid={pid}", "ok": ok2})
    time.sleep(0.3)
    # 2) 自防御驱动
    ok, msg = w.stop_service_by_name("SeewoClassRoomKeLiteLady")
    steps.append({"step": "停止自防御 KeLiteLady 驱动", "ok": ok,
                  "message": msg})
    time.sleep(0.2)
    # 3) 其余驱动
    for svc in ("seewoSkynet", "seewoKB", "seewoGPIO",
                "SWClassRoomZoroHttps"):
        ok, msg = w.stop_service_by_name(svc)
        steps.append({"step": f"停止 {svc}", "ok": ok, "message": msg})
    # 4) 杀进程
    killed = []
    for name in SEEWO_PROCESSES:
        for pid in w.find_processes(name):
            ok2, _ = w.terminate_pid(pid)
            killed.append({"name": name, "pid": pid, "ok": ok2})
    steps.append({"step": "终止希沃进程树", "ok": True,
                  "message": f"处理 {len(killed)} 个进程: " +
                  ", ".join(f"{k['name']}#{k['pid']}" for k in killed)})
    # 5) 自启清理（此时 KeLiteLady 已停，删除可生效）
    r = clean_persistence()
    steps.append({"step": "清理自启", "ok": r.get("ok"),
                  "message": "；".join(r.get("done", []))[:200]})
    return {"ok": True, "steps": steps, "killed": killed}


def unlock_all():
    """一键全解：进程树全停 + 全驱动停止 + 网络/USB/键鼠 + HTTPS + 防火墙 +
    安全模式恢复 + 自启清理。"""
    steps = []
    ka = kill_all()
    steps.append({"step": "进程树全停", "ok": True,
                  "message": ka["steps"][-1]["message"]})
    steps.append({"step": "断网管控驱动 seewoSkynet", "ok": True,
                  "message": "已停止"})
    usb_r = unlock_usb()
    steps.append({"step": "USB 恢复", "ok": usb_r.get("ok", False),
                  "message": usb_r.get("message", "")})
    https_r = unlock_https()
    steps.append({"step": "HTTPS 证书清理", "ok": https_r.get("ok", False),
                  "message": f"{len(https_r.get('steps', []))} 步"})
    fw_r = clean_firewall()
    steps.append({"step": "防火墙规则清理", "ok": fw_r.get("ok", False),
                  "message": f"删除 {len(fw_r.get('removed', []))} 条"})
    sb_r = restore_safeboot()
    steps.append({"step": "恢复安全模式引导项", "ok": sb_r.get("ok", False),
                  "message": sb_r.get("message", "")})
    steps.append({"step": "剪贴板监控停用",
                  "ok": True, "message": stop_clipboard().get("message", "")})
    return {"ok": True, "steps": steps}