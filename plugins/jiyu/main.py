# -*- coding: utf-8 -*-
"""极域工具箱 (jiyu) 插件入口 — 合并自 极域对抗器(jytrainer) + 极域工具包(mythwaretoolkit)。

覆盖全部能力：
    广播窗口化/全屏化、密码读取、进程控制、驱动加载、DLL 注入、UDP 命令、
    SYSTEM 提权、退出黑屏、解网络/USB 限制、一键解禁、动态密码计算器、
    防杀/防截屏、机房助手处理。
"""
import threading
import time
from pathlib import Path

from jylib import win32hk as w
from jylib import toolkit
from jylib import jiyu, inject, driver as drv, udpattack

# 内置驱动二进制（插件自带 payload）
DRIVER_RES = Path(__file__).resolve().parent / "payload" / "JiYuTrainerDriver.sys"

_udp = udpattack.UdpAttack()


def _wintypes_hwnd(v):
    from ctypes import wintypes
    return wintypes.HWND(v)


def register(api):
    # ---------------- 只读：状态 ---------------

    @api.handler("status", permission="readonly",
                 description="极域状态：进程/版本/密码/广播/驱动/权限")
    def status(args, ctx):
        sm, err = None, None
        try:
            sm = jiyu.student_main_info()
        except Exception as e:  # noqa: BLE001
            err = str(e)
        pw, perr = jiyu.get_password()
        bws = jiyu.list_broadcast_windows()
        sys_ok = False
        try:
            w.enable_debug_privilege()
            hTok = w.system_token()
            sys_ok = hTok is not None
            if hTok:
                w.close_handle(hTok)
        except Exception:  # noqa: BLE001
            sys_ok = False
        return {
            "elevated": w.is_elevated(),
            "version": jiyu.read_version(),
            "studentMain": sm,
            "studentMainError": err,
            "passwordAvailable": pw is not None and not perr,
            "broadcastWindows": bws,
            "driver": drv.driver_status(),
            "hookDllReady": False,
            "processProtected": False,
            "systemTokenAvailable": sys_ok,
        }

    @api.handler("version", permission="readonly",
                 description="极域版本号")
    def version(args, ctx):
        return {"version": jiyu.read_version()}

    @api.handler("windows", permission="readonly",
                 description="列出所有极域相关窗口")
    def windows(args, ctx):
        pid, name = jiyu.find_student_main()
        out = []
        for hwnd in w.find_window_windows():
            if not hwnd["text"]:
                continue
            is_jy = hwnd["pid"] == pid or jiyu.is_gb_window_text(hwnd["text"])
            if is_jy or (args.get("all")):
                out.append(hwnd)
        return {"windows": out}

    @api.handler("password", permission="readonly",
                 description="读取极域解锁/卸载密码（注册表解密）")
    def password(args, ctx):
        pwd, err = jiyu.get_password(bool(args.get("force")))
        if err:
            return {"ok": False, "message": err}
        return {"ok": True, "password": pwd}

    # ---------------- 管理：广播窗口 ---------------

    @api.handler("broadcast.toggle", permission="admin",
                 description="切换广播窗口 全屏/窗口化")
    def bcast_toggle(args, ctx):
        return jiyu.toggle_broadcast_window(args.get("hwnd"))

    @api.handler("broadcast.windowed", permission="admin",
                 description="强制将广播窗口改为窗口模式")
    def bcast_windowed(args, ctx):
        return jiyu.set_broadcast_windowed(args.get("hwnd"))

    @api.handler("broadcast.fullscreen", permission="admin",
                 description="强制将广播窗口改为全屏模式")
    def bcast_full(args, ctx):
        return jiyu.set_broadcast_fullscreen(args.get("hwnd"))

    # ---------------- 管理：进程控制 ----------------

    @api.handler("process.kill", permission="admin",
                 description="强杀极域 StudentMain 进程")
    def proc_kill(args, ctx):
        return jiyu.kill_student_main()

    @api.handler("process.suspend", permission="admin",
                 description="挂起极域进程")
    def proc_suspend(args, ctx):
        return jiyu.suspend_student_main()

    @api.handler("process.resume", permission="admin",
                 description="恢复极域进程")
    def proc_resume(args, ctx):
        return jiyu.resume_student_main()

    @api.handler("process.start", permission="admin",
                 description="启动极域（从注册表路径，降权到登录用户）")
    def proc_start(args, ctx):
        pid, name = jiyu.find_student_main()
        if pid:
            return {"ok": True, "message": "极域已在运行", "pid": pid}
        h = w.reg_open(w.HKEY_LOCAL_MACHINE,
                       "SOFTWARE\\TopDomain\\e-Learning Class "
                       "Standard\\1.00",
                       w.KEY_QUERY_VALUE, w.KEY_WOW64_32KEY)
        if not h:
            return {"ok": False, "message": "未找到极域注册表路径"}
        r = w.reg_query_value(h, "TargetDirectory")
        w.reg_close(h)
        if not r or r[0] != "sz":
            return {"ok": False, "message": "未找到 TargetDirectory"}
        full = r[1].rstrip("\\") + "\\StudentMain.exe"
        pid = w.shell_runas(full) or w.launch_process(full)
        return {"ok": pid > 0, "message": f"已启动 {full}",
                "pid": pid} if pid else \
            {"ok": False, "message": "启动失败（可能需要管理员）"}

    @api.handler("process.kill_pid", permission="admin",
                 description="按 PID 强杀进程")
    def proc_kill_pid(args, ctx):
        pid = int(args.get("pid") or 0)
        if pid <= 0:
            return {"ok": False, "message": "缺少 pid"}
        ok, err = w.terminate_pid(pid)
        return {"ok": ok, "error": err if not ok else None}

    @api.handler("process.list", permission="readonly",
                 description="进程列表（模糊搜索）")
    def proc_list(args, ctx):
        kw = (args.get("name") or "").lower()
        out = []
        for p in w.list_processes():
            if not kw or kw in p["name"].lower():
                out.append(p)
        return {"processes": out}

    # ---------------- 管理：驱动 ---------------

    @api.handler("driver.load", permission="admin",
                 description="加载 JiYuTrainerDriver 内核驱动（仅 x86）")
    def driver_load(args, ctx):
        if not DRIVER_RES.is_file():
            return {"ok": False, "message": f"驱动文件不存在: {DRIVER_RES}"}
        return dict(zip(("ok", "message"), drv.load_driver(str(DRIVER_RES))))

    @api.handler("driver.unload", permission="admin",
                 description="卸载 JiYuTrainerDriver 驱动")
    def driver_unload(args, ctx):
        ok, msg = drv.unload_driver()
        return {"ok": ok, "message": msg}

    @api.handler("driver.status", permission="readonly",
                 description="驱动加载状态")
    def driver_status(args, ctx):
        return drv.driver_status()

    @api.handler("driver.force_kill", permission="admin",
                 description="驱动强杀进程（需先加载驱动）")
    def driver_force_kill(args, ctx):
        pid = int(args.get("pid") or 0)
        if pid <= 0:
            return {"ok": False, "message": "缺少 pid"}
        ok, status, _ = drv.force_kill(pid)
        return {"ok": bool(ok), "pid": pid, "statusHex":
                f"0x{status:08X}" if status is not None else None}

    # ---------------- 管理：DLL 注入 ---------------

    @api.handler("inject.dll", permission="admin",
                 description="向指定进程注入 DLL（VirtualAllocEx 远程线程）")
    def inject_dll(args, ctx):
        pid = int(args.get("pid") or 0)
        dll = str(args.get("dll") or "")
        if pid <= 0 or not dll:
            return {"ok": False, "message": "缺少 pid 或 dll 路径"}
        if not Path(dll).is_file():
            return {"ok": False, "message": f"DLL 不存在: {dll}"}
        ok, msg = inject.inject_dll(pid, dll)
        return {"ok": ok, "message": msg}

    @api.handler("inject.unload", permission="admin",
                 description="从进程卸载指定模块")
    def inject_unload(args, ctx):
        pid = int(args.get("pid") or 0)
        mod = str(args.get("module") or "")
        if pid <= 0 or not mod:
            return {"ok": False, "message": "缺少 pid 或 module"}
        ok, msg = inject.uninject_dll(pid, mod)
        return {"ok": ok, "message": msg}

    # ---------------- 管理：UDP 命令 ---------------

    @api.handler("udp.send", permission="admin",
                 description="发送 UDP 数据包（text/cmd/shutdown/reboot）到极域端口")
    def udp_send(args, ctx):
        ip = str(args.get("ip") or "").strip()
        port = int(args.get("port") or 0)
        kind = str(args.get("kind") or "text")
        payload = str(args.get("payload") or "")
        if not ip or port <= 0:
            return {"ok": False, "message": "缺少 ip/port"}
        if kind == "text":
            return _udp.send_text(ip, port, payload)
        if kind == "cmd":
            return _udp.send_command(ip, port, payload)
        if kind == "shutdown":
            return _udp.send_shutdown(ip, port)
        if kind == "reboot":
            return _udp.send_reboot(ip, port)
        return {"ok": False, "message": f"未知 kind: {kind}"}

    @api.handler("udp.packets", permission="readonly",
                 description="查看 UDP 基础数据包（供分析）")
    def udp_packets(args, ctx):
        return {
            "kinds": ["text", "cmd", "reboot", "shutdown"],
            "bufferSize": udpattack.SEND_BUFFER_SIZE,
            "packSize": udpattack.PACK_BUFFER_SIZE,
            "textOffset": udpattack.MSG_OFFSET,
            "cmdOffset": udpattack.CMD_OFFSET,
            "packets": [list(p) for p in udpattack.BASE_PACK],
        }

    # ---------------- 管理：提权 ---------------

    @api.handler("elevate.system", permission="admin",
                 description="复制 SYSTEM 令牌并运行命令行（需 SeDebugPrivilege）")
    def elevate_system(args, ctx):
        cmd = str(args.get("cmd") or "")
        if not cmd:
            return {"ok": False, "message": "缺少 cmd"}
        w.enable_debug_privilege()
        hTok = w.system_token()
        if not hTok:
            return {"ok": False, "message": "无法获取 SYSTEM 令牌"}
        try:
            pid = w.create_process_with_token(hTok, cmd)
            return {"ok": pid > 0, "pid": pid}
        finally:
            w.close_handle(hTok)

    @api.handler("elevate.check", permission="readonly",
                 description="当前权限与 SYSTEM 令牌可用性")
    def elevate_check(args, ctx):
        w.enable_debug_privilege()
        hTok = w.system_token()
        sys_ok = hTok is not None
        if hTok:
            w.close_handle(hTok)
        return {"elevated": w.is_elevated(), "debugPrivilege": True,
                "systemTokenAvailable": sys_ok}

    # ---------------- 退出黑屏 / 解限制 / 解禁 ----------------

    @api.handler("exit_black_screen", permission="admin",
                 description="退出黑屏安静（4 级递进）")
    def black(args, ctx):
        level = int(args.get("level") or 4)
        return toolkit.exit_black_screen(max_level=max(1, min(4, level)))

    @api.handler("unlock.network", permission="admin",
                 description="解除极域网络限制（TDNetFilter）")
    def net(args, ctx):
        return toolkit.remove_network_restrictions()

    @api.handler("unlock.usb", permission="admin",
                 description="解除极域 U 盘限制（soft/hard）")
    def usb(args, ctx):
        mode = str(args.get("mode") or "soft")
        if mode not in ("soft", "hard"):
            return {"ok": False, "message": "mode 必须为 soft|hard"}
        return toolkit.remove_usb_restrictions(mode)

    @api.handler("unlock.system", permission="admin",
                 description="一键解禁系统程序（注册表）")
    def unlock(args, ctx):
        return toolkit.unlock_system_programs()

    # ---------------- 密码计算器 ----------------

    @api.handler("psd.calc", permission="readonly",
                 description="学生机房管理助手临时密码计算器")
    def psd(args, ctx):
        year = int(args.get("year") or 0)
        month = int(args.get("month") or 0)
        day = int(args.get("day") or 0)
        name = str(args.get("computerName") or "")
        if not (year and month and day):
            import datetime
            now = datetime.datetime.now()
            year, month, day = now.year, now.month, now.day
        return toolkit.calc_temp_password(year, month, day, name)

    # ---------------- 防杀 / 防截屏 ----------------

    @api.handler("protect.self", permission="admin",
                 description="给当前进程 ACL 加 DENY 防杀")
    def protect_self(args, ctx):
        ok, msg = w.protect_current_process()
        return {"ok": ok, "message": msg}

    @api.handler("protect.screenshot", permission="admin",
                 description="对窗口启用/关闭防截屏 (WDA_EXCLUDEFROMCAPTURE)")
    def protect_screenshot(args, ctx):
        hwnd = int(args.get("hwnd") or 0)
        enable = bool(args.get("enable", True))
        if not hwnd:
            return {"ok": False, "message": "缺少 hwnd"}
        return {"ok": w.set_display_affinity(
            _wintypes_hwnd(hwnd),
            w.WDA_EXCLUDEFROMCAPTURE if enable else w.WDA_NONE)}

    # ---------------- 机房助手 ----------------

    @api.handler("assistant.kill", permission="admin",
                 description="杀掉学生机房管理助手（按版本随机进程名）")
    def asst(args, ctx):
        return toolkit.kill_student_assistant()

    # ---------------- 快捷键 ----------------

    @api.shortcut("bcast_toggle", "Ctrl+Alt+J",
                  "切换极域广播窗口 全屏/窗口化")
    def sc_bcast(evt, ctx):
        return jiyu.toggle_broadcast_window()

    @api.shortcut("black", "Ctrl+Alt+B", "退出黑屏安静")
    def sc_black(evt, ctx):
        return toolkit.exit_black_screen()

    @api.shortcut("class-stop", "Super+Alt+A",
                  "结束极域并删除驱动（需审批）")
    def sc_class_stop(evt, ctx):
        kill = jiyu.kill_student_main()
        dk, dmsg = drv.unload_driver()
        return {"kill": kill, "driver": {"ok": dk, "message": dmsg}}