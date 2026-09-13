/* stbdrv.c — SystemToolBox 内核驱动
 *
 * 能力（全部在 SYSTEM 系统线程内执行，绕过管理员 ACL）：
 *   - 进程：结束(ZwTerminateProcess) / 挂起、恢复(整进程 NtSuspend/ResumeProcess)
 *   - 进程启动 / 命令执行：ZwCreateProcessEx 直接创建（不经 cmd.exe/taskkill）
 *   - 内存：MmCopyVirtualMemory 跨进程读写
 *   - 文件：ZwCreateFile/ZwReadFile/ZwWriteFile/ZwDeleteFile 内核句柄
 *   - 注册表：Zw 注册表 API（HKLM/HKCU/HKCR/HKU）
 *   - 组策略：写入 HKLM\SOFTWARE\Policies（注册表承载）
 *   - 令牌提权：复制 winlogon 的 SYSTEM 令牌到客户端进程（提到系统级）
 *
 * 构建：Windows Driver Kit (WDK) + 测试签名（见 stbdrv.inf / README.md）。
 */
#include <ntifs.h>        /* 含 ntddk.h，并声明 ZwAllocate/FreeVirtualMemory、令牌 Zw* */
#include <ntstrsafe.h>
#include "stbdrv.h"

/* ---- RTL_USER_PROCESS_PARAMETERS（winternl.h 与 ntddk 冲突，本地定义所需部分） ---- */
#ifndef _STB_RTL_UPP_DEFINED
#define _STB_RTL_UPP_DEFINED
typedef struct _STB_CURDIR {
    UNICODE_STRING DosPath;
    HANDLE Handle;
} STB_CURDIR;
#define STB_MAX_DRIVE_LETTERS 32
typedef struct _STB_DRIVE_LETTER_CURDIR {
    USHORT Flags;
    USHORT Length;
    ULONG TimeStamp;
    UNICODE_STRING DosPath;
} STB_DRIVE_LETTER_CURDIR;
typedef struct _STB_RTL_USER_PROCESS_PARAMETERS {
    ULONG MaximumLength;
    ULONG Length;
    ULONG Flags;
    ULONG DebugFlags;
    HANDLE ConsoleHandle;
    ULONG ConsoleFlags;
    HANDLE StandardInput;
    HANDLE StandardOutput;
    HANDLE StandardError;
    STB_CURDIR CurrentDirectory;
    UNICODE_STRING DllPath;
    UNICODE_STRING ImagePathName;
    UNICODE_STRING CommandLine;
    PVOID Environment;
    ULONG StartingX;
    ULONG StartingY;
    ULONG CountX;
    ULONG CountY;
    ULONG CountCharsX;
    ULONG CountCharsY;
    ULONG FillAttribute;
    ULONG WindowFlags;
    ULONG ShowWindowFlags;
    UNICODE_STRING WindowTitle;
    UNICODE_STRING DesktopInfo;
    UNICODE_STRING ShellInfo;
    UNICODE_STRING RuntimeData;
    STB_DRIVE_LETTER_CURDIR CurrentDirectories[STB_MAX_DRIVE_LETTERS];
} STB_RTL_USER_PROCESS_PARAMETERS, *PSTB_RTL_USER_PROCESS_PARAMETERS;
#endif

/* ---- 内核缺失的类型 / 访问权限常量（本地补齐） ---- */
typedef enum _THREAD_STATE {
    Initialized, Ready, Running, Standby, Terminated, Wait,
    Transition, Unknown
} THREAD_STATE;

#ifndef PROCESS_TERMINATE
#define PROCESS_TERMINATE                 0x0001
#define PROCESS_SUSPEND_RESUME            0x0800
#define PROCESS_QUERY_LIMITED_INFORMATION 0x1000
#define PROCESS_ALL_ACCESS                (0x000F0000 | 0x00100000 | 0xFFFF)
#define THREAD_ALL_ACCESS                 (0x000F0000 | 0x00100000 | 0x3FF)
#define TOKEN_QUERY                       0x0008
#define TOKEN_DUPLICATE                   0x0002
#define TOKEN_ASSIGN_PRIMARY              0x0001
#define TOKEN_ALL_ACCESS                  (0x000F0000 | 0x01FF)
#endif

/* 最小 PEB（x64：ProcessParameters 偏移 0x20） */
typedef struct _STB_PEB {
    UCHAR Reserved1[2];
    UCHAR BeingDebugged;
    UCHAR Reserved2[1];
    PVOID Reserved3[2];
    PVOID Ldr;              /* PPEB_LDR_DATA，本驱动不用 */
    PVOID ProcessParameters;
} STB_PEB, *PSTB_PEB;

/* 最小 DOS 头（e_lfanew 偏移 0x3C） */
typedef struct _STB_DOS_HEADER {
    USHORT e_magic;
    USHORT e_cblp;
    USHORT e_cp;
    USHORT e_crlc;
    USHORT e_cparhdr;
    USHORT e_minalloc;
    USHORT e_maxalloc;
    USHORT e_ss;
    USHORT e_sp;
    USHORT e_csum;
    USHORT e_ip;
    USHORT e_cs;
    USHORT e_lfarlc;
    USHORT e_ovno;
    USHORT e_res[4];
    USHORT e_oemid;
    USHORT e_oeminfo;
    USHORT e_res2[10];
    LONG e_lfanew;
} STB_DOS_HEADER, *PSTB_DOS_HEADER;

/* 最小 PE 头（仅取 AddressOfEntryPoint；ntddk 只声明了 PIMAGE_NT_HEADERS64 而未定义结构） */
typedef struct _STB_IMAGE_OPTIONAL_HEADER64 {
    USHORT Magic;
    UCHAR  MajorLinkerVersion;
    UCHAR  MinorLinkerVersion;
    ULONG  SizeOfCode;
    ULONG  SizeOfInitializedData;
    ULONG  SizeOfUninitializedData;
    ULONG  AddressOfEntryPoint;      /* 偏移 0x14 */
    ULONG  BaseOfCode;
    ULONG_PTR ImageBase;
    ULONG  SectionAlignment;
    ULONG  FileAlignment;
    USHORT MajorOperatingSystemVersion;
    USHORT MinorOperatingSystemVersion;
    USHORT MajorImageVersion;
    USHORT MinorImageVersion;
    USHORT MajorSubsystemVersion;
    USHORT MinorSubsystemVersion;
    ULONG  Win32VersionValue;
    ULONG  SizeOfImage;
    ULONG  SizeOfHeaders;
    ULONG  CheckSum;
    USHORT Subsystem;
    USHORT DllCharacteristics;
    ULONG_PTR SizeOfStackReserve;
    ULONG_PTR SizeOfStackCommit;
    ULONG_PTR SizeOfHeapReserve;
    ULONG_PTR SizeOfHeapCommit;
    ULONG  LoaderFlags;
    ULONG  NumberOfRvaAndSizes;
} STB_IMAGE_OPTIONAL_HEADER64, *PSTB_IMAGE_OPTIONAL_HEADER64;

typedef struct _STB_IMAGE_NT_HEADERS64 {
    ULONG Signature;
    UCHAR FileHeaderPlaceholder[20];   /* IMAGE_FILE_HEADER 20 字节 */
    STB_IMAGE_OPTIONAL_HEADER64 OptionalHeader;   /* 偏移 0x18 */
} STB_IMAGE_NT_HEADERS64, *PSTB_IMAGE_NT_HEADERS64;

/* ---- WDK 头未声明但 ntoskrnl 确实导出的例程（自声明，签名与 MSDN 一致） ---- */
NTSYSAPI NTSTATUS NTAPI ZwQueryInformationProcess(HANDLE ProcessHandle,
    PROCESSINFOCLASS ProcessInformationClass, PVOID ProcessInformation,
    ULONG ProcessInformationLength, PULONG ReturnLength);
NTSYSAPI NTSTATUS NTAPI ZwCreateProcessEx(PHANDLE ProcessHandle,
    ACCESS_MASK DesiredAccess, POBJECT_ATTRIBUTES ObjectAttributes,
    HANDLE ParentProcess, ULONG Flags, HANDLE SectionHandle,
    HANDLE DebugPort, HANDLE ExceptionPort, ULONG JobMemberLevel);
NTSYSAPI NTSTATUS NTAPI ZwQuerySystemInformation(ULONG SystemInformationClass,
    PVOID SystemInformation, ULONG SystemInformationLength,
    PULONG ReturnLength);
NTSYSAPI NTSTATUS NTAPI ZwTerminateProcess(HANDLE ProcessHandle,
    NTSTATUS ExitStatus);
NTSYSAPI NTSTATUS NTAPI ZwOpenProcess(PHANDLE ProcessHandle,
    ACCESS_MASK DesiredAccess, POBJECT_ATTRIBUTES ObjectAttributes,
    PCLIENT_ID ClientId);
NTSYSAPI NTSTATUS NTAPI ZwSetInformationFile(HANDLE FileHandle,
    PIO_STATUS_BLOCK IoStatusBlock, PVOID FileInformation,
    ULONG Length, FILE_INFORMATION_CLASS FileInformationClass);

/* 内核导出但 WDK 头未声明的例程 */
NTKERNELAPI PSTB_PEB PsGetProcessPeb(PEPROCESS Process);
NTKERNELAPI PVOID PsGetProcessSectionBaseAddress(PEPROCESS Process);
NTKERNELAPI NTSTATUS MmCopyVirtualMemory(PEPROCESS FromProcess,
    PVOID FromAddress, PEPROCESS ToProcess, PVOID ToAddress,
    SIZE_T BufferSize, KPROCESSOR_MODE PreviousMode,
    PSIZE_T NumberOfBytesCopied);

/* ---- WDK 导入库刻意隐藏、但 ntoskrnl 运行时有导出的服务：
 * 在 DriverEntry 用 MmGetSystemRoutineAddress 动态解析（Nt* 即系统服务例程）---- */
typedef NTSTATUS(NTAPI* PFN_NTWRITEVM)(HANDLE ProcessHandle,
    PVOID BaseAddress, PVOID Buffer, SIZE_T NumberOfBytesToWrite,
    PSIZE_T NumberOfBytesWritten);
typedef NTSTATUS(NTAPI* PFN_NTCREATETHREADEX)(PHANDLE ThreadHandle,
    ACCESS_MASK DesiredAccess, POBJECT_ATTRIBUTES ObjectAttributes,
    HANDLE ProcessHandle, PVOID StartRoutine, PVOID Argument,
    ULONG CreateFlags, SIZE_T ZeroBits, SIZE_T StackSize,
    PSIZE_T MaximumStackSize);
typedef NTSTATUS(NTAPI* PFN_NTSUSPENDPROC)(HANDLE ProcessHandle);
typedef NTSTATUS(NTAPI* PFN_NTRESUMEPROC)(HANDLE ProcessHandle);

static PFN_NTWRITEVM        StbNtWriteVirtualMemory;
static PFN_NTCREATETHREADEX StbNtCreateThreadEx;
static PFN_NTSUSPENDPROC    StbNtSuspendProcess;
static PFN_NTRESUMEPROC     StbNtResumeProcess;

static PVOID StbResolveRoutine(PCWSTR name)
{
    UNICODE_STRING u;
    RtlInitUnicodeString(&u, name);
    return MmGetSystemRoutineAddress(&u);
}

#define STB_REQUIRE(pfn) \
    if ((pfn) == NULL) { return STATUS_NOT_IMPLEMENTED; }

/* ---------------- 能力声明 ---------------- */

NTSTATUS StbCreateDevice(PDRIVER_OBJECT DriverObject);
VOID     StbUnload(PDRIVER_OBJECT DriverObject);
NTSTATUS StbDispatchCreateClose(PDEVICE_OBJECT DeviceObject, PIRP Irp);
NTSTATUS StbDispatchDeviceControl(PDEVICE_OBJECT DeviceObject, PIRP Irp);

NTSTATUS StbOpInfo(PSTB_DRV_INFO req);
NTSTATUS StbOpProcKill(ULONG pid);
NTSTATUS StbOpProcSuspendResume(ULONG pid, BOOLEAN suspend);
NTSTATUS StbOpCreateProcess(PWSTR path, PWSTR args, PWSTR workdir,
                            ULONG_PTR ldrInit, PULONG outPid);
NTSTATUS StbOpMemRead(ULONG pid, ULONG_PTR address, PUCHAR out,
                      ULONG size, PULONG actual);
NTSTATUS StbOpMemWrite(ULONG pid, ULONG_PTR address, PUCHAR in,
                       ULONG size, PULONG actual);
NTSTATUS StbOpFileRead(PWSTR path, PUCHAR out, ULONG maxSize, PULONG actual);
NTSTATUS StbOpFileWrite(PWSTR path, PUCHAR in, ULONG size, ULONG flags);
NTSTATUS StbOpFileDelete(PWSTR path);
NTSTATUS StbOpFileMkdir(PWSTR path);
NTSTATUS StbOpRegRead(PSTB_REG_OP req, PEPROCESS requester);
NTSTATUS StbOpRegWrite(PSTB_REG_OP req, PEPROCESS requester);
NTSTATUS StbOpRegDelete(PSTB_REG_OP req, PEPROCESS requester);
NTSTATUS StbOpRegList(PSTB_REG_OP req, PEPROCESS requester);
NTSTATUS StbOpPolicySet(PSTB_POLICY_SET req);
NTSTATUS StbOpTokenElevate(PEPROCESS requester, PSTB_TOKEN_ELEVATE req);

/* ---------------- 请求调度（SYSTEM 工作线程） ---------------- */

typedef enum _STB_OP {
    OpInfo = 0, OpProcKill = 1, OpProcSuspend = 2, OpProcResume = 3,
    OpProcStart = 4, OpMemRead = 5, OpMemWrite = 6,
    OpFileRead = 7, OpFileWrite = 8, OpFileDelete = 9, OpFileMkdir = 10,
    OpRegRead = 11, OpRegWrite = 12, OpRegDelete = 13, OpRegList = 14,
    OpPolicySet = 15, OpCmdExec = 16, OpTokenElevate = 17,
} STB_OP;

typedef struct _STB_WORK {
    KEVENT    Done;
    ULONG     Op;
    PVOID     Req;          /* IRP 系统缓冲中的请求结构 */
    PEPROCESS Requester;    /* 已引用：IOCTL 发起进程 */
} STB_WORK;

static VOID StbWorker(PVOID Context)
{
    STB_WORK* w = (STB_WORK*)Context;
    NTSTATUS st = STATUS_SUCCESS;

    switch (w->Op) {
    case OpInfo:
        st = StbOpInfo((PSTB_DRV_INFO)w->Req);
        break;
    case OpProcKill:
        st = StbOpProcKill(((PSTB_PROC_OP)w->Req)->pid);
        break;
    case OpProcSuspend:
        st = StbOpProcSuspendResume(((PSTB_PROC_OP)w->Req)->pid, TRUE);
        break;
    case OpProcResume:
        st = StbOpProcSuspendResume(((PSTB_PROC_OP)w->Req)->pid, FALSE);
        break;
    case OpProcStart: {
        PSTB_PROC_START r = (PSTB_PROC_START)w->Req;
        st = StbOpCreateProcess(r->path, r->args, r->workdir,
                                r->ldrInitThunk, &r->pid);
        break;
    }
    case OpCmdExec: {
        PSTB_CMD_EXEC r = (PSTB_CMD_EXEC)w->Req;
        st = StbOpCreateProcess(r->path, r->args, r->workdir,
                                r->ldrInitThunk, &r->pid);
        break;
    }
    case OpMemRead: {
        PSTB_MEM_READ r = (PSTB_MEM_READ)w->Req;
        st = StbOpMemRead(r->pid, r->address, r->data, r->size, &r->size);
        break;
    }
    case OpMemWrite: {
        PSTB_MEM_WRITE r = (PSTB_MEM_WRITE)w->Req;
        st = StbOpMemWrite(r->pid, r->address, r->data, r->size, &r->size);
        break;
    }
    case OpFileRead: {
        PSTB_FILE_READ r = (PSTB_FILE_READ)w->Req;
        st = StbOpFileRead(r->path, r->data, r->size, &r->size);
        break;
    }
    case OpFileWrite: {
        PSTB_FILE_WRITE r = (PSTB_FILE_WRITE)w->Req;
        st = StbOpFileWrite(r->path, r->data, r->size, r->flags);
        break;
    }
    case OpFileDelete:
        st = StbOpFileDelete(((PSTB_FILE_PATH)w->Req)->path);
        break;
    case OpFileMkdir:
        st = StbOpFileMkdir(((PSTB_FILE_PATH)w->Req)->path);
        break;
    case OpRegRead:
        st = StbOpRegRead((PSTB_REG_OP)w->Req, w->Requester);
        break;
    case OpRegWrite:
        st = StbOpRegWrite((PSTB_REG_OP)w->Req, w->Requester);
        break;
    case OpRegDelete:
        st = StbOpRegDelete((PSTB_REG_OP)w->Req, w->Requester);
        break;
    case OpRegList:
        st = StbOpRegList((PSTB_REG_OP)w->Req, w->Requester);
        break;
    case OpPolicySet:
        st = StbOpPolicySet((PSTB_POLICY_SET)w->Req);
        break;
    case OpTokenElevate:
        st = StbOpTokenElevate(w->Requester, (PSTB_TOKEN_ELEVATE)w->Req);
        break;
    default:
        st = STATUS_INVALID_DEVICE_REQUEST;
        break;
    }

    ((PSTB_HDR)w->Req)->status = st;
    KeSetEvent(&w->Done, IO_NO_INCREMENT, FALSE);
    PsTerminateSystemThread(STATUS_SUCCESS);
}

static NTSTATUS StbRunAsSystem(ULONG op, PVOID req, PEPROCESS requester)
{
    STB_WORK w;
    HANDLE hThread = NULL;
    NTSTATUS st;

    KeInitializeEvent(&w.Done, NotificationEvent, FALSE);
    w.Op = op;
    w.Req = req;
    w.Requester = requester;

    st = PsCreateSystemThread(&hThread, THREAD_ALL_ACCESS, NULL, NULL, NULL,
                              StbWorker, &w);
    if (!NT_SUCCESS(st)) {
        ((PSTB_HDR)req)->status = st;
        return st;
    }
    KeWaitForSingleObject(&w.Done, Executive, KernelMode, FALSE, NULL);
    ZwClose(hThread);
    return ((PSTB_HDR)req)->status;
}

/* ---------------- DriverEntry / 卸载 ---------------- */

NTSTATUS DriverEntry(PDRIVER_OBJECT DriverObject, PUNICODE_STRING RegistryPath)
{
    NTSTATUS st;
    UNREFERENCED_PARAMETER(RegistryPath);
    st = StbCreateDevice(DriverObject);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    DriverObject->MajorFunction[IRP_MJ_CREATE] = StbDispatchCreateClose;
    DriverObject->MajorFunction[IRP_MJ_CLOSE]  = StbDispatchCreateClose;
    DriverObject->MajorFunction[IRP_MJ_DEVICE_CONTROL] =
        StbDispatchDeviceControl;
    DriverObject->DriverUnload = StbUnload;

    /* 动态解析被 WDK 导入库隐藏的 Nt* 服务（运行时有导出；失败则相关 op 返回
     * STATUS_NOT_IMPLEMENTED，不阻止驱动加载） */
    StbNtWriteVirtualMemory =
        (PFN_NTWRITEVM)StbResolveRoutine(L"NtWriteVirtualMemory");
    StbNtCreateThreadEx =
        (PFN_NTCREATETHREADEX)StbResolveRoutine(L"NtCreateThreadEx");
    StbNtSuspendProcess =
        (PFN_NTSUSPENDPROC)StbResolveRoutine(L"NtSuspendProcess");
    StbNtResumeProcess =
        (PFN_NTRESUMEPROC)StbResolveRoutine(L"NtResumeProcess");
    return STATUS_SUCCESS;
}

VOID StbUnload(PDRIVER_OBJECT DriverObject)
{
    UNICODE_STRING dosName = RTL_CONSTANT_STRING(STBDRV_DOS_NAME);
    IoDeleteSymbolicLink(&dosName);
    IoDeleteDevice(DriverObject->DeviceObject);
}

NTSTATUS StbCreateDevice(PDRIVER_OBJECT DriverObject)
{
    UNICODE_STRING devName = RTL_CONSTANT_STRING(STBDRV_DEVICE_NAME);
    UNICODE_STRING dosName = RTL_CONSTANT_STRING(STBDRV_DOS_NAME);
    PDEVICE_OBJECT dev = NULL;
    NTSTATUS st = IoCreateDevice(DriverObject, 0, &devName, FILE_DEVICE_UNKNOWN,
                                 0, FALSE, &dev);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = IoCreateSymbolicLink(&dosName, &devName);
    if (!NT_SUCCESS(st)) {
        IoDeleteDevice(dev);
        return st;
    }
    dev->Flags |= DO_BUFFERED_IO;   /* METHOD_BUFFERED */
    dev->Flags &= ~DO_DEVICE_INITIALIZING;
    return STATUS_SUCCESS;
}

NTSTATUS StbDispatchCreateClose(PDEVICE_OBJECT DeviceObject, PIRP Irp)
{
    UNREFERENCED_PARAMETER(DeviceObject);
    Irp->IoStatus.Status = STATUS_SUCCESS;
    Irp->IoStatus.Information = 0;
    IoCompleteRequest(Irp, IO_NO_INCREMENT);
    return STATUS_SUCCESS;
}

/* ---------------- IOCTL 分派 ---------------- */

NTSTATUS StbDispatchDeviceControl(PDEVICE_OBJECT DeviceObject, PIRP Irp)
{
    PIO_STACK_LOCATION irpSp = IoGetCurrentIrpStackLocation(Irp);
    ULONG code = irpSp->Parameters.DeviceIoControl.IoControlCode;
    ULONG inLen = irpSp->Parameters.DeviceIoControl.InputBufferLength;
    ULONG outLen = irpSp->Parameters.DeviceIoControl.OutputBufferLength;
    PVOID buf = Irp->AssociatedIrp.SystemBuffer;
    NTSTATUS status;
    ULONG op = 0;
    ULONG minSize = sizeof(STB_HDR);

    UNREFERENCED_PARAMETER(DeviceObject);
    Irp->IoStatus.Information = 0;
    if (buf == NULL || inLen < sizeof(STB_HDR) || outLen < sizeof(STB_HDR)) {
        status = STATUS_BUFFER_TOO_SMALL;
        goto done;
    }
    if (((PSTB_HDR)buf)->magic != STBDRV_TAG) {
        status = STATUS_INVALID_PARAMETER;
        goto done;
    }

    switch (code) {
    case IOCTL_STB_DRV_INFO:      op = OpInfo;       minSize = sizeof(STB_DRV_INFO);      break;
    case IOCTL_STB_PROC_KILL:     op = OpProcKill;   minSize = sizeof(STB_PROC_OP);       break;
    case IOCTL_STB_PROC_SUSPEND:  op = OpProcSuspend; minSize = sizeof(STB_PROC_OP);      break;
    case IOCTL_STB_PROC_RESUME:   op = OpProcResume; minSize = sizeof(STB_PROC_OP);       break;
    case IOCTL_STB_PROC_START:    op = OpProcStart;  minSize = sizeof(STB_PROC_START);    break;
    case IOCTL_STB_MEM_READ:      op = OpMemRead;    minSize = sizeof(STB_MEM_READ);      break;
    case IOCTL_STB_MEM_WRITE:     op = OpMemWrite;   minSize = sizeof(STB_MEM_WRITE);     break;
    case IOCTL_STB_FILE_READ:     op = OpFileRead;   minSize = sizeof(STB_FILE_READ);     break;
    case IOCTL_STB_FILE_WRITE:    op = OpFileWrite;  minSize = sizeof(STB_FILE_WRITE);    break;
    case IOCTL_STB_FILE_DELETE:   op = OpFileDelete; minSize = sizeof(STB_FILE_PATH);     break;
    case IOCTL_STB_FILE_MKDIR:    op = OpFileMkdir;  minSize = sizeof(STB_FILE_PATH);     break;
    case IOCTL_STB_REG_READ:      op = OpRegRead;    minSize = sizeof(STB_REG_OP);        break;
    case IOCTL_STB_REG_WRITE:     op = OpRegWrite;   minSize = sizeof(STB_REG_OP);        break;
    case IOCTL_STB_REG_DELETE:    op = OpRegDelete;  minSize = sizeof(STB_REG_OP);        break;
    case IOCTL_STB_REG_LIST:      op = OpRegList;    minSize = sizeof(STB_REG_OP);        break;
    case IOCTL_STB_POLICY_SET:    op = OpPolicySet;  minSize = sizeof(STB_POLICY_SET);    break;
    case IOCTL_STB_CMD_EXEC:      op = OpCmdExec;    minSize = sizeof(STB_CMD_EXEC);      break;
    case IOCTL_STB_TOKEN_ELEVATE: op = OpTokenElevate; minSize = sizeof(STB_TOKEN_ELEVATE); break;
    default:
        status = STATUS_INVALID_DEVICE_REQUEST;
        goto done;
    }
    if (inLen < minSize || outLen < minSize) {
        status = STATUS_BUFFER_TOO_SMALL;
        goto done;
    }
    if (((PSTB_HDR)buf)->op != op) {
        status = STATUS_INVALID_PARAMETER;
        goto done;
    }

    {
        PEPROCESS requester = IoGetRequestorProcess(Irp);
        if (requester != NULL) {
            ObReferenceObject(requester);
        }
        StbRunAsSystem(op, buf, requester);   /* 状态已写入 hdr.status */
        if (requester != NULL) {
            ObDereferenceObject(requester);
        }
        Irp->IoStatus.Information = outLen;
        status = STATUS_SUCCESS;              /* 恒成功，供客户端读 hdr.status */
    }

done:
    Irp->IoStatus.Status = status;
    IoCompleteRequest(Irp, IO_NO_INCREMENT);
    return status;
}

/* ---------------- 信息 ---------------- */

NTSTATUS StbOpInfo(PSTB_DRV_INFO req)
{
    req->version = STB_IFACE_VERSION;
    req->caps = STB_CAP_PROC | STB_CAP_MEM | STB_CAP_FILE |
                STB_CAP_REG | STB_CAP_GP | STB_CAP_EXEC | STB_CAP_TOKEN;
    return STATUS_SUCCESS;
}

/* ---------------- 进程：结束 / 挂起 / 恢复 ---------------- */

NTSTATUS StbOpProcKill(ULONG pid)
{
    HANDLE hProc = NULL;
    CLIENT_ID cid;
    OBJECT_ATTRIBUTES oa;
    NTSTATUS st;

    if (pid <= 4) {
        return STATUS_ACCESS_DENIED;    /* 保护系统关键进程 */
    }
    cid.UniqueProcess = (HANDLE)(ULONG_PTR)pid;
    cid.UniqueThread = NULL;
    InitializeObjectAttributes(&oa, NULL, OBJ_KERNEL_HANDLE, NULL, NULL);
    st = ZwOpenProcess(&hProc, PROCESS_TERMINATE, &oa, &cid);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = ZwTerminateProcess(hProc, 0);
    ZwClose(hProc);
    return st;
}

/* SystemExtendedProcessInformation 结构（winternl.h 布局, class=57） */
typedef struct _SYSTEM_EXTENDED_THREAD_INFORMATION {
    CLIENT_ID   ClientId;
    KPRIORITY   Priority;
    LONG        BasePriority;
    ULONG       Affinity;
    THREAD_STATE ThreadState;
    KWAIT_REASON WaitReason;
} SYSTEM_EXTENDED_THREAD_INFORMATION, *PSYSTEM_EXTENDED_THREAD_INFORMATION;

typedef struct _SYSTEM_EXTENDED_PROCESS_INFORMATION {
    ULONG       NextEntryOffset;
    ULONG       NumberOfThreads;
    LARGE_INTEGER WorkingSetPrivateSize;
    ULONG       HardFaultCount;
    ULONG       NumberOfThreadsHighWatermark;
    ULONGLONG   CycleTime;
    LARGE_INTEGER CreateTime;
    LARGE_INTEGER UserTime;
    LARGE_INTEGER KernelTime;
    UNICODE_STRING ImageName;
    KPRIORITY   BasePriority;
    HANDLE      UniqueProcessId;
    HANDLE      InheritedFromUniqueProcessId;
    ULONG       HandleCount;
    ULONG       SessionId;
    ULONG_PTR   UniqueProcessKey;
    ULONG_PTR   PeakVirtualSize;
    ULONG_PTR   VirtualSize;
    ULONG       PageFaultCount;
    ULONG_PTR   PeakWorkingSetSize;
    ULONG_PTR   WorkingSetSize;
    ULONG_PTR   QuotaPeakPagedPoolUsage;
    ULONG_PTR   QuotaPagedPoolUsage;
    ULONG_PTR   QuotaPeakNonPagedPoolUsage;
    ULONG_PTR   QuotaNonPagedPoolUsage;
    ULONG_PTR   PagefileUsage;
    ULONG_PTR   PeakPagefileUsage;
    ULONG_PTR   PrivatePageCount;
    LARGE_INTEGER ReadOperationCount;
    LARGE_INTEGER WriteOperationCount;
    LARGE_INTEGER OtherOperationCount;
    LARGE_INTEGER ReadTransferCount;
    LARGE_INTEGER WriteTransferCount;
    LARGE_INTEGER OtherTransferCount;
    SYSTEM_EXTENDED_THREAD_INFORMATION Threads[1];
} SYSTEM_EXTENDED_PROCESS_INFORMATION, *PSYSTEM_EXTENDED_PROCESS_INFORMATION;

static NTSTATUS StbQueryExtendedProcesses(PVOID* outBuf, PULONG outSize)
{
    ULONG size = 0;
    NTSTATUS st = ZwQuerySystemInformation(
        57 /*SystemExtendedProcessInformation*/, NULL, 0, &size);
    if (st != STATUS_INFO_LENGTH_MISMATCH && size == 0) {
        return STATUS_INFO_LENGTH_MISMATCH;
    }
    *outBuf = ExAllocatePoolWithTag(NonPagedPool, size, STBDRV_TAG);
    if (*outBuf == NULL) {
        return STATUS_INSUFFICIENT_RESOURCES;
    }
    st = ZwQuerySystemInformation(57, *outBuf, size, outSize);
    if (!NT_SUCCESS(st)) {
        ExFreePoolWithTag(*outBuf, STBDRV_TAG);
        *outBuf = NULL;
    }
    return st;
}

NTSTATUS StbOpProcSuspendResume(ULONG pid, BOOLEAN suspend)
{
    /* 整进程挂起/恢复：ZwOpenProcess + NtSuspendProcess/NtResumeProcess
     * （Nt* 为系统服务，运行时有导出；WDK 导入库刻意隐藏，运行时解析） */
    HANDLE hProc = NULL;
    CLIENT_ID cid;
    OBJECT_ATTRIBUTES oa;
    NTSTATUS st;

    if (pid <= 4) {
        return STATUS_ACCESS_DENIED;   /* 不挂起系统关键进程 */
    }
    if (suspend) {
        STB_REQUIRE(StbNtSuspendProcess);
    } else {
        STB_REQUIRE(StbNtResumeProcess);
    }
    cid.UniqueProcess = (HANDLE)(ULONG_PTR)pid;
    cid.UniqueThread = NULL;
    InitializeObjectAttributes(&oa, NULL, OBJ_KERNEL_HANDLE, NULL, NULL);
    st = ZwOpenProcess(&hProc, PROCESS_SUSPEND_RESUME, &oa, &cid);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    if (suspend) {
        st = StbNtSuspendProcess(hProc);
    } else {
        st = StbNtResumeProcess(hProc);
    }
    ZwClose(hProc);
    return st;
}

/* ---------------- 进程/命令：内核创建进程（不经 cmd） ---------------- */

NTSTATUS StbBuildProcessParameters(PEPROCESS Process, HANDLE hProc,
                                   PWSTR imagePath, PWSTR commandLine,
                                   PWSTR workdir,
                                   PVOID* outParamsUser)
{
    SIZE_T imageLen = (wcslen(imagePath) + 1);
    SIZE_T cmdLen = (wcslen(commandLine) + 1);
    SIZE_T wdLen = (wcslen(workdir) + 1);
    /* +3：DllPath 空串 NUL + 空环境块双 NUL */
    SIZE_T strBytes = (imageLen + cmdLen + wdLen + 3) * sizeof(WCHAR);
    ULONG paramSize = (ULONG)(sizeof(STB_RTL_USER_PROCESS_PARAMETERS) + strBytes);
    PVOID userBase = NULL;
    NTSTATUS st = STATUS_SUCCESS;

    STB_REQUIRE(StbNtWriteVirtualMemory);

    paramSize = (paramSize + 0xF) & ~0xF;

    st = ZwAllocateVirtualMemory(hProc, &userBase, 0,
                                 (PSIZE_T)&paramSize,
                                 MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!NT_SUCCESS(st)) {
        return st;
    }

    {
        PSTB_RTL_USER_PROCESS_PARAMETERS pp = NULL;
        PWSTR s;
        SIZE_T written = 0;
        SIZE_T freeSize = paramSize;
        pp = (PSTB_RTL_USER_PROCESS_PARAMETERS)
            ExAllocatePoolWithTag(NonPagedPool, paramSize, STBDRV_TAG);
        if (pp == NULL) {
            ZwFreeVirtualMemory(hProc, &userBase, &freeSize, MEM_RELEASE);
            return STATUS_INSUFFICIENT_RESOURCES;
        }
        RtlZeroMemory(pp, paramSize);
        pp->MaximumLength = (USHORT)paramSize;
        pp->Length = (USHORT)(sizeof(STB_RTL_USER_PROCESS_PARAMETERS) + strBytes);

        s = (PWSTR)((PUCHAR)pp + sizeof(STB_RTL_USER_PROCESS_PARAMETERS));

        RtlInitUnicodeString(&pp->ImagePathName, s);
        RtlStringCchCopyW(s, imageLen, imagePath);
        s += imageLen;

        RtlInitUnicodeString(&pp->CommandLine, s);
        RtlStringCchCopyW(s, cmdLen, commandLine);
        s += cmdLen;

        pp->CurrentDirectory.DosPath.Length = (USHORT)((wdLen - 1) * sizeof(WCHAR));
        pp->CurrentDirectory.DosPath.MaximumLength = (USHORT)(wdLen * sizeof(WCHAR));
        pp->CurrentDirectory.DosPath.Buffer = s;
        RtlStringCchCopyW(s, wdLen, workdir);
        s += wdLen;

        RtlInitUnicodeString(&pp->DllPath, s);
        *s = 0;
        s += 1;

        pp->Environment = s;   /* 空环境块：两个 NUL */
        *s = 0;
        *(s + 1) = 0;
        s += 2;

        st = StbNtWriteVirtualMemory(hProc, userBase, pp, paramSize, &written);
        ExFreePoolWithTag(pp, STBDRV_TAG);
        if (!NT_SUCCESS(st)) {
            SIZE_T freeSize2 = paramSize;
            ZwFreeVirtualMemory(hProc, &userBase, &freeSize2, MEM_RELEASE);
            return st;
        }
    }

    /* PEB->ProcessParameters = userBase */
    {
        PSTB_PEB peb = PsGetProcessPeb(Process);
        if (peb != NULL) {
            ULONG_PTR ptr = (ULONG_PTR)userBase;
            SIZE_T written = 0;
            st = StbNtWriteVirtualMemory(
                hProc, (PVOID)((PUCHAR)peb +
                               FIELD_OFFSET(STB_PEB, ProcessParameters)),
                &ptr, sizeof(ptr), &written);
            if (!NT_SUCCESS(st)) {
                SIZE_T freeSize3 = paramSize;
                ZwFreeVirtualMemory(hProc, &userBase, &freeSize3, MEM_RELEASE);
                return st;
            }
        }
    }
    *outParamsUser = userBase;
    return STATUS_SUCCESS;
}

NTSTATUS StbOpCreateProcess(PWSTR path, PWSTR args, PWSTR workdir,
                            ULONG_PTR ldrInit, PULONG outPid)
{
    NTSTATUS st = STATUS_SUCCESS;
    HANDLE hFile = NULL, hSection = NULL, hProc = NULL;
    PEPROCESS newProc = NULL;
    PWSTR commandLine = NULL;
    SIZE_T cmdLen = 0;

    if (ldrInit == 0 || path == NULL || path[0] == 0) {
        return STATUS_INVALID_PARAMETER;
    }
    if (workdir == NULL || workdir[0] == 0) {
        workdir = L"C:\\Windows\\System32";
    }
    STB_REQUIRE(StbNtCreateThreadEx);

    {
        SIZE_T plen = wcslen(path);
        SIZE_T alen = (args != NULL) ? wcslen(args) : 0;
        cmdLen = plen + alen + 4;
        commandLine = ExAllocatePoolWithTag(NonPagedPool,
                                            cmdLen * sizeof(WCHAR), STBDRV_TAG);
        if (commandLine == NULL) {
            return STATUS_INSUFFICIENT_RESOURCES;
        }
        if (alen > 0) {
            RtlStringCchPrintfW(commandLine, cmdLen, L"\"%s\" %s", path, args);
        } else {
            RtlStringCchPrintfW(commandLine, cmdLen, L"\"%s\"", path);
        }
    }

    {
        UNICODE_STRING uImage;
        OBJECT_ATTRIBUTES oa;
        IO_STATUS_BLOCK iosb;
        RtlInitUnicodeString(&uImage, path);
        InitializeObjectAttributes(&oa, &uImage, OBJ_KERNEL_HANDLE |
                                   OBJ_CASE_INSENSITIVE, NULL, NULL);
        st = ZwCreateFile(&hFile, GENERIC_READ | SYNCHRONIZE, &oa, &iosb, NULL,
                          FILE_ATTRIBUTE_NORMAL,
                          FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                          FILE_OPEN, FILE_SYNCHRONOUS_IO_NONALERT, NULL, 0);
        if (!NT_SUCCESS(st)) {
            goto cleanup;
        }
        st = ZwCreateSection(&hSection, SECTION_ALL_ACCESS, NULL, NULL,
                             PAGE_READONLY, SEC_IMAGE, hFile);
        ZwClose(hFile);
        hFile = NULL;
        if (!NT_SUCCESS(st)) {
            goto cleanup;
        }
    }

    /* 以 System 为父创建新进程（镜像已映射） */
    st = ZwCreateProcessEx(&hProc, PROCESS_ALL_ACCESS, NULL,
                           (HANDLE)ZwCurrentProcess(), TRUE, hSection,
                           NULL, NULL, 0);
    ZwClose(hSection);
    hSection = NULL;
    if (!NT_SUCCESS(st)) {
        goto cleanup;
    }

    {
        PROCESS_BASIC_INFORMATION pbi;
        ULONG ret = 0;
        st = ZwQueryInformationProcess(hProc, ProcessBasicInformation, &pbi,
                                       sizeof(pbi), &ret);
        if (!NT_SUCCESS(st)) {
            goto cleanup;
        }
        st = PsLookupProcessByProcessId(
            (HANDLE)(ULONG_PTR)pbi.UniqueProcessId, &newProc);
        if (!NT_SUCCESS(st)) {
            goto cleanup;
        }
        if (outPid != NULL) {
            *outPid = (ULONG)(ULONG_PTR)pbi.UniqueProcessId;
        }

        {
            PVOID ppUser = NULL;
            st = StbBuildProcessParameters(newProc, hProc, path,
                                           commandLine, workdir, &ppUser);
            if (!NT_SUCCESS(st)) {
                goto cleanup;
            }
        }

        /* 初始线程：LdrInitializeThunk(参数=高32位映像基址 | 低32位入口点) */
        {
            PVOID imageBase = PsGetProcessSectionBaseAddress(newProc);
            HANDLE hThread = NULL;
            ULONG_PTR entry = 0;
            if (imageBase != NULL) {
                PSTB_DOS_HEADER dos = (PSTB_DOS_HEADER)imageBase;
                PSTB_IMAGE_NT_HEADERS64 nt = (PSTB_IMAGE_NT_HEADERS64)
                    ((PUCHAR)imageBase + dos->e_lfanew);
                entry = (ULONG_PTR)imageBase +
                        nt->OptionalHeader.AddressOfEntryPoint;
            }
            if (entry == 0) {
                st = STATUS_INVALID_IMAGE_FORMAT;
                goto cleanup;
            }
            {
                ULONG_PTR startParam =
                    ((ULONG_PTR)imageBase & 0xFFFFFFFF00000000ULL) |
                    (entry & 0xFFFFFFFF);
                st = StbNtCreateThreadEx(&hThread, THREAD_ALL_ACCESS, NULL,
                                         hProc, (PKSTART_ROUTINE)ldrInit,
                                         (PVOID)startParam, 0, 0, 0, NULL);
                if (NT_SUCCESS(st)) {
                    ZwClose(hThread);
                }
            }
        }
    }

cleanup:
    if (hSection != NULL) {
        ZwClose(hSection);
    }
    if (hProc != NULL) {
        ZwClose(hProc);
    }
    if (newProc != NULL) {
        ObDereferenceObject(newProc);
    }
    if (commandLine != NULL) {
        ExFreePoolWithTag(commandLine, STBDRV_TAG);
    }
    return st;
}

/* ---------------- 内存：读写 ---------------- */

NTSTATUS StbOpMemRead(ULONG pid, ULONG_PTR address, PUCHAR out,
                      ULONG size, PULONG actual)
{
    PEPROCESS target = NULL;
    SIZE_T done = 0;
    NTSTATUS st = PsLookupProcessByProcessId((HANDLE)(ULONG_PTR)pid, &target);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = MmCopyVirtualMemory(target, (PVOID)address, PsGetCurrentProcess(),
                             out, size, KernelMode, &done);
    ObDereferenceObject(target);
    if (NT_SUCCESS(st) && actual != NULL) {
        *actual = (ULONG)done;
    }
    return st;
}

NTSTATUS StbOpMemWrite(ULONG pid, ULONG_PTR address, PUCHAR in,
                       ULONG size, PULONG actual)
{
    PEPROCESS target = NULL;
    SIZE_T done = 0;
    NTSTATUS st = PsLookupProcessByProcessId((HANDLE)(ULONG_PTR)pid, &target);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = MmCopyVirtualMemory(PsGetCurrentProcess(), in, target,
                             (PVOID)address, size, KernelMode, &done);
    ObDereferenceObject(target);
    if (NT_SUCCESS(st) && actual != NULL) {
        *actual = (ULONG)done;
    }
    return st;
}

/* ---------------- 文件：读写 / 删除 / 建目录 ---------------- */

static NTSTATUS StbOpenFile(PWSTR path, ACCESS_MASK access, ULONG share,
                            ULONG disposition, PHANDLE outHandle)
{
    UNICODE_STRING u;
    OBJECT_ATTRIBUTES oa;
    IO_STATUS_BLOCK iosb;
    RtlInitUnicodeString(&u, path);
    InitializeObjectAttributes(&oa, &u, OBJ_KERNEL_HANDLE |
                               OBJ_CASE_INSENSITIVE, NULL, NULL);
    return ZwCreateFile(outHandle, access, &oa, &iosb, NULL,
                        FILE_ATTRIBUTE_NORMAL, share, disposition,
                        FILE_SYNCHRONOUS_IO_NONALERT, NULL, 0);
}

NTSTATUS StbOpFileRead(PWSTR path, PUCHAR out, ULONG maxSize, PULONG actual)
{
    HANDLE h = NULL;
    IO_STATUS_BLOCK iosb;
    NTSTATUS st = StbOpenFile(path, GENERIC_READ,
                              FILE_SHARE_READ | FILE_SHARE_WRITE,
                              FILE_OPEN, &h);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = ZwReadFile(h, NULL, NULL, NULL, &iosb, out, maxSize, NULL, NULL);
    ZwClose(h);
    if (NT_SUCCESS(st) && actual != NULL) {
        *actual = (ULONG)iosb.Information;
    }
    return st;
}

NTSTATUS StbOpFileWrite(PWSTR path, PUCHAR in, ULONG size, ULONG flags)
{
    HANDLE h = NULL;
    IO_STATUS_BLOCK iosb;
    NTSTATUS st = StbOpenFile(path, GENERIC_WRITE | SYNCHRONIZE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE,
                              (flags == 1) ? FILE_OPEN_IF : FILE_OVERWRITE_IF,
                              &h);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    if (flags == 1) {
        LARGE_INTEGER off;
        off.QuadPart = 0;
        ZwSetInformationFile(h, &iosb, &off, sizeof(off),
                             FileEndOfFileInformation);
        off.QuadPart = 0;
        ZwSetInformationFile(h, &iosb, &off, sizeof(off),
                             FilePositionInformation);
    }
    st = ZwWriteFile(h, NULL, NULL, NULL, &iosb, in, size, NULL, NULL);
    ZwClose(h);
    return st;
}

NTSTATUS StbOpFileDelete(PWSTR path)
{
    UNICODE_STRING u;
    OBJECT_ATTRIBUTES oa;
    RtlInitUnicodeString(&u, path);
    InitializeObjectAttributes(&oa, &u, OBJ_KERNEL_HANDLE |
                               OBJ_CASE_INSENSITIVE, NULL, NULL);
    return ZwDeleteFile(&oa);
}

NTSTATUS StbOpFileMkdir(PWSTR path)
{
    /* 只创建一层；多级由客户端逐级调用 */
    HANDLE h = NULL;
    IO_STATUS_BLOCK iosb;
    UNICODE_STRING u;
    OBJECT_ATTRIBUTES oa;
    RtlInitUnicodeString(&u, path);
    InitializeObjectAttributes(&oa, &u, OBJ_KERNEL_HANDLE |
                               OBJ_CASE_INSENSITIVE, NULL, NULL);
    {
        NTSTATUS st = ZwCreateFile(&h, GENERIC_WRITE, &oa, &iosb, NULL,
                                   FILE_ATTRIBUTE_NORMAL,
                                   FILE_SHARE_READ | FILE_SHARE_WRITE,
                                   FILE_CREATE,
                                   FILE_SYNCHRONOUS_IO_NONALERT, NULL, 0);
        if (NT_SUCCESS(st) && h != NULL) {
            ZwClose(h);
        }
        return st;
    }
}

/* ---------------- 注册表 ---------------- */

static NTSTATUS StbOpenRegRoot(STB_REG_HIVE hive, PEPROCESS requester,
                               PHANDLE outRoot)
{
    NTSTATUS st;
    HANDLE root = NULL;
    UNICODE_STRING path;
    OBJECT_ATTRIBUTES oa;

    switch (hive) {
    case StbHiveHklm:
        RtlInitUnicodeString(&path, L"\\Registry\\Machine");
        break;
    case StbHiveHkcr:
        RtlInitUnicodeString(&path, L"\\Registry\\Machine\\Software\\Classes");
        break;
    case StbHiveHku:
        RtlInitUnicodeString(&path, L"\\Registry\\User");
        break;
    case StbHiveHkcu: {
        /* SYSTEM 线程无"当前用户"：用发起进程令牌的 SID 解析
         * \Registry\User\<SID>。查不到则回退 HKU 根。 */
        HANDLE hTok = NULL;
        ULONG need = 0;
        PTOKEN_USER user = NULL;
        UNICODE_STRING sidStr;
        PWSTR full = NULL;
        RtlInitUnicodeString(&sidStr, NULL);

        st = ZwOpenProcessTokenEx(requester, TOKEN_QUERY, OBJ_KERNEL_HANDLE,
                                  &hTok);
        if (NT_SUCCESS(st)) {
            st = ZwQueryInformationToken(hTok, TokenUser, NULL, 0, &need);
            if (st == STATUS_BUFFER_TOO_SMALL && need > 0) {
                user = ExAllocatePoolWithTag(PagedPool, need, STBDRV_TAG);
                if (user != NULL) {
                    st = ZwQueryInformationToken(hTok, TokenUser, user,
                                                 need, &need);
                }
            }
            ZwClose(hTok);
        }
        if (!NT_SUCCESS(st) || user == NULL) {
            if (user != NULL) {
                ExFreePoolWithTag(user, STBDRV_TAG);
            }
            RtlInitUnicodeString(&path, L"\\Registry\\User");
            break;
        }
        st = RtlConvertSidToUnicodeString(&sidStr, user->User.Sid, TRUE);
        ExFreePoolWithTag(user, STBDRV_TAG);
        if (!NT_SUCCESS(st)) {
            RtlInitUnicodeString(&path, L"\\Registry\\User");
            break;
        }
        {
            SIZE_T total = (wcslen(L"\\Registry\\User\\") +
                            sidStr.Length / sizeof(WCHAR) + 1) *
                           sizeof(WCHAR);
            full = ExAllocatePoolWithTag(PagedPool, total, STBDRV_TAG);
            if (full == NULL) {
                RtlFreeUnicodeString(&sidStr);
                return STATUS_INSUFFICIENT_RESOURCES;
            }
            RtlStringCchPrintfW(full, total / sizeof(WCHAR),
                                L"\\Registry\\User\\%wZ", &sidStr);
            RtlFreeUnicodeString(&sidStr);
            RtlInitUnicodeString(&path, full);
            InitializeObjectAttributes(&oa, &path, OBJ_KERNEL_HANDLE |
                                       OBJ_CASE_INSENSITIVE, NULL, NULL);
            st = ZwOpenKey(&root, KEY_READ | KEY_WRITE, &oa);
            ExFreePoolWithTag(full, STBDRV_TAG);
            if (!NT_SUCCESS(st)) {
                /* 用户键不存在时回退 HKU 根 */
                RtlInitUnicodeString(&path, L"\\Registry\\User");
                InitializeObjectAttributes(&oa, &path, OBJ_KERNEL_HANDLE |
                                           OBJ_CASE_INSENSITIVE, NULL, NULL);
                st = ZwOpenKey(&root, KEY_READ | KEY_WRITE, &oa);
            }
            if (NT_SUCCESS(st)) {
                *outRoot = root;
            }
            return st;
        }
    }
    default:
        return STATUS_INVALID_PARAMETER;
    }

    InitializeObjectAttributes(&oa, &path, OBJ_KERNEL_HANDLE |
                               OBJ_CASE_INSENSITIVE, NULL, NULL);
    st = ZwOpenKey(&root, KEY_READ | KEY_WRITE, &oa);
    if (!NT_SUCCESS(st)) {
        st = ZwOpenKey(&root, KEY_READ, &oa);
    }
    if (NT_SUCCESS(st)) {
        *outRoot = root;
    }
    return st;
}

static NTSTATUS StbOpenRegKey(HANDLE root, PWSTR subkey, ACCESS_MASK access,
                              BOOLEAN create, PHANDLE outKey)
{
    UNICODE_STRING u;
    OBJECT_ATTRIBUTES oa;
    RtlInitUnicodeString(&u, subkey);
    InitializeObjectAttributes(&oa, &u, OBJ_KERNEL_HANDLE |
                               OBJ_CASE_INSENSITIVE, NULL, NULL);
    if (create) {
        ULONG disp = 0;
        return ZwCreateKey(outKey, access, &oa, 0, NULL, 0, &disp);
    }
    return ZwOpenKey(outKey, access, &oa);
}

NTSTATUS StbOpRegRead(PSTB_REG_OP req, PEPROCESS requester)
{
    HANDLE root = NULL, key = NULL;
    PKEY_VALUE_PARTIAL_INFORMATION part = NULL;
    ULONG need = 0;
    NTSTATUS st = StbOpenRegRoot((STB_REG_HIVE)req->hive, requester, &root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = StbOpenRegKey(root, req->subkey, KEY_QUERY_VALUE, FALSE, &key);
    ZwClose(root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    /* 先取长度 */
    {
        UNICODE_STRING uName;
        RtlInitUnicodeString(&uName, req->name);
        st = ZwQueryValueKey(key, &uName, KeyValuePartialInformation,
                             NULL, 0, &need);
        if (st != STATUS_BUFFER_OVERFLOW && st != STATUS_BUFFER_TOO_SMALL &&
            !NT_SUCCESS(st)) {
            ZwClose(key);
            return st;
        }
        part = (PKEY_VALUE_PARTIAL_INFORMATION)
            ExAllocatePoolWithTag(PagedPool, need, STBDRV_TAG);
        if (part == NULL) {
            ZwClose(key);
            return STATUS_INSUFFICIENT_RESOURCES;
        }
        st = ZwQueryValueKey(key, &uName, KeyValuePartialInformation,
                             part, need, &need);
    }
    ZwClose(key);
    if (!NT_SUCCESS(st)) {
        ExFreePoolWithTag(part, STBDRV_TAG);
        return st;
    }
    req->type = part->Type;
    req->size = (need > FIELD_OFFSET(KEY_VALUE_PARTIAL_INFORMATION, Data))
        ? need - FIELD_OFFSET(KEY_VALUE_PARTIAL_INFORMATION, Data) : 0;
    if (req->size > STB_MAX_DATA) {
        req->size = STB_MAX_DATA;
    }
    RtlCopyMemory(req->data, part->Data, req->size);
    ExFreePoolWithTag(part, STBDRV_TAG);
    return STATUS_SUCCESS;
}

NTSTATUS StbOpRegWrite(PSTB_REG_OP req, PEPROCESS requester)
{
    HANDLE root = NULL, key = NULL;
    NTSTATUS st = StbOpenRegRoot((STB_REG_HIVE)req->hive, requester, &root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = StbOpenRegKey(root, req->subkey, KEY_SET_VALUE, TRUE, &key);
    ZwClose(root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    UNICODE_STRING uName;
    RtlInitUnicodeString(&uName, req->name);
    st = ZwSetValueKey(key, &uName, 0, req->type, req->data, req->size);
    ZwClose(key);
    return st;
}

NTSTATUS StbOpRegDelete(PSTB_REG_OP req, PEPROCESS requester)
{
    HANDLE root = NULL, key = NULL;
    NTSTATUS st = StbOpenRegRoot((STB_REG_HIVE)req->hive, requester, &root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    if (req->name[0] != 0) {
        UNICODE_STRING uName;
        RtlInitUnicodeString(&uName, req->name);
        st = StbOpenRegKey(root, req->subkey, KEY_SET_VALUE, FALSE, &key);
        ZwClose(root);
        if (!NT_SUCCESS(st)) {
            return st;
        }
        st = ZwDeleteValueKey(key, &uName);
        ZwClose(key);
        return st;
    }
    st = StbOpenRegKey(root, req->subkey, KEY_ALL_ACCESS, FALSE, &key);
    ZwClose(root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = ZwDeleteKey(key);
    ZwClose(key);
    return st;
}

NTSTATUS StbOpRegList(PSTB_REG_OP req, PEPROCESS requester)
{
    HANDLE root = NULL, key = NULL;
    NTSTATUS st = StbOpenRegRoot((STB_REG_HIVE)req->hive, requester, &root);
    ULONG i = 0;
    ULONG used = 4;                 /* 前 4 字节 = 值数量 */
    ULONG count = 0;
    ULONG outSize = STB_MAX_DATA;

    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = StbOpenRegKey(root, req->subkey, KEY_QUERY_VALUE, FALSE, &key);
    ZwClose(root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    for (; used < outSize; i++) {
        ULONG need = 0;
        PKEY_VALUE_FULL_INFORMATION full = NULL;
        ULONG nameLen, dataLen;
        st = ZwEnumerateValueKey(key, i, KeyValueFullInformation,
                                 NULL, 0, &need);
        if (st == STATUS_NO_MORE_ENTRIES) {
            st = STATUS_SUCCESS;
            break;
        }
        if (st != STATUS_BUFFER_OVERFLOW && st != STATUS_BUFFER_TOO_SMALL &&
            !NT_SUCCESS(st)) {
            break;
        }
        full = (PKEY_VALUE_FULL_INFORMATION)
            ExAllocatePoolWithTag(PagedPool, need, STBDRV_TAG);
        if (full == NULL) {
            st = STATUS_INSUFFICIENT_RESOURCES;
            break;
        }
        st = ZwEnumerateValueKey(key, i, KeyValueFullInformation,
                                 full, need, &need);
        if (!NT_SUCCESS(st)) {
            ExFreePoolWithTag(full, STBDRV_TAG);
            break;
        }
        nameLen = full->NameLength;
        dataLen = full->DataLength;
        if (used + 4 + nameLen + 4 + 4 + dataLen > outSize) {
            ExFreePoolWithTag(full, STBDRV_TAG);
            break;
        }
        RtlCopyMemory(req->data + used, &nameLen, 4);      used += 4;
        RtlCopyMemory(req->data + used, full->Name, nameLen); used += nameLen;
        RtlCopyMemory(req->data + used, &full->Type, 4);   used += 4;
        RtlCopyMemory(req->data + used, &dataLen, 4);      used += 4;
        if (dataLen > 0) {
            RtlCopyMemory(req->data + used,
                          (PUCHAR)full + full->DataOffset, dataLen);
            used += dataLen;
        }
        count++;
        ExFreePoolWithTag(full, STBDRV_TAG);
    }
    ZwClose(key);
    req->size = used;
    RtlCopyMemory(req->data, &count, 4);
    return st;
}

/* ---------------- 组策略（HKLM\SOFTWARE\Policies） ---------------- */

NTSTATUS StbOpPolicySet(PSTB_POLICY_SET req)
{
    HANDLE root = NULL, key = NULL;
    NTSTATUS st = StbOpenRegRoot(StbHiveHklm, NULL, &root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = StbOpenRegKey(root, req->policyPath, KEY_SET_VALUE, TRUE, &key);
    ZwClose(root);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    {
        UNICODE_STRING uName;
        RtlInitUnicodeString(&uName, req->name);
        st = ZwSetValueKey(key, &uName, 0, req->type, req->data, req->size);
    }
    ZwClose(key);
    return st;
}

/* ---------------- 令牌提权：复制 SYSTEM 令牌到客户端 ---------------- */

static NTSTATUS StbFindPidByName(PCWSTR name, PULONG outPid)
{
    PVOID buf = NULL;
    ULONG size = 0;
    PSYSTEM_EXTENDED_PROCESS_INFORMATION p;
    UNICODE_STRING u;
    NTSTATUS st = StbQueryExtendedProcesses(&buf, &size);

    if (!NT_SUCCESS(st)) {
        return st;
    }
    RtlInitUnicodeString(&u, name);
    p = (PSYSTEM_EXTENDED_PROCESS_INFORMATION)buf;
    for (;;) {
        if (p->ImageName.Buffer != NULL &&
            RtlEqualUnicodeString(&u, &p->ImageName, TRUE)) {
            *outPid = (ULONG)(ULONG_PTR)p->UniqueProcessId;
            ExFreePoolWithTag(buf, STBDRV_TAG);
            return STATUS_SUCCESS;
        }
        if (p->NextEntryOffset == 0) {
            break;
        }
        p = (PSYSTEM_EXTENDED_PROCESS_INFORMATION)
            ((PUCHAR)p + p->NextEntryOffset);
    }
    ExFreePoolWithTag(buf, STBDRV_TAG);
    return STATUS_NOT_FOUND;
}

NTSTATUS StbOpTokenElevate(PEPROCESS requester, PSTB_TOKEN_ELEVATE req)
{
    NTSTATUS st;
    ULONG winlogonPid = 0;
    HANDLE hProc = NULL, hTok = NULL, hNewTok = NULL, hClientTok = NULL;
    CLIENT_ID cid;
    OBJECT_ATTRIBUTES oa;

    if (requester == NULL) {
        return STATUS_INVALID_PARAMETER;
    }
    st = StbFindPidByName(L"winlogon.exe", &winlogonPid);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    cid.UniqueProcess = (HANDLE)(ULONG_PTR)winlogonPid;
    cid.UniqueThread = NULL;
    InitializeObjectAttributes(&oa, NULL, OBJ_KERNEL_HANDLE, NULL, NULL);
    st = ZwOpenProcess(&hProc, PROCESS_QUERY_LIMITED_INFORMATION, &oa, &cid);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    st = ZwOpenProcessTokenEx(hProc, TOKEN_QUERY | TOKEN_DUPLICATE |
                              TOKEN_ASSIGN_PRIMARY,
                              OBJ_KERNEL_HANDLE, &hTok);
    ZwClose(hProc);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    /* 复制为 PRIMARY 令牌（可用于 CreateProcessAsUser）；保留全部特权 */
    st = ZwDuplicateToken(hTok, TOKEN_ALL_ACCESS, NULL, FALSE,
                          TokenPrimary, &hNewTok);
    ZwClose(hTok);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    /* 把新令牌句柄注入客户端进程句柄表 */
    st = ZwDuplicateObject(PsGetCurrentProcess(), hNewTok, requester,
                           &hClientTok, TOKEN_ALL_ACCESS, 0, 0);
    ZwClose(hNewTok);
    if (!NT_SUCCESS(st)) {
        return st;
    }
    req->token = (ULONG_PTR)hClientTok;
    req->pid = winlogonPid;
    return STATUS_SUCCESS;
}