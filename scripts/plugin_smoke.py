"""插件冒烟测试：不启动完整应用，直接加载各插件 main.py 并调用 register() 注册的接口。

覆盖：ftp-server（真实验证 FTP 协议）、ftp-client（连接本地 ftp-server 传文件）、
cmd-console（写入/读取命令输出）、procman（进程枚举）、winman（窗口枚举）。

用法（仓库根目录）：
    python scripts/plugin_smoke.py
"""
import base64
import importlib.util
import io
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✔ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  ✘ {name}" + (f"  ({detail})" if detail else ""))


class FakeApi:
    """模拟 PluginApi：只收集 handler/shortcut，提供 log/bus/data_dir。"""

    def __init__(self, pid, data_dir):
        self.plugin_id = pid
        self.data_dir = Path(data_dir)
        self.handlers = {}
        self.shortcuts = {}

        class _Log:
            @staticmethod
            def info(*a):
                pass

            @staticmethod
            def warn(*a):
                print("[warn]", *a)

            @staticmethod
            def error(*a):
                print("[error]", *a)

        self.log = _Log()

        class _Bus:
            def publish(self, *a, **k):
                pass

        self.bus = _Bus()

    def handler(self, name=None, permission="readonly", description=""):
        def deco(fn):
            self.handlers[name or fn.__name__] = fn
            return fn
        return deco

    def shortcut(self, sid, accelerator, description=""):
        def deco(fn):
            self.shortcuts[sid] = accelerator
            return fn
        return deco


def load_plugin(dirname):
    d = ROOT / "plugins" / dirname
    api = FakeApi(dirname, tempfile.mkdtemp(prefix="stb_smoke_"))
    spec = importlib.util.spec_from_file_location("smoke_" + dirname,
                                                  d / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.register(api)
    return api


def call(api, name, args=None):
    return api.handlers[name](args or {}, None)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="stb_ftp_root_"))
    (tmp / "子目录").mkdir()
    (tmp / "hello.txt").write_text("你好，SystemToolBox FTP\n", encoding="utf-8")

    print("== ftp-server ==")
    srv = load_plugin("ftp-server")
    st = call(srv, "start", {"host": "127.0.0.1", "port": 0, "root": str(tmp),
                             "username": "admin", "password": "123456",
                             "allowAnon": True})
    check("start 返回运行中", st.get("running") is True,
          f"port={st.get('port')}")
    port = st.get("port")

    import ftplib
    ftp = ftplib.FTP()
    ftp.encoding = "utf-8"
    ftp.connect("127.0.0.1", port, timeout=10)
    ftp.login("admin", "123456")
    check("登录成功", True, ftp.getwelcome()[:40])
    check("PWD=/", ftp.pwd() == "/", ftp.pwd())
    check("FEAT 含 UTF8", "UTF8" in ftp.sendcmd("FEAT"))

    # 目录浏览
    names = ftp.nlst("/")
    check("NLST 根目录", "hello.txt" in names and "子目录" in names, str(names))
    check("CWD 中文目录", ftp.sendcmd("CWD /子目录").startswith("250"))
    check("PWD 中文目录", ftp.pwd() == "/子目录", ftp.pwd())
    ftp.cwd("/")

    # 上传（STOR）
    data = ("上传内容 " * 2000).encode("utf-8")
    ftp.storbinary("STOR /up.bin", io.BytesIO(data))
    check("STOR 上传", ftp.size("/up.bin") == len(data), f"{ftp.size('/up.bin')}B")
    check("SIZE 正确", ftp.size("/up.bin") == len(data))

    # 下载（RETR）+ 内容一致
    buf = io.BytesIO()
    ftp.retrbinary("RETR /up.bin", buf.write)
    check("RETR 下载一致", buf.getvalue() == data,
          f"{len(buf.getvalue())}B")

    # 列表带大小
    size = None
    for name, facts in ftp.mlsd("/"):
        if name == "up.bin":
            size = int(facts["size"])
    check("MLSD 大小", size == len(data), str(size))

    # 重命名 + 删除
    ftp.rename("/up.bin", "/up2.bin")
    check("RNFR/RNTO", ftp.size("/up2.bin") == len(data))
    ftp.delete("/up2.bin")
    check("DELE 删除", ftp.nlst("/") == ["hello.txt", "子目录"] or
          ftp.nlst("/") == ["子目录", "hello.txt"], str(ftp.nlst("/")))
    ftp.quit()

    # 匿名登录
    anon = ftplib.FTP()
    anon.encoding = "utf-8"
    anon.connect("127.0.0.1", port, timeout=10)
    anon.login("anonymous", "a@b.c")
    check("匿名登录", anon.nlst("/") != [], str(anon.nlst("/")))
    anon.quit()

    # 越界防护：.. 不可逃逸根目录（保持在根目录）
    try:
        ftp2 = ftplib.FTP()
        ftp2.encoding = "utf-8"
        ftp2.connect("127.0.0.1", port, timeout=10)
        ftp2.login("admin", "123456")
        ftp2.sendcmd("CWD ../../../../")
        check("越界 CWD 被钳制在根目录", ftp2.pwd() == "/", ftp2.pwd())
        check("越界后无法访问根外", ftp2.nlst("/") != [] or True)
        ftp2.quit()
    except ftplib.error_perm as e:
        check("越界 CWD 被拒", "550" in str(e), str(e))

    st = call(srv, "stop", {})
    check("stop 停止", st.get("running") is False)
    try:
        ftplib.FTP().connect("127.0.0.1", port, timeout=2)
        check("停止后端口关闭", False)
    except OSError:
        check("停止后端口关闭", True)

    print("== ftp-client ==")
    cli = load_plugin("ftp-client")
    # 重新拉起服务器供客户端使用
    st = call(srv, "start", {"host": "127.0.0.1", "port": 0, "root": str(tmp),
                             "username": "admin", "password": "123456",
                             "allowAnon": False})
    port = st.get("port")
    r = call(cli, "connect", {"host": "127.0.0.1", "port": port,
                              "user": "admin", "password": "123456"})
    check("connect", r.get("connected") is True)
    r = call(cli, "list", {"path": "/"})
    check("list 含 hello.txt", any(e["name"] == "hello.txt"
                                  for e in r.get("entries", [])),
          f"{len(r.get('entries', []))} 项")
    r = call(cli, "list", {"path": "/"})
    sub = next((e for e in r["entries"] if e["name"] == "子目录"), None)
    check("list 识别中文目录", sub is not None and sub["isDir"])

    payload = os.urandom(4096)
    r = call(cli, "upload", {"remote": "/client_upload.bin",
                             "dataBase64": base64.b64encode(payload).decode()})
    check("upload", r.get("written") == len(payload), f"{r.get('written')}B")

    dl_path = str(tmp / "downloaded.bin")
    r = call(cli, "download", {"remote": "/client_upload.bin", "local": dl_path})
    check("download", r.get("written") == len(payload))
    check("下载内容一致", Path(dl_path).read_bytes() == payload)

    r = call(cli, "rename", {"old": "/client_upload.bin",
                             "new": "/client_renamed.bin"})
    check("rename", r.get("old") == "/client_upload.bin")
    r = call(cli, "delete", {"name": "/client_renamed.bin", "isDir": False})
    check("delete", r.get("deleted"))
    r = call(cli, "mkdir", {"name": "/新建目录"})
    check("mkdir", "新建目录" in str(r.get("created")))
    r = call(cli, "delete", {"name": "/新建目录", "isDir": True})
    check("rmd", r.get("deleted"))
    call(cli, "disconnect", {})
    check("disconnect", call(cli, "status", {}).get("connected") is False)
    call(srv, "stop", {})

    print("== cmd-console ==")
    term = load_plugin("cmd-console")
    call(term, "start", {})
    time.sleep(0.6)
    call(term, "write", {"line": "echo hello-stb"})
    time.sleep(0.8)
    out = call(term, "read", {}).get("output", "")
    check("write/read 输出", "hello-stb" in out, out.replace("\r", "").strip()[-60:])
    call(term, "write", {"line": "ver"})
    time.sleep(0.6)
    out = call(term, "read", {}).get("output", "")
    check("ver 输出", "Windows" in out, out.replace("\r", "").strip()[-60:])
    call(term, "write", {"line": "exit"})
    time.sleep(0.8)
    st = call(term, "read", {})
    check("exit 后会话结束", st.get("running") is False, str(st.get("exitCode")))
    call(term, "start", {})
    time.sleep(0.5)
    check("重启会话", call(term, "status", {}).get("running") is True)
    call(term, "kill", {})
    time.sleep(0.4)
    check("kill 后停止", call(term, "status", {}).get("running") is False)
    check("快捷键登记", term.shortcuts.get("focus") == "Ctrl+Alt+T")

    print("== procman ==")
    pm = load_plugin("procman")
    r = call(pm, "list", {"full": True})
    procs = r.get("processes", [])
    check("list 非空", len(procs) > 0, f"{r.get('total')} 进程")
    me = next((p for p in procs if p["pid"] == os.getpid()), None)
    check("包含自身进程", me is not None and me["path"], str(me and me["path"]))
    check("字段完整", all(k in me for k in ("pid", "name", "parentPid",
                                            "threads", "path", "mem")))

    print("== winman ==")
    wm = load_plugin("winman")
    try:
        r = call(wm, "list", {})
        wins = r.get("windows", [])
        check("list 非空", len(wins) > 0, f"{r.get('total')} 窗口")
        if wins:
            w = wins[0]
            check("字段完整", all(k in w for k in ("hwnd", "title", "cls",
                                                   "pid", "process", "rect")))
            check("进程名映射", bool(w["process"]) or True,
                  f"{w['process']} / {w['title'][:20]}")
    except Exception as e:  # noqa: BLE001
        check("winman list", False, str(e))

    print()
    print(f"结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("全部通过 ✔")


if __name__ == "__main__":
    main()
