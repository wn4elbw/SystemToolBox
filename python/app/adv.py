"""高级操作（adv.*）：双路径执行器。

路径选择（由设置"高级选项" + 驱动就绪状态决定）：
- 驱动路径：内核驱动 stbdrv 经 DeviceIoControl 完成（进程/内存/文件/注册表/
  组策略/命令执行/令牌提权，不调用 cmd/taskkill）
- 直调路径：ctypes 直接调用 Win32 API（OpenProcess/TerminateProcess/
  NtSuspendProcess/ReadProcessMemory/winreg/CreateProcessW 等）

所有操作均要求调用方满足管理员 API 门控（config.API_ADMIN）。
"""
import ctypes
import ctypes.wintypes as wt
import threading

from . import config, driver
from .log import info, error as log_error
from .api.router import ApiError

# ---------------- 直调路径：Win32 声明 ----------------

kernel32 = ctypes.windll.kernel32
ntdll = ctypes.windll.ntdll
advapi32 = ctypes.windll.advapi32

# 部分 Python 发行版裁剪了 ctypes.use_last_error，导致 _last_error()
# 恒为 0（真实错误码丢失）。这里直接调用 GetLastError 获取真实错误码。
kernel32.GetLastError.restype = ctypes.c_ulong


def _last_error() -> int:
    """读取最近一次 Win32 调用的错误码（GetLastError）。"""
    return int(kernel32.GetLastError())

HANDLE = ctypes.c_void_p
DWORD = wt.DWORD
LPCWSTR = ctypes.c_wchar_p

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x2
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400

kernel32.OpenProcess.restype = HANDLE
kernel32.OpenProcess.argtypes = [DWORD, ctypes.c_int, DWORD]
kernel32.CreateProcessW.restype = ctypes.c_int
kernel32.CreateProcessW.argtypes = [
    LPCWSTR, LPCWSTR, LPVOID := ctypes.c_void_p, LPVOID, ctypes.c_int,
    DWORD, LPVOID, LPCWSTR,
    ctypes.c_void_p, ctypes.c_void_p]  # STARTUPINFO* / PROCESS_INFORMATION*
kernel32.ReadProcessMemory.argtypes = [HANDLE, LPVOID, LPVOID, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.argtypes = [HANDLE, LPVOID, LPVOID,
                                        ctypes.c_size_t,
                                        ctypes.POINTER(ctypes.c_size_t)]
kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
kernel32.WaitForSingleObject.restype = DWORD
kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
ntdll.NtSuspendProcess.argtypes = [HANDLE]
ntdll.NtResumeProcess.argtypes = [HANDLE]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD),
                ("th32ProcessID", DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", DWORD), ("cntThreads", DWORD),
                ("th32ParentProcessID", DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


kernel32.CreateToolhelp32Snapshot.restype = HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [DWORD, DWORD]
kernel32.Process32FirstW.restype = ctypes.c_int
kernel32.Process32FirstW.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = ctypes.c_int
kernel32.Process32NextW.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("lpReserved", LPCWSTR), ("lpDesktop", LPCWSTR),
                ("lpTitle", LPCWSTR), ("dwX", DWORD), ("dwY", DWORD),
                ("dwXSize", DWORD), ("dwYSize", DWORD),
                ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
                ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
                ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
                ("hStdError", HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


_lock = threading.RLock()


def _need_admin():
    if not driver.is_admin():
        raise ApiError("need_admin", "高级操作需要管理员权限运行本软件",
                       http=403)


def _driver_or_direct(op_name, fn_driver, fn_direct, *args, **kwargs):
    """按驱动就绪状态选择路径。fn_driver 失败且驱动不可用时回退直调。"""
    m = driver.manager
    if m.ready():
        try:
            r = fn_driver(*args, **kwargs)
            if r is not None:
                info(f"高级操作[{op_name}] 由内核驱动完成")
                return r
        except IOError as e:
            log_error(f"高级操作[{op_name}] 驱动路径失败，回退直调: {e}")
    return fn_direct(*args, **kwargs)


# ---------------- 进程 ----------------

def proc_list():
    """进程列表（toolhelp 快照）。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == 0xFFFFFFFFFFFFFFFF:
        raise ApiError("io_error", "CreateToolhelp32Snapshot 失败")
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        out = []
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                out.append({"pid": pe.th32ProcessID,
                            "parentPid": pe.th32ParentProcessID,
                            "threads": pe.cntThreads,
                            "name": pe.szExeFile})
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        return out
    finally:
        kernel32.CloseHandle(snap)


def _open_proc(pid, access):
    h = kernel32.OpenProcess(access, False, pid)
    if not h or h in (0, 0xFFFFFFFFFFFFFFFF):
        raise ApiError("not_found", f"无法打开进程 pid={pid}"
                       "（不存在或无权限）", http=404)
    return h


def proc_kill_direct(pid):
    _need_admin()
    pid = int(pid)
    if pid <= 4:
        raise ApiError("denied", "禁止结束系统关键进程 (pid<=4)")
    h = _open_proc(pid, PROCESS_TERMINATE)
    try:
        if not kernel32.TerminateProcess(h, 0):
            raise ApiError("io_error", f"结束进程失败 pid={pid}，"
                           f"错误码 {_last_error()}")
        return {"pid": pid, "killed": True, "via": "win32"}
    finally:
        kernel32.CloseHandle(h)


def proc_suspend_direct(pid, suspend):
    _need_admin()
    pid = int(pid)
    h = _open_proc(pid, PROCESS_SUSPEND_RESUME)
    try:
        fn = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
        st = fn(h)
        if st != 0:
            raise ApiError("io_error",
                           f"{'挂起' if suspend else '恢复'}进程失败 pid={pid}，"
                           f"status=0x{st:x}")
        return {"pid": pid, "suspended": bool(suspend), "via": "win32"}
    finally:
        kernel32.CloseHandle(h)


def proc_start_direct(path, args="", workdir=""):
    _need_admin()
    cmdline = '"' + path + '"'
    if args:
        cmdline += " " + args
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()
    ok = kernel32.CreateProcessW(path, cmdline, None, None, False,
                                 CREATE_NO_WINDOW, None,
                                 workdir or None,
                                 ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise ApiError("io_error", f"启动进程失败 {path}，"
                       f"错误码 {_last_error()}")
    pid = pi.dwProcessId
    kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(pi.hProcess)
    return {"pid": pid, "path": path, "via": "win32"}


# ---------------- 内存 ----------------

def mem_read_direct(pid, address, size):
    _need_admin()
    pid = int(pid)
    size = min(max(1, int(size)), driver.STB_MAX_DATA)
    h = _open_proc(pid, PROCESS_VM_READ | PROCESS_QUERY_INFORMATION)
    try:
        buf = ctypes.create_string_buffer(size)
        nread = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(h, ctypes.c_void_p(address), buf,
                                          size, ctypes.byref(nread)):
            raise ApiError("io_error", f"读内存失败 pid={pid} addr=0x{address:x}"
                           f" 错误码 {_last_error()}")
        return {"pid": pid, "address": address, "size": nread.value,
                "data": buf.raw[:nread.value].hex()}
    finally:
        kernel32.CloseHandle(h)


def mem_write_direct(pid, address, data_hex):
    _need_admin()
    pid = int(pid)
    try:
        raw = bytes.fromhex(data_hex)
    except ValueError:
        raise ApiError("bad_args", "data 需为十六进制字符串")
    if len(raw) > driver.STB_MAX_DATA:
        raise ApiError("bad_args", "data 超过 4KB 上限")
    h = _open_proc(pid, PROCESS_VM_WRITE | PROCESS_VM_OPERATION |
                   PROCESS_QUERY_INFORMATION)
    try:
        nw = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(h, ctypes.c_void_p(address),
                                           raw, len(raw), ctypes.byref(nw)):
            raise ApiError("io_error", f"写内存失败 pid={pid} addr=0x{address:x}"
                           f" 错误码 {_last_error()}")
        return {"pid": pid, "address": address, "written": nw.value}
    finally:
        kernel32.CloseHandle(h)


# ---------------- 注册表 ----------------

def _winreg_path(hive_name, subkey):
    import winreg
    root = driver.WINREG_SAM.get(str(hive_name).upper())
    if root is None:
        raise ApiError("bad_args", f"未知 hive: {hive_name}")
    return winreg, root


def reg_read_direct(hive, subkey, name):
    _need_admin()
    import winreg
    winreg, root = _winreg_path(hive, subkey)
    try:
        with winreg.OpenKey(root, subkey) as k:
            value, typ = winreg.QueryValueEx(k, name)
        return {"hive": hive, "subkey": subkey, "name": name,
                "type": _reg_type_name(typ),
                "value": _reg_value_to_str(value)}
    except FileNotFoundError:
        raise ApiError("not_found", f"注册表项不存在: {hive}\\{subkey}\\{name}",
                       http=404)
    except OSError as e:
        raise ApiError("io_error", f"读注册表失败: {e}")


def reg_write_direct(hive, subkey, name, type_, value):
    _need_admin()
    import winreg
    winreg, root = _winreg_path(hive, subkey)
    try:
        with winreg.CreateKeyEx(root, subkey,
                                access=winreg.KEY_SET_VALUE |
                                winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, name, 0, _reg_type_id(type_), value)
        return {"hive": hive, "subkey": subkey, "name": name, "written": True}
    except OSError as e:
        raise ApiError("io_error", f"写注册表失败: {e}")


def reg_delete_direct(hive, subkey, name=None):
    _need_admin()
    import winreg
    winreg, root = _winreg_path(hive, subkey)
    try:
        with winreg.OpenKey(root, subkey, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            if name:
                winreg.DeleteValue(k, name)
            else:
                winreg.DeleteKey(k, "")
        return {"hive": hive, "subkey": subkey, "name": name, "deleted": True}
    except FileNotFoundError:
        raise ApiError("not_found", "注册表项不存在", http=404)
    except OSError as e:
        raise ApiError("io_error", f"删注册表失败: {e}")


def reg_list_direct(hive, subkey):
    _need_admin()
    import winreg
    winreg, root = _winreg_path(hive, subkey)
    try:
        with winreg.OpenKey(root, subkey) as k:
            values, i = [], 0
            while True:
                try:
                    n, v, t = winreg.EnumValue(k, i)
                    values.append({"name": n, "type": _reg_type_name(t),
                                   "value": _reg_value_to_str(v)})
                    i += 1
                except OSError:
                    break
        return {"hive": hive, "subkey": subkey, "values": values}
    except FileNotFoundError:
        raise ApiError("not_found", f"注册表项不存在: {hive}\\{subkey}",
                       http=404)


def _reg_type_id(t):
    import winreg
    return {"string": winreg.REG_SZ, "expand": winreg.REG_EXPAND_SZ,
            "dword": winreg.REG_DWORD, "qword": winreg.REG_QWORD,
            "binary": winreg.REG_BINARY,
            "multi": winreg.REG_MULTI_SZ}.get(str(t).lower(),
                                              winreg.REG_SZ)


def _reg_type_name(t):
    import winreg
    return {winreg.REG_SZ: "string", winreg.REG_EXPAND_SZ: "expand",
            winreg.REG_DWORD: "dword", winreg.REG_QWORD: "qword",
            winreg.REG_BINARY: "binary",
            winreg.REG_MULTI_SZ: "multi"}.get(t, str(t))


def _reg_value_to_str(v):
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, (list, tuple)):
        return list(v)
    return v


# ---------------- 组策略（注册表承载） ----------------

def gp_set_direct(policy, name, type_, value):
    """写入 HKLM\\SOFTWARE\\Policies\\<policy>\\...（本地组策略的注册表形式）。"""
    _need_admin()
    return reg_write_direct("HKLM", f"SOFTWARE\\Policies\\{policy}",
                            name, type_, value)


# ---------------- 命令执行（不经过 cmd/taskkill） ----------------

def cmd_exec_direct(path, args="", workdir=""):
    """直接以当前(已提权)令牌 CreateProcessW 启动程序，无 cmd.exe 参与。"""
    _need_admin()
    return proc_start_direct(path, args, workdir)


# ---------------- 文件（直调：stdlib，需管理员可写目标） ----------------

def file_read_direct(path, size=driver.STB_MAX_DATA):
    _need_admin()
    import os
    p = str(path)
    if not os.path.isfile(p):
        raise ApiError("not_found", f"文件不存在: {p}", http=404)
    try:
        with open(p, "rb") as f:
            raw = f.read(max(1, min(int(size), driver.STB_MAX_DATA)))
        return {"path": p, "size": len(raw), "data": raw.hex(),
                "via": "win32"}
    except OSError as e:
        raise ApiError("io_error", f"读取失败: {e}")


def file_write_direct(path, data_hex, append=False):
    _need_admin()
    import os
    p = str(path)
    try:
        raw = bytes.fromhex(data_hex)
    except ValueError:
        raise ApiError("bad_args", "data 需为十六进制字符串")
    if len(raw) > driver.STB_MAX_DATA:
        raise ApiError("bad_args", "data 超过 4KB 上限")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "ab" if append else "wb") as f:
            f.write(raw)
        return {"path": p, "written": len(raw), "via": "win32"}
    except OSError as e:
        raise ApiError("io_error", f"写入失败: {e}")


def file_delete_direct(path):
    _need_admin()
    import os
    p = str(path)
    if not os.path.exists(p):
        raise ApiError("not_found", f"不存在: {p}", http=404)
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            os.rmdir(p)
        else:
            os.remove(p)
        return {"path": p, "deleted": True, "via": "win32"}
    except OSError as e:
        raise ApiError("io_error", f"删除失败: {e}")


def file_mkdir_direct(path):
    _need_admin()
    import os
    p = str(path)
    try:
        os.makedirs(p, exist_ok=True)
        return {"path": p, "created": True, "via": "win32"}
    except OSError as e:
        raise ApiError("io_error", f"建目录失败: {e}")


# ---------------- 驱动路径实现 ----------------

def _drv_run(code, req):
    m = driver.manager
    st = m.ioctl(code, req)
    if st != 0:
        raise ApiError("driver_error", f"驱动操作失败 status=0x{st:x}",
                       http=500)
    return req


def proc_kill_driver(pid):
    _need_admin()
    req = driver.STB_PROC_OP()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 1
    req.pid = int(pid)
    _drv_run(driver.IOCTL_STB_PROC_KILL, req)
    return {"pid": pid, "killed": True, "via": "driver"}


def proc_suspend_driver(pid, suspend):
    _need_admin()
    req = driver.STB_PROC_OP()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 2 if suspend else 3
    req.pid = int(pid)
    code = driver.IOCTL_STB_PROC_SUSPEND if suspend \
        else driver.IOCTL_STB_PROC_RESUME
    _drv_run(code, req)
    return {"pid": pid, "suspended": bool(suspend), "via": "driver"}


def proc_start_driver(path, args="", workdir=""):
    _need_admin()
    req = driver.STB_PROC_START()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 4
    req.path = str(path)
    req.args = str(args)
    req.workdir = str(workdir)
    req.ldrInitThunk = _ldr_init_thunk()
    _drv_run(driver.IOCTL_STB_PROC_START, req)
    return {"pid": req.pid, "path": path, "via": "driver"}


def mem_read_driver(pid, address, size):
    _need_admin()
    req = driver.STB_MEM_READ()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 5
    req.pid = int(pid)
    req.address = int(address)
    req.size = min(max(1, int(size)), driver.STB_MAX_DATA)
    _drv_run(driver.IOCTL_STB_MEM_READ, req)
    return {"pid": pid, "address": address, "size": req.size,
            "data": bytes(req.data[:req.size]).hex(), "via": "driver"}


def mem_write_driver(pid, address, data_hex):
    _need_admin()
    raw = bytes.fromhex(data_hex)
    if len(raw) > driver.STB_MAX_DATA:
        raise ApiError("bad_args", "data 超过 4KB 上限")
    req = driver.STB_MEM_WRITE()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 6
    req.pid = int(pid)
    req.address = int(address)
    req.size = len(raw)
    ctypes.memmove(req.data, raw, len(raw))
    _drv_run(driver.IOCTL_STB_MEM_WRITE, req)
    return {"pid": pid, "address": address, "written": len(raw),
            "via": "driver"}


def file_read_driver(path, size):
    _need_admin()
    req = driver.STB_FILE_READ()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 7
    req.path = str(path)
    req.size = min(max(1, int(size)), driver.STB_MAX_DATA)
    _drv_run(driver.IOCTL_STB_FILE_READ, req)
    return {"path": path, "size": req.size,
            "data": bytes(req.data[:req.size]).hex(), "via": "driver"}


def file_write_driver(path, data_hex, append=False):
    _need_admin()
    raw = bytes.fromhex(data_hex)
    if len(raw) > driver.STB_MAX_DATA:
        raise ApiError("bad_args", "data 超过 4KB 上限")
    req = driver.STB_FILE_WRITE()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 8
    req.path = str(path)
    req.flags = 1 if append else 0
    req.size = len(raw)
    ctypes.memmove(req.data, raw, len(raw))
    _drv_run(driver.IOCTL_STB_FILE_WRITE, req)
    return {"path": path, "written": len(raw), "via": "driver"}


def file_delete_driver(path):
    _need_admin()
    req = driver.STB_FILE_PATH()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 9
    req.path = str(path)
    _drv_run(driver.IOCTL_STB_FILE_DELETE, req)
    return {"path": path, "deleted": True, "via": "driver"}


def file_mkdir_driver(path):
    _need_admin()
    req = driver.STB_FILE_PATH()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 10
    req.path = str(path)
    _drv_run(driver.IOCTL_STB_FILE_MKDIR, req)
    return {"path": path, "created": True, "via": "driver"}


def reg_read_driver(hive, subkey, name):
    _need_admin()
    req = driver.STB_REG_OP()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 11
    req.hive = _hive_id(hive)
    req.subkey = str(subkey)
    req.name = str(name)
    _drv_run(driver.IOCTL_STB_REG_READ, req)
    return {"hive": hive, "subkey": subkey, "name": name,
            "type": _reg_type_name(req.type),
            "value": _reg_bytes(req.type, req.data, req.size),
            "via": "driver"}


def reg_write_driver(hive, subkey, name, type_, value):
    _need_admin()
    req = driver.STB_REG_OP()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 12
    req.hive = _hive_id(hive)
    req.subkey = str(subkey)
    req.name = str(name)
    req.type = _reg_type_id(type_)
    raw, size = _encode_reg_value(type_, value)
    req.size = size
    ctypes.memmove(req.data, raw, size)
    _drv_run(driver.IOCTL_STB_REG_WRITE, req)
    return {"hive": hive, "subkey": subkey, "name": name, "written": True,
            "via": "driver"}


def reg_delete_driver(hive, subkey, name=None):
    _need_admin()
    req = driver.STB_REG_OP()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 13
    req.hive = _hive_id(hive)
    req.subkey = str(subkey)
    req.name = str(name or "")
    _drv_run(driver.IOCTL_STB_REG_DELETE, req)
    return {"hive": hive, "subkey": subkey, "name": name, "deleted": True,
            "via": "driver"}


def reg_list_driver(hive, subkey):
    _need_admin()
    req = driver.STB_REG_OP()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 14
    req.hive = _hive_id(hive)
    req.subkey = str(subkey)
    _drv_run(driver.IOCTL_STB_REG_LIST, req)
    # 驱动返回 UTF-16 序列化值列表：count | name_len name | type | size data ...
    return _parse_reg_list(req.data, req.size, hive, subkey)


def gp_set_driver(policy, name, type_, value):
    _need_admin()
    req = driver.STB_POLICY_SET()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 15
    req.policyPath = "SOFTWARE\\Policies\\" + str(policy)
    req.name = str(name)
    req.type = _reg_type_id(type_)
    raw, size = _encode_reg_value(type_, value)
    req.size = size
    ctypes.memmove(req.data, raw, size)
    _drv_run(driver.IOCTL_STB_POLICY_SET, req)
    return {"policy": policy, "name": name, "written": True, "via": "driver"}


def cmd_exec_driver(path, args="", workdir=""):
    _need_admin()
    req = driver.STB_CMD_EXEC()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 16
    req.path = str(path)
    req.args = str(args)
    req.workdir = str(workdir)
    req.ldrInitThunk = _ldr_init_thunk()
    _drv_run(driver.IOCTL_STB_CMD_EXEC, req)
    return {"pid": req.pid, "path": path, "via": "driver"}


def token_elevate_driver():
    _need_admin()
    req = driver.STB_TOKEN_ELEVATE()
    req.hdr.magic = driver.STB_MAGIC
    req.hdr.op = 17
    _drv_run(driver.IOCTL_STB_TOKEN_ELEVATE, req)
    if req.token:
        # 令牌句柄保留给本进程使用（驱动已在客户端进程创建该句柄）
        info(f"已通过内核驱动取得 SYSTEM 令牌(来自 pid={req.pid})")
    return {"system": bool(req.token), "tokenPid": req.pid, "via": "driver"}


# ---------------- 工具 ----------------

def _hive_id(hive):
    h = str(hive).upper()
    if h not in driver.REG_HIVE.values():
        raise ApiError("bad_args", f"未知 hive: {hive}")
    return {v: k for k, v in driver.REG_HIVE.items()}[h]


def _ldr_init_thunk():
    """ntdll!LdrInitializeThunk 地址（驱动创建进程时作为初始线程入口）。"""
    return ctypes.cast(ntdll.LdrInitializeThunk, ctypes.c_void_p).value or 0


def _encode_reg_value(type_, value):
    import winreg
    t = _reg_type_id(type_)
    if t == winreg.REG_DWORD:
        raw = ctypes.c_uint32(int(value)).value.to_bytes(4, "little")
    elif t == winreg.REG_QWORD:
        raw = ctypes.c_uint64(int(value)).value.to_bytes(8, "little")
    elif t == winreg.REG_BINARY:
        raw = bytes.fromhex(str(value)) if isinstance(value, str) \
            else bytes(value)
    elif t == winreg.REG_MULTI_SZ:
        raw = ("\0".join(str(x) for x in value) + "\0\0").encode("utf-16-le")
    else:
        raw = (str(value) + "\0").encode("utf-16-le")
    return raw, len(raw)


def _reg_bytes(reg_type, data, size):
    import winreg
    raw = bytes(data[:size])
    t = {1: winreg.REG_SZ, 2: winreg.REG_EXPAND_SZ, 4: winreg.REG_DWORD,
         8: winreg.REG_QWORD, 3: winreg.REG_BINARY,
         7: winreg.REG_MULTI_SZ}.get(reg_type)
    if t == winreg.REG_DWORD:
        return int.from_bytes(raw[:4], "little")
    if t == winreg.REG_QWORD:
        return int.from_bytes(raw[:8], "little")
    if t == winreg.REG_BINARY:
        return raw.hex()
    if t == winreg.REG_MULTI_SZ:
        return raw.decode("utf-16-le", "replace").rstrip("\0").split("\0")
    return raw.decode("utf-16-le", "replace").rstrip("\0")


def _parse_reg_list(data, size, hive, subkey):
    """解析驱动返回的 REG_LIST 序列化结果。"""
    raw = bytes(data[:size])
    off, out = 0, []
    if len(raw) < 4:
        return {"hive": hive, "subkey": subkey, "values": []}
    count = int.from_bytes(raw[0:4], "little")
    off = 4
    for _ in range(count):
        if off + 4 > len(raw):
            break
        nlen = int.from_bytes(raw[off:off + 4], "little")
        off += 4
        if off + nlen > len(raw):
            break
        name = raw[off:off + nlen].decode("utf-16-le", "replace")
        off += nlen
        if off + 4 + 4 > len(raw):
            break
        typ = int.from_bytes(raw[off:off + 4], "little")
        vlen = int.from_bytes(raw[off + 4:off + 8], "little")
        off += 8
        if off + vlen > len(raw):
            break
        val = _reg_bytes(typ, raw[off:off + vlen], vlen)
        off += vlen
        out.append({"name": name, "type": _reg_type_name(typ), "value": val})
    return {"hive": hive, "subkey": subkey, "values": out}


# ---------------- 统一入口（adv.* API 使用） ----------------

def run(op: str, args: dict) -> dict:
    """按操作分发，选择驱动/直调路径。args 为 API 调用参数。"""
    with _lock:
        if op == "proc.list":
            return {"processes": proc_list()}
        if op == "proc.kill":
            return _driver_or_direct("proc.kill", proc_kill_driver,
                                     proc_kill_direct, args["pid"])
        if op == "proc.suspend":
            return _driver_or_direct("proc.suspend", proc_suspend_driver,
                                     proc_suspend_direct, args["pid"], True)
        if op == "proc.resume":
            return _driver_or_direct("proc.resume", proc_suspend_driver,
                                     proc_suspend_direct, args["pid"], False)
        if op == "proc.start":
            return _driver_or_direct("proc.start", proc_start_driver,
                                     proc_start_direct, args["path"],
                                     args.get("args") or "",
                                     args.get("workdir") or "")
        if op == "mem.read":
            return _driver_or_direct("mem.read", mem_read_driver,
                                     mem_read_direct, args["pid"],
                                     args["address"], args["size"])
        if op == "mem.write":
            return _driver_or_direct("mem.write", mem_write_driver,
                                     mem_write_direct, args["pid"],
                                     args["address"], args["data"])
        if op == "file.read":
            return _driver_or_direct("file.read", file_read_driver,
                                     file_read_direct, args["path"],
                                     args.get("size", driver.STB_MAX_DATA))
        if op == "file.write":
            return _driver_or_direct("file.write", file_write_driver,
                                     file_write_direct, args["path"],
                                     args["data"], args.get("append"))
        if op == "file.delete":
            return _driver_or_direct("file.delete", file_delete_driver,
                                     file_delete_direct, args["path"])
        if op == "file.mkdir":
            return _driver_or_direct("file.mkdir", file_mkdir_driver,
                                     file_mkdir_direct, args["path"])
        if op == "reg.read":
            return _driver_or_direct("reg.read", reg_read_driver,
                                     reg_read_direct, args["hive"],
                                     args["subkey"], args["name"])
        if op == "reg.write":
            return _driver_or_direct("reg.write", reg_write_driver,
                                     reg_write_direct, args["hive"],
                                     args["subkey"], args["name"],
                                     args["type"], args["value"])
        if op == "reg.delete":
            return _driver_or_direct("reg.delete", reg_delete_driver,
                                     reg_delete_direct, args["hive"],
                                     args["subkey"], args.get("name"))
        if op == "reg.list":
            return _driver_or_direct("reg.list", reg_list_driver,
                                     reg_list_direct, args["hive"],
                                     args["subkey"])
        if op == "gp.set":
            return _driver_or_direct("gp.set", gp_set_driver,
                                     gp_set_direct, args["policy"],
                                     args["name"], args["type"],
                                     args["value"])
        if op == "cmd.exec":
            return _driver_or_direct("cmd.exec", cmd_exec_driver,
                                     cmd_exec_direct, args["path"],
                                     args.get("args") or "",
                                     args.get("workdir") or "")
        if op == "token.elevate":
            return _driver_or_direct("token.elevate", token_elevate_driver,
                                     token_elevate_fallback)
        raise ApiError("bad_args", f"未知高级操作: {op}")


def token_elevate_fallback():
    """直调路径：无驱动时无法获取 SYSTEM 令牌，仅确认管理员 + 调试特权。"""
    _need_admin()
    driver.enable_debug_privilege()
    return {"system": False, "tokenPid": 0, "via": "win32",
            "note": "无内核驱动时仅能提升到管理员(SeDebugPrivilege)；"
                    "如需 SYSTEM 请开启高级选项并加载驱动"}