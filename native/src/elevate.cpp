/*
 * elevate.cpp — 权限控制：管理员令牌检测、UAC 提权拉起。
 */
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

#include <windows.h>
#include <shellapi.h>

#include <wchar.h>

#include "native.h"

/* 当前进程是否持有提升（管理员）令牌 */
int native_is_elevated(void) {
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token))
        return -1;
    TOKEN_ELEVATION elev{};
    DWORD size = 0;
    BOOL ok = GetTokenInformation(token, TokenElevation, &elev, sizeof(elev),
                                  &size);
    CloseHandle(token);
    if (!ok) return -1;
    return elev.TokenIsElevated ? 1 : 0;
}

/* 以管理员权限（runas）拉起一个程序，触发 UAC 确认 */
int native_elevate_run(const wchar_t* file, const wchar_t* args,
                       const wchar_t* workdir) {
    if (!file) return -1;
    HINSTANCE r = ShellExecuteW(nullptr, L"runas", file,
                                args ? args : L"", workdir ? workdir : L"",
                                SW_SHOWNORMAL);
    return (reinterpret_cast<INT_PTR>(r) > 32) ? 0 : -1;
}

/* 以管理员权限拉起当前进程所在 exe（args 传给新进程） */
int native_relaunch_elevated(const wchar_t* args, const wchar_t* workdir) {
    wchar_t self[MAX_PATH] = L"";
    DWORD n = GetModuleFileNameW(nullptr, self, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return -1;
    return native_elevate_run(self, args, workdir);
}