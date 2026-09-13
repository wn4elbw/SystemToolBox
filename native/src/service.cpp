/*
 * service.cpp — 驱动 / 服务管理（SCM）。
 * 枚举、状态查询、启动、停止、设置启动类型。
 */
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

#include <windows.h>
#include <winsvc.h>

#include <string>
#include <vector>

#include "native.h"
#include "json_out.h"

namespace {

std::wstring svc_state_str(DWORD state) {
    switch (state) {
        case SERVICE_STOPPED:          return L"stopped";
        case SERVICE_START_PENDING:    return L"start_pending";
        case SERVICE_STOP_PENDING:     return L"stop_pending";
        case SERVICE_RUNNING:          return L"running";
        case SERVICE_CONTINUE_PENDING: return L"continue_pending";
        case SERVICE_PAUSE_PENDING:    return L"pause_pending";
        case SERVICE_PAUSED:           return L"paused";
        default:                       return L"unknown";
    }
}

std::wstring start_type_str(DWORD t) {
    switch (t) {
        case SERVICE_BOOT_START:      return L"boot";
        case SERVICE_SYSTEM_START:    return L"system";
        case SERVICE_AUTO_START:      return L"auto";
        case SERVICE_DEMAND_START:    return L"demand";
        case SERVICE_DISABLED:        return L"disabled";
        default:                      return L"unknown";
    }
}

/* 打开 SCM。若需要管理权限，服务句柄权限含 START/STOP/CHANGE。 */
SC_HANDLE open_scm() {
    return OpenSCManagerW(nullptr, nullptr,
                          SC_MANAGER_ENUMERATE_SERVICE | SC_MANAGER_CONNECT);
}

/* 打开服务：优先带管理权限，失败时退回查询权限。 */
SC_HANDLE open_service(SC_HANDLE scm, const wchar_t* name, bool write) {
    DWORD access = SERVICE_QUERY_STATUS | SERVICE_QUERY_CONFIG;
    if (write) access |= SERVICE_START | SERVICE_STOP | SERVICE_CHANGE_CONFIG;
    SC_HANDLE h = OpenServiceW(scm, name, access);
    if (!h && write) h = OpenServiceW(scm, name, SERVICE_QUERY_STATUS | SERVICE_QUERY_CONFIG);
    return h;
}

/* 服务状态 -> JSON 对象（不含 name 字段；startType 由调用方用
 * QUERY_SERVICE_CONFIG 提供，SERVICE_STATUS_PROCESS 无该字段） */
void status_json(std::string& out, SC_HANDLE h) {
    SERVICE_STATUS_PROCESS ss;
    DWORD needed = 0;
    if (!QueryServiceStatusEx(h, SC_STATUS_PROCESS_INFO,
                              reinterpret_cast<LPBYTE>(&ss),
                              sizeof(ss), &needed)) {
        out = R"({"error":"query_status_failed"})";
        return;
    }
    out += "{\"state\":";
    nj::put_string(out, svc_state_str(ss.dwCurrentState));
    out += ",\"pid\":";
    out += std::to_string(ss.dwProcessId);
    out += "}";
}

/* 返回 0 成功，负数失败 */
int write_status_for(const wchar_t* name, char* buf, size_t len,
                     bool includeName) {
    SC_HANDLE scm = open_scm();
    if (!scm) return -1;
    SC_HANDLE h = open_service(scm, name, false);
    if (!h) {
        CloseServiceHandle(scm);
        return -2;  /* 不存在或无权限 */
    }

    QUERY_SERVICE_CONFIGW* cfg = nullptr;
    DWORD cfgNeed = 0;
    QueryServiceConfigW(h, cfg, 0, &cfgNeed);
    if (cfgNeed > 0) {
        cfg = reinterpret_cast<QUERY_SERVICE_CONFIGW*>(new BYTE[cfgNeed]);
        if (!QueryServiceConfigW(h, cfg, cfgNeed, &cfgNeed)) {
            delete[] cfg;
            cfg = nullptr;
        }
    }

    std::string out;
    out += '{';
    if (includeName) {
        out += "\"name\":";
        nj::put_string(out, name);
        out += ',';
    }
    out += "\"display\":";
    nj::put_string(out, cfg ? cfg->lpDisplayName : L"");
    out += ",\"isDriver\":";
    out += (cfg && (cfg->dwServiceType & (SERVICE_KERNEL_DRIVER |
                                          SERVICE_FILE_SYSTEM_DRIVER))) ? "true" : "false";
    out += ",\"startType\":";
    nj::put_string(out, cfg ? start_type_str(cfg->dwStartType) : L"unknown");
    out += ',';

    std::string st;
    status_json(st, h);   /* {state,startType,pid} */
    if (!st.empty() && st.front() == '{') st = st.substr(1);
    if (!st.empty() && st.back() == '}') st.pop_back();
    out += st;
    out += '}';

    delete[] cfg;
    CloseServiceHandle(h);
    CloseServiceHandle(scm);
    nj::copy_out(out, buf, len);
    return static_cast<int>(out.size());
}

}  // namespace

/* 查询服务配置的启动类型；失败返回 "unknown" */
std::wstring query_start_type(SC_HANDLE scm, const wchar_t* name) {
    SC_HANDLE h = open_service(scm, name, false);
    if (!h) return L"unknown";
    QUERY_SERVICE_CONFIGW* cfg = nullptr;
    DWORD need = 0;
    QueryServiceConfigW(h, cfg, 0, &need);
    if (need > 0) {
        cfg = reinterpret_cast<QUERY_SERVICE_CONFIGW*>(new BYTE[need]);
        if (!QueryServiceConfigW(h, cfg, need, &need)) {
            delete[] cfg;
            cfg = nullptr;
        }
    }
    std::wstring r = cfg ? start_type_str(cfg->dwStartType) : L"unknown";
    delete[] cfg;
    CloseServiceHandle(h);
    return r;
}

int native_service_list(char* buf, size_t len) {
    SC_HANDLE scm = open_scm();
    if (!scm) return -1;

    DWORD bytesNeeded = 0, servicesReturned = 0, resumeHandle = 0;
    EnumServicesStatusExW(scm, SC_ENUM_PROCESS_INFO,
                          SERVICE_WIN32 | SERVICE_DRIVER,
                          SERVICE_STATE_ALL,
                          nullptr, 0, &bytesNeeded, &servicesReturned,
                          &resumeHandle, nullptr);
    if (bytesNeeded == 0) {
        CloseServiceHandle(scm);
        return -2;
    }
    std::vector<BYTE> raw(bytesNeeded);
    if (!EnumServicesStatusExW(scm, SC_ENUM_PROCESS_INFO,
                               SERVICE_WIN32 | SERVICE_DRIVER,
                               SERVICE_STATE_ALL,
                               raw.data(), bytesNeeded, &bytesNeeded,
                               &servicesReturned, &resumeHandle, nullptr)) {
        CloseServiceHandle(scm);
        return -3;
    }

    std::string out = "[";
    ENUM_SERVICE_STATUS_PROCESSW* items =
        reinterpret_cast<ENUM_SERVICE_STATUS_PROCESSW*>(raw.data());
    for (DWORD i = 0; i < servicesReturned; ++i) {
        if (i) out += ',';
        const ENUM_SERVICE_STATUS_PROCESSW& it = items[i];
        std::wstring startType = query_start_type(scm, it.lpServiceName);
        out += "{\"name\":";
        nj::put_string(out, it.lpServiceName);
        out += ",\"display\":";
        nj::put_string(out, it.lpDisplayName);
        out += ",\"state\":";
        nj::put_string(out, svc_state_str(it.ServiceStatusProcess.dwCurrentState));
        out += ",\"startType\":";
        nj::put_string(out, startType.c_str());
        out += ",\"pid\":";
        out += std::to_string(it.ServiceStatusProcess.dwProcessId);
        out += ",\"isDriver\":";
        bool driver = (it.ServiceStatusProcess.dwServiceType &
                       (SERVICE_KERNEL_DRIVER | SERVICE_FILE_SYSTEM_DRIVER)) != 0;
        out += driver ? "true" : "false";
        out += '}';
    }
    out += ']';

    CloseServiceHandle(scm);
    nj::copy_out(out, buf, len);
    return static_cast<int>(out.size());
}

int native_service_status(const wchar_t* name, char* buf, size_t len) {
    if (!name) return -1;
    return write_status_for(name, buf, len, true);
}

int native_driver_status(const wchar_t* name, char* buf, size_t len) {
    return native_service_status(name, buf, len);
}

int native_service_start(const wchar_t* name) {
    if (!name) return -1;
    SC_HANDLE scm = open_scm();
    if (!scm) return -2;
    SC_HANDLE h = open_service(scm, name, true);
    if (!h) { CloseServiceHandle(scm); return -3; }
    BOOL ok = StartServiceW(h, 0, nullptr);
    DWORD err = ok ? 0 : GetLastError();
    CloseServiceHandle(h);
    CloseServiceHandle(scm);
    return ok ? 0 : (int)err;
}

int native_service_stop(const wchar_t* name) {
    if (!name) return -1;
    SC_HANDLE scm = open_scm();
    if (!scm) return -2;
    SC_HANDLE h = open_service(scm, name, true);
    if (!h) { CloseServiceHandle(scm); return -3; }
    SERVICE_STATUS ss;
    BOOL ok = ControlService(h, SERVICE_CONTROL_STOP, &ss);
    DWORD err = ok ? 0 : GetLastError();
    CloseServiceHandle(h);
    CloseServiceHandle(scm);
    return ok ? 0 : (int)err;
}

int native_service_set_start(const wchar_t* name, int startType) {
    if (!name) return -1;
    DWORD type = SERVICE_DEMAND_START;
    switch (startType) {
        case 0: type = SERVICE_BOOT_START;    break;
        case 1: type = SERVICE_SYSTEM_START;  break;
        case 2: type = SERVICE_AUTO_START;    break;
        case 3: type = SERVICE_DEMAND_START;  break;
        case 4: type = SERVICE_DISABLED;      break;
        default: return -4;
    }
    SC_HANDLE scm = open_scm();
    if (!scm) return -2;
    SC_HANDLE h = open_service(scm, name, true);
    if (!h) { CloseServiceHandle(scm); return -3; }
    BOOL ok = ChangeServiceConfigW(h, SERVICE_NO_CHANGE, type, SERVICE_NO_CHANGE,
                                   nullptr, nullptr, nullptr, nullptr,
                                   nullptr, nullptr, nullptr);
    DWORD err = ok ? 0 : GetLastError();
    CloseServiceHandle(h);
    CloseServiceHandle(scm);
    return ok ? 0 : (int)err;
}