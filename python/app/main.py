"""后端入口：python -m app.main [--port N] [--host H] [--token T] [--debug]

启动流程：
  1. 初始化存储（state/grants）
  2. 树状扫描插件 -> 布局（侧栏/顶栏/冲突）-> 加载插件
  3. 注册 API 路由
  4. 启动 HTTP 服务，打印 STB_READY host/port/token 供宿主识别

安全：访问令牌优先取 --token / 环境变量 STB_TOKEN，否则随机生成；
所有 HTTP 请求须携带该令牌（X-STB-Token 头、?token= 参数，或首次通过
校验后种下的 stb_token Cookie——页面子资源请求自动携带），
浏览器直接访问端口将无法浏览页面或调用接口。
"""
import argparse
import os
import secrets
import sys

from . import config
from .api.router import Router
from .context import AppContext
from .log import info, set_debug
from .server import start_server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="SystemToolBox-backend")
    parser.add_argument("--host", default=config.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    parser.add_argument("--token", default=os.environ.get("STB_TOKEN") or "",
                       help="访问令牌（缺省随机生成）")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    set_debug(args.debug)
    info(f"SystemToolBox 后端启动 (v{config.APP_VERSION})")

    token = args.token or secrets.token_urlsafe(24)

    ctx = AppContext()
    ctx.router = Router(ctx)
    ctx.rescan()

    server = start_server(ctx, host=args.host, port=args.port, token=token)
    host, port = server.server_address

    # 宿主（Electron）通过该行识别就绪、端口与访问令牌
    print(f"STB_READY host={host} port={port} token={token}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        info("后端退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())