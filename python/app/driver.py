"""内核驱动客户端（stbdrv.sys）。

职责：
- 管理员检测（应用自动提权后应已是管理员）
- 通过服务控制管理器(SCM) 加载/卸载/查询 内核驱动 stbdrv
- 通过 DeviceIoControl 发送 IOCTL 完成高级操作（驱动路径）
- 驱动不可用时抛出 IOError，由 adv.py 回退到直调实现

设计约束：不调用 cmd / taskkill 等原生工具；进程、内存、文件、注册表、
组策略操作全部经驱动 Zw*/Ke* API 或（回退路径）Win32 直调完成。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import threading
from pathlib import Path

from .log import info, error as log_error

# ---------------- 常量 ----------------

DRV_SERVICE = "STBDriver"
DRV_DISPLAY = "SystemToolBox Kernel Driver"
DRV_NAME = "stbdrv"
DEVICE_NAME = r"\\.\STBDriver"

# 默认 .sys 位置：native/driver/bin/stbdrv.sys
_drv_dir = Path(__file__).resolve().parents[2] / "native" / "driver"
DEFAULT_SYS_PATH = _drv_dir / "bin" / f"{DRV_NAME}.sys"
DEFAULT_INF_PATH = _drv_dir / f"{DRV_NAME}.inf"

STB_MAGIC = 0x53744244          # "StBD"
STB_IFACE_VERSION = 1

# IOCTL 码 = CTL_CODE(FILE_DEVICE_UNKNOWN(0x22), 0x800+i, METHOD_BUFFERED(0),
#                     FILE_READ_DATA|FILE_WRITE_DATA(3))，与 stbdrv.h 一致
_CTL_BASE = (0x22 << 16) | (3 << 14)
IOCTL_STB_DRV_INFO = _CTL_BASE | 0x800
IOCTL_STB_PROC_KILL = _CTL_BASE | 0x801
IOCTL_STB_PROC_SUSPEND = _CTL_BASE | 0x802
IOCTL_STB_PROC_RESUME = _CTL_BASE | 0x803
IOCTL_STB_PROC_START = _CTL_BASE | 0x804
IOCTL_STB_MEM_READ = _CTL_BASE | 0x805
IOCTL_STB_MEM_WRITE = _CTL_BASE | 0x806
IOCTL_STB_FILE_READ = _CTL_BASE | 0x807
IOCTL_STB_FILE_WRITE = _CTL_BASE | 0x808
IOCTL_STB_FILE_DELETE = _CTL_BASE | 0x809
IOCTL_STB_FILE_MKDIR = _CTL_BASE | 0x80A
IOCTL_STB_REG_READ = _CTL_BASE | 0x80B
IOCTL_STB_REG_WRITE = _CTL_BASE | 0x80C
IOCTL_STB_REG_DELETE = _CTL_BASE | 0x80D
IOCTL_STB_REG_LIST = _CTL_BASE | 0x80E
IOCTL_STB_POLICY_SET = _CTL_BASE | 0x80F
IOCTL_STB_CMD_EXEC = _CTL_BASE | 0x810
IOCTL_STB_TOKEN_ELEVATE = _CTL_BASE | 0x811

# 固定尺寸（与 stbdrv.h 一致）
STB_MAX_PATH = 260   # WCHAR
STB_MAX_NAME = 64    # WCHAR
STB_MAX_DATA = 4096

# 注册表 hive 枚举
REG_HIVE = {0: "HKLM", 1: "HKCU", 2: "HKCR", 3: "HKU"}

def _winreg_sam():
    import winreg
    return {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
        "HKU": winreg.HKEY_USERS,
    }

WINREG_SAM = _winreg_sam()

# 驱动状态机
class DriverState:
    OFF = "off"            # 未安装/未启用
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"

# ---------------- Win32 原生声明（x64 安全） ----------------

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

# 部分 Python 发行版裁剪了 ctypes.use_last_error，导致 _last_error()
# 恒为 0（真实错误码丢失）。这里直接调用 GetLastError 获取真实错误码。
kernel32.GetLastError.restype = ctypes.c_ulong


def _last_error() -> int:
    """读取最近一次 Win32 调用的错误码（GetLastError）。"""
    return int(kernel32.GetLastError())

DWORD = wt.DWORD
HANDLE = ctypes.c_void_p
BOOL = ctypes.c_int
LPVOID = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80

SC_MANAGER_ALL_ACCESS = 0xF003F
SERVICE_KERNEL_DRIVER = 0x1
SERVICE_DEMAND_START = 0x3
SERVICE_ALL_ACCESS = 0xF01FF
SERVICE_START = 0x10
SERVICE_STOP = 0x20
SERVICE_QUERY_STATUS = 0x4
SERVICE_QUERY_CONFIG = 0x1
SERVICE_CONTROL_STOP = 0x1
SERVICE_RUNNING = 0x4
SERVICE_STOPPED = 0x1
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_MARKED_FOR_DELETE = 1072

OPEN_PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
TH32CS_SNAPPROCESS = 0x2
SE_PRIVILEGE_ENABLED = 0x2
TOKEN_ADJUST_PRIVILEGES = 0x20
TOKEN_QUERY = 0x8
SE_DEBUG_NAME = "SeDebugPrivilege"
CREATE_NO_WINDOW = 0x08000000

kernel32.CreateFileW.restype = HANDLE
kernel32.CreateFileW.argtypes = [LPCWSTR, DWORD, DWORD, LPVOID, DWORD,
                                 DWORD, HANDLE]
kernel32.DeviceIoControl.argtypes = [HANDLE, DWORD, LPVOID, DWORD, LPVOID,
                                     DWORD, ctypes.POINTER(DWORD), LPVOID]
kernel32.DeviceIoControl.restype = BOOL
kernel32.GetCurrentProcess.restype = HANDLE
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.CloseHandle.restype = BOOL

advapi32.OpenSCManagerW.restype = HANDLE
advapi32.OpenSCManagerW.argtypes = [LPCWSTR, LPCWSTR, DWORD]
advapi32.OpenServiceW.restype = HANDLE
advapi32.OpenServiceW.argtypes = [HANDLE, LPCWSTR, DWORD]
advapi32.CreateServiceW.restype = HANDLE
advapi32.CreateServiceW.argtypes = [
    HANDLE, LPCWSTR, LPCWSTR, DWORD, DWORD, DWORD, DWORD,
    LPCWSTR, LPCWSTR, LPVOID, LPCWSTR, LPCWSTR, LPCWSTR]
advapi32.CloseServiceHandle.argtypes = [HANDLE]
advapi32.CloseServiceHandle.restype = BOOL


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", DWORD), ("dwCurrentState", DWORD),
                ("dwControlsAccepted", DWORD), ("dwWin32ExitCode", DWORD),
                ("dwServiceSpecificExitCode", DWORD),
                ("dwCheckPoint", DWORD), ("dwWaitHint", DWORD)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", DWORD), ("HighPart", ctypes.c_long)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1)]


# ---------------- 服务/令牌函数原型（必须在上述结构定义之后） ----------------
# 注意：本 Python 构建里 restype=c_void_p 的函数返回裸 Python int（如
# OpenSCManagerW 句柄可达 2^40+）；若接收该 int 的函数未声明 argtypes，
# ctypes 会按 32 位 c_int 转换并抛出
# "argument 1: OverflowError: int too long to convert"（句柄值 >2^31 时必现）。
# 因此凡接收句柄的 advapi32/kernel32 调用都必须声明 argtypes。

advapi32.QueryServiceStatus.restype = BOOL
advapi32.QueryServiceStatus.argtypes = [
    HANDLE, ctypes.POINTER(SERVICE_STATUS)]
advapi32.StartServiceW.restype = BOOL
advapi32.StartServiceW.argtypes = [HANDLE, DWORD, LPVOID]
advapi32.ControlService.restype = BOOL
advapi32.ControlService.argtypes = [
    HANDLE, DWORD, ctypes.POINTER(SERVICE_STATUS)]
advapi32.DeleteService.restype = BOOL
advapi32.DeleteService.argtypes = [HANDLE]

advapi32.OpenProcessToken.restype = BOOL
advapi32.OpenProcessToken.argtypes = [
    HANDLE, DWORD, ctypes.POINTER(HANDLE)]
advapi32.LookupPrivilegeValueW.restype = BOOL
advapi32.LookupPrivilegeValueW.argtypes = [
    LPCWSTR, LPCWSTR, ctypes.POINTER(LUID)]
advapi32.AdjustTokenPrivileges.restype = BOOL
advapi32.AdjustTokenPrivileges.argtypes = [
    HANDLE, BOOL, LPVOID, DWORD, LPVOID, LPVOID]


# ---------------- ctypes 结构（与 stbdrv.h 严格对应） ----------------

class STB_HDR(ctypes.Structure):
    _fields_ = [("magic", DWORD), ("op", DWORD), ("status", ctypes.c_long)]


class STB_DRV_INFO(ctypes.Structure):
    _fields_ = [("hdr", STB_HDR), ("version", DWORD),
                ("caps", DWORD), ("reserved", DWORD)]


class STB_PROC_OP(ctypes.Structure):
    _fields_ = [("hdr", STB_HDR), ("pid", DWORD), ("reserved", DWORD)]


class STB_PROC_START(ctypes.Structure):
    _fields_ = [
        ("hdr", STB_HDR),
        ("path", ctypes.c_wchar * STB_MAX_PATH),
        ("args", ctypes.c_wchar * STB_MAX_PATH),
        ("workdir", ctypes.c_wchar * STB_MAX_PATH),
        ("ldrInitThunk", ctypes.c_size_t),
        ("pid", DWORD), ("reserved", DWORD),
    ]


class STB_MEM_READ(ctypes.Structure):
    _fields_ = [
        ("hdr", STB_HDR), ("pid", DWORD), ("reserved1", DWORD),
        ("address", ctypes.c_size_t), ("size", DWORD), ("reserved2", DWORD),
        ("data", ctypes.c_ubyte * STB_MAX_DATA),
    ]


class STB_MEM_WRITE(ctypes.Structure):
    _fields_ = [
        ("hdr", STB_HDR), ("pid", DWORD), ("reserved1", DWORD),
        ("address", ctypes.c_size_t), ("size", DWORD), ("reserved2", DWORD),
        ("data", ctypes.c_ubyte * STB_MAX_DATA),
    ]


class STB_FILE_READ(ctypes.Structure):
    _fields_ = [
        ("hdr", STB_HDR),
        ("path", ctypes.c_wchar * STB_MAX_PATH),
        ("size", DWORD), ("reserved", DWORD),
        ("data", ctypes.c_ubyte * STB_MAX_DATA),
    ]


class STB_FILE_WRITE(ctypes.Structure):
    _fields_ = [
        ("hdr", STB_HDR),
        ("path", ctypes.c_wchar * STB_MAX_PATH),
        ("flags", DWORD), ("size", DWORD),
        ("data", ctypes.c_ubyte * STB_MAX_DATA),
    ]


class STB_FILE_PATH(ctypes.Structure):
    _fields_ = [("hdr", STB_HDR),
                ("path", ctypes.c_wchar * STB_MAX_PATH)]


class STB_REG_OP(ctypes.Structure):
    _fields_ = [
        ("hdr", STB_HDR),
        ("hive", DWORD), ("reserved1", DWORD),
        ("subkey", ctypes.c_wchar * STB_MAX_PATH),
        ("name", ctypes.c_wchar * STB_MAX_NAME),
        ("type", DWORD), ("reserved2", DWORD),
        ("size", DWORD), ("reserved3", DWORD),
        ("data", ctypes.c_ubyte * STB_MAX_DATA),
    ]


class STB_POLICY_SET(ctypes.Structure):
    _fields_ = [
        ("hdr", STB_HDR),
        ("policyPath", ctypes.c_wchar * STB_MAX_PATH),
        ("name", ctypes.c_wchar * STB_MAX_NAME),
        ("type", DWORD), ("reserved", DWORD),
        ("size", DWORD), ("reserved2", DWORD),
        ("data", ctypes.c_ubyte * STB_MAX_DATA),
    ]


class STB_CMD_EXEC(ctypes.Structure):
    _fields_ = [
        ("hdr", STB_HDR),
        ("path", ctypes.c_wchar * STB_MAX_PATH),
        ("args", ctypes.c_wchar * STB_MAX_PATH),
        ("workdir", ctypes.c_wchar * STB_MAX_PATH),
        ("ldrInitThunk", ctypes.c_size_t),
        ("pid", DWORD), ("reserved", DWORD),
    ]


class STB_TOKEN_ELEVATE(ctypes.Structure):
    _fields_ = [("hdr", STB_HDR), ("token", ctypes.c_size_t),
                ("pid", DWORD), ("reserved", DWORD)]


# ---------------- 工具 ----------------

def _run(cmd, timeout=8):
    """运行外部命令并返回 (rc, stdout, stderr)。仅用于环境自检。

    字节模式 + 编码回退：bcdedit 等输出可能为系统 ANSI 码页(GBK)，若用
    text=True 在 UTF-8 强制环境下解码，读取线程会抛 UnicodeDecodeError
    导致 communicate 永久挂起；改为 bytes 后再解码不会崩线程。
    """
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        raw_out, raw_err = p.stdout, p.stderr
        out = _decode_win(raw_out)
        err = _decode_win(raw_err)
        return p.returncode, out, err
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def _decode_win(raw: bytes) -> str:
    """先按 UTF-8 解码，失败再按系统 ANSI 码页（中文系统为 GBK）。"""
    for enc in ("utf-8", "mbcs"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ---------------- 管理员 / 提权 ----------------

def is_admin() -> bool:
    """Shell32.IsUserAnAdmin —— 当前进程是否管理员（提权后应为 True）。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def enable_debug_privilege() -> bool:
    """为当前进程开启 SeDebugPrivilege（访问受保护进程的钥匙）。"""
    hToken = HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                     ctypes.byref(hToken)):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, SE_DEBUG_NAME,
                                              ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        return bool(advapi32.AdjustTokenPrivileges(hToken, False,
                                                   ctypes.byref(tp), 0,
                                                   None, None))
    finally:
        kernel32.CloseHandle(hToken)


# ---------------- 服务（SCM）管理 ----------------

class WinService:
    def __init__(self, name=DRV_SERVICE):
        self.name = name
        self._schm = HANDLE()
        self._svc = HANDLE()
        self.open_error = ""   # 最近一次管理器/服务打开失败的原因

    def _open_manager(self):
        if self._schm:
            return True
        self._schm = advapi32.OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)
        ok = bool(self._schm)
        self.open_error = "" if ok else (
            f"OpenSCManager 失败(错误码 {_last_error()})")
        return ok

    def _open_service(self, access=SERVICE_ALL_ACCESS):
        if not self._open_manager():
            return False
        self._svc = advapi32.OpenServiceW(self._schm, self.name, access)
        ok = bool(self._svc)
        if not ok:
            self.open_error = (f"OpenService({self.name}) 失败"
                               f"(错误码 {_last_error()})")
        return ok

    def exists(self) -> bool:
        return self._open_manager() and self._open_service(SERVICE_QUERY_STATUS)

    def create(self, sys_path: str) -> bool:
        if not self._open_manager():
            return False
        self._svc = advapi32.CreateServiceW(
            self._schm, self.name, DRV_DISPLAY,
            SERVICE_ALL_ACCESS, SERVICE_KERNEL_DRIVER, SERVICE_DEMAND_START,
            0, sys_path, None, None, None, None, None)
        ok = bool(self._svc)
        if not ok:
            self.open_error = (f"CreateService({self.name}) 失败"
                               f"(错误码 {_last_error()})")
        return ok

    def start(self) -> str:
        """启动驱动。返回 'running' / 'already' / 'start_failed(错误码)'。"""
        st = SERVICE_STATUS()
        if not self._open_service(SERVICE_START | SERVICE_QUERY_STATUS):
            return f"open_service_failed({_last_error()})"
        if advapi32.QueryServiceStatus(self._svc, ctypes.byref(st)) and \
                st.dwCurrentState == SERVICE_RUNNING:
            return "already"
        if advapi32.StartServiceW(self._svc, 0, None):
            return "running"
        err = _last_error()
        if err == ERROR_SERVICE_ALREADY_RUNNING:
            return "already"
        return f"start_failed({err})"

    def stop(self) -> bool:
        st = SERVICE_STATUS()
        if not self._open_service(SERVICE_STOP | SERVICE_QUERY_STATUS):
            return _last_error() == ERROR_SERVICE_DOES_NOT_EXIST
        if not advapi32.ControlService(self._svc, SERVICE_CONTROL_STOP,
                                       ctypes.byref(st)):
            return _last_error() == ERROR_SERVICE_NOT_ACTIVE
        return True

    def delete(self) -> bool:
        if not self._open_service():
            return _last_error() == ERROR_SERVICE_DOES_NOT_EXIST
        if not advapi32.DeleteService(self._svc):
            return _last_error() == ERROR_SERVICE_MARKED_FOR_DELETE
        return True

    def status(self) -> dict:
        st = SERVICE_STATUS()
        exists = self._open_manager() and self._open_service(SERVICE_QUERY_STATUS)
        state = "unknown"
        if exists and advapi32.QueryServiceStatus(self._svc, ctypes.byref(st)):
            state = {SERVICE_RUNNING: "running",
                     SERVICE_STOPPED: "stopped"}.get(st.dwCurrentState,
                                                     "unknown")
        return {"service": self.name, "exists": exists, "state": state}

    def close(self):
        if self._svc:
            advapi32.CloseServiceHandle(self._svc)
            self._svc = HANDLE()
        if self._schm:
            advapi32.CloseServiceHandle(self._schm)
            self._schm = HANDLE()


# ---------------- 驱动加载器 ----------------

class DriverManager:
    """stbdrv.sys 生命周期与 IOCTL 通信。"""

    def __init__(self, sys_path: Path = None):
        self.sys_path = Path(sys_path or DEFAULT_SYS_PATH)
        self._handle = HANDLE()
        self._lock = threading.RLock()
        self.state = DriverState.OFF
        self.last_error = ""
        self.version = ""
        self.caps = []

    # ---- 状态 ----

    def svc(self) -> WinService:
        return WinService()

    def status(self) -> dict:
        with self._lock:
            svc = self.svc()
            try:
                svc_info = svc.status()
            finally:
                svc.close()
            return {
                "state": self.state,
                "service": svc_info,
                "sysPath": str(self.sys_path),
                "sysExists": self.sys_path.is_file(),
                "version": self.version,
                "caps": self.caps,
                "elevated": is_admin(),
                "testSigning": self._test_signing(),
                "error": self.last_error,
            }

    @staticmethod
    def _test_signing() -> dict:
        rc, out, _ = _run(["bcdedit", "/enum", "{current}"])
        on = "testsigning" in out.lower() and "yes" in out.lower()
        return {"enabled": bool(on), "queryable": rc == 0}

    def ready(self) -> bool:
        return self.state == DriverState.READY

    # ---- 加载 / 卸载 ----

    def load(self) -> dict:
        """安装并启动驱动。返回 {ok, state, message}。"""
        with self._lock:
            if self.state == DriverState.READY:
                return {"ok": True, "state": self.state,
                        "message": "驱动已就绪"}
            if not is_admin():
                self.state = DriverState.ERROR
                self.last_error = "需要管理员权限才能加载内核驱动"
                return {"ok": False, "state": self.state,
                        "message": self.last_error}
            if not self.sys_path.is_file():
                self.state = DriverState.ERROR
                self.last_error = (f"驱动文件不存在: {self.sys_path}。"
                                   "请先用 WDK 编译 native/driver 得到 stbdrv.sys")
                return {"ok": False, "state": self.state,
                        "message": self.last_error}

            self.state = DriverState.LOADING
            self.last_error = ""
            svc = self.svc()
            try:
                if not svc.exists():
                    if not svc.create(str(self.sys_path)):
                        err = _last_error()
                        self.state = DriverState.ERROR
                        self.last_error = (f"创建服务失败({err})"
                                           f"，请确认已以管理员运行 · {svc.open_error}")
                        return {"ok": False, "state": self.state,
                                "message": self.last_error}
                res = svc.start()
                if res.startswith("start_failed"):
                    err = res.split("(")[1].rstrip(")")
                    self.state = DriverState.ERROR
                    self.last_error = _explain_driver_error(
                        int(err) if err.isdigit() else err)
                    return {"ok": False, "state": self.state,
                            "message": self.last_error}
                if res.startswith("open_service_failed"):
                    err = res.split("(")[1].rstrip(")")
                    self.state = DriverState.ERROR
                    self.last_error = (f"打开服务失败(错误码 {err})"
                                       f" · {svc.open_error}")
                    return {"ok": False, "state": self.state,
                            "message": self.last_error}
            finally:
                svc.close()

            ok_open = self.open()
            ok = ok_open and self.info()
            self.state = DriverState.READY if ok else DriverState.ERROR
            if ok:
                info(f"内核驱动已加载: {self.sys_path}")
            return {"ok": ok, "state": self.state,
                    "message": "驱动就绪" if ok else self.last_error}

    def unload(self) -> dict:
        with self._lock:
            self.close()
            svc = self.svc()
            try:
                res = svc.stop()
            finally:
                svc.close()
            self.state = DriverState.OFF
            info(f"内核驱动已停止: {res}")
            return {"ok": True, "state": self.state,
                    "message": "驱动已停止" if res else "驱动未在运行"}

    # ---- DeviceIoControl ----

    def open(self) -> bool:
        self.close()
        self._handle = kernel32.CreateFileW(
            DEVICE_NAME, GENERIC_READ | GENERIC_WRITE,
            0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        # CreateFileW 的 restype 是 c_void_p：ctypes 直接返回 int（或 None）
        ok = bool(self._handle) and self._handle != 0xFFFFFFFFFFFFFFFF \
            and self._handle != 0
        if not ok:
            self._handle = HANDLE()
        return ok

    def close(self):
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = HANDLE()

    def ioctl(self, code, req):
        """发送缓冲 IOCTL，返回驱动填写的 status（NTSTATUS 码）。"""
        with self._lock:
            if not self._handle:
                raise IOError("驱动设备不可用，请先加载驱动")
            n = DWORD(0)
            ok = kernel32.DeviceIoControl(
                self._handle, code, ctypes.byref(req), ctypes.sizeof(req),
                ctypes.byref(req), ctypes.sizeof(req), ctypes.byref(n), None)
            if not ok:
                raise IOError(f"DeviceIoControl 失败，错误码 "
                              f"{_last_error()}")
            return int(req.hdr.status)

    def info(self) -> bool:
        req = STB_DRV_INFO()
        req.hdr.magic = STB_MAGIC
        req.hdr.op = 0
        req.hdr.version = STB_IFACE_VERSION
        try:
            st = self.ioctl(IOCTL_STB_DRV_INFO, req)
        except IOError as e:
            self.last_error = str(e)
            return False
        if st != 0:
            self.last_error = f"驱动接口版本协商失败(status=0x{st:x})"
            return False
        self.version = f"{req.version >> 16}.{req.version & 0xFFFF}"
        caps = req.caps
        all_caps = ["proc", "mem", "file", "reg", "gp", "exec", "token"]
        self.caps = [c for i, c in enumerate(all_caps) if caps & (1 << i)]
        return True


def _explain_driver_error(err) -> str:
    table = {
        6: "ERROR_INVALID_HANDLE：服务句柄无效——依赖 SCM 的调用前未取得管理器/服务句柄，"
           "或句柄已失效。已修复：若再次出现请检查应用是否以管理员身份启动",
        577: "ERROR_INVALID_IMAGE_HASH：驱动未签名。请开启测试签名 "
             "(管理员: bcdedit /set testsigning on + 重启) 或使用已签名驱动",
        1275: "驱动被拒绝加载（未签名或测试签名未开启）",
        4201: "无法识别驱动镜像（.sys 不是有效内核镜像）",
        2: "指定的驱动镜像文件找不到",
        5: "拒绝访问（驱动加载需要管理员权限）",
        1072: "服务正在停止，请稍后重试",
        1060: "指定的服务未安装（服务创建失败或已被删除）",
        1058: "服务被禁用，无法启动（管理员: sc config stbdrv start= demand）",
    }
    return table.get(err, f"启动服务失败(错误码 {err})")


# 模块级单例
manager = DriverManager()