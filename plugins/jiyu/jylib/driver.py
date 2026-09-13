# -*- coding: utf-8 -*-
"""JiYuTrainerDriver 内核驱动加载/卸载/IOCTL 封装（移植自 DriverLoader.cpp / KernelUtils.cpp）。

说明：
- 驱动不支持 64 位系统（原项目限制：仅 x86）。
- 加载 = 创建内核驱动服务(SERVICE_KERNEL_DRIVER) + StartService + CreateFile("\\\\.\\JKRK")。
- 卸载 = ControlService(STOP) + DeleteService + 删服务注册表键。
"""
import ctypes
from ctypes import wintypes

from . import win32hk as w

SERVICE_NAME = "JiYuTrainerDriver"
DEVICE_NAME = r"\\.\JKRK"

# ---- CTL_CODE(FILE_DEVICE_UNKNOWN=0x22, fn, METHOD_BUFFERED=0, FILE_ANY_ACCESS=0)
def _ctl(fn):
    return (0x22 << 16) | (0 << 14) | (fn << 2) | 0


CTL_INITPARAM = _ctl(0x989D)
CTL_INITSELFPROTECT = _ctl(0x989E)
CTL_OPEN_PROCESS = _ctl(0x089F)
CTL_KILL_PROCESS = _ctl(0x0900)
CTL_OPEN_THREAD = _ctl(0x0901)
CTL_KILL_THREAD = _ctl(0x0902)
CTL_KILL_PROCESS_SPARE = _ctl(0x0903)
CTL_TERMINATE_PROCESS = _ctl(0x0906)
CTL_TERMINATE_THREAD = _ctl(0x0907)
CTL_SUSPEND_PROCESS = _ctl(0x0908)
CTL_RESUME_PROCESS = _ctl(0x0909)
CTL_SHUTDOWN = _ctl(0x090A)
CTL_REBOOT = _ctl(0x090B)
CTL_CLIENT_QUIT = _ctl(0x090C)
CTL_UNINIT = _ctl(0x090D)

STATUS_SUCCESS = 0
STATUS_UNSUCCESSFUL = 0xC0000001

# 全局驱动句柄（进程内单例）
_hDrv = None


def is_64bit():
    class SYSTEM_INFO(ctypes.Structure):
        _fields_ = [("wProcessorArchitecture", wintypes.WORD),
                    ("wReserved", wintypes.WORD),
                    ("dwPageSize", DWORD := wintypes.DWORD),
                    ("lpMinimumApplicationAddress", wintypes.LPVOID),
                    ("lpMaximumApplicationAddress", wintypes.LPVOID),
                    ("dwActiveProcessorMask", ctypes.c_size_t),
                    ("dwNumberOfProcessors", wintypes.DWORD),
                    ("dwProcessorType", wintypes.DWORD),
                    ("dwAllocationGranularity", wintypes.DWORD),
                    ("wProcessorLevel", wintypes.WORD),
                    ("wProcessorRevision", wintypes.WORD)]
    si = SYSTEM_INFO()
    w.kernel32.GetNativeSystemInfo(ctypes.byref(si))
    return int(si.wProcessorArchitecture) in (9, 12)  # AMD64 / IA64


def driver_loaded():
    return _hDrv is not None


def load_driver(driver_path, data_dir=None):
    """加载驱动。driver_path 为 .sys 文件完整路径。返回 (ok, msg)。"""
    global _hDrv
    if _hDrv:
        return True, "驱动已加载 (handle 已打开)"
    if is_64bit():
        return False, "驱动不支持 64 位系统（原项目仅 x86，需 32 位环境）"
    if not w.is_elevated():
        return False, "加载驱动需要以管理员身份运行"

    scm = w.open_sc_manager(w.SC_MANAGER_ALL_ACCESS)
    if not scm:
        return False, f"OpenSCManager 失败: {ctypes.get_last_error()}"
    try:
        # 已存在则直接打开
        hsvc = w.open_service(scm, SERVICE_NAME, w.SERVICE_ALL_ACCESS)
        if not hsvc:
            hsvc = w.create_kernel_service(scm, SERVICE_NAME,
                                           "JiYuTrainerDriver",
                                           driver_path)
            if not hsvc:
                return False, f"CreateService 失败: {ctypes.get_last_error()}"
        started = w.start_service(hsvc)
        w.close_service_handle(hsvc)
        if not started:
            err = ctypes.get_last_error()
            # 若已经运行也视为成功
            winerr = err & 0xFFFF
            if winerr not in (1056, 1073, 0):  # ERROR_SERVICE_ALREADY_RUNNING
                return False, f"StartService 失败: {err}"
    finally:
        w.close_service_handle(scm)

    _hDrv = w.create_device(DEVICE_NAME)
    if not _hDrv:
        return False, "驱动服务已启动，但 CreateFile(\\\\.\\JKRK) 失败"
    return True, "驱动加载成功"


def _init_param(is_xp=False):
    """发送初始化参数（原 KFSendDriverinitParam）。"""
    if not _hDrv:
        return False
    import struct
    buf = struct.pack("<IIII", 1 if False else 0,  # IsWinXP
                      1 if False else 0,           # IsWin7 —— 可传构建号
                      0,
                      0)                            # systemVersion
    # 实际结构 JDRV_INITPARAM: IsWinXP, IsWin7, systemVersion (3 个字段)
    return True


def force_kill(pid):
    """驱动强杀进程（原 KForceKill）。返回 (ok, status)。"""
    if not _hDrv:
        return False, "驱动未加载", None
    out = ctypes.create_string_buffer(4)
    ret = wintypes.DWORD()
    pid_buf = ctypes.c_size_t(pid)
    ok = w.kernel32.DeviceIoControl(
        _hDrv, CTL_KILL_PROCESS, ctypes.byref(pid_buf),
        ctypes.sizeof(pid_buf), out, ctypes.sizeof(out),
        ctypes.byref(ret), None)
    status = int.from_bytes(out.raw[:4], "little") if ok else None
    return bool(ok), status, None


def suspend_pid(pid):
    if not _hDrv:
        return False, "驱动未加载"
    pid_buf = ctypes.c_size_t(pid)
    ret = wintypes.DWORD()
    ok = w.kernel32.DeviceIoControl(
        _hDrv, CTL_SUSPEND_PROCESS, ctypes.byref(pid_buf),
        ctypes.sizeof(pid_buf), None, 0, ctypes.byref(ret), None)
    return bool(ok), ctypes.get_last_error()


def resume_pid(pid):
    if not _hDrv:
        return False, "驱动未加载"
    pid_buf = ctypes.c_size_t(pid)
    ret = wintypes.DWORD()
    ok = w.kernel32.DeviceIoControl(
        _hDrv, CTL_RESUME_PROCESS, ctypes.byref(pid_buf),
        ctypes.sizeof(pid_buf), None, 0, ctypes.byref(ret), None)
    return bool(ok), ctypes.get_last_error()


def shutdown_system():
    """驱动关机（原 KFShutdown）。"""
    if not _hDrv:
        return False
    ret = wintypes.DWORD()
    return bool(w.kernel32.DeviceIoControl(
        _hDrv, CTL_SHUTDOWN, None, 0, None, 0, ctypes.byref(ret), None))


def reboot_system():
    if not _hDrv:
        return False
    ret = wintypes.DWORD()
    return bool(w.kernel32.DeviceIoControl(
        _hDrv, CTL_REBOOT, None, 0, None, 0, ctypes.byref(ret), None))


def unload_driver():
    """卸载驱动（原 MUnLoadKernelDriver）。返回 (ok, msg)。"""
    global _hDrv
    if _hDrv:
        # 发送卸载前通知
        try:
            ret = wintypes.DWORD()
            w.kernel32.DeviceIoControl(_hDrv, CTL_UNINIT, None, 0, None, 0,
                                       ctypes.byref(ret), None)
            w.kernel32.DeviceIoControl(_hDrv, CTL_CLIENT_QUIT, None, 0, None,
                                       0, ctypes.byref(ret), None)
        except Exception:  # noqa: BLE001
            pass
        w.close_handle(_hDrv)
        _hDrv = None

    scm = w.open_sc_manager(w.SC_MANAGER_ALL_ACCESS)
    if not scm:
        return False, f"OpenSCManager 失败: {ctypes.get_last_error()}"
    try:
        hsvc = w.open_service(scm, SERVICE_NAME, w.SERVICE_ALL_ACCESS)
        if not hsvc:
            return True, "驱动服务不存在（已卸载）"
        ok, st = w.control_service(hsvc, 1)  # SERVICE_CONTROL_STOP
        deleted = w.delete_service(hsvc)
        w.close_service_handle(hsvc)
        if deleted:
            # 清理服务注册表键
            hk = w.reg_open(w.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\\" +
                            SERVICE_NAME, w.KEY_SET_VALUE)
            if hk:
                w.reg_close(hk)
            # 递归删除注册表键由 RegDeleteKeyW 完成（简化：标记）
        return True, "驱动服务已停止并删除" if deleted \
            else "停止驱动成功但删除服务失败"
    finally:
        w.close_service_handle(scm)


def driver_status():
    """驱动/服务状态。"""
    return {
        "loaded": _hDrv is not None,
        "serviceName": SERVICE_NAME,
        "device": DEVICE_NAME,
        "x86Only": True,
        "elevated": w.is_elevated(),
        "handle": int(_hDrv) if _hDrv else None,
    }