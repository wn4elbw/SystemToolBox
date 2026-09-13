"""进程管理插件 — 进程枚举 / 结束 / 挂起 / 恢复 / 启动。

实现：插件内直接调用 Win32 API（ctypes）：
- 枚举：CreateToolhelp32Snapshot + QueryFullProcessImageNameW + GetProcessMemoryInfo；
- 结束：OpenProcess(PROCESS_TERMINATE) + TerminateProcess（不调用 taskkill）；
- 挂起/恢复：NtSuspendProcess / NtResumeProcess；
- 启动：CreateProcessW（经 subprocess 传递命令行，不经过 cmd.exe）。

安全护栏：
- 禁止结束 pid<=4 的系统关键进程、本软件自身进程、后端进程（os.getpid()）；
- 结束/挂起/恢复/启动均为 admin 级接口，approval 插件首次调用需审批。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import threading

from app.api.router import ApiError

kernel32 = ctypes.windll.kernel32
ntdll = ctypes.windll.ntdll
psapi = ctypes.windll.psapi

HANDLE = ctypes.c_void_p
DWORD = wt.DWORD

TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_INFORMATION = 0x0200

kernel32.OpenProcess.restype = HANDLE
kernel32.OpenProcess.argtypes = [DWORD, ctypes.c_int, DWORD]
kernel32.TerminateProcess.argtypes = [HANDLE, ctypes.c_uint]
kernel32.QueryFullProcessImageNameW.argtypes = [
    HANDLE, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD)]
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.GetPriorityClass.argtypes = [HANDLE]
kernel32.GetPriorityClass.restype = DWORD
kernel32.SetPriorityClass.argtypes = [HANDLE, DWORD]
kernel32.SetPriorityClass.restype = ctypes.c_int
kernel32.GetProcessAffinityMask.argtypes = [HANDLE, ctypes.POINTER(
    ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
kernel32.GetProcessAffinityMask.restype = ctypes.c_int
kernel32.SetProcessAffinityMask.argtypes = [HANDLE, ctypes.c_size_t]
kernel32.SetProcessAffinityMask.restype = ctypes.c_int
ntdll.NtSuspendProcess.argtypes = [HANDLE]
ntdll.NtResumeProcess.argtypes = [HANDLE]

# 优先级类
IDLE_PRIORITY_CLASS = 0x00000040
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS = 0x00000080
REALTIME_PRIORITY_CLASS = 0x00000100
_PRIORITY_NAMES = {
    IDLE_PRIORITY_CLASS: "低",
    BELOW_NORMAL_PRIORITY_CLASS: "低于标准",
    NORMAL_PRIORITY_CLASS: "标准",
    ABOVE_NORMAL_PRIORITY_CLASS: "高于标准",
    HIGH_PRIORITY_CLASS: "高",
    REALTIME_PRIORITY_CLASS: "实时",
}
_PRIORITY_VALUES = {v: k for k, v in _PRIORITY_NAMES.items()}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD),
                ("th32ProcessID", DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", DWORD), ("cntThreads", DWORD),
                ("th32ParentProcessID", DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("PageFaultCount", DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


# 用独立的函数原型绑定 toolhelp 快照系列，避免与其他插件（如 winman）在
# 共享的 kernel32.Process32FirstW 上互相覆盖 argtypes（各自结构体类不同）
_SnapProto = ctypes.WINFUNCTYPE(ctypes.c_void_p, DWORD, DWORD)
_EntryProto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                 ctypes.POINTER(PROCESSENTRY32W))
CreateToolhelp32Snapshot = _SnapProto(("CreateToolhelp32Snapshot", kernel32))
Process32FirstW = _EntryProto(("Process32FirstW", kernel32))
Process32NextW = _EntryProto(("Process32NextW", kernel32))

# 注意：不要给共享的 kernel32.CreateToolhelp32Snapshot / Process32FirstW /
# Process32NextW 赋值 argtypes（其它插件如 jytrainer/mythwaretoolkit 直接调用它们
# 且结构体类不同，覆盖会互相破坏）；本模块一律使用上面的独立原型。
psapi.GetProcessMemoryInfo.restype = ctypes.c_int
psapi.GetProcessMemoryInfo.argtypes = [HANDLE, ctypes.POINTER(
    PROCESS_MEMORY_COUNTERS_EX), DWORD]


# ---------------- 枚举 ----------------

def _snapshot():
    snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == 0xFFFFFFFFFFFFFFFF:
        raise ApiError("io_error", "CreateToolhelp32Snapshot 失败")
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        out = []
        if Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                out.append({"pid": pe.th32ProcessID,
                            "parentPid": pe.th32ParentProcessID,
                            "threads": pe.cntThreads,
                            "name": pe.szExeFile})
                if not Process32NextW(snap, ctypes.byref(pe)):
                    break
        return out
    finally:
        kernel32.CloseHandle(snap)


def _open(pid, access):
    h = kernel32.OpenProcess(access, False, pid)
    if not h or h in (0, 0xFFFFFFFFFFFFFFFF):
        raise ApiError("not_found", f"无法打开进程 pid={pid}"
                       "（不存在或无权限）", http=404)
    return h


def _image_path(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h or h in (0, 0xFFFFFFFFFFFFFFFF):
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = DWORD(1024)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                 ctypes.byref(size))
        return buf.value if ok else None
    finally:
        kernel32.CloseHandle(h)


def _working_set(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                             False, pid)
    if not h or h in (0, 0xFFFFFFFFFFFFFFFF):
        return None
    try:
        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc),
                                          pmc.cb):
            return None
        return pmc.WorkingSetSize
    finally:
        kernel32.CloseHandle(h)


def _priority(pid):
    """读取进程优先级类，返回 (数值, 名称)。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h or h in (0, 0xFFFFFFFFFFFFFFFF):
        return None, None
    try:
        cls = kernel32.GetPriorityClass(h)
        return cls, _PRIORITY_NAMES.get(cls, "未知")
    finally:
        kernel32.CloseHandle(h)


def _affinity(pid):
    """读取进程 CPU 亲和性，返回 (mask, 总CPU数)。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h or h in (0, 0xFFFFFFFFFFFFFFFF):
        return None, 0
    try:
        mask = ctypes.c_size_t(0)
        sys_mask = ctypes.c_size_t(0)
        if not kernel32.GetProcessAffinityMask(h, ctypes.byref(mask),
                                               ctypes.byref(sys_mask)):
            return None, 0
        return mask.value, bin(sys_mask.value & 0xFFFFFFFFFFFFFFFF).count("1")
    finally:
        kernel32.CloseHandle(h)


# ---------------- 挂起跟踪 ----------------

_suspended = set()
_sus_lock = threading.RLock()


def _self_pids():
    """本软件相关进程 pid 集合（后端 + Electron 父进程链），禁止结束。"""
    own = {os.getpid()}
    name = "SystemToolBox"
    for p in _snapshot():
        if name.lower() in p["name"].lower():
            own.add(p["pid"])
    return own


# ---------------- 插件入口 ----------------

def register(api):
    @api.handler("list", permission="readonly",
                 description="进程列表（含父进程/线程数/优先级/CPU亲和性；full 时含路径与内存）")
    def proc_list(args, ctx):
        full = bool(args.get("full"))
        procs = _snapshot()
        with _sus_lock:
            susp = set(_suspended)
        for p in procs:
            p["suspended"] = p["pid"] in susp
            p["path"] = None
            p["mem"] = None
            p["priority"] = None
            p["priorityName"] = None
            p["affinity"] = None
            p["numCpus"] = None
        if full:
            for p in procs:
                p["path"] = _image_path(p["pid"])
                p["mem"] = _working_set(p["pid"])
                p["priority"], p["priorityName"] = _priority(p["pid"])
                p["affinity"], p["numCpus"] = _affinity(p["pid"])
        procs.sort(key=lambda x: x["pid"])
        return {"total": len(procs), "processes": procs}

    @api.handler("kill", permission="admin",
                 description="结束进程（需审批；保护系统关键进程与本软件）")
    def proc_kill(args, ctx):
        pid = int(args.get("pid") or 0)
        if pid <= 4:
            raise ApiError("denied", "禁止结束系统关键进程 (pid<=4)")
        if pid == os.getpid():
            raise ApiError("denied", "不能结束后端自身进程")
        if pid in _self_pids():
            raise ApiError("denied", "不能结束 SystemToolBox 自身进程")
        h = _open(pid, PROCESS_TERMINATE)
        try:
            if not kernel32.TerminateProcess(h, 0):
                raise ApiError("io_error", f"结束进程失败 pid={pid}，"
                               f"错误码 {ctypes.get_last_error()}")
        finally:
            kernel32.CloseHandle(h)
        with _sus_lock:
            _suspended.discard(pid)
        return {"pid": pid, "killed": True}

    @api.handler("suspend", permission="admin",
                 description="挂起进程（需审批）")
    def proc_suspend(args, ctx):
        pid = int(args.get("pid") or 0)
        h = _open(pid, PROCESS_SUSPEND_RESUME)
        try:
            st = ntdll.NtSuspendProcess(h)
            if st != 0:
                raise ApiError("io_error", f"挂起失败 pid={pid} "
                               f"status=0x{st:x}")
        finally:
            kernel32.CloseHandle(h)
        with _sus_lock:
            _suspended.add(pid)
        return {"pid": pid, "suspended": True}

    @api.handler("resume", permission="admin",
                 description="恢复被挂起的进程（需审批）")
    def proc_resume(args, ctx):
        pid = int(args.get("pid") or 0)
        h = _open(pid, PROCESS_SUSPEND_RESUME)
        try:
            st = ntdll.NtResumeProcess(h)
            if st != 0:
                raise ApiError("io_error", f"恢复失败 pid={pid} "
                               f"status=0x{st:x}")
        finally:
            kernel32.CloseHandle(h)
        with _sus_lock:
            _suspended.discard(pid)
        return {"pid": pid, "suspended": False}

    @api.handler("start", permission="admin",
                 description="启动新进程（需审批；不经过 cmd.exe）")
    def proc_start(args, ctx):
        path = str(args.get("path") or "").strip()
        if not path:
            raise ApiError("bad_args", "请填写程序路径")
        cmdline = '"' + path + '"'
        if args.get("args"):
            cmdline += " " + str(args["args"])
        workdir = str(args.get("workdir") or "") or None
        try:
            proc = subprocess.Popen(cmdline, shell=False, cwd=workdir)
        except OSError as e:
            raise ApiError("start_failed", f"启动失败: {e}")
        return {"pid": proc.pid, "path": path}

    @api.handler("priority", permission="admin",
                 description="设置进程优先级（需审批；low/normal/high 等）")
    def proc_priority(args, ctx):
        pid = int(args.get("pid") or 0)
        name = str(args.get("priority") or "").strip()
        value = _PRIORITY_VALUES.get(name)
        if value is None:
            raise ApiError("bad_args", "优先级须为: " +
                           "/".join(sorted(_PRIORITY_VALUES,
                                           key=lambda k: _PRIORITY_VALUES[k])))
        if pid == os.getpid():
            raise ApiError("denied", "不能修改后端自身进程优先级")
        h = _open(pid, PROCESS_SET_INFORMATION)
        try:
            if not kernel32.SetPriorityClass(h, value):
                raise ApiError("io_error", f"设置优先级失败 pid={pid}，"
                               f"错误码 {ctypes.get_last_error()}")
        finally:
            kernel32.CloseHandle(h)
        return {"pid": pid, "priority": name, "set": True}

    @api.handler("affinity", permission="admin",
                 description="设置进程 CPU 亲和性（需审批；mask 例 0xF=前4核）")
    def proc_affinity(args, ctx):
        pid = int(args.get("pid") or 0)
        try:
            mask = int(str(args.get("mask") or "0"), 0)
        except ValueError:
            raise ApiError("bad_args", "mask 必须是整数（如 0xF）")
        if mask <= 0:
            raise ApiError("bad_args", "mask 必须大于 0")
        if pid == os.getpid():
            raise ApiError("denied", "不能修改后端自身进程亲和性")
        # 校验 mask 不超出系统 CPU 范围
        _, ncpus = _affinity(pid)
        if ncpus and mask >> ncpus:
            raise ApiError("bad_args", f"mask 超出系统 CPU 范围（共 {ncpus} 核）")
        h = _open(pid, PROCESS_SET_INFORMATION)
        try:
            if not kernel32.SetProcessAffinityMask(h, mask):
                raise ApiError("io_error", f"设置亲和性失败 pid={pid}，"
                               f"错误码 {ctypes.get_last_error()}")
        finally:
            kernel32.CloseHandle(h)
        return {"pid": pid, "mask": mask, "set": True}

    api.log.info("procman 插件已注册: list/kill/suspend/resume/start/"
                 "priority/affinity")
