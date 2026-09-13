# -*- coding: utf-8 -*-
"""希沃反制 (antiseewo) 插件入口。

基于 `学习\反编译\`（希沃易课堂学生端+教师端反编译静态报告 + refimpl）移植：
    黑屏退出、广播窗口化/全屏化、解除键鼠/网络/USB/HTTPS审计限制、
    防杀(ACL)、防截屏、持久化清理、卸载密码读取、状态检测。
"""
from sewow import win32hk as w
from sewow import seewo


def register(api):
    # ---------------- 只读：状态 ----------------

    @api.handler("status", permission="readonly",
                 description="希沃易课堂状态：进程/服务/黑屏/广播/持久化/权限")
    def status(args, ctx):
        try:
            st = seewo.seewo_status()
        except Exception as e:  # noqa: BLE001
            st = {"error": str(e)}
        st["elevated"] = w.is_elevated()
        return st

    @api.handler("processes", permission="readonly",
                 description="希沃相关进程列表")
    def processes(args, ctx):
        return {"processes": seewo.find_seewo_processes()}

    @api.handler("services", permission="readonly",
                 description="希沃相关驱动/守护服务状态")
    def services(args, ctx):
        return {"services": seewo.service_status()}

    @api.handler("password", permission="readonly",
                 description="读取卸载/配置密码（本地明文，尽力而为）")
    def password(args, ctx):
        return seewo.read_uninstall_password()

    # ---------------- 黑屏 ----------------

    @api.handler("black.detect", permission="readonly",
                 description="检测是否处于黑屏状态")
    def black_detect(args, ctx):
        return seewo.detect_black_screen()

    @api.handler("black.exit", permission="admin",
                 description="退出黑屏（停守护+关黑屏窗+解键盘过滤+停自防御）")
    def black_exit(args, ctx):
        return seewo.exit_black_screen()

    @api.handler("screen.restore", permission="admin",
                 description="恢复被隐藏的希沃窗口可见性")
    def screen_restore(args, ctx):
        return seewo.restore_screen_logical()

    # ---------------- 广播 ----------------

    @api.handler("broadcast.list", permission="readonly",
                 description="列出广播窗口")
    def bcast_list(args, ctx):
        return {"windows": seewo.find_broadcast_windows()}

    @api.handler("broadcast.windowed", permission="admin",
                 description="广播窗口化（去全屏 + 3/4 屏居中）")
    def bcast_win(args, ctx):
        return seewo.set_broadcast_windowed(args.get("hwnd"))

    @api.handler("broadcast.fullscreen", permission="admin",
                 description="广播窗口恢复全屏")
    def bcast_full(args, ctx):
        return seewo.set_broadcast_fullscreen(args.get("hwnd"))

    @api.handler("broadcast.toggle", permission="admin",
                 description="广播窗口 全屏/窗口化 切换")
    def bcast_toggle(args, ctx):
        return seewo.toggle_broadcast(args.get("hwnd"))

    # ---------------- 解除限制 ----------------

    @api.handler("unlock.network", permission="admin",
                 description="解除网络限制（停 SWSkyNet + HTTPS 审计驱动）")
    def un_net(args, ctx):
        return seewo.unlock_network()

    @api.handler("unlock.usb", permission="admin",
                 description="恢复 USB 存储（usbstor Start=3）")
    def un_usb(args, ctx):
        return seewo.unlock_usb()

    @api.handler("unlock.input", permission="admin",
                 description="解除键鼠锁定（停键盘过滤驱动）")
    def un_input(args, ctx):
        return seewo.unlock_input()

    # ---------------- 防杀 / 防截屏 ----------------

    @api.handler("protect.self", permission="admin",
                 description="给当前进程加 ACL DENY 防杀")
    def protect_self(args, ctx):
        return seewo.protect_self()

    @api.handler("screenshot.block", permission="admin",
                 description="防截屏：对指定 hwnd 或全部窗口禁捕获")
    def screenshot_block(args, ctx):
        return seewo.block_screenshot(args.get("hwnd") or 0)

    # ---------------- 持久化清理 ----------------

    @api.handler("persistence.list", permission="readonly",
                 description="定位持久化点（Run 键 + 服务）")
    def persist_list(args, ctx):
        return seewo.find_persistence()

    @api.handler("persistence.clean", permission="admin",
                 description="清理持久化（删 Run 自启 + 停管控服务）")
    def persist_clean(args, ctx):
        return seewo.clean_persistence()

    # ---------------- HTTPS 审计 / 系统加固残留清理 ----------------

    @api.handler("unlock.https", permission="admin",
                 description="解除 HTTPS 审计：停驱动 + 删 ZoroEx CA 根证书 + 删 ZoroCa 文件")
    def un_https(args, ctx):
        return seewo.unlock_https()

    @api.handler("safeboot.restore", permission="admin",
                 description="恢复安全模式引导项（反 disable_safeboot.bat）")
    def safeboot(args, ctx):
        return seewo.restore_safeboot()

    @api.handler("firewall.clean", permission="admin",
                 description="删除希沃创建的防火墙放行规则")
    def fw_clean(args, ctx):
        return seewo.clean_firewall()

    @api.handler("clipboard.stop", permission="admin",
                 description="停用剪贴板共享监控进程")
    def clip_stop(args, ctx):
        return seewo.stop_clipboard()

    # ---------------- 进程树全停 / 一键全解 ----------------

    @api.handler("process.kill_all", permission="admin",
                 description="完整停止希沃：守护→自防御→驱动→进程树→自启")
    def kill_all(args, ctx):
        return seewo.kill_all()

    @api.handler("unlock.all", permission="admin",
                 description="一键全解：进程树 + 全部驱动 + 网络/USB/键鼠 + HTTPS + 防火墙 + 安全模式")
    def unlock_all(args, ctx):
        return seewo.unlock_all()

    # ---------------- 快捷键 ----------------

    @api.shortcut("exit_black", "Ctrl+Alt+Shift+B",
                  "退出希沃黑屏")
    def sc_black(evt, ctx):
        return seewo.exit_black_screen()

    @api.shortcut("bcast_win", "Ctrl+Alt+Shift+W",
                  "希沃广播窗口化")
    def sc_bcast(evt, ctx):
        return seewo.set_broadcast_windowed()

    @api.shortcut("class-stop", "Super+Alt+D",
                  "结束希沃并删除驱动（需审批）")
    def sc_class_stop(evt, ctx):
        return seewo.kill_all()