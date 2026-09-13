/*
 * native.cpp — C++ 兼容层 ABI 导出入口。
 * 组合 C 层 core（系统信息）与 C++ 层（驱动/服务/提权），统一导出 C ABI。
 */
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

#include <windows.h>

#include <string>

#include "native.h"
#include "json_out.h"
#include "sysinfo.h"

/* ---------------- 版本 ---------------- */

extern "C" NATIVE_API const char* native_version(void) {
    return "1.0.0";
}

/* ---------------- 系统信息（封装 C 层 core） ---------------- */

extern "C" NATIVE_API int native_sysinfo_json(char* buf, size_t len) {
    SysInfoSnapshot snap;
    if (sysinfo_snapshot(&snap) != 0) return -1;
    int need = sysinfo_snapshot_to_json(&snap, buf, len);
    return need;
}

extern "C" NATIVE_API int native_machine_guid(char* buf, size_t len) {
    wchar_t guid[64] = L"";
    DWORD size = sizeof(guid);
    DWORD type = 0;
    LONG r = RegGetValueW(HKEY_LOCAL_MACHINE,
                          L"SOFTWARE\\Microsoft\\Cryptography",
                          L"MachineGuid", RRF_RT_REG_SZ, &type,
                          guid, &size);
    std::string out;
    if (r == ERROR_SUCCESS) {
        out = "{\"machineGuid\":";
        nj::put_string(out, guid);
        out += '}';
    } else {
        out = R"({"machineGuid":""})";
    }
    nj::copy_out(out, buf, len);
    return static_cast<int>(out.size());
}

extern "C" NATIVE_API int native_system_drive(char* buf, size_t len) {
    wchar_t winDir[MAX_PATH] = L"";
    UINT n = GetWindowsDirectoryW(winDir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return -1;
    wchar_t root[4] = { winDir[0], L':', L'\\', L'\0' };

    std::string out = "{\"root\":";
    nj::put_string(out, root);

    ULARGE_INTEGER freeBytes, total;
    if (GetDiskFreeSpaceExW(root, &freeBytes, &total, nullptr)) {
        out += ",\"totalBytes\":";
        out += std::to_string(total.QuadPart);
        out += ",\"freeBytes\":";
        out += std::to_string(freeBytes.QuadPart);
    }
    wchar_t fs[16] = L"";
    if (GetVolumeInformationW(root, nullptr, 0, nullptr, nullptr, nullptr,
                              fs, 16)) {
        out += ",\"fs\":";
        nj::put_string(out, fs);
    }
    out += '}';
    nj::copy_out(out, buf, len);
    return static_cast<int>(out.size());
}