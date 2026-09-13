"""API 路由器：固定路由 + /api/rpc 分发，集中做权限裁决。"""
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import config
from ..log import error, info


class ApiError(Exception):
    """可预期的业务错误：code 为稳定错误码，http 为建议 HTTP 状态。"""

    def __init__(self, code: str, message: str, http: int = 200, extra=None):
        super().__init__(message)
        self.code = code
        self.http = http
        self.extra = extra


def api_ok(data):
    return {"ok": True, "data": data}


def api_err(code, message, http=200, extra=None):
    err = {"code": code, "message": message}
    if extra is not None:
        err.update(extra)
    return {"ok": False, "error": err}


@dataclass
class Handler:
    """一个可调用 API 的注册信息。permission: API_READONLY / API_ADMIN。"""
    name: str
    fn: Callable
    permission: str = config.API_READONLY
    description: str = ""


@dataclass
class ApiCallCtx:
    """调用上下文，透传给插件 handler(args, ctx)。"""
    bridge: object
    plugin_id: str
    api_name: str
    manifest: object = None

    def native(self):
        return self.bridge


def require_args(args, *keys):
    """校验参数存在，缺省抛 ApiError。"""
    missing = [k for k in keys if k not in args]
    if missing:
        raise ApiError("bad_args", f"缺少参数: {', '.join(missing)}")


def _summarize(args: dict, limit: int = 160) -> str:
    """参数摘要：紧凑 JSON 并截断，避免敏感信息与超长内容刷屏。"""
    try:
        s = json.dumps(args, ensure_ascii=False, separators=(",", ":"),
                       default=str)
    except (TypeError, ValueError):
        s = str(args)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s or "{}"


class Router:
    def __init__(self, ctx):
        self.ctx = ctx
        self._routes = []          # [(method, regex, fn)]
        self._register_defaults()

    # ---------------- 固定路由 ---------------- 

    def route(self, method: str, pattern: str):
        rx = re.compile(f"^{pattern}$")

        def deco(fn):
            self._routes.append((method.upper(), rx, fn))
            return fn
        return deco

    def _register_defaults(self):
        from . import approvals, host, manager, meta, system
        system.register(self.ctx)
        meta.register(self.ctx, self)
        approvals.register(self.ctx, self)
        manager.register(self.ctx, self)
        host.register(self.ctx, self)

        # RPC 分发主入口
        @self.route("POST", "/api/rpc")
        def _rpc(c, m, body):
            return self.rpc(c, body)

    def handle(self, method: str, path: str, body: dict):
        """返回 (http_status, payload_dict)。"""
        try:
            for m, rx, fn in self._routes:
                if m != method.upper():
                    continue
                mm = rx.fullmatch(path)
                if mm:
                    return 200, fn(self.ctx, mm, body)
            return 404, api_err("not_found", f"未知接口: {method} {path}")
        except ApiError as e:
            return e.http, api_err(e.code, str(e), http=e.http,
                                   extra=e.extra)
        except Exception as e:  # noqa: BLE001
            import traceback
            error(f"接口异常 {method} {path}: {e!r}\n{traceback.format_exc()}")
            return 500, api_err("internal_error", f"内部错误: {e}")

    # ---------------- RPC 分发与权限裁决 ---------------- 

    def rpc(self, ctx, body: dict):
        """POST /api/rpc 的核心分发逻辑。"""
        if not isinstance(body, dict):
            raise ApiError("bad_args", "请求体必须是 JSON 对象")
        plugin_id = body.get("plugin")
        api_name = body.get("api")
        args = body.get("args") or {}
        if not isinstance(plugin_id, str) or not isinstance(api_name, str):
            raise ApiError("bad_args", "缺少 plugin / api")
        if isinstance(args, dict) is False:
            args = {}

        # 内置系统 API：宿主自身调用，视为完全信任
        if plugin_id == config.BUILTIN_PLUGIN_ID:
            handler = (ctx.registry.get(config.BUILTIN_PLUGIN_ID) or {}).get(api_name)
            if handler is None:
                raise ApiError("api_not_found",
                               f"内置接口不存在: {api_name}", http=404)
            return self._run(handler, args, ctx, plugin_id, api_name, None)

        # 插件调用：完整权限链路
        m = ctx.manifests.get(plugin_id)
        if m is None:
            raise ApiError("plugin_not_found",
                           f"插件不存在: {plugin_id}", http=404)
        if m.status == "disabled":
            raise ApiError("plugin_disabled",
                           f"插件已停用: {plugin_id}")
        if m.status == "conflict":
            raise ApiError("plugin_conflict",
                           f"插件顶栏冲突未加载: {plugin_id}")
        if m.status == "error":
            raise ApiError("plugin_error",
                           f"插件加载失败: {m.load_error}")

        handler = (ctx.registry.get(plugin_id) or {}).get(api_name)
        if handler is None:
            # 回退：调用软件本体内置 API（如 file.*、share.*），
            # 权限仍按调用方插件级别裁决（而非"system 完全信任"）
            builtin = ctx.registry.get(config.BUILTIN_PLUGIN_ID) or {}
            handler = builtin.get(api_name)
            if handler is None:
                raise ApiError("api_not_found",
                               f"插件 {plugin_id} 无接口: {api_name}", http=404)
            self._enforce_permission(ctx, m, handler)
            return self._run(handler, args, ctx, plugin_id, api_name, m)

        self._enforce_permission(ctx, m, handler)
        return self._run(handler, args, ctx, plugin_id, api_name, m)

    def _enforce_permission(self, ctx, manifest, handler: Handler):
        """readonly / approval / full 三级裁决（规格 §4）。"""
        if handler.permission == config.API_READONLY:
            return  # 只读接口，任何级别都放行

        effective = manifest.effective_permission
        if effective == config.PERM_FULL:
            return  # 全权
        if effective == config.PERM_READONLY:
            raise ApiError(
                "permission_denied",
                f"插件为只读权限，无权调用 {handler.name}",
                http=403)

        # approval：查授权
        grant = ctx.store.get_api_grant(manifest.id, handler.name)
        if grant == "allowed":
            return
        if grant == "denied":
            raise ApiError(
                "permission_denied",
                f"接口 {handler.name} 已被拒绝，可在插件管理中开放",
                http=403)

        # 第一次需要权限：创建审批请求（弹窗申请）
        req = ctx.approvals.create(manifest, handler)
        info(f"审批申请: {manifest.id}.{handler.name} "
             f"-> request {req.id}")
        raise ApiError(
            "approval_required",
            f"插件「{manifest.name}」需要权限调用 {handler.name}",
            http=403,
            extra={"request": req.to_dict()})

    def _run(self, handler: Handler, args: dict, ctx,
             plugin_id: str, api_name: str, manifest):
        call_ctx = ApiCallCtx(ctx.bridge, plugin_id, api_name, manifest)
        t0 = time.monotonic()
        try:
            data = handler.fn(args, call_ctx)
            if manifest is not None:
                ms = (time.monotonic() - t0) * 1000
                info(f"插件调用[{plugin_id}] {api_name} 参数={_summarize(args)} "
                     f"-> 成功 ({ms:.0f}ms)")
            return api_ok(data)
        except ApiError as e:
            if manifest is not None:
                ms = (time.monotonic() - t0) * 1000
                info(f"插件调用[{plugin_id}] {api_name} 参数={_summarize(args)} "
                     f"-> 失败 {e.code} ({ms:.0f}ms)")
            raise
        except Exception:
            if manifest is not None:
                ms = (time.monotonic() - t0) * 1000
                info(f"插件调用[{plugin_id}] {api_name} 参数={_summarize(args)} "
                     f"-> 异常 ({ms:.0f}ms)")
            raise