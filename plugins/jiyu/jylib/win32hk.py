# -*- coding: utf-8 -*-
"""ctypes 基础封装：进程、窗口、注册表、服务、设备、令牌。

只依赖标准库 ctypes，供 jytrainer / mythwaretoolkit 插件共用。
"""
import ctypes
from ctypes import wintypes

# ---------------------------------------------------------------- 基础类型
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
fltlib = ctypes.WinDLL("fltlib", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
ws2_32 = ctypes.WinDLL("ws2_32", use_last_error=True)

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
HWND = wintypes.HWND
LPCWSTR = wintypes.LPCWSTR
LPWSTR = wintypes.LPWSTR
LPVOID = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t
NTSTATUS = ctypes.c_long

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_vm_operation = 0x0008
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_CREATE_THREAD = 0x0002
PROCESS_TERMINATE = 0x0001
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_ALL_ACCESS = 0x1F0FFF
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPTHREAD = 0x00000004
THREAD_TERMINATE = 0x0001

# ---------------------------------------------------------------- 进程快照
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD), ("cntUsage", DWORD), ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ULONG_PTR), ("th32ModuleID", DWORD),
        ("cntThreads", DWORD), ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def create_toolhelp32_snapshot():
    return kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)


def list_processes():
    """返回 [{pid, ppid, name}, ...]（Toolhelp 快照）。"""
    snap = create_toolhelp32_snapshot()
    if snap == wintypes.HANDLE(-1).value or snap == ctypes.c_void_p(-1).value:
        return []
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out = []
    ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        out.append({"pid": int(pe.th32ProcessID),
                    "ppid": int(pe.th32ParentProcessID),
                    "name": pe.szExeFile})
        ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    kernel32.CloseHandle(snap)
    return out


def find_process(name):
    """按 exe 名精确查找，返回第一个 pid 或 0。"""
    name_l = name.lower()
    for p in list_processes():
        if p["name"].lower() == name_l:
            return p["pid"]
    return 0


def find_processes(name):
    """按 exe 名查找全部，返回 pid 列表。"""
    name_l = name.lower()
    return [p["pid"] for p in list_processes()
            if p["name"].lower() == name_l]


def open_process(pid, access=PROCESS_ALL_ACCESS):
    return kernel32.OpenProcess(access, False, pid)


def terminate_process(handle, exit_code=0):
    return bool(kernel32.TerminateProcess(handle, exit_code))


def terminate_pid(pid):
    h = open_process(pid, PROCESS_TERMINATE)
    if not h:
        return False, ctypes.get_last_error()
    ok = terminate_process(h, 0)
    kernel32.CloseHandle(h)
    return ok, 0 if ok else ctypes.get_last_error()


# ntdll 动态导出（NtSuspendProcess / NtResumeProcess / NtTerminateProcess）
ntdll.NtSuspendProcess.restype = NTSTATUS
ntdll.NtSuspendProcess.argtypes = [HANDLE]
ntdll.NtResumeProcess.restype = NTSTATUS
ntdll.NtResumeProcess.argtypes = [HANDLE]
NtSuspendProcess = ntdll.NtSuspendProcess
NtResumeProcess = ntdll.NtResumeProcess


def suspend_process(pid):
    h = open_process(pid, PROCESS_SUSPEND_RESUME)
    if not h:
        return False, ctypes.get_last_error()
    st = NtSuspendProcess(h)
    kernel32.CloseHandle(h)
    return st == 0, int(st)


def resume_process(pid):
    h = open_process(pid, PROCESS_SUSPEND_RESUME)
    if not h:
        return False, ctypes.get_last_error()
    st = NtResumeProcess(h)
    kernel32.CloseHandle(h)
    return st == 0, int(st)


# ---------------------------------------------------------------- 窗口
WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)

user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [HWND, LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [HWND, LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = DWORD
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]


def enum_windows(callback):
    """枚举所有顶层窗口，回调 (hwnd, lparam) -> bool 继续/停止。"""
    user32.EnumWindows(WNDENUMPROC(callback), 0)


def get_window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def get_window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_window_pid(hwnd):
    pid = DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def enum_windows_of_pid(pid):
    """枚举属于指定 pid 的顶层窗口，返回 hwnd 列表。"""
    found = []

    def cb(hwnd, lp):
        if get_window_pid(hwnd) == pid:
            found.append(hwnd)
        return True

    enum_windows(cb)
    return found


def find_window_windows():
    """遍历所有窗口，返回 [{hwnd, pid, class, text, visible, toplevel}]。"""
    out = []

    def cb(hwnd, lp):
        out.append({
            "hwnd": int(hwnd),
            "pid": get_window_pid(hwnd),
            "class": get_window_class(hwnd),
            "text": get_window_text(hwnd),
            "visible": bool(user32.IsWindowVisible(hwnd)),
        })
        return True

    enum_windows(cb)
    return out


# 窗口样式
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_BORDER = 0x00800000
WS_CAPTION = 0x00C00000
WS_SIZEBOX = 0x00040000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_OVERLAPPEDWINDOW = (WS_CAPTION | 0x00020000 | WS_SYSMENU | WS_THICKFRAME | 0x00040000 | 0x00010000)
WS_EX_TOPMOST = 0x00000008
WS_CHILD = 0x40000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = wintypes.HWND(-1)
HWND_NOTOPMOST = wintypes.HWND(-2)
SW_HIDE = 0
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOWNORMAL = 1

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [HWND, ctypes.c_int]
GetWindowLong = user32.GetWindowLongW
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [HWND, ctypes.c_int, ctypes.c_long]
SetWindowLong = user32.SetWindowLongW
user32.SetWindowPos.restype = BOOL
user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
SetWindowPos = user32.SetWindowPos
user32.IsWindowVisible.restype = BOOL
user32.IsWindowVisible.argtypes = [HWND]
user32.ShowWindow.restype = BOOL
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
ShowWindow = user32.ShowWindow
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetCursorPos.restype = BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]


def get_window_style(hwnd):
    return GetWindowLong(hwnd, GWL_STYLE)


def get_window_exstyle(hwnd):
    return GetWindowLong(hwnd, GWL_EXSTYLE)


def set_window_style(hwnd, style):
    return SetWindowLong(hwnd, GWL_STYLE, style)


def set_window_exstyle(hwnd, ex):
    return SetWindowLong(hwnd, GWL_EXSTYLE, ex)


def make_wparam_rect(w, h):
    return (h << 16) | (w & 0xFFFF)


WM_SIZE = 0x0005
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_GETTEXT = 0x000D
user32.PostMessageW.restype = BOOL
user32.PostMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM,
                                wintypes.LPARAM]


def get_cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def get_screen_size():
    return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))


user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = BOOL


# ---------------------------------------------------------------- 注册表
HKEY = HANDLE
KEY_QUERY_VALUE = 0x0001
KEY_SET_VALUE = 0x0002
KEY_WOW64_64KEY = 0x0100
KEY_WOW64_32KEY = 0x0200
REG_SZ = 1
REG_DWORD = 4
HKEY_LOCAL_MACHINE = HKEY(0x80000002)
HKEY_CURRENT_USER = HKEY(0x80000001)
ERROR_SUCCESS = 0
advapi32.RegOpenKeyExW.restype = ctypes.c_long
advapi32.RegOpenKeyExW.argtypes = [HKEY, LPCWSTR, DWORD, DWORD,
                                   ctypes.POINTER(HKEY)]
advapi32.RegQueryValueExW.restype = ctypes.c_long
advapi32.RegDeleteValueW.restype = ctypes.c_long
advapi32.RegDeleteValueW.argtypes = [HKEY, LPCWSTR]


def reg_open(root, subkey, access=KEY_QUERY_VALUE, wow64=0):
    h = HKEY()
    rc = advapi32.RegOpenKeyExW(root, subkey, 0, access | wow64,
                                ctypes.byref(h))
    if rc != ERROR_SUCCESS:
        return None
    return h


def reg_query_value(hkey, name):
    """读取 REG_SZ 或 REG_DWORD；返回 ('sz'|'dw', value) 或 None。"""
    # 先查类型/长度
    ptype = DWORD()
    psize = DWORD(0)
    rc = advapi32.RegQueryValueExW(hkey, name, None,
                                   ctypes.byref(ptype), None,
                                   ctypes.byref(psize))
    if rc != ERROR_SUCCESS or psize.value == 0:
        return None
    typ = int(ptype.value)
    buf = ctypes.create_string_buffer(int(psize.value))
    rc = advapi32.RegQueryValueExW(hkey, name, None,
                                   ctypes.byref(ptype), buf,
                                   ctypes.byref(psize))
    if rc != ERROR_SUCCESS:
        return None
    raw = buf.raw[: int(psize.value)]
    if typ == REG_DWORD:
        import struct
        return ("dw", struct.unpack("<I", raw[:4])[0])
    if typ == REG_SZ:
        return ("sz", raw.decode("utf-16-le", errors="replace")
                .rstrip("\x00"))
    return ("raw", raw)


def reg_delete_value(hkey, name):
    return advapi32.RegDeleteValueW(hkey, name) == ERROR_SUCCESS


def reg_close(hkey):
    advapi32.RegCloseKey(hkey)


# ---------------------------------------------------------------- 服务(SCM)
SC_MANAGER_CONNECT = 0x0001
SC_MANAGER_ALL_ACCESS = 0xF003F
SERVICE_STOP = 0x0020
SERVICE_DELETE = 0x01000
SERVICE_ALL_ACCESS = 0xF01FF
SERVICE_KERNEL_DRIVER = 0x00000001
SERVICE_DEMAND_START = 0x00000003
SERVICE_ERROR_IGNORE = 0x00000000
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", DWORD), ("dwCurrentState", DWORD),
        ("dwControlsAccepted", DWORD), ("dwWin32ExitCode", DWORD),
        ("dwServiceSpecificExitCode", DWORD), ("dwCheckPoint", DWORD),
        ("dwWaitHint", DWORD),
    ]


def open_sc_manager(access=SC_MANAGER_CONNECT):
    return advapi32.OpenSCManagerW(None, None, access)


def open_service(scm, name, access=SERVICE_ALL_ACCESS):
    return advapi32.OpenServiceW(scm, name, access)


def create_kernel_service(scm, name, display, driver_path):
    """创建内核驱动服务（SERVICE_KERNEL_DRIVER）。返回句柄或 None。"""
    h = advapi32.CreateServiceW(
        scm, name, display, SERVICE_ALL_ACCESS,
        SERVICE_KERNEL_DRIVER, SERVICE_DEMAND_START, SERVICE_ERROR_IGNORE,
        driver_path, None, None, None, None, None)
    return h or None


def start_service(hsvc):
    return bool(advapi32.StartServiceW(hsvc, 0, None))


def control_service(hsvc, code):
    st = SERVICE_STATUS()
    ok = advapi32.ControlService(hsvc, code, ctypes.byref(st))
    return ok, st


def delete_service(hsvc):
    return bool(advapi32.DeleteService(hsvc))


def close_service_handle(h):
    advapi32.CloseServiceHandle(h)


# ---------------------------------------------------------------- 设备 IOCTL
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 0x00000001
FILE_ATTRIBUTE_NORMAL = 0x80


def create_device(path, access=GENERIC_READ | GENERIC_WRITE):
    h = kernel32.CreateFileW(path, access, FILE_SHARE_READ, None,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    return h if h and h != wintypes.HANDLE(-1).value \
        and h != ctypes.c_void_p(-1).value else None


def device_io_control(h, code, in_buf=None, in_size=0):
    """同步 DeviceIoControl；返回 (ok, last_error)。"""
    out = ctypes.create_string_buffer(64)
    ret = DWORD()
    ok = kernel32.DeviceIoControl(
        h, code,
        in_buf if in_buf else None, in_size,
        out, ctypes.sizeof(out), ctypes.byref(ret), None)
    return bool(ok), ctypes.get_last_error()


# ---------------------------------------------------------------- 令牌 / 提权
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_DUPLICATE = 0x0002
TOKEN_ASSIGN_PRIMARY = 0x0001
TokenPrimary = 1
SecurityIdentification = 2
SE_PRIVILEGE_ENABLED = 0x00000002
MAXIMUM_ALLOWED = 0x02000000
LOGON_NETCREDENTIALS_ONLY = 0x00000002
NORMAL_PRIORITY_CLASS = 0x00000020
CREATE_NEW_PROCESS_GROUP = 0x00000200
STARTF_USESHOWWINDOW = 0x00000001
SW_SHOW = 5
SW_HIDE = 0


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", DWORD), ("HighPart", ctypes.c_long)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD), ("lpReserved", LPWSTR), ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR), ("dwX", DWORD), ("dwY", DWORD),
        ("dwXSize", DWORD), ("dwYSize", DWORD), ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD), ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.POINTER(
            ctypes.c_byte)), ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


def enable_debug_privilege():
    """启用 SeDebugPrivilege。返回 bool。"""
    hToken = HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                     ctypes.byref(hToken)):
        return False
    luid = LUID()
    if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege",
                                          ctypes.byref(luid)):
        kernel32.CloseHandle(hToken)
        return False
    tp = TOKEN_PRIVILEGES()
    tp.PrivilegeCount = 1
    tp.Privileges[0].Luid = luid
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    ok = bool(advapi32.AdjustTokenPrivileges(
        hToken, False, ctypes.byref(tp), ctypes.sizeof(TOKEN_PRIVILEGES),
        None, None))
    kernel32.CloseHandle(hToken)
    return ok


def open_process_token(pid, access=TOKEN_DUPLICATE | TOKEN_QUERY):
    hProc = open_process(pid, PROCESS_QUERY_INFORMATION)
    if not hProc:
        return None
    hTok = HANDLE()
    if not advapi32.OpenProcessToken(hProc, access, ctypes.byref(hTok)):
        kernel32.CloseHandle(hProc)
        return None
    kernel32.CloseHandle(hProc)
    return hTok


def system_token():
    """复制 lsass/winlogon 的 SYSTEM 令牌（需 SeDebugPrivilege）。"""
    for name in ("lsass.exe", "winlogon.exe", "services.exe"):
        pid = find_process(name)
        if not pid:
            continue
        hTok = open_process_token(pid)
        if not hTok:
            continue
        hDup = HANDLE()
        ok = advapi32.DuplicateTokenEx(
            hTok, MAXIMUM_ALLOWED, None, SecurityIdentification,
            TokenPrimary, ctypes.byref(hDup))
        kernel32.CloseHandle(hTok)
        if ok:
            return hDup
    return None


def create_process_with_token(hTok, cmdline, show=SW_SHOW):
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = show
    pi = PROCESS_INFORMATION()
    ok = advapi32.CreateProcessWithTokenW(
        hTok, LOGON_NETCREDENTIALS_ONLY, None, cmdline,
        NORMAL_PRIORITY_CLASS | CREATE_NEW_PROCESS_GROUP,
        None, None, ctypes.byref(si), ctypes.byref(pi))
    if ok:
        pid = int(pi.dwProcessId)
        kernel32.CloseHandle(pi.hProcess)
        kernel32.CloseHandle(pi.hThread)
        return pid
    return 0


def is_elevated():
    """简单判断当前进程是否管理员提权。返回 bool。"""
    hToken = HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_QUERY, ctypes.byref(hToken)):
        return False
    info = DWORD()
    size = DWORD()
    # TokenElevation = 20
    ok = advapi32.GetTokenInformation(hToken, 20, ctypes.byref(info),
                                      ctypes.sizeof(DWORD),
                                      ctypes.byref(size))
    kernel32.CloseHandle(hToken)
    return bool(ok and info.value)


def shell_runas(exe, params="", show=SW_SHOW):
    """ShellExecuteEx runas 提权启动外部程序。返回 pid 或 0。"""
    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", DWORD), ("fMask", DWORD), ("hwnd", HWND),
            ("lpVerb", LPCWSTR), ("lpFile", LPCWSTR), ("lpParameters",
                                                       LPCWSTR),
            ("lpDirectory", LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", HANDLE), ("lpIDList", LPVOID),
            ("lpClass", LPCWSTR), ("hkeyClass", HANDLE),
            ("dwHotKey", DWORD), ("hIcon", HANDLE), ("hProcess", HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.nShow = show
    ok = shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        return 0
    pid = 0
    if sei.hProcess:
        pid = int(kernel32.GetProcessId(sei.hProcess))
        kernel32.CloseHandle(sei.hProcess)
    return pid


def launch_process(exe, params="", show=SW_SHOW):
    """CreateProcessW 启动外部程序（不提权）。返回 pid 或 0。"""
    cmdline = exe
    if params:
        cmdline = '"' + exe + '" ' + params
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = show
    pi = PROCESS_INFORMATION()
    ok = kernel32.CreateProcessW(
        exe, cmdline, None, None, False,
        NORMAL_PRIORITY_CLASS | CREATE_NEW_PROCESS_GROUP,
        None, None, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        return 0
    pid = int(pi.dwProcessId)
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(pi.hThread)
    return pid


# ---------------------------------------------------------------- 防截屏
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011
user32.SetWindowDisplayAffinity.restype = BOOL
user32.SetWindowDisplayAffinity.argtypes = [HWND, DWORD]


def set_display_affinity(hwnd, mode=WDA_EXCLUDEFROMCAPTURE):
    return bool(user32.SetWindowDisplayAffinity(hwnd, mode))


# ---------------------------------------------------------------- 防杀进程 ACL
def protect_current_process():
    """给当前进程 ACL 添加 Everyone DENY PROCESS_TERMINATE。"""
    import ctypes  # noqa: F811 (局部别名，避免与顶部混淆)
    from ctypes import wintypes as _wt

    advapi32.GetSecurityInfo.restype = ctypes.c_long
    advapi32.SetEntriesInAclW.restype = ctypes.c_long
    advapi32.SetSecurityInfo.restype = ctypes.c_long

    SE_KERNEL_OBJECT = 6
    DACL_SECURITY_INFORMATION = 0x00000004
    DENY_ACCESS = 0x00000001
    NO_INHERITANCE = 0x00000000
    TRUSTEE_IS_SID = 0
    SECURITY_WORLD_SID_AUTHORITY = bytes([0, 0, 0, 0, 0, 1])
    SECURITY_WORLD_RID = 0

    class ACL(ctypes.Structure):
        _fields_ = [("AclRevision", _wt.BYTE),
                    ("Sbz1", _wt.BYTE),
                    ("AclSize", _wt.WORD),
                    ("AceCount", _wt.WORD),
                    ("Sbz2", _wt.WORD)]

    class TRUSTEEW(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", LPVOID),
                    ("MultipleTrusteeOperation", _wt.BYTE),
                    ("TrusteeForm", _wt.BYTE),
                    ("TrusteeType", _wt.BYTE),
                    ("ptstrName", LPVOID)]

    class EXPLICIT_ACCESSW(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", DWORD),
            ("grfAccessMode", _wt.BYTE),
            ("grfInheritance", DWORD),
            ("Trustee", TRUSTEEW),
        ]

    hProc = kernel32.GetCurrentProcess()
    pOldDacl = ctypes.POINTER(ACL)()
    pSD = ctypes.POINTER(ctypes.c_byte)()

    rc = advapi32.GetSecurityInfo(
        hProc, SE_KERNEL_OBJECT, DACL_SECURITY_INFORMATION,
        None, None, ctypes.byref(pOldDacl), None, ctypes.byref(pSD))
    if rc != 0:
        return False, f"GetSecurityInfo: {rc}"

    # 构造 Everyone SID（S-1-1-0）
    class SID_IDENTIFIER_AUTHORITY(ctypes.Structure):
        _fields_ = [("Value", ctypes.c_ubyte * 6)]

    pSid = ctypes.c_void_p()
    auth = SID_IDENTIFIER_AUTHORITY()
    for i, v in enumerate([0, 0, 0, 0, 0, 1]):
        auth.Value[i] = v
    if not advapi32.AllocateAndInitializeSid(
            ctypes.byref(auth), 1, SECURITY_WORLD_RID, 0, 0, 0, 0, 0, 0, 0,
            ctypes.byref(pSid)):
        return False, "AllocateAndInitializeSid failed"

    ea = EXPLICIT_ACCESSW()
    ea.grfAccessPermissions = PROCESS_TERMINATE
    ea.grfAccessMode = DENY_ACCESS
    ea.grfInheritance = NO_INHERITANCE
    ea.Trustee.TrusteeForm = TRUSTEE_IS_SID
    ea.Trustee.ptstrName = pSid

    pNewDacl = ctypes.POINTER(ACL)()
    rc = advapi32.SetEntriesInAclW(1, ctypes.byref(ea), pOldDacl,
                                   ctypes.byref(pNewDacl))
    if rc != 0:
        return False, f"SetEntriesInAcl: {rc}"

    rc = advapi32.SetSecurityInfo(
        hProc, SE_KERNEL_OBJECT, DACL_SECURITY_INFORMATION,
        None, None, pNewDacl, None)
    advapi32.FreeSid(pSid)
    if rc != 0:
        return False, f"SetSecurityInfo: {rc}"
    return True, "ok"


# ---------------------------------------------------------------- Windows 句柄工具
def close_handle(h):
    kernel32.CloseHandle(h)


def get_last_error():
    return ctypes.get_last_error()


# ---------------------------------------------------------------- TCP 表（UDP 端口发现）
class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", DWORD), ("dwLocalAddr", DWORD), ("dwLocalPort", DWORD),
        ("dwRemoteAddr", DWORD), ("dwRemotePort", DWORD),
        ("dwOwningPid", DWORD),
    ]


class MIB_TCPTABLE_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwNumEntries", DWORD),
                ("table", MIB_TCPROW_OWNER_PID * 1)]


def get_tcp_listen_ports(pid):
    """返回某进程在 127.0.0.1 上监听的 TCP 端口列表。"""
    ports = []
    try:
        for size in (4096, 16384, 65536):
            buf = ctypes.create_string_buffer(size)
            dwSize = DWORD(size)
            rc = iphlpapi.GetExtendedTcpTable(
                buf, ctypes.byref(dwSize), True, 2, 5, 0)
            if rc == 0:
                table = ctypes.cast(
                    buf, ctypes.POINTER(MIB_TCPTABLE_OWNER_PID)).contents
                n = int(table.dwNumEntries)
                # 表实际行数是变长的，逐条读取
                base = ctypes.addressof(table.table)
                row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)
                for i in range(n):
                    row = ctypes.cast(
                        base + i * row_size,
                        ctypes.POINTER(MIB_TCPROW_OWNER_PID)).contents
                    # MIB_TCP_STATE_LISTEN = 2
                    if int(row.dwState) == 2 and int(row.dwOwningPid) == pid:
                        # 本地地址 127.0.0.1 (0x0100007f) 或任意(0)
                        addr = int(row.dwLocalAddr)
                        if addr in (0x0100007F, 0):
                            ports.append(int(row.dwLocalPort))
                break
            if rc == 122:  # ERROR_INSUFFICIENT_BUFFER
                continue
            break
    except Exception:  # noqa: BLE001
        pass
    return ports