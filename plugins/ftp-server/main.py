"""FTP服务器插件 — 纯标准库实现的轻量 FTP 服务器。

特性：
- 多账户 + 匿名访问（可配）；PASV/EPSV 被动模式 + PORT/EPRT 主动模式；
- LIST / NLST / MLSD / RETR / STOR / APPE / STOU / DELE / MKD / RMD /
  RNFR/RNTO / SIZE / MDTM / REST(断点续传上下行) / STAT / TYPE / FEAT /
  UTF8；
- 根目录越界防护（路径统一解析到根目录内，拒绝 .. 逃逸）；
- 默认数据目录：插件文件夹下的 Data 子目录（自动创建），可在启动参数
  中指定其它根目录；
- 运行状态 / 客户端 / 操作日志通过 API 暴露给页面；
- 启动与停止为 admin 级接口（插件为 approval 权限时首次调用走审批流）。

安全说明：本服务器仅用于本机/内网工具场景，无 TLS 加密（AUTH 返回 502），
监听地址默认 127.0.0.1；需要对外暴露时请自行评估风险。
"""
import json
import os
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from app.api.router import ApiError

MAX_LINE = 8192          # 控制连接单行上限
DATA_TIMEOUT = 30        # 数据连接超时（秒）
MAX_LOG = 300            # 日志保留条数

# 默认数据目录：插件文件夹下的 Data 子目录
DATA_DIR = Path(__file__).resolve().parent / "Data"

# 命令别名（部分客户端使用 X 前缀形式）
_CMD_ALIASES = {
    "XMKD": "MKD", "XRMD": "RMD", "XCWD": "CWD", "XPWD": "PWD",
    "XCUP": "CDUP", "XSITE": "SITE",
}


class FtpError(Exception):
    """FTP 会话内的可预期错误：直接回给客户端。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------- 会话（每个控制连接一个线程） ----------------

class FtpSession(threading.Thread):
    def __init__(self, server, conn, addr):
        super().__init__(daemon=True)
        self.server = server
        self.conn = conn
        self.addr = addr
        self.file = None
        self.user = None
        self.logged_in = False
        self.cwd = "/"
        self.type = "A"
        self.rest = 0
        self.data_listener = None        # PASV 监听 socket
        self.data_addr = None            # PORT 目标地址
        self.rename_from = None

    # ---------------- 收发 ----------------

    def _send(self, text: str):
        self.conn.sendall((text + "\r\n").encode("utf-8", "replace"))

    def _reply(self, code, msg):
        self._send(f"{code} {msg}")

    def _reply_multiline(self, code, lines):
        self._send(f"{code}-{lines[0]}")
        for ln in lines[1:-1]:
            self._send(f" {ln}")
        self._send(f"{code} {lines[-1]}")

    def _read_line(self):
        """读取一行控制命令（UTF-8 解码，容错）。"""
        line = self.file.readline(MAX_LINE)
        if not line:
            return None
        return line.decode("utf-8", "replace").rstrip("\r\n")

    # ---------------- 路径 ----------------

    def _resolve(self, raw):
        """把客户端路径解析为根目录内的绝对路径；越界抛 550。

        相对路径基于会话当前目录 self.cwd 解析（FTP 风格 / 开头）。
        """
        p = (raw or "").strip()
        if not p:
            p = self.cwd
        if p.startswith("/"):
            base = self.server.root
            parts = p.split("/")
        else:
            base = os.path.join(self.server.root, self.cwd.lstrip("/"))
            parts = p.split("/")
        root_real = os.path.realpath(self.server.root)
        joined = os.path.realpath(
            os.path.join(base, *[x for x in parts if x and x != "."]))
        if joined != root_real and not joined.startswith(root_real + os.sep):
            raise FtpError(550, "路径超出允许范围")
        return joined

    # ---------------- 数据连接 ----------------

    def _advertise_ip(self):
        """PASV 公告地址：优先服务器绑定地址，其次本机局域网 IP。"""
        host = self.server.host
        if host and host not in ("0.0.0.0", "::"):
            return host
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                if ip:
                    return ip
            finally:
                s.close()
        except OSError:
            pass
        return "127.0.0.1"

    def _open_pasv(self):
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("0.0.0.0", 0))
        ls.listen(4)
        ls.settimeout(DATA_TIMEOUT)
        self.data_listener = ls
        port = ls.getsockname()[1]
        ip = self._advertise_ip()
        return ip, port

    def _accept_data(self):
        if self.data_listener is None:
            raise FtpError(425, "请先使用 PASV 或 PORT")
        try:
            c, _ = self.data_listener.accept()
            return c
        except socket.timeout:
            raise FtpError(425, "数据连接超时")
        finally:
            self._close_data_listener()

    def _open_active(self):
        if not self.data_addr:
            raise FtpError(425, "请先使用 PASV 或 PORT")
        ip, port = self.data_addr
        c = socket.create_connection((ip, port), timeout=DATA_TIMEOUT)
        return c

    def _data_conn(self):
        if self.data_listener is not None:
            return self._accept_data()
        if self.data_addr is not None:
            return self._open_active()
        raise FtpError(425, "请先使用 PASV 或 PORT")

    def _close_data_listener(self):
        if self.data_listener is not None:
            try:
                self.data_listener.close()
            except OSError:
                pass
            self.data_listener = None

    # ---------------- 列表格式化 ----------------

    @staticmethod
    def _stat_line(path):
        st = os.stat(path)
        is_dir = os.path.isdir(path)
        mode = "drwxr-xr-x" if is_dir else "-rw-r--r--"
        mtime = datetime.fromtimestamp(st.st_mtime)
        now = datetime.now()
        if (now - mtime).days < 180:
            date = mtime.strftime("%b %d %H:%M")
        else:
            date = mtime.strftime("%b %d  %Y")
        size = 0 if is_dir else st.st_size
        name = os.path.basename(path.rstrip(os.sep)) or path
        if is_dir:
            name += "/"
        return (f"{mode} 1 ftp ftp {size:>12} {date} {name}").encode(
            "utf-8", "replace") + b"\r\n"

    def _list_lines(self, target):
        """生成 LIST 输出字节。target 为已解析的本地路径。"""
        out = []
        if os.path.isdir(target):
            try:
                names = sorted(os.listdir(target))
            except OSError as e:
                raise FtpError(550, f"列目录失败: {e}")
            for n in names:
                try:
                    out.append(self._stat_line(os.path.join(target, n)))
                except OSError:
                    continue
        else:
            if not os.path.isfile(target):
                raise FtpError(550, "文件或目录不存在")
            out.append(self._stat_line(target))
        return out

    # ---------------- 命令分发 ----------------

    def handle(self, line: str):
        if not line.strip():
            return
        cmd, _, arg = line.partition(" ")
        cmd = cmd.upper()
        arg = arg.strip()

        # 登录前只允许部分命令
        if not self.logged_in:
            if cmd in ("USER", "PASS", "QUIT", "NOOP", "SYST", "FEAT", "OPTS"):
                self._dispatch(cmd, arg)
                return
            raise FtpError(530, "请先登录")
        self._dispatch(cmd, arg)

    def _dispatch(self, cmd, arg):
        cmd = _CMD_ALIASES.get(cmd, cmd)
        handler = getattr(self, "do_" + cmd, None)
        if handler is None:
            raise FtpError(502, f"命令不支持: {cmd}")
        handler(arg)

    # ---------------- 命令实现 ----------------

    def do_USER(self, arg):
        self.user = arg
        if self.server.allow_anon and arg.lower() in ("anonymous", "ftp"):
            self._reply(331, "匿名账户，请用任意邮箱作为密码")
        else:
            self._reply(331, f"用户 {arg} 需要密码")

    def do_PASS(self, arg):
        if self.user is None:
            raise FtpError(503, "请先发送 USER")
        ok = False
        if self.server.allow_anon and self.user.lower() in ("anonymous", "ftp"):
            ok = True
        elif self.server.accounts.get(self.user) == arg:
            ok = True
        if not ok:
            self.server.log(f"[{self.addr[0]}] 登录失败: {self.user}")
            raise FtpError(530, "用户名或密码错误")
        self.logged_in = True
        self.server.log(f"[{self.addr[0]}] 登录成功: {self.user}")
        self._reply(230, "登录成功，欢迎使用 SystemToolBox FTP 服务器")

    def do_SYST(self, arg):
        self._reply(215, "UNIX Type: L8")

    def do_FEAT(self, arg):
        self._reply_multiline(211, ["Features:",
                                    " UTF8",
                                    " SIZE",
                                    " MDTM",
                                    " MLSD",
                                    " PASV",
                                    " EPSV",
                                    " EPRT",
                                    " REST STREAM",
                                    " STOU",
                                    " STAT",
                                    "End"])

    def do_OPTS(self, arg):
        if arg.upper().startswith("UTF8"):
            self._reply(200, "UTF8 模式开启")
        else:
            self._reply(501, "仅支持 UTF8")

    def do_NOOP(self, arg):
        self._reply(200, "OK")

    def do_QUIT(self, arg):
        self._reply(221, "再见")
        raise SystemExit

    def do_TYPE(self, arg):
        t = arg.upper()
        if t in ("A", "I", "A N", "A T"):
            self.type = "A" if t.startswith("A") else "I"
            self._reply(200, f"TYPE 设置为 {self.type}")
        else:
            self._reply(504, "仅支持 TYPE A/I")

    def do_MODE(self, arg):
        if arg.upper() == "S":
            self._reply(200, "MODE S 已设置")
        else:
            self._reply(504, "仅支持流模式")

    def do_STRU(self, arg):
        if arg.upper() == "F":
            self._reply(200, "STRU F 已设置")
        else:
            self._reply(504, "仅支持文件结构")

    def do_PWD(self, arg):
        self._reply(257, f'"{self.cwd}" 是当前目录')

    def do_CWD(self, arg):
        try:
            target = self._resolve(arg or ".")
        except FtpError:
            target = self.server.root     # 已在根目录时 .. 保持在根
        if not os.path.isdir(target):
            raise FtpError(550, "目录不存在")
        rel = self._to_ftp_path(target)
        self.cwd = rel
        self._reply(250, f"目录已切换: {rel}")

    def do_CDUP(self, arg):
        self.do_CWD("..")

    def _to_ftp_path(self, local):
        """本地绝对路径 -> FTP 风格路径（/ 开头）。"""
        rel = os.path.relpath(local, self.server.root)
        if rel == ".":
            return "/"
        return "/" + rel.replace(os.sep, "/")

    def do_PASV(self, arg):
        ip, port = self._open_pasv()
        self.data_addr = None
        p1, p2 = port // 256, port % 256
        # 经典格式：h1,h2,h3,h4,p1,p2（全部用逗号分隔，不用点）
        hi = ",".join(str(int(x)) for x in ip.split("."))
        self._reply(227, f"进入被动模式 ({hi},{p1},{p2})")

    def do_EPSV(self, arg):
        ip, port = self._open_pasv()
        self.data_addr = None
        self._reply(229, f"进入扩展被动模式 (|||{port}|)")

    def do_PORT(self, arg):
        try:
            nums = [int(x) for x in arg.split(",")]
            if len(nums) != 6 or any(not 0 <= x <= 255 for x in nums):
                raise ValueError
        except ValueError:
            raise FtpError(501, "PORT 参数格式错误")
        ip = ".".join(str(x) for x in nums[:4])
        self.data_addr = (ip, nums[4] * 256 + nums[5])
        self._close_data_listener()
        self._reply(200, "PORT 命令成功")

    def do_EPRT(self, arg):
        """扩展主动模式：EPRT |1|ip|port| 或 EPRT |2|ipv6|port|。"""
        m = re.match(r"\|([12])\|([^|]+)\|(\d+)\|", (arg or "").strip())
        try:
            if not m:
                raise ValueError
            fam, ip, port = m.group(1), m.group(2), int(m.group(3))
            if not 0 <= port <= 65535:
                raise ValueError
            if fam == "2":
                import ipaddress
                ipaddress.IPv6Address(ip)     # 校验合法性，非法则 ValueError
        except ValueError:
            raise FtpError(501, "EPRT 参数格式错误")
        self.data_addr = (ip, port)
        self._close_data_listener()
        self._reply(200, "EPRT 命令成功")

    def do_LIST(self, arg):
        target = self._resolve(arg) if arg else self._resolve(self.cwd)
        self._reply(150, "打开数据连接")
        data = self._data_conn()
        try:
            for line in self._list_lines(target):
                data.sendall(line)
        finally:
            try:
                data.close()
            except OSError:
                pass
        self._reply(226, "传输完成")

    def do_MLSD(self, arg):
        """RFC 3659 机器可读目录列表（FileZilla / ftplib 等现代客户端优先使用）。

        行格式：facts(以;结尾) SP 名称 —— facts 在前、以分号收尾、再跟空格与名称，
        与 RFC3659 及 ftplib 的 mlsd 解析器兼容。
        """
        target = self._resolve(arg) if arg else self._resolve(self.cwd)
        if not os.path.isdir(target):
            raise FtpError(550, "目录不存在")
        self._reply(150, "打开数据连接")
        data = self._data_conn()
        try:
            for n in sorted(os.listdir(target)):
                fp = os.path.join(target, n)
                try:
                    st = os.stat(fp)
                    is_dir = os.path.isdir(fp)
                    mtime = datetime.fromtimestamp(st.st_mtime).strftime(
                        "%Y%m%d%H%M%S")
                    facts = (f"type={'dir' if is_dir else 'file'};"
                             f"modify={mtime};"
                             f"size={0 if is_dir else st.st_size};")
                    data.sendall(f"{facts} {n}\r\n".encode("utf-8",
                                                           "replace"))
                except OSError:
                    continue
        finally:
            try:
                data.close()
            except OSError:
                pass
        self._reply(226, "传输完成")

    def do_NLST(self, arg):
        target = self._resolve(arg) if arg else self._resolve(self.cwd)
        if not os.path.isdir(target):
            raise FtpError(550, "目录不存在")
        self._reply(150, "打开数据连接")
        data = self._data_conn()
        try:
            for n in sorted(os.listdir(target)):
                data.sendall(n.encode("utf-8", "replace") + b"\r\n")
        finally:
            try:
                data.close()
            except OSError:
                pass
        self._reply(226, "传输完成")

    def _send_file(self, local):
        size = os.path.getsize(local)
        self._reply(150, f"开始发送文件 ({size} 字节)")
        data = self._data_conn()
        try:
            with open(local, "rb") as f:
                if self.rest:
                    f.seek(self.rest)
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    data.sendall(chunk)
            self.rest = 0
        finally:
            try:
                data.close()
            except OSError:
                pass
        self._reply(226, "传输完成")

    def _recv_file(self, local, append=False):
        self._reply(150, "开始接收文件")
        data = self._data_conn()
        try:
            if append:
                mode = "ab"
                seek = 0
            elif self.rest:
                # 断点续传：不能用 "w+b"（会截断为 0，seek 后产生空洞/NUL）。
                # 目标存在时用 r+b 原位续写；不存在则新建并从偏移写。
                mode = "r+b" if os.path.exists(local) else "w+b"
                seek = self.rest
            else:
                mode = "wb"
                seek = 0
            with open(local, mode) as f:
                if seek:
                    f.seek(seek)
                while True:
                    chunk = data.recv(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            self.rest = 0
        finally:
            try:
                data.close()
            except OSError:
                pass
        self._reply(226, "接收完成")

    def do_STAT(self, arg):
        """STAT：无参数返回服务器状态；带参数返回文件/目录信息。"""
        if arg:
            local = self._resolve(arg)
            if not os.path.isdir(local):
                if not os.path.isfile(local):
                    raise FtpError(550, "文件或目录不存在")
                lines = [self._stat_line(local).decode("utf-8",
                                                       "replace").rstrip("\r\n")]
            else:
                names = sorted(os.listdir(local))
                lines = [self._stat_line(os.path.join(local, n))
                         .decode("utf-8", "replace").rstrip("\r\n")
                         for n in names]
            self._reply_multiline(213, lines)
        else:
            mode = ("PASV" if self.data_listener is not None
                    else "PORT" if self.data_addr else "无")
            self._reply_multiline(211, [
                f"服务器状态: 用户 {self.user or '-'} 当前目录 {self.cwd} "
                f"类型 {self.type} 数据连接 {mode}",
                "SystemToolBox FTP 服务器（标准库实现）",
                "结束"])

    def do_RETR(self, arg):
        local = self._resolve(arg)
        if not os.path.isfile(local):
            raise FtpError(550, "文件不存在")
        self._send_file(local)

    def do_STOR(self, arg):
        local = self._resolve(arg)
        self._recv_file(local, append=False)

    def do_APPE(self, arg):
        local = self._resolve(arg)
        self._recv_file(local, append=True)

    def do_STOU(self, arg):
        """STOU：以唯一文件名存储（同名则追加序号）。"""
        local = self._resolve(arg) if arg else self._resolve(self.cwd)
        if os.path.isdir(local):
            local = os.path.join(local, "stou")
        base, i = local, 1
        while os.path.exists(local):
            local = f"{base}.{i}"
            i += 1
        self._recv_file(local)

    def do_REST(self, arg):
        try:
            self.rest = max(0, int(arg))
        except ValueError:
            raise FtpError(501, "REST 参数错误")
        self._reply(350, f"断点位置设置为 {self.rest}")

    def do_SIZE(self, arg):
        local = self._resolve(arg)
        if not os.path.isfile(local):
            raise FtpError(550, "文件不存在")
        self._reply(213, str(os.path.getsize(local)))

    def do_MDTM(self, arg):
        local = self._resolve(arg)
        if not os.path.isfile(local):
            raise FtpError(550, "文件不存在")
        mtime = datetime.fromtimestamp(os.path.getmtime(local))
        self._reply(213, mtime.strftime("%Y%m%d%H%M%S"))

    def do_DELE(self, arg):
        local = self._resolve(arg)
        if not os.path.isfile(local):
            raise FtpError(550, "文件不存在")
        try:
            os.remove(local)
        except OSError as e:
            raise FtpError(550, f"删除失败: {e}")
        self._reply(250, "删除成功")

    def do_MKD(self, arg):
        local = self._resolve(arg)
        try:
            os.makedirs(local, exist_ok=True)
        except OSError as e:
            raise FtpError(550, f"建目录失败: {e}")
        self._reply(257, f'"{self._to_ftp_path(local)}" 已创建')

    def do_RMD(self, arg):
        local = self._resolve(arg)
        if not os.path.isdir(local):
            raise FtpError(550, "目录不存在")
        try:
            os.rmdir(local)          # 只删空目录，防误删
        except OSError as e:
            raise FtpError(550, f"删除目录失败(需为空): {e}")
        self._reply(250, "目录已删除")

    def do_RNFR(self, arg):
        local = self._resolve(arg)
        if not os.path.exists(local):
            raise FtpError(550, "源路径不存在")
        self.rename_from = local
        self._reply(350, "请发送 RNTO 指定新路径")

    def do_RNTO(self, arg):
        if self.rename_from is None:
            raise FtpError(503, "请先发送 RNFR")
        local = self._resolve(arg)
        try:
            os.rename(self.rename_from, local)
        except OSError as e:
            raise FtpError(550, f"重命名失败: {e}")
        self.rename_from = None
        self._reply(250, "重命名成功")

    def do_AUTH(self, arg):
        self._reply(502, "本服务器不支持 TLS 加密")

    def do_PBSZ(self, arg):
        self._reply(502, "本服务器不支持 TLS 加密")

    def do_PROT(self, arg):
        self._reply(502, "本服务器不支持 TLS 加密")

    def do_ALLO(self, arg):
        self._reply(202, "无需分配空间")

    def do_ABOR(self, arg):
        self._close_data_listener()
        self._reply(226, "已中止")

    def do_HELP(self, arg):
        self._reply_multiline(214, [
            "支持的命令:",
            " USER PASS SYST FEAT OPTS PWD CWD CDUP TYPE MODE STRU",
            " PASV EPSV PORT EPRT LIST NLST MLSD STAT RETR STOR APPE STOU",
            " REST SIZE MDTM DELE MKD RMD RNFR RNTO NOOP QUIT",
            "结束"])

    # ---------------- 会话主循环 ----------------

    def run(self):
        self.conn.settimeout(DATA_TIMEOUT * 4)
        self.file = self.conn.makefile("rb")
        self._reply(220, "SystemToolBox FTP 服务器就绪")
        self.server.log(f"[{self.addr[0]}] 新连接")
        try:
            while True:
                try:
                    line = self._read_line()
                except socket.timeout:
                    self._reply(421, "连接空闲超时")
                    break
                except OSError:
                    break
                if line is None:
                    break
                if not line:
                    continue
                try:
                    self.handle(line)
                    self.server.log(f"[{self.addr[0]}] {line}")
                except SystemExit:
                    break
                except FtpError as e:
                    try:
                        self._reply(e.code, str(e))
                    except OSError:
                        break
                except Exception as e:  # noqa: BLE001
                    self.server.log(f"[{self.addr[0]}] 会话异常: {e!r}")
                    try:
                        self._reply(500, f"内部错误: {e}")
                    except OSError:
                        break
        finally:
            self._close_data_listener()
            try:
                self.conn.close()
            except OSError:
                pass
            self.server.drop_client(self.addr)
            self.server.log(f"[{self.addr[0]}] 连接关闭")


# ---------------- 服务器 ----------------

class FtpServer:
    def __init__(self, host, port, root, accounts, allow_anon, log_sink):
        self.host = host
        self.port = port
        self.root = str(Path(root).resolve())
        self.accounts = accounts          # {user: password}
        self.allow_anon = bool(allow_anon)
        self.log_sink = log_sink          # deque 引用
        self._srv = None
        self._thread = None
        self._stop = threading.Event()
        self._clients = {}                # addr -> session
        self._lock = threading.Lock()
        self.started_at = None
        self.last_error = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def log(self, text):
        if self.log_sink is not None:
            self.log_sink.append(f"{time.strftime('%H:%M:%S')} {text}")

    def drop_client(self, addr):
        with self._lock:
            self._clients.pop(addr, None)

    def client_addrs(self):
        with self._lock:
            return sorted(self._clients.keys(), key=lambda a: str(a))

    def start(self):
        if self.running:
            raise FtpError(0, "服务器已在运行")
        if not os.path.isdir(self.root):
            raise FtpError(0, f"根目录不存在: {self.root}")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, int(self.port)))
        except OSError as e:
            srv.close()
            raise FtpError(0, f"监听 {self.host}:{self.port} 失败: {e}")
        srv.listen(8)
        srv.settimeout(0.5)
        self._srv = srv
        self._stop.clear()
        self.started_at = time.time()
        self.last_error = None
        self.port = srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.log(f"服务器启动: {self.host}:{self.port} 根目录 {self.root}")
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._clients[addr] = None
            sess = FtpSession(self, conn, addr)
            with self._lock:
                self._clients[addr] = sess
            sess.start()

    def stop(self):
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
        with self._lock:
            sessions = list(self._clients.values())
        for s in sessions:
            if s is not None:
                try:
                    s.conn.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.log("服务器已停止")
        return True


# ---------------- 插件入口 ----------------

_server = None
_logs = deque(maxlen=MAX_LOG)
_state_lock = threading.RLock()
_cfg_file = None


def _load_saved_config(api):
    try:
        p = Path(api.data_dir) / "config.json"
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_config(api, cfg):
    try:
        p = Path(api.data_dir) / "config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError:
        pass


def _status_dict(api):
    with _state_lock:
        cfg = _load_saved_config(api)
        if _server is not None and _server.running:
            s = _server
            return {
                "running": True,
                "host": s.host,
                "port": s.port,
                "root": s.root,
                "allowAnon": s.allow_anon,
                "accounts": list(s.accounts.keys()),
                "startedAt": s.started_at,
                "clients": s.client_addrs(),
                "logs": list(_logs)[-80:],
                "lastConfig": cfg,
                "defaultRoot": str(DATA_DIR),
            }
        return {
            "running": False,
            "logs": list(_logs)[-80:],
            "lastConfig": cfg,
            "lastError": _server.last_error if _server else None,
            "defaultRoot": str(DATA_DIR),
        }


def register(api):
    global _cfg_file
    _cfg_file = api.data_dir / "config.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    @api.handler("status", permission="readonly",
                 description="FTP服务器运行状态与日志")
    def status(args, ctx):
        return _status_dict(api)

    @api.handler("start", permission="admin",
                 description="启动 FTP 服务器（监听端口，需审批）")
    def start(args, ctx):
        global _server
        saved = _load_saved_config(api)
        host = str(args.get("host") or saved.get("host") or "127.0.0.1")
        try:
            port = int(args.get("port", saved.get("port", 21)))
        except (TypeError, ValueError):
            port = 21
        root = str(args.get("root") or saved.get("root") or "")
        allow_anon = bool(args.get("allowAnon", saved.get("allowAnon", False)))
        username = str(args.get("username") or saved.get("username") or "")
        password = str(args.get("password") or saved.get("password") or "")

        accounts = {}
        if username:
            accounts[username] = password
        if not accounts and not allow_anon:
            raise ApiError("bad_args",
                           "请至少配置一个账户，或开启匿名访问")
        if not root:
            root = str(DATA_DIR)              # 默认：插件文件夹 Data 目录
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        _save_config(api, {"host": host, "port": port, "root": root,
                           "allowAnon": allow_anon, "username": username,
                           "password": password})

        with _state_lock:
            if _server is not None:
                try:
                    _server.stop()
                except Exception:  # noqa: BLE001
                    pass
            try:
                _server = FtpServer(host, port, root, accounts,
                                    allow_anon, _logs).start()
            except FtpError as e:
                raise ApiError("start_failed", str(e))
            except Exception as e:  # noqa: BLE001
                raise ApiError("start_failed", f"启动失败: {e}")
        # 发布运行状态到共享总线（任何插件/主页可读）
        try:
            api.bus.publish(api.plugin_id, "ftp-server.status",
                            {"running": True, "port": _server.port,
                             "root": _server.root})
        except Exception:  # noqa: BLE001
            pass
        return _status_dict(api)

    @api.handler("stop", permission="admin",
                 description="停止 FTP 服务器")
    def stop(args, ctx):
        global _server
        with _state_lock:
            if _server is not None:
                _server.stop()
                _server = None
        try:
            api.bus.publish(api.plugin_id, "ftp-server.status",
                            {"running": False})
        except Exception:  # noqa: BLE001
            pass
        return _status_dict(api)

    api.log.info("ftp-server 插件已注册: status/start/stop")
