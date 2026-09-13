"""native_core.dll 桥接 + Win32 ctypes 兜底。

优先加载 C++ 兼容层编译产物（native_core.dll，其中系统信息由 C 层 core
提供）。DLL 缺失时使用纯 ctypes 直连 Win32 API 的兜底实现，保证软件在
未编译原生层的情况下依然可运行（驱动/服务等管理类操作需要 DLL）。
"""
import ctypes
import json
from ctypes import wintypes
from pathlib import Path

from .. import config
from ..log import debug, warn


class BridgeError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# 纯 ctypes Win32 兜底实现
# ----------------------------------------------------------------------

class _SysInfoFallback:
    """用 ctypes 直连 Win32 API 采集系统信息（不依赖 DLL）。"""

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    psapi = ctypes.windll.psapi
    shell32 = ctypes.windll.shell32

    HKEY_LOCAL_MACHINE = ctypes.c_void_p(0x80000002)
    RRF_RT_REG_SZ = 0x2
    RRF_RT_REG_DWORD = 0x10
    RRF_RT_REG_MULTI_SZ = 0x20
    DRIVE_REMOVABLE = 2
    DRIVE_FIXED = 3

    @classmethod
    def _reg_enum_subkeys(cls, base):
        """枚举 HKLM\base 下的直接子键名。"""
        KEY_READ = 0x20019
        hkey = wintypes.HKEY()
        if cls.advapi32.RegOpenKeyExW(
                cls.HKEY_LOCAL_MACHINE, base, 0, KEY_READ,
                ctypes.byref(hkey)) != 0:
            return []
        out = []
        i = 0
        while True:
            name = ctypes.create_unicode_buffer(256)
            size = wintypes.DWORD(len(name))
            r = cls.advapi32.RegEnumKeyExW(
                hkey, i, name, ctypes.byref(size), None, None, None, None)
            if r == 259:            # ERROR_NO_MORE_ITEMS
                break
            if r == 0:
                out.append(name.value)
            i += 1
        cls.advapi32.RegCloseKey(hkey)
        return out

    @classmethod
    def _reg_read_multi_string(cls, subkey, name):
        buf = ctypes.create_unicode_buffer(2048)
        size = wintypes.DWORD(len(buf) * 2)
        typ = wintypes.DWORD(0)
        r = cls.advapi32.RegGetValueW(
            cls.HKEY_LOCAL_MACHINE, subkey, name, cls.RRF_RT_REG_MULTI_SZ,
            ctypes.byref(typ), buf, ctypes.byref(size))
        if r != 0:
            return []
        data = ctypes.string_at(buf, min(size.value, len(buf) * 2)) \
            .decode("utf-16-le", "replace")
        return [s for s in data.split("\x00") if s]

    @classmethod
    def _reg_read_string(cls, subkey, name, default=""):
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(buf))
        typ = wintypes.DWORD(0)
        r = cls.advapi32.RegGetValueW(
            cls.HKEY_LOCAL_MACHINE, subkey, name, cls.RRF_RT_REG_SZ,
            ctypes.byref(typ), buf, ctypes.byref(size))
        return buf.value if r == 0 and buf.value else default

    @classmethod
    def _reg_read_dword(cls, subkey, name, default=0):
        val = wintypes.DWORD(0)
        size = wintypes.DWORD(ctypes.sizeof(val))
        typ = wintypes.DWORD(0)
        r = cls.advapi32.RegGetValueW(
            cls.HKEY_LOCAL_MACHINE, subkey, name, cls.RRF_RT_REG_DWORD,
            ctypes.byref(typ), ctypes.byref(val), ctypes.byref(size))
        return int(val.value) if r == 0 else default

    @classmethod
    def _logical_cores(cls):
        class SYSTEM_INFO(ctypes.Structure):
            _fields_ = [
                ("wProcessorArchitecture", wintypes.WORD),
                ("wReserved", wintypes.WORD),
                ("dwPageSize", wintypes.DWORD),
                ("lpMinimumApplicationAddress", ctypes.c_void_p),
                ("lpMaximumApplicationAddress", ctypes.c_void_p),
                ("dwActiveProcessorMask", ctypes.c_size_t),
                ("dwNumberOfProcessors", wintypes.DWORD),
                ("dwProcessorType", wintypes.DWORD),
            ]
        si = SYSTEM_INFO()
        cls.kernel32.GetSystemInfo(ctypes.byref(si))
        return int(si.dwNumberOfProcessors)

    @classmethod
    def _physical_cores(cls):
        class SLPI(ctypes.Structure):
            _fields_ = [
                ("ProcessorMask", ctypes.c_size_t),
                ("Relationship", wintypes.DWORD),
                ("union", ctypes.c_ubyte * 12),   # 与 CACHE_DESCRIPTOR 对齐
            ]
        RelationProcessorCore = 0
        fn = cls.kernel32.GetLogicalProcessorInformation
        need = wintypes.DWORD(0)
        fn(None, ctypes.byref(need))   # 首次调用仅取所需字节数
        if need.value <= 0:
            return 0
        buf = (ctypes.c_ubyte * need.value)()
        ok = fn(ctypes.cast(buf, ctypes.POINTER(SLPI)), ctypes.byref(need))
        if not ok:
            return 0
        ptr = ctypes.cast(buf, ctypes.POINTER(SLPI))
        n = need.value // ctypes.sizeof(SLPI)
        cores = sum(1 for i in range(n)
                    if ptr[i].Relationship == RelationProcessorCore)
        return cores

    @classmethod
    def _memory(cls):
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        if cls.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return int(m.ullTotalPhys), int(m.ullAvailPhys)
        return 0, 0

    @classmethod
    def _os_info(cls):
        sub = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
        product = cls._reg_read_string(sub, "ProductName", "Windows")
        build = cls._reg_read_string(sub, "CurrentBuildNumber", "0")
        major = cls._reg_read_dword(sub, "CurrentMajorVersionNumber", 10)
        minor = cls._reg_read_dword(sub, "CurrentMinorVersionNumber", 0)
        return product, f"{major}.{minor}.{build} Build {build}"

    @classmethod
    def _disks(cls):
        buf = ctypes.create_unicode_buffer(4 * 26 * 2)
        n = cls.kernel32.GetLogicalDriveStringsW(len(buf), buf)
        if n == 0 or n >= len(buf):
            return []
        out = []
        p = buf.value
        while p:
            drive = p[:3]
            t = cls.kernel32.GetDriveTypeW(drive)
            if t in (cls.DRIVE_FIXED, cls.DRIVE_REMOVABLE):
                free = ctypes.c_ulonglong(0)
                total = ctypes.c_ulonglong(0)
                ok = cls.kernel32.GetDiskFreeSpaceExW(
                    drive, ctypes.byref(free), ctypes.byref(total), None)
                fs = ctypes.create_unicode_buffer(16)
                cls.kernel32.GetVolumeInformationW(
                    drive, None, 0, None, None, None, fs, len(fs))
                out.append({
                    "root": drive,
                    "fs": fs.value,
                    "totalBytes": int(total.value) if ok else 0,
                    "freeBytes": int(free.value) if ok else 0,
                })
            p = buf.value[len(p):]
        return out

    @classmethod
    def _cpu_usage(cls):
        """GetSystemTimes 两次采样差分计算 CPU 占用率（0-100，首采返回 0）。"""
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]

        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        if not cls.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return 0.0

        def tot(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        idle_t, kern_t, user_t = tot(idle), tot(kern), tot(user)
        prev = getattr(cls, "_cpu_prev", None)
        cls._cpu_prev = (idle_t, kern_t, user_t)
        if not prev:
            return 0.0
        p_idle, p_kern, p_user = prev
        idle_d = idle_t - p_idle
        kern_d = kern_t - p_kern
        user_d = user_t - p_user
        busy = (kern_d + user_d) - idle_d      # 内核时间含空闲
        total = busy + idle_d
        if total <= 0:
            return 0.0
        return round(min(100.0, max(0.0, busy / total * 100.0)), 1)

    @classmethod
    def sysinfo(cls):
        total, free = cls._memory()
        product, version = cls._os_info()
        name = ctypes.create_unicode_buffer(64)
        size = wintypes.DWORD(len(name))
        cls.kernel32.GetComputerNameW(name, ctypes.byref(size))
        user = ctypes.create_unicode_buffer(64)
        usize = wintypes.DWORD(len(user))
        cls.advapi32.GetUserNameW(user, ctypes.byref(usize))

        pids = (wintypes.DWORD * 2048)()
        cb = wintypes.DWORD(0)
        pcount = 0
        if cls.psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(cb)):
            pcount = int(cb.value // ctypes.sizeof(wintypes.DWORD))

        physical = cls._physical_cores()
        logical = cls._logical_cores()

        return {
            "cpuName": cls._reg_read_string(
                "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0",
                "ProcessorNameString", "Unknown CPU"),
            "cores": physical or logical,
            "logicalCores": logical,
            "cpuUsage": cls._cpu_usage(),
            "memoryTotal": total,
            "memoryFree": free,
            "memoryUsed": total - free if total else 0,
            "osName": product,
            "osVersion": version,
            "uptimeSec": int(cls.kernel32.GetTickCount64() // 1000),
            "machineName": name.value,
            "userName": user.value,
            "processCount": pcount,
            "disks": cls._disks(),
        }

    @classmethod
    def is_elevated(cls):
        TOKEN_QUERY = 0x0008
        TokenElevation = 20

        class TOKEN_ELEVATION(ctypes.Structure):
            _fields_ = [("TokenIsElevated", wintypes.DWORD)]

        # 显式声明原型：GetCurrentProcess 返回 (HANDLE)-1，未声明 restype
        # 时默认按 c_int 转换会溢出 ("int too long to convert")；
        # OpenProcessToken / GetTokenInformation 位于 advapi32
        k32 = cls.kernel32
        a32 = cls.advapi32
        if not hasattr(k32, "_stb_proto"):
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            a32.OpenProcessToken.argtypes = [
                ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE)]
            a32.OpenProcessToken.restype = wintypes.BOOL
            a32.GetTokenInformation.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            a32.GetTokenInformation.restype = wintypes.BOOL
            k32._stb_proto = True

        token = wintypes.HANDLE()
        if not a32.OpenProcessToken(
                k32.GetCurrentProcess(), TOKEN_QUERY,
                ctypes.byref(token)):
            return -1
        te = TOKEN_ELEVATION()
        size = wintypes.DWORD(ctypes.sizeof(te))
        ok = a32.GetTokenInformation(
            token, TokenElevation, ctypes.byref(te), size, ctypes.byref(size))
        k32.CloseHandle(token)
        return te.TokenIsElevated if ok else -1

    @classmethod
    def elevate_run(cls, file, args, workdir):
        SW_SHOWNORMAL = 1
        r = cls.shell32.ShellExecuteW(None, "runas", file, args or "",
                                      workdir or "", SW_SHOWNORMAL)
        return 0 if r > 32 else -1

    @classmethod
    def machine_guid(cls):
        return cls._reg_read_string(
            "SOFTWARE\\Microsoft\\Cryptography", "MachineGuid", "")

    @classmethod
    def system_drive(cls):
        win = ctypes.create_unicode_buffer(260)
        n = cls.kernel32.GetWindowsDirectoryW(win, len(win))
        if n == 0 or n >= len(win):
            return {"root": "C:\\", "totalBytes": 0, "freeBytes": 0, "fs": ""}
        root = win.value[:3]
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        ok = cls.kernel32.GetDiskFreeSpaceExW(
            root, ctypes.byref(free), ctypes.byref(total), None)
        fs = ctypes.create_unicode_buffer(16)
        cls.kernel32.GetVolumeInformationW(root, None, 0, None, None, None,
                                           fs, len(fs))
        return {
            "root": root,
            "totalBytes": int(total.value) if ok else 0,
            "freeBytes": int(free.value) if ok else 0,
            "fs": fs.value,
        }

    # ---------- 扩展系统信息（BIOS/主板/GPU/电池/网络） ----------

    @classmethod
    def _bios_board(cls):
        sub = "HARDWARE\\DESCRIPTION\\System\\BIOS"
        return {
            "vendor": cls._reg_read_string(sub, "BIOSVendor", ""),
            "version": cls._reg_read_string(sub, "BIOSVersion", ""),
            "systemManufacturer": cls._reg_read_string(
                sub, "SystemManufacturer", ""),
            "systemProduct": cls._reg_read_string(sub, "SystemProductName", ""),
            "systemVersion": cls._reg_read_string(sub, "SystemVersion", ""),
        }

    @classmethod
    def _gpus(cls):
        base = (r"SYSTEM\CurrentControlSet\Control\Class"
                r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
        out = []
        seen = set()
        for sub in cls._reg_enum_subkeys(base):
            if not sub.startswith("000"):
                continue
            desc = cls._reg_read_string(base + "\\" + sub, "DriverDesc", "")
            if desc and desc not in seen:
                seen.add(desc)
                out.append({"name": desc, "key": sub})
        return out

    @classmethod
    def _battery(cls):
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD),
            ]
        sps = SYSTEM_POWER_STATUS()
        if not cls.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            return {"present": False}
        percent = int(sps.BatteryLifePercent)
        flag = int(sps.BatteryFlag)
        return {
            "present": not bool(flag & 128),
            "acLine": bool(sps.ACLineStatus & 1),
            "charging": bool(flag & 8),
            "percent": percent if percent <= 100 else None,
            "lifeSeconds": int(sps.BatteryLifeTime)
            if sps.BatteryLifeTime != 0xFFFFFFFF else None,
        }

    @classmethod
    def _network(cls):
        base = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
        conn_base = (r"SYSTEM\CurrentControlSet\Control\Network"
                     r"\{4d36e972-e325-11ce-bfc1-08002be10318}")
        out = []
        for guid in cls._reg_enum_subkeys(base):
            kb = base + "\\" + guid
            ips = cls._reg_read_multi_string(kb, "DhcpIPAddress") \
                or cls._reg_read_multi_string(kb, "IPAddress")
            gw = cls._reg_read_multi_string(kb, "DhcpDefaultGateway")
            dns = (cls._reg_read_string(kb, "DhcpNameServer", "")
                   or cls._reg_read_string(kb, "NameServer", ""))
            if not ips and not dns:
                continue
            name = cls._reg_read_string(
                conn_base + "\\" + guid + "\\Connection", "Name", guid)
            out.append({
                "name": name,
                "guid": guid,
                "ipv4": [x for x in ips if x],
                "gateway": [x for x in gw if x],
                "dns": dns,
            })
        return out

    @classmethod
    def extended(cls):
        """扩展系统信息：BIOS/主板/GPU/电池/网络适配器/机器GUID/系统盘。"""
        return {
            "bios": cls._bios_board(),
            "gpus": cls._gpus(),
            "battery": cls._battery(),
            "network": cls._network(),
            "machineGuid": cls.machine_guid(),
            "systemDrive": cls.system_drive(),
        }


# ----------------------------------------------------------------------
# native_core.dll 桥接
# ----------------------------------------------------------------------

def _candidate_dll_paths():
    yield config.NATIVE_DIR / "native_core.dll"
    yield config.ROOT / "build" / "bin" / "native_core.dll"


class NativeBridge:
    """封装 native_core.dll 的 C ABI（可选加载）。"""

    def __init__(self):
        self._dll = None
        self._path = None
        for p in _candidate_dll_paths():
            if p.is_file():
                try:
                    self._dll = ctypes.CDLL(str(p))
                    self._path = p
                    break
                except OSError as e:
                    warn(f"加载 {p} 失败: {e}")
        if self._dll:
            self._setup_prototypes()
            debug(f"native_core.dll 已加载: {self._path}")

    @property
    def available(self) -> bool:
        return self._dll is not None

    @property
    def path(self):
        return str(self._path) if self._path else None

    def _setup_prototypes(self):
        d = self._dll
        d.native_version.restype = ctypes.c_char_p

        d.native_sysinfo_json.restype = ctypes.c_int
        d.native_sysinfo_json.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        d.native_machine_guid.restype = ctypes.c_int
        d.native_machine_guid.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        d.native_system_drive.restype = ctypes.c_int
        d.native_system_drive.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

        d.native_is_elevated.restype = ctypes.c_int
        d.native_elevate_run.restype = ctypes.c_int
        d.native_elevate_run.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                         wintypes.LPCWSTR]

        d.native_service_list.restype = ctypes.c_int
        d.native_service_list.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        d.native_service_status.restype = ctypes.c_int
        d.native_service_status.argtypes = [wintypes.LPCWSTR,
                                            ctypes.c_char_p, ctypes.c_size_t]
        d.native_driver_status.restype = ctypes.c_int
        d.native_driver_status.argtypes = [wintypes.LPCWSTR,
                                           ctypes.c_char_p, ctypes.c_size_t]
        d.native_service_start.restype = ctypes.c_int
        d.native_service_start.argtypes = [wintypes.LPCWSTR]
        d.native_service_stop.restype = ctypes.c_int
        d.native_service_stop.argtypes = [wintypes.LPCWSTR]
        d.native_service_set_start.restype = ctypes.c_int
        d.native_service_set_start.argtypes = [wintypes.LPCWSTR, ctypes.c_int]

    # ---------- 通用调用 ----------

    def _json_call(self, fn, *args):
        buf = ctypes.create_string_buffer(1 << 16)
        rc = fn(buf, len(buf), *args)
        if rc < 0:
            raise BridgeError(f"native 调用失败 rc={rc}")
        return json.loads(buf.value.decode("utf-8"))

    def sysinfo(self) -> dict:
        if not self.available:
            return _SysInfoFallback.sysinfo()
        buf = ctypes.create_string_buffer(1 << 16)
        rc = self._dll.native_sysinfo_json(buf, len(buf))
        if rc < 0:
            raise BridgeError(f"native_sysinfo_json 失败 rc={rc}")
        return json.loads(buf.value.decode("utf-8"))

    def sysinfo_raw_json(self) -> str:
        buf = ctypes.create_string_buffer(1 << 16)
        rc = self._dll.native_sysinfo_json(buf, len(buf))
        if rc < 0:
            raise BridgeError(f"native_sysinfo_json 失败 rc={rc}")
        return buf.value.decode("utf-8")

    def machine_guid(self) -> dict:
        if not self.available:
            return {"machineGuid": _SysInfoFallback.machine_guid()}
        buf = ctypes.create_string_buffer(256)
        rc = self._dll.native_machine_guid(buf, len(buf))
        if rc < 0:
            raise BridgeError(f"native_machine_guid 失败 rc={rc}")
        return json.loads(buf.value.decode("utf-8"))

    def system_drive(self) -> dict:
        if not self.available:
            return _SysInfoFallback.system_drive()
        buf = ctypes.create_string_buffer(1024)
        rc = self._dll.native_system_drive(buf, len(buf))
        if rc < 0:
            raise BridgeError(f"native_system_drive 失败 rc={rc}")
        return json.loads(buf.value.decode("utf-8"))

    def extended(self) -> dict:
        """扩展系统信息（纯注册表/Win32 采集，DLL 可有可无）。"""
        return _SysInfoFallback.extended()

    def is_elevated(self) -> int:
        if not self.available:
            return _SysInfoFallback.is_elevated()
        return int(self._dll.native_is_elevated())

    def elevate_run(self, file, args="", workdir="") -> int:
        if not self.available:
            return _SysInfoFallback.elevate_run(file, args, workdir)
        return int(self._dll.native_elevate_run(file, args, workdir))

    # ---------- 驱动 / 服务（仅 DLL 可用） ----------

    def _require_native(self, op: str):
        if not self.available:
            raise BridgeError(f"{op} 需要 native_core.dll（请先执行 "
                              f"scripts/build.ps1 编译原生层）")

    def service_list(self) -> list:
        self._require_native("服务枚举")
        return self._json_call(self._dll.native_service_list)

    def service_status(self, name: str) -> dict:
        self._require_native("服务状态")
        return self._json_call(self._dll.native_service_status, name)

    def driver_status(self, name: str) -> dict:
        return self.service_status(name)

    def service_start(self, name: str) -> int:
        self._require_native("服务启动")
        return int(self._dll.native_service_start(name))

    def service_stop(self, name: str) -> int:
        self._require_native("服务停止")
        return int(self._dll.native_service_stop(name))

    def service_set_start(self, name: str, start_type: int) -> int:
        self._require_native("设置启动类型")
        return int(self._dll.native_service_set_start(name, start_type))

    def version(self) -> str:
        if not self.available:
            return None
        return self._dll.native_version().decode("utf-8", "replace")


# 全局单例
bridge = NativeBridge()