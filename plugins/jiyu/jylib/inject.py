# -*- coding: utf-8 -*-
"""DLL 注入/卸载（移植自 JiYuTrainer TrainerWorker.cpp InjectDll/UnInjectDll）。

注入：OpenProcess -> VirtualAllocEx(写 DLL 路径) ->
      WriteProcessMemory -> CreateRemoteThread(LoadLibraryW)
卸载：GetModuleHandleW 取远程度块句柄 -> CreateRemoteThread(FreeLibrary)
"""
import ctypes
from ctypes import wintypes

from .win32hk import kernel32, ntdll

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
SIZE_T = ctypes.c_size_t
LPVOID = ctypes.c_void_p

PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_DECOMMIT = 0x4000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF

# ntdll NtOpenProcess
class CLIENT_ID(ctypes.Structure):
    _fields_ = [("UniqueProcess", HANDLE), ("UniqueThread", HANDLE)]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Length", DWORD), ("RootDirectory", HANDLE),
                ("ObjectName", LPVOID), ("Attributes", DWORD),
                ("SecurityDescriptor", LPVOID), ("SecurityQualityOfService",
                                                 LPVOID)]


ntdll.NtOpenProcess.restype = ctypes.c_long
ntdll.NtOpenProcess.argtypes = [
    ctypes.POINTER(HANDLE), DWORD, ctypes.POINTER(OBJECT_ATTRIBUTES),
    ctypes.POINTER(CLIENT_ID)]

kernel32.VirtualAllocEx.restype = LPVOID
kernel32.VirtualAllocEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD, DWORD]
kernel32.VirtualFreeEx.restype = wintypes.BOOL
kernel32.VirtualFreeEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.CreateRemoteThread.restype = HANDLE
kernel32.CreateRemoteThread.argtypes = [HANDLE, LPVOID, SIZE_T, LPVOID,
                                        LPVOID, DWORD, LPVOID]
kernel32.WaitForSingleObject.restype = DWORD
kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
kernel32.GetExitCodeThread.restype = wintypes.BOOL
kernel32.GetExitCodeThread.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetProcAddress.restype = LPVOID
kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]


def _open_process_nt(pid):
    """用 NtOpenProcess 打开进程句柄（同等权限下更隐蔽）。"""
    h = HANDLE()
    cid = CLIENT_ID()
    cid.UniqueProcess = HANDLE(pid)
    oa = OBJECT_ATTRIBUTES()
    oa.Length = ctypes.sizeof(OBJECT_ATTRIBUTES)
    st = ntdll.NtOpenProcess(ctypes.byref(h), PROCESS_ALL_ACCESS,
                             ctypes.byref(oa), ctypes.byref(cid))
    if st != 0:
        return None, st
    return h, 0


def inject_dll(pid, dll_path):
    """向 pid 注入 dll_path（经典 LoadLibraryW 远程线程法）。返回 (ok, msg)。"""
    hProc, st = _open_process_nt(pid)
    if not hProc:
        return False, f"NtOpenProcess 失败: 0x{st:08X}"

    path_w = dll_path + "\x00"
    size = len(path_w.encode("utf-16-le"))

    remote = kernel32.VirtualAllocEx(hProc, None, size, MEM_COMMIT,
                                     PAGE_READWRITE)
    if not remote:
        kernel32.CloseHandle(hProc)
        return False, f"VirtualAllocEx 失败: {ctypes.get_last_error()}"

    if not kernel32.WriteProcessMemory(hProc, remote, path_w.encode(
            "utf-16-le"), size, None):
        kernel32.VirtualFreeEx(hProc, remote, 0, MEM_DECOMMIT)
        kernel32.CloseHandle(hProc)
        return False, f"WriteProcessMemory 失败: {ctypes.get_last_error()}"

    load_lib = kernel32.GetProcAddress(kernel32.GetModuleHandleW("Kernel32"),
                                       b"LoadLibraryW")
    if not load_lib:
        kernel32.VirtualFreeEx(hProc, remote, 0, MEM_DECOMMIT)
        kernel32.CloseHandle(hProc)
        return False, "GetProcAddress(LoadLibraryW) 失败"

    hThread = kernel32.CreateRemoteThread(hProc, None, 0, load_lib, remote,
                                          0, None)
    if not hThread:
        kernel32.VirtualFreeEx(hProc, remote, 0, MEM_DECOMMIT)
        kernel32.CloseHandle(hProc)
        return False, f"CreateRemoteThread 失败: {ctypes.get_last_error()}"

    kernel32.WaitForSingleObject(hThread, INFINITE)
    kernel32.VirtualFreeEx(hProc, remote, 0, MEM_DECOMMIT)
    kernel32.CloseHandle(hThread)
    kernel32.CloseHandle(hProc)
    return True, f"已注入 {dll_path} -> pid {pid}"


def uninject_dll(pid, module_name):
    """卸载 pid 中已加载的 module_name（GetModuleHandleW + FreeLibrary）。"""
    hProc, st = _open_process_nt(pid)
    if not hProc:
        return False, f"NtOpenProcess 失败: 0x{st:08X}"

    name_w = module_name + "\x00"
    size = len(name_w.encode("utf-16-le"))

    remote = kernel32.VirtualAllocEx(hProc, None, size, MEM_COMMIT,
                                     PAGE_READWRITE)
    if not remote:
        kernel32.CloseHandle(hProc)
        return False, f"VirtualAllocEx 失败: {ctypes.get_last_error()}"
    if not kernel32.WriteProcessMemory(hProc, remote, name_w.encode(
            "utf-16-le"), size, None):
        kernel32.VirtualFreeEx(hProc, remote, 0, MEM_DECOMMIT)
        kernel32.CloseHandle(hProc)
        return False, f"WriteProcessMemory 失败: {ctypes.get_last_error()}"

    get_mod = kernel32.GetProcAddress(kernel32.GetModuleHandleW("Kernel32"),
                                      b"GetModuleHandleW")
    hThread = kernel32.CreateRemoteThread(hProc, None, 0, get_mod, remote,
                                          0, None)
    if not hThread:
        kernel32.VirtualFreeEx(hProc, remote, 0, MEM_DECOMMIT)
        kernel32.CloseHandle(hProc)
        return False, f"创建 GetModuleHandle 线程失败: {ctypes.get_last_error()}"

    kernel32.WaitForSingleObject(hThread, INFINITE)
    mod = DWORD()
    kernel32.GetExitCodeThread(hThread, ctypes.byref(mod))
    kernel32.VirtualFreeEx(hProc, remote, 0, MEM_DECOMMIT)
    kernel32.CloseHandle(hThread)

    if mod.value == 0:
        kernel32.CloseHandle(hProc)
        return False, f"{module_name} 未在 pid {pid} 中加载"

    free_lib = kernel32.GetProcAddress(kernel32.GetModuleHandleW("Kernel32"),
                                       b"FreeLibrary")
    hThread2 = kernel32.CreateRemoteThread(hProc, None, 0, free_lib,
                                           LPVOID(mod.value), 0, None)
    if not hThread2:
        kernel32.CloseHandle(hProc)
        return False, f"创建 FreeLibrary 线程失败: {ctypes.get_last_error()}"
    kernel32.WaitForSingleObject(hThread2, INFINITE)
    kernel32.CloseHandle(hThread2)
    kernel32.CloseHandle(hProc)
    return True, f"已卸载 {module_name} from pid {pid}"