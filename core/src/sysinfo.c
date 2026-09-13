/*
 * sysinfo.c — C 最底层实现：直接调用 Win32 API 采集系统信息。
 *
 * 遵守约定：不依赖任何 C/C++ 运行时之外的库；只做采集，不做决策。
 */
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define UNICODE
#define _UNICODE

#include <wchar.h>
#include <windows.h>
#include <psapi.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>

#include "sysinfo.h"

/* UTF-8 / 宽字符转换辅助 */
static int wstr_to_utf8(const wchar_t* in, char* out, size_t outLen) {
    if (!in || !out || outLen == 0) return -1;
    int n = WideCharToMultiByte(CP_UTF8, 0, in, -1, out, (int)outLen, NULL, NULL);
    if (n <= 0) { out[0] = '\0'; return -1; }
    return 0;
}

/* ---------- 小型 JSON 追加器 ---------- */
typedef struct JsonBuf {
    char* buf;
    size_t cap;   /* 含结尾 NUL */
    size_t used;  /* 已写入字节（不含 NUL，<= cap-1） */
    size_t need;  /* 累计所需长度 */
} JsonBuf;

static void jb_init(JsonBuf* b, char* buf, size_t cap) {
    b->buf = buf; b->cap = cap; b->used = 0; b->need = 0;
    if (cap > 0) buf[0] = '\0';
}

static void jb_raw(JsonBuf* b, const char* s, size_t n) {
    b->need += n;
    size_t room = (b->cap > 0) ? (b->cap - 1) : 0;
    if (room > b->used) {
        size_t take = n;
        if (take > room - b->used) take = room - b->used;
        memcpy(b->buf + b->used, s, take);
        b->used += take;
        b->buf[b->used] = '\0';
    }
}

static void jb_printf(JsonBuf* b, const char* fmt, ...) {
    char tmp[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    if (n < 0) return;
    jb_raw(b, tmp, (size_t)n);
}

/* 追加一个 JSON 字符串（宽字符 → UTF-8 → 转义） */
static void jb_wstring(JsonBuf* b, const wchar_t* s) {
    char utf8[512];
    wstr_to_utf8(s, utf8, sizeof(utf8));
    jb_raw(b, "\"", 1);
    for (const char* p = utf8; *p; ++p) {
        unsigned char ch = (unsigned char)*p;
        switch (ch) {
            case '"':  jb_raw(b, "\\\"", 2); break;
            case '\\': jb_raw(b, "\\\\", 2); break;
            case '\n': jb_raw(b, "\\n", 2); break;
            case '\r': jb_raw(b, "\\r", 2); break;
            case '\t': jb_raw(b, "\\t", 2); break;
            default:
                if (ch < 0x20) {
                    char esc[8];
                    snprintf(esc, sizeof(esc), "\\u%04x", ch);
                    jb_raw(b, esc, 6);
                } else {
                    jb_raw(b, (const char*)&ch, 1);
                }
        }
    }
    jb_raw(b, "\"", 1);
}

/* ---------- 采集辅助 ---------- */

static int reg_get_wstr(HKEY root, const wchar_t* sub, const wchar_t* name,
                        wchar_t* out, DWORD count) {
    DWORD size = count * sizeof(wchar_t);
    DWORD type = 0;
    LONG r = RegGetValueW(root, sub, name, RRF_RT_REG_SZ, &type, out, &size);
    if (r != ERROR_SUCCESS) { out[0] = L'\0'; return -1; }
    out[count - 1] = L'\0';
    return 0;
}

static void get_cpu_info(SysInfoSnapshot* s) {
    /* 名称来自注册表 */
    reg_get_wstr(HKEY_LOCAL_MACHINE,
                 L"HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0",
                 L"ProcessorNameString", s->cpuName, 128);

    /* 逻辑处理器数 */
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    s->logicalCores = si.dwNumberOfProcessors;

    /* 物理核心数 */
    DWORD len = 0;
    GetLogicalProcessorInformation(NULL, &len);
    if (len > 0) {
        SYSTEM_LOGICAL_PROCESSOR_INFORMATION* info =
            (SYSTEM_LOGICAL_PROCESSOR_INFORMATION*)malloc(len);
        if (info) {
            if (GetLogicalProcessorInformation(info, &len)) {
                DWORD n = len / sizeof(SYSTEM_LOGICAL_PROCESSOR_INFORMATION);
                for (DWORD i = 0; i < n; ++i) {
                    if (info[i].Relationship == RelationProcessorCore)
                        s->cores++;
                }
            }
            free(info);
        }
    }
    if (s->cores == 0) s->cores = s->logicalCores;
}

typedef LONG (WINAPI* RtlGetVersionFn)(PRTL_OSVERSIONINFOW);

static void get_os_info(SysInfoSnapshot* s) {
    wchar_t product[64] = L"";
    reg_get_wstr(HKEY_LOCAL_MACHINE, L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion",
                 L"ProductName", product, 64);
    wcsncpy(s->osName, product, 63);
    s->osName[63] = L'\0';

    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    RtlGetVersionFn fn = ntdll
        ? (RtlGetVersionFn)(void*)GetProcAddress(ntdll, "RtlGetVersion") : NULL;
    OSVERSIONINFOW vi;
    memset(&vi, 0, sizeof(vi));
    vi.dwOSVersionInfoSize = sizeof(vi);
    LONG ok = (fn && fn(&vi) == 0) ? 0 : -1;
    if (ok != 0) {
        /* 兜底：已知值 */
        vi.dwMajorVersion = 10; vi.dwMinorVersion = 0;
        vi.dwBuildNumber = 0;   vi.dwPlatformId = 2;
    }
    swprintf(s->osVersion, 80, L"%lu.%lu.%lu Build %lu",
             (unsigned long)vi.dwMajorVersion, (unsigned long)vi.dwMinorVersion,
             (unsigned long)vi.dwBuildNumber, (unsigned long)vi.dwBuildNumber);
}

static void get_disks(SysInfoSnapshot* s) {
    wchar_t drvBuf[4 * 26 * 2];
    DWORD len = GetLogicalDriveStringsW(sizeof(drvBuf) / sizeof(wchar_t), drvBuf);
    if (len == 0 || len >= sizeof(drvBuf) / sizeof(wchar_t)) return;

    for (wchar_t* p = drvBuf; *p; p += wcslen(p) + 1) {
        if (s->diskCount >= 26) break;
        UINT type = GetDriveTypeW(p);
        if (type != DRIVE_FIXED && type != DRIVE_REMOVABLE) continue;

        SysDiskInfo* d = &s->disks[s->diskCount];
        memset(d, 0, sizeof(*d));
        wcsncpy(d->root, p, 3);
        d->root[3] = L'\0';

        ULARGE_INTEGER total, freeBytes;
        if (GetDiskFreeSpaceExW(p, &freeBytes, &total, NULL)) {
            d->totalBytes = total.QuadPart;
            d->freeBytes = freeBytes.QuadPart;
        }
        wchar_t fsName[16] = L"";
        DWORD fsFlags = 0;
        if (GetVolumeInformationW(p, NULL, 0, NULL, NULL, &fsFlags,
                                  fsName, 16))
            wcsncpy(d->fs, fsName, 15);
        s->diskCount++;
    }
}

static void get_process_count(SysInfoSnapshot* s) {
    DWORD pids[2048];
    DWORD cb = 0;
    if (EnumProcesses(pids, sizeof(pids), &cb))
        s->processCount = cb / sizeof(DWORD);
}

/* ---------- 对外接口 ---------- */

const char* core_version(void) {
    return "1.0.0";
}

int sysinfo_snapshot(SysInfoSnapshot* out) {
    if (!out) return -1;
    memset(out, 0, sizeof(*out));

    MEMORYSTATUSEX mem;
    memset(&mem, 0, sizeof(mem));
    mem.dwLength = sizeof(mem);
    if (GlobalMemoryStatusEx(&mem)) {
        out->memoryTotal = mem.ullTotalPhys;
        out->memoryFree = mem.ullAvailPhys;
    }

    get_cpu_info(out);
    get_os_info(out);

    out->uptimeSec = GetTickCount64() / 1000ULL;

    DWORD n = 64;
    GetComputerNameW(out->machineName, &n);
    n = 64;
    GetUserNameW(out->userName, &n);

    get_process_count(out);
    get_disks(out);
    return 0;
}

int sysinfo_snapshot_to_json(const SysInfoSnapshot* s, char* buf, size_t len) {
    JsonBuf b;
    jb_init(&b, buf, len);

    jb_raw(&b, "{\"cpuName\":", 11);
    jb_wstring(&b, s->cpuName);
    jb_printf(&b, ",\"cores\":%u,\"logicalCores\":%u,", s->cores, s->logicalCores);
    jb_printf(&b, "\"memoryTotal\":%llu,\"memoryFree\":%llu,\"memoryUsed\":%llu,",
              (unsigned long long)s->memoryTotal,
              (unsigned long long)s->memoryFree,
              (unsigned long long)(s->memoryTotal - s->memoryFree));
    jb_raw(&b, "\"osName\":", 9);
    jb_wstring(&b, s->osName);
    jb_raw(&b, ",\"osVersion\":", 13);
    jb_wstring(&b, s->osVersion);
    jb_printf(&b, ",\"uptimeSec\":%llu,", (unsigned long long)s->uptimeSec);
    jb_raw(&b, "\"machineName\":", 14);
    jb_wstring(&b, s->machineName);
    jb_raw(&b, ",\"userName\":", 12);
    jb_wstring(&b, s->userName);
    jb_printf(&b, ",\"processCount\":%u,\"disks\":[", s->processCount);

    for (uint32_t i = 0; i < s->diskCount; ++i) {
        const SysDiskInfo* d = &s->disks[i];
        if (i) jb_raw(&b, ",", 1);
        jb_raw(&b, "{\"root\":", 8);
        jb_wstring(&b, d->root);
        jb_raw(&b, ",\"fs\":", 6);
        jb_wstring(&b, d->fs);
        jb_printf(&b, ",\"totalBytes\":%llu,\"freeBytes\":%llu}",
                  (unsigned long long)d->totalBytes,
                  (unsigned long long)d->freeBytes);
    }
    jb_raw(&b, "]}", 2);

    return (int)b.need;
}