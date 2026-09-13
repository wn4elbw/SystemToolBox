"""FTP客户端插件 — 基于标准库 ftplib。

功能：
- 连接/断开、PWD/CWD/CDUP、目录列表（优先 MLSD，退化 NLST+SIZE/MDTM，再退化 LIST 解析）；
- 上传（本地文件或 base64 数据）、下载、新建目录、删除、重命名；
- 会话保存在插件内存中（每次连接一个会话）。

权限说明：
- 浏览类接口 readonly（连接后即可用）；
- 上传/下载/删除/重命名等写操作 admin —— 插件为 approval 权限时首次调用走审批流。
"""
import base64
import io
import os
import re
import threading
import time

from app.api.router import ApiError

_session = None            # {ftp, host, port, user, connectedAt, cwd}
_session_lock = threading.RLock()


def _fmt_mtime(raw):
    """YYYYMMDDHHMMSS -> 'MM-DD HH:MM'；其他原样返回。"""
    if raw and len(raw) == 14 and raw.isdigit():
        try:
            import datetime
            return datetime.datetime.strptime(raw, "%Y%m%d%H%M%S").strftime(
                "%Y-%m-%d %H:%M")
        except ValueError:
            return raw
    return raw


def _list_entries(ftp, path):
    """目录列表：MLSD -> NLST 增强 -> LIST 解析。"""
    # 1) MLSD（标准、带类型与大小）
    try:
        out = []
        for name, facts in ftp.mlsd(path):
            typ = facts.get("type", "file")
            size = facts.get("size")
            try:
                size = int(size) if size not in (None, "") else None
            except (TypeError, ValueError):
                size = None
            out.append({
                "name": name,
                "isDir": typ == "dir",
                "size": size,
                "mtime": _fmt_mtime(facts.get("modify")),
            })
        return sorted(out, key=lambda e: (not e["isDir"], e["name"].lower()))
    except (ftplib.error_perm, ftplib.error_temp, OSError):
        pass
    # 2) NLST + 逐个 SIZE/MDTM 增强
    try:
        names = ftp.nlst(path)
    except (ftplib.error_perm, ftplib.error_temp, OSError):
        return _parse_dir_listing(ftp, path)
    out = []
    for n in names:
        is_dir = n.endswith("/")
        clean = n.rstrip("/")
        size = mtime = None
        if not is_dir:
            try:
                size = int(ftp.size(clean))
            except Exception:  # noqa: BLE001
                pass
        try:
            mtime = _fmt_mtime(
                ftp.sendcmd("MDTM " + clean).split(" ", 1)[1].strip())
        except Exception:  # noqa: BLE001
            pass
        out.append({"name": clean, "isDir": is_dir, "size": size,
                    "mtime": mtime})
    return sorted(out, key=lambda e: (not e["isDir"], e["name"].lower()))


def _parse_dir_listing(ftp, path):
    """LIST 原始输出解析：兼容 Unix 与 Windows 服务器格式。"""
    lines = []
    try:
        ftp.dir(path, lines.append)
    except Exception:  # noqa: BLE001
        raise ApiError("list_failed", f"无法列出目录: {path}")
    win_re = re.compile(
        r"^(\d\d-\d\d-\d\d)\s+(\d\d:\d\d(?:AM|PM))\s+(<DIR>|\d+)\s+(.+)$")
    out = []
    for ln in lines:
        if ln.startswith(("-", "d", "l", "b", "c")):
            parts = ln.split(None, 8)
            if len(parts) >= 9:
                is_dir = ln.startswith("d")
                size = 0 if is_dir else _to_int(parts[4])
                out.append({"name": parts[8], "isDir": is_dir, "size": size,
                            "mtime": None})
                continue
        m = win_re.match(ln)
        if m:
            is_dir = m.group(3) == "<DIR>"
            size = 0 if is_dir else _to_int(m.group(3))
            out.append({"name": m.group(4).strip(), "isDir": is_dir,
                        "size": size, "mtime": m.group(1) + " " + m.group(2)})
            continue
        name = ln.split()[-1] if ln.strip() else ln
        out.append({"name": name, "isDir": False, "size": None, "mtime": None})
    return sorted(out, key=lambda e: (not e["isDir"], e["name"].lower()))


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _cur():
    with _session_lock:
        if _session is None or _session.get("ftp") is None:
            raise ApiError("not_connected", "尚未连接 FTP 服务器")
        return _session


def _sync_cwd():
    with _session_lock:
        try:
            _session["cwd"] = _session["ftp"].pwd()
        except Exception:  # noqa: BLE001
            pass
        return _session["cwd"]


def _session_view():
    with _session_lock:
        if _session is None or _session.get("ftp") is None:
            return {"connected": False}
        return {
            "connected": True,
            "host": _session["host"],
            "port": _session["port"],
            "user": _session["user"],
            "connectedAt": _session["connectedAt"],
            "cwd": _session["cwd"],
            "welcome": _session.get("welcome", ""),
        }


def register(api):
    global ftplib
    import ftplib

    @api.handler("status", permission="readonly",
                 description="当前 FTP 连接状态")
    def status(args, ctx):
        return _session_view()

    @api.handler("connect", permission="readonly",
                 description="连接 FTP 服务器")
    def connect(args, ctx):
        global _session
        host = str(args.get("host") or "").strip()
        if not host:
            raise ApiError("bad_args", "请填写服务器地址")
        try:
            port = int(args.get("port", 21))
        except (TypeError, ValueError):
            port = 21
        user = str(args.get("user") or "anonymous")
        password = str(args.get("password") or "")
        timeout = min(max(float(args.get("timeout") or 15), 2), 120)
        disconnect(None, None)
        ftp = ftplib.FTP()
        ftp.encoding = "utf-8"
        try:
            ftp.connect(host, port, timeout=timeout)
            ftp.login(user, password)
        except ftplib.error_perm as e:
            raise ApiError("login_failed", f"登录失败: {e}")
        except (OSError, ftplib.error_temp) as e:
            raise ApiError("connect_failed", f"连接失败: {e}")
        with _session_lock:
            _session = {
                "ftp": ftp, "host": host, "port": port, "user": user,
                "password": password,
                "connectedAt": time.time(),
                "cwd": "/",
                "welcome": ftp.getwelcome(),
            }
        try:
            ftp.cwd("/")
        except Exception:  # noqa: BLE001
            pass
        api.bus.publish(api.plugin_id, "ftp-client.session",
                        {"connected": True, "host": host})
        return _session_view()

    @api.handler("disconnect", permission="readonly",
                 description="断开 FTP 连接")
    def disconnect(args, ctx):
        global _session
        with _session_lock:
            ftp = _session.get("ftp") if _session else None
            _session = None
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                try:
                    ftp.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            api.bus.publish(api.plugin_id, "ftp-client.session",
                            {"connected": False})
        except Exception:  # noqa: BLE001
            pass
        return {"connected": False}

    @api.handler("pwd", permission="readonly", description="当前远程目录")
    def pwd(args, ctx):
        return {"cwd": _cur()["ftp"].pwd()}

    @api.handler("cwd", permission="readonly", description="切换远程目录")
    def cwd(args, ctx):
        path = str(args.get("path") or "")
        if not path:
            raise ApiError("bad_args", "请指定目录")
        try:
            _cur()["ftp"].cwd(path)
        except ftplib.error_perm as e:
            raise ApiError("cwd_failed", f"切换目录失败: {e}")
        return {"cwd": _sync_cwd()}

    @api.handler("cdUp", permission="readonly", description="返回上级目录")
    def cdUp(args, ctx):
        try:
            _cur()["ftp"].cwd("..")
        except ftplib.error_perm as e:
            raise ApiError("cwd_failed", f"返回上级失败: {e}")
        return {"cwd": _sync_cwd()}

    @api.handler("list", permission="readonly",
                 description="列出远程目录（自动识别列表格式）")
    def list_dir(args, ctx):
        ftp = _cur()["ftp"]
        path = str(args.get("path") or "") or None
        try:
            entries = _list_entries(ftp, path or "/")
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError("list_failed", f"列目录失败: {e}")
        return {"cwd": _sync_cwd(), "entries": entries}

    @api.handler("download", permission="admin",
                 description="下载远程文件到本地（需审批）")
    def download(args, ctx):
        remote = str(args.get("remote") or "")
        local = str(args.get("local") or "")
        if not remote:
            raise ApiError("bad_args", "请指定远程文件")
        if not local:
            raise ApiError("bad_args", "请指定本地保存路径")
        ftp = _cur()["ftp"]
        try:
            os.makedirs(os.path.dirname(os.path.abspath(local)),
                        exist_ok=True)
            written = [0]
            with open(local, "wb") as f:
                def cb(chunk):
                    f.write(chunk)
                    written[0] += len(chunk)
                ftp.retrbinary("RETR " + remote, cb)
        except ftplib.error_perm as e:
            raise ApiError("download_failed", f"下载失败: {e}")
        except OSError as e:
            raise ApiError("download_failed", f"本地写入失败: {e}")
        return {"remote": remote, "local": local, "written": written[0]}

    @api.handler("upload", permission="admin",
                 description="上传文件到远程（本地路径或 base64 数据，需审批）")
    def upload(args, ctx):
        remote = str(args.get("remote") or "")
        if not remote:
            raise ApiError("bad_args", "请指定远程文件路径")
        data = None
        if args.get("dataBase64"):
            try:
                data = base64.b64decode(str(args["dataBase64"]))
            except (ValueError, TypeError) as e:
                raise ApiError("bad_args", f"base64 解码失败: {e}")
        elif args.get("local"):
            local = str(args["local"])
            try:
                with open(local, "rb") as f:
                    data = f.read()
            except OSError as e:
                raise ApiError("upload_failed", f"读取本地文件失败: {e}")
        else:
            raise ApiError("bad_args", "请提供本地路径或 base64 数据")
        ftp = _cur()["ftp"]
        try:
            ftp.storbinary("STOR " + remote, io.BytesIO(data))
        except ftplib.error_perm as e:
            raise ApiError("upload_failed", f"上传失败: {e}")
        return {"remote": remote, "written": len(data)}

    @api.handler("mkdir", permission="admin",
                 description="远程新建目录（需审批）")
    def mkdir(args, ctx):
        name = str(args.get("name") or "")
        if not name:
            raise ApiError("bad_args", "请填写目录名")
        try:
            created = _cur()["ftp"].mkd(name)
        except ftplib.error_perm as e:
            raise ApiError("mkdir_failed", f"建目录失败: {e}")
        return {"created": created or name}

    @api.handler("delete", permission="admin",
                 description="删除远程文件或空目录（需审批）")
    def delete(args, ctx):
        name = str(args.get("name") or "")
        is_dir = bool(args.get("isDir"))
        if not name:
            raise ApiError("bad_args", "请指定要删除的路径")
        ftp = _cur()["ftp"]
        try:
            if is_dir:
                ftp.rmd(name)
            else:
                ftp.delete(name)
        except ftplib.error_perm as e:
            raise ApiError("delete_failed", f"删除失败: {e}")
        return {"deleted": name}

    @api.handler("rename", permission="admin",
                 description="远程重命名/移动（需审批）")
    def rename(args, ctx):
        old = str(args.get("old") or "")
        new = str(args.get("new") or "")
        if not old or not new:
            raise ApiError("bad_args", "请填写旧路径与新路径")
        try:
            _cur()["ftp"].rename(old, new)
        except ftplib.error_perm as e:
            raise ApiError("rename_failed", f"重命名失败: {e}")
        return {"old": old, "new": new}

    api.log.info("ftp-client 插件已注册: status/connect/disconnect/"
                 "pwd/cwd/cdUp/list/download/upload/mkdir/delete/rename")
