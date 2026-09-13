/*
 * sysinfo.h — C 最底层：系统信息采集公共接口
 *
 * 仅暴露纯 C 结构体与函数，供 C++ 兼容层（native）与 Python ctypes 使用。
 */
#ifndef CORE_SYSINFO_H
#define CORE_SYSINFO_H

#include <stddef.h>
#include <stdint.h>
#include <wchar.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 单个磁盘卷信息 */
typedef struct SysDiskInfo {
    wchar_t root[4];      /* 根路径，如 L"C:" */
    wchar_t fs[16];       /* 文件系统，如 L"NTFS" */
    uint64_t totalBytes;
    uint64_t freeBytes;
} SysDiskInfo;

/* 系统信息快照 */
typedef struct SysInfoSnapshot {
    wchar_t cpuName[128];     /* 处理器名称（注册表） */
    uint32_t cores;           /* 物理核心数 */
    uint32_t logicalCores;    /* 逻辑处理器数 */
    uint64_t memoryTotal;     /* 物理内存总量（字节） */
    uint64_t memoryFree;      /* 可用物理内存（字节） */
    wchar_t osName[64];       /* 系统名称，如 L"Windows 10 Pro" */
    wchar_t osVersion[80];    /* 版本号，如 L"10.0.19045 Build 19045" */
    uint64_t uptimeSec;       /* 开机时长（秒） */
    wchar_t machineName[64];  /* 计算机名 */
    wchar_t userName[64];     /* 当前用户名 */
    uint32_t processCount;    /* 进程数 */
    uint32_t diskCount;       /* 磁盘卷数量 */
    SysDiskInfo disks[26];    /* 磁盘卷列表 */
} SysInfoSnapshot;

/* C 层版本号 */
const char* core_version(void);

/*
 * 采集系统信息快照。
 * 返回 0 表示成功；非 0 为失败码（不保证部分填写）。
 */
int sysinfo_snapshot(SysInfoSnapshot* out);

/*
 * 将快照序列化为 JSON 字符串。
 * buf 不足时返回所需长度（不含结尾 NUL），并截断写满 buf。
 */
int sysinfo_snapshot_to_json(const SysInfoSnapshot* snap,
                             char* buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* CORE_SYSINFO_H */