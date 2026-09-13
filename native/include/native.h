/*
 * native.h — C++ 兼容层导出 ABI（纯 C 接口，供 ctypes 使用）。
 *
 * 约定：
 *  - 返回 int 的函数：0 成功，负数为失败码（具体含义见各函数注释）。
 *  - 输出字符串一律由调用方提供缓冲区，函数保证 NUL 结尾并返回所需长度
 *    （不足时截断）。
 */
#ifndef NATIVE_NATIVE_H
#define NATIVE_NATIVE_H

#include <stddef.h>
#include <stdint.h>

#ifdef _WIN32
# ifdef NATIVE_CORE_EXPORTS
#  define NATIVE_API __declspec(dllexport)
# else
#  define NATIVE_API __declspec(dllimport)
# endif
#else
# define NATIVE_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* 兼容层版本号 */
NATIVE_API const char* native_version(void);

/* ---------------- 系统信息（封装 C 层 core） ---------------- */

/* 系统信息 JSON（字段见 docs）：成功返回大于 0 的所需长度 */
NATIVE_API int native_sysinfo_json(char* buf, size_t len);

/* 机器 GUID（注册表 MachineGuid），返回 JSON 字符串 */
NATIVE_API int native_machine_guid(char* buf, size_t len);

/* 系统盘信息 JSON：{root,fs,totalBytes,freeBytes} */
NATIVE_API int native_system_drive(char* buf, size_t len);

/* ---------------- 权限控制（提权 / 令牌） ---------------- */

/* 当前进程是否已提权（管理员令牌）：1 是，0 否，<0 错误 */
NATIVE_API int native_is_elevated(void);

/*
 * 以管理员权限拉起一个程序（触发 UAC）。
 * file/args/workdir 均可为宽字符串；返回 0 表示成功启动（不代表用户同意）。
 */
NATIVE_API int native_elevate_run(const wchar_t* file,
                                  const wchar_t* args,
                                  const wchar_t* workdir);

/* 以管理员权限拉起本进程所在的 exe（如 python.exe），args 传给子进程 */
NATIVE_API int native_relaunch_elevated(const wchar_t* args,
                                        const wchar_t* workdir);

/* ---------------- 驱动 / 服务管理（SCM） ---------------- */

/*
 * 枚举服务与驱动，输出 JSON 数组：
 * [{name,display,state,startType,pid,isDriver}]
 * 需要管理员权限才能看到驱动；失败返回负数。
 */
NATIVE_API int native_service_list(char* buf, size_t len);

/* 单个服务/驱动状态 JSON：{name,display,state,startType,pid,isDriver} */
NATIVE_API int native_service_status(const wchar_t* name,
                                     char* buf, size_t len);

/* 启动服务/驱动（需要管理员）。 */
NATIVE_API int native_service_start(const wchar_t* name);

/* 停止服务/驱动（需要管理员）。 */
NATIVE_API int native_service_stop(const wchar_t* name);

/*
 * 设置启动类型：0=BOOT,1=SYSTEM,2=AUTO,3=DEMAND,4=DISABLED
 * （需要管理员）。
 */
NATIVE_API int native_service_set_start(const wchar_t* name, int startType);

/* 驱动状态（与 service_status 同源，语义上更明确） */
NATIVE_API int native_driver_status(const wchar_t* name,
                                    char* buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* NATIVE_NATIVE_H */