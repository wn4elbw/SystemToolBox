/* stbdrv.h — SystemToolBox 内核驱动公共 ABI
 *
 * 与 python/app/driver.py、python/app/adv.py 中的 ctypes 结构严格对应。
 * 所有 IOCTL 使用 METHOD_BUFFERED：请求/响应用同一系统缓冲，请求结构首个字段
 * 必须是 STB_HDR；驱动完成后回填 hdr.status（NTSTATUS，0=成功）。
 */
#pragma once

#include <ntddk.h>

#define STBDRV_DEVICE_NAME L"\\Device\\STBDriver"
#define STBDRV_DOS_NAME   L"\\DosDevices\\STBDriver"
#define STBDRV_TAG        'StBD'          /* = 0x53744244，与 python 端 magic 一致 */
#define STB_IFACE_VERSION 0x00010000      /* 主.次: 1.0 */

#define STB_MAX_PATH 260                  /* WCHAR */
#define STB_MAX_NAME 64                   /* WCHAR */
#define STB_MAX_DATA 4096

/* IOCTL 码 = CTL_CODE(FILE_DEVICE_UNKNOWN, 0x800+i, METHOD_BUFFERED,
 *                     FILE_READ_DATA|FILE_WRITE_DATA) */
#define STB_IOCTL(code) \
    CTL_CODE(FILE_DEVICE_UNKNOWN, (code), METHOD_BUFFERED, \
             FILE_READ_DATA | FILE_WRITE_DATA)

#define IOCTL_STB_DRV_INFO     STB_IOCTL(0x800)
#define IOCTL_STB_PROC_KILL    STB_IOCTL(0x801)
#define IOCTL_STB_PROC_SUSPEND STB_IOCTL(0x802)
#define IOCTL_STB_PROC_RESUME  STB_IOCTL(0x803)
#define IOCTL_STB_PROC_START   STB_IOCTL(0x804)
#define IOCTL_STB_MEM_READ     STB_IOCTL(0x805)
#define IOCTL_STB_MEM_WRITE    STB_IOCTL(0x806)
#define IOCTL_STB_FILE_READ    STB_IOCTL(0x807)
#define IOCTL_STB_FILE_WRITE   STB_IOCTL(0x808)
#define IOCTL_STB_FILE_DELETE  STB_IOCTL(0x809)
#define IOCTL_STB_FILE_MKDIR   STB_IOCTL(0x80A)
#define IOCTL_STB_REG_READ     STB_IOCTL(0x80B)
#define IOCTL_STB_REG_WRITE    STB_IOCTL(0x80C)
#define IOCTL_STB_REG_DELETE   STB_IOCTL(0x80D)
#define IOCTL_STB_REG_LIST     STB_IOCTL(0x80E)
#define IOCTL_STB_POLICY_SET   STB_IOCTL(0x80F)
#define IOCTL_STB_CMD_EXEC     STB_IOCTL(0x810)
#define IOCTL_STB_TOKEN_ELEVATE STB_IOCTL(0x811)

/* 能力位（IOCTL_STB_DRV_INFO -> caps） */
#define STB_CAP_PROC   0x0001
#define STB_CAP_MEM    0x0002
#define STB_CAP_FILE   0x0004
#define STB_CAP_REG    0x0008
#define STB_CAP_GP     0x0010
#define STB_CAP_EXEC   0x0020
#define STB_CAP_TOKEN  0x0040

/* 注册表 hive 枚举 */
typedef enum _STB_REG_HIVE {
    StbHiveHklm = 0,
    StbHiveHkcu = 1,
    StbHiveHkcr = 2,
    StbHiveHku  = 3,
} STB_REG_HIVE;

/* ---------------- 请求结构（首字段均为 STB_HDR） ---------------- */

typedef struct _STB_HDR {
    ULONG      magic;     /* STBDRV_TAG */
    ULONG      op;        /* 子操作号（校验用） */
    NTSTATUS   status;    /* 驱动回填 */
} STB_HDR, *PSTB_HDR;

typedef struct _STB_DRV_INFO {
    STB_HDR hdr;
    ULONG   version;      /* STB_IFACE_VERSION */
    ULONG   caps;
    ULONG   reserved;
} STB_DRV_INFO, *PSTB_DRV_INFO;

typedef struct _STB_PROC_OP {       /* kill / suspend / resume */
    STB_HDR hdr;
    ULONG   pid;
    ULONG   reserved;
} STB_PROC_OP, *PSTB_PROC_OP;

typedef struct _STB_PROC_START {    /* start / cmd_exec */
    STB_HDR      hdr;
    WCHAR        path[STB_MAX_PATH];
    WCHAR        args[STB_MAX_PATH];
    WCHAR        workdir[STB_MAX_PATH];
    ULONG_PTR    ldrInitThunk;      /* 客户端传入 ntdll!LdrInitializeThunk 地址 */
    ULONG        pid;               /* out: 新进程 PID */
    ULONG        reserved;
} STB_PROC_START, *PSTB_PROC_START;

typedef struct _STB_MEM_READ {
    STB_HDR  hdr;
    ULONG    pid;
    ULONG    reserved1;
    ULONG_PTR address;
    ULONG    size;                  /* in: 请求字节数; out: 实际读取数 */
    ULONG    reserved2;
    UCHAR    data[STB_MAX_DATA];
} STB_MEM_READ, *PSTB_MEM_READ;

typedef struct _STB_MEM_WRITE {
    STB_HDR  hdr;
    ULONG    pid;
    ULONG    reserved1;
    ULONG_PTR address;
    ULONG    size;
    ULONG    reserved2;
    UCHAR    data[STB_MAX_DATA];
} STB_MEM_WRITE, *PSTB_MEM_WRITE;

typedef struct _STB_FILE_READ {
    STB_HDR hdr;
    WCHAR   path[STB_MAX_PATH];
    ULONG   size;                   /* in: 请求上限; out: 实际读取数 */
    ULONG   reserved;
    UCHAR   data[STB_MAX_DATA];
} STB_FILE_READ, *PSTB_FILE_READ;

typedef struct _STB_FILE_WRITE {
    STB_HDR hdr;
    WCHAR   path[STB_MAX_PATH];
    ULONG   flags;                  /* 0=覆盖, 1=追加 */
    ULONG   size;
    UCHAR   data[STB_MAX_DATA];
} STB_FILE_WRITE, *PSTB_FILE_WRITE;

typedef struct _STB_FILE_PATH {     /* delete / mkdir */
    STB_HDR hdr;
    WCHAR   path[STB_MAX_PATH];
} STB_FILE_PATH, *PSTB_FILE_PATH;

typedef struct _STB_REG_OP {        /* read / write / delete / list */
    STB_HDR  hdr;
    ULONG    hive;                  /* STB_REG_HIVE */
    ULONG    reserved1;
    WCHAR    subkey[STB_MAX_PATH];
    WCHAR    name[STB_MAX_NAME];    /* 空串 = 默认值 / 删除键 */
    ULONG    type;                  /* REG_*（write） */
    ULONG    reserved2;
    ULONG    size;                  /* write: 数据字节数; read/list: out */
    ULONG    reserved3;
    UCHAR    data[STB_MAX_DATA];    /* read: out 裸值; write: in;
                                     * list: out 序列化(见 stbdrv.c) */
} STB_REG_OP, *PSTB_REG_OP;

typedef struct _STB_POLICY_SET {
    STB_HDR  hdr;
    WCHAR    policyPath[STB_MAX_PATH]; /* 形如 SOFTWARE\Policies\xxx */
    WCHAR    name[STB_MAX_NAME];
    ULONG    type;
    ULONG    reserved;
    ULONG    size;
    ULONG    reserved2;
    UCHAR    data[STB_MAX_DATA];
} STB_POLICY_SET, *PSTB_POLICY_SET;

typedef struct _STB_CMD_EXEC {
    STB_HDR      hdr;
    WCHAR        path[STB_MAX_PATH];
    WCHAR        args[STB_MAX_PATH];
    WCHAR        workdir[STB_MAX_PATH];
    ULONG_PTR    ldrInitThunk;
    ULONG        pid;               /* out */
    ULONG        reserved;
} STB_CMD_EXEC, *PSTB_CMD_EXEC;

typedef struct _STB_TOKEN_ELEVATE {
    STB_HDR  hdr;
    ULONG_PTR token;                /* out: 客户端进程内的 SYSTEM 令牌句柄 */
    ULONG    pid;                   /* out: 令牌来源进程(winlogon) PID */
    ULONG    reserved;
} STB_TOKEN_ELEVATE, *PSTB_TOKEN_ELEVATE;