"""内置系统 API（宿主自带）：只读系统信息 + 需审批的管理类接口。"""
from .. import config
from ..log import info
from .router import Handler, require_args


def register(ctx):
    handlers = ctx.registry.setdefault(config.BUILTIN_PLUGIN_ID, {})
    bridge = ctx.bridge

    def add(name, fn, permission=config.API_READONLY, description=""):
        handlers[name] = Handler(name, fn, permission, description)

    # ---------------- 只读：系统信息 ---------------- 

    def sys_info(args, c):
        return bridge.sysinfo()
    add("info", sys_info, config.API_READONLY, "完整系统信息快照")

    def sys_summary(args, c):
        s = bridge.sysinfo()
        return {
            "osName": s.get("osName"),
            "osVersion": s.get("osVersion"),
            "cpuName": s.get("cpuName"),
            "cores": s.get("cores"),
            "logicalCores": s.get("logicalCores"),
            "cpuUsage": s.get("cpuUsage"),
            "memoryTotal": s.get("memoryTotal"),
            "memoryFree": s.get("memoryFree"),
            "memoryUsed": s.get("memoryUsed"),
            "uptimeSec": s.get("uptimeSec"),
            "machineName": s.get("machineName"),
            "userName": s.get("userName"),
            "processCount": s.get("processCount"),
        }
    add("summary", sys_summary, config.API_READONLY, "系统信息摘要")

    def sys_disks(args, c):
        return bridge.sysinfo().get("disks", [])
    add("disks", sys_disks, config.API_READONLY, "磁盘卷列表")

    def sys_machine_guid(args, c):
        return bridge.machine_guid()
    add("machine_guid", sys_machine_guid, config.API_READONLY, "机器 GUID")

    def sys_system_drive(args, c):
        return bridge.system_drive()
    add("system_drive", sys_system_drive, config.API_READONLY, "系统盘信息")

    def sys_is_elevated(args, c):
        return {"elevated": bridge.is_elevated() == 1}
    add("is_elevated", sys_is_elevated, config.API_READONLY, "当前是否管理员")

    # ---------------- 只读：扩展系统信息 ---------------- 

    def sys_extended(args, c):
        return bridge.extended()
    add("extended", sys_extended, config.API_READONLY,
        "扩展系统信息: BIOS/主板/GPU/电池/网络适配器/机器GUID")

    # ---------------- 只读：信息共享总线 ---------------- 

    def sys_share_list(args, c):
        return ctx.bus.list()
    add("share.list", sys_share_list, config.API_READONLY,
        "列出共享信息命名空间")

    def sys_share_get(args, c):
        require_args(args, "ns")
        it = ctx.bus.get(str(args["ns"]))
        if it is None:
            from .router import ApiError
            raise ApiError("not_found", f"共享信息不存在: {args['ns']}")
        return it
    add("share.get", sys_share_get, config.API_READONLY,
        "读取某命名空间的共享信息")

    def sys_refresh_share(args, c):
        """重新采集并发布系统信息到共享总线。"""
        try:
            s = bridge.sysinfo()
        except Exception as e:  # noqa: BLE001
            from .router import ApiError
            raise ApiError("collect_error", f"采集失败: {e}")
        ctx.bus.publish(config.BUILTIN_PLUGIN_ID, "system.info", s)
        ctx.bus.publish(config.BUILTIN_PLUGIN_ID, "system.summary", {
            "osName": s.get("osName"), "osVersion": s.get("osVersion"),
            "cpuName": s.get("cpuName"), "cores": s.get("cores"),
            "logicalCores": s.get("logicalCores"),
            "memoryTotal": s.get("memoryTotal"),
            "memoryFree": s.get("memoryFree"),
            "uptimeSec": s.get("uptimeSec"),
        })
        ctx.bus.publish(config.BUILTIN_PLUGIN_ID, "system.extended",
                        bridge.extended())
        return {"published": [x["ns"] for x in ctx.bus.list()]}
    add("share.refresh", sys_refresh_share, config.API_READONLY,
        "重新发布系统信息到共享总线")

    # ---------------- 只读：插件 / 快捷键概览 ---------------- 

    def sys_plugins(args, c):
        from ..plugin.model import manifest_to_dict
        return {
            "total": len(ctx.manifests),
            "enabled": sum(1 for m in ctx.manifests.values()
                           if m.status == "ok"),
            "disabled": sum(1 for m in ctx.manifests.values()
                            if m.status == "disabled"),
            "conflicts": [{"id": x.get("id"), "topbar": x.get("topbar")}
                          for x in ctx.conflicts],
            "plugins": [manifest_to_dict(m) for m in ctx.manifests.values()],
        }
    add("plugins", sys_plugins, config.API_READONLY, "插件概览")

    def sys_shortcuts(args, c):
        return {"shortcuts": ctx.shortcuts.list_active(ctx._perm_lookup)}
    add("shortcuts", sys_shortcuts, config.API_READONLY,
        "当前生效的全局快捷键列表")

    # ---------------- 信息API（软件本体内置） ----------------

    def sys_infoapi(args, c):
        """组合：扩展系统信息 + 共享摘要。"""
        ext = bridge.extended()
        summary = ctx.bus.get("system.summary")
        return {
            "extended": ext,
            "sharedSummary": (summary or {}).get("data"),
            "shares": [x["ns"] for x in ctx.bus.list()],
        }
    add("infoapi", sys_infoapi, config.API_READONLY,
        "信息API: 扩展系统信息 + 共享总线摘要")

    # ---------------- 管理类：文件读取 / 操作 ---------------- 

    def _safe_text(data: bytes, max_len=65536):
        text = data[:max_len].decode("utf-8", "replace")
        return text, len(data) > max_len

    def file_read(args, c):
        require_args(args, "path")
        import os
        p = str(args["path"])
        if not os.path.isfile(p):
            raise ApiError("not_found", f"文件不存在: {p}", http=404)
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise ApiError("io_error", f"读取失败: {e}")
        text, truncated = _safe_text(raw)
        return {"path": p, "size": len(raw), "content": text,
                "truncated": truncated}
    add("file.read", file_read, config.API_ADMIN, "读取文本文件(≤64KB)")

    def file_write(args, c):
        require_args(args, "path")
        import os
        p = str(args["path"])
        content = str(args.get("content") or "")
        mode = "a" if args.get("append") else "w"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
            with open(p, mode, encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            raise ApiError("io_error", f"写入失败: {e}")
        return {"path": p, "written": len(content)}
    add("file.write", file_write, config.API_ADMIN, "写入文本文件(可追加)")

    def file_list(args, c):
        require_args(args, "path")
        import os
        p = str(args["path"])
        if not os.path.isdir(p):
            raise ApiError("not_found", f"目录不存在: {p}", http=404)
        try:
            names = sorted(os.listdir(p))
        except OSError as e:
            raise ApiError("io_error", f"列目录失败: {e}")
        out = []
        for n in names:
            fp = os.path.join(p, n)
            try:
                st = os.stat(fp)
                out.append({"name": n, "isDir": os.path.isdir(fp),
                            "size": st.st_size,
                            "mtime": int(st.st_mtime)})
            except OSError:
                continue
        return {"path": p, "entries": out}
    add("file.list", file_list, config.API_ADMIN, "列出目录内容")

    def file_delete(args, c):
        require_args(args, "path")
        import os
        p = str(args["path"])
        if not os.path.exists(p):
            raise ApiError("not_found", f"不存在: {p}", http=404)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                os.rmdir(p)          # 只删空目录，防误删
            else:
                os.remove(p)
        except OSError as e:
            raise ApiError("io_error", f"删除失败: {e}")
        return {"path": p, "deleted": True}
    add("file.delete", file_delete, config.API_ADMIN, "删除文件/空目录")

    def file_mkdir(args, c):
        require_args(args, "path")
        import os
        p = str(args["path"])
        try:
            os.makedirs(p, exist_ok=True)
        except OSError as e:
            raise ApiError("io_error", f"建目录失败: {e}")
        return {"path": p, "created": True}
    add("file.mkdir", file_mkdir, config.API_ADMIN, "创建目录(可多级)")

    # ---------------- 管理类（需插件权限审批） ---------------- 

    def sys_elevate_run(args, c):
        require_args(args, "file")
        rc = bridge.elevate_run(str(args["file"]),
                                str(args.get("args") or ""),
                                str(args.get("workdir") or ""))
        return {"started": rc == 0, "rc": rc}
    add("elevate_run", sys_elevate_run, config.API_ADMIN,
        "以管理员权限运行程序（触发 UAC）")

    def sys_relaunch_elevated(args, c):
        rc = bridge.elevate_run(
            args.get("file") or "",
            str(args.get("args") or ""),
            str(args.get("workdir") or ""))
        return {"started": rc == 0, "rc": rc}
    add("relaunch_elevated", sys_relaunch_elevated, config.API_ADMIN,
        "以管理员权限重新启动")

    # ---------------- 管理类：服务 / 驱动 ---------------- 

    def svc_list(args, c):
        return bridge.service_list()
    add("services.list", svc_list, config.API_ADMIN, "枚举服务与驱动")

    def svc_status(args, c):
        require_args(args, "name")
        return bridge.service_status(str(args["name"]))
    add("services.status", svc_status, config.API_ADMIN, "服务状态")

    def svc_start(args, c):
        require_args(args, "name")
        rc = bridge.service_start(str(args["name"]))
        return {"rc": rc, "started": rc == 0}
    add("services.start", svc_start, config.API_ADMIN, "启动服务")

    def svc_stop(args, c):
        require_args(args, "name")
        rc = bridge.service_stop(str(args["name"]))
        return {"rc": rc, "stopped": rc == 0}
    add("services.stop", svc_stop, config.API_ADMIN, "停止服务")

    def svc_set_start(args, c):
        require_args(args, "name", "startType")
        rc = bridge.service_set_start(str(args["name"]),
                                      int(args["startType"]))
        return {"rc": rc, "changed": rc == 0}
    add("services.set_start", svc_set_start, config.API_ADMIN,
        "设置服务启动类型(0-4)")

    def drv_status(args, c):
        require_args(args, "name")
        return bridge.driver_status(str(args["name"]))
    add("drivers.status", drv_status, config.API_ADMIN, "驱动状态")

    # ---------------- 管理类：高级操作（驱动双路径，需开启高级选项） ----------------

    def _advanced_on():
        return bool(ctx.store.get_host_setting("advanced", False))

    # 各操作必需参数（注册期捕获，避免依赖请求上下文对象）
    _ADV_REQUIRED = {
        "proc.kill": ("pid",), "proc.suspend": ("pid",),
        "proc.resume": ("pid",),
        "proc.start": ("path",),
        "mem.read": ("pid", "address", "size"),
        "mem.write": ("pid", "address", "data"),
        "file.read": ("path",), "file.write": ("path", "data"),
        "file.delete": ("path",), "file.mkdir": ("path",),
        "reg.read": ("hive", "subkey", "name"),
        "reg.write": ("hive", "subkey", "name", "type", "value"),
        "reg.delete": ("hive", "subkey"),
        "reg.list": ("hive", "subkey"),
        "gp.set": ("policy", "name", "type", "value"),
        "cmd.exec": ("path",),
        "token.elevate": (),
    }

    def _adv(op):
        required = _ADV_REQUIRED.get(op, ())

        def h(args, c):
            if not _advanced_on():
                from .router import ApiError
                raise ApiError("advanced_disabled",
                               "高级选项未开启：设置 → 更改设置 → 高级选项",
                               http=403)
            require_args(args, *required)
            from .. import adv
            return adv.run(op, args)
        h._adv_op = op
        return h
    for _op, _desc in [
        ("proc.list", "进程列表"),
        ("proc.kill", "结束进程(不调用 taskkill；有驱动则由内核 ZwTerminateProcess 完成)"),
        ("proc.suspend", "挂起进程(不调用 taskkill；驱动路径为逐线程 KeSuspendThread)"),
        ("proc.resume", "恢复进程(驱动路径为逐线程 KeResumeThread)"),
        ("proc.start", "启动进程(不调用 cmd；驱动路径经内核创建进程)"),
        ("mem.read", "读取进程内存(驱动路径 MmCopyVirtualMemory)"),
        ("mem.write", "写入进程内存(驱动路径 MmCopyVirtualMemory)"),
        ("file.read", "读取文件(驱动路径 Zw* 内核句柄)"),
        ("file.write", "写入文件(驱动路径 Zw* 内核句柄)"),
        ("file.delete", "删除文件/空目录(驱动路径 ZwDeleteFile)"),
        ("file.mkdir", "创建目录(驱动路径 ZwCreateFile)"),
        ("reg.read", "读取注册表(驱动路径 Zw* 注册表 API)"),
        ("reg.write", "写入注册表(驱动路径 ZwSetValueKey)"),
        ("reg.delete", "删除注册表值/键(驱动路径 ZwDeleteValueKey/Key)"),
        ("reg.list", "枚举注册表值(驱动路径 ZwEnumerateValueKey)"),
        ("gp.set", "写入组策略(注册表承载, 驱动路径 HKLM\\SOFTWARE\\Policies)"),
        ("cmd.exec", "以 SYSTEM/管理员令牌执行程序(不经过 cmd.exe)"),
        ("token.elevate", "通过驱动获取 SYSTEM 令牌(提到系统级)"),
    ]:
        add("adv." + _op, _adv(_op), config.API_ADMIN, _desc)

    info(f"内置系统 API 已注册: {len(handlers)} 个")