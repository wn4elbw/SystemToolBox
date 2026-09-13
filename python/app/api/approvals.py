"""审批流：插件第一次需要权限时的弹窗申请与裁决。"""
import threading
import uuid
from datetime import datetime

from ..log import info
from .router import ApiError, api_ok


class ApprovalRequest:
    def __init__(self, plugin_id, plugin_name, api, description):
        self.id = uuid.uuid4().hex[:12]
        self.plugin_id = plugin_id
        self.plugin_name = plugin_name
        self.api = api
        self.description = description or ""
        self.created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status = "pending"     # pending | resolved
        self.result = None          # once | always | deny

    def to_dict(self):
        return {
            "id": self.id,
            "pluginId": self.plugin_id,
            "pluginName": self.plugin_name,
            "api": self.api,
            "description": self.description,
            "created": self.created,
            "status": self.status,
            "result": self.result,
        }


class ApprovalRegistry:
    """内存中的审批请求表（进程内有效，重启即清）。"""

    def __init__(self, store):
        self._store = store
        self._reqs = {}
        self._lock = threading.Lock()

    def create(self, manifest, handler):
        req = ApprovalRequest(manifest.id, manifest.name, handler.name,
                              handler.description)
        with self._lock:
            self._reqs[req.id] = req
        return req

    def get(self, req_id):
        with self._lock:
            return self._reqs.get(req_id)

    def pending(self):
        with self._lock:
            return [r.to_dict() for r in self._reqs.values()
                    if r.status == "pending"]

    def resolve(self, req_id, decision):
        """decision: once(会话内放行) | always(持久放行) | deny(持久拒绝)
        | deny_once(仅本次拒绝，不记录，下次再弹)"""
        if decision not in ("once", "always", "deny", "deny_once"):
            raise ApiError("bad_args", f"非法裁决: {decision}")
        with self._lock:
            req = self._reqs.get(req_id)
            if req is None:
                raise ApiError("not_found", "审批请求不存在", http=404)
            if req.status != "pending":
                raise ApiError("conflict", "审批请求已处理")
            req.status = "resolved"
            req.result = decision

        if decision == "always":
            self._store.set_api_grant(req.plugin_id, req.api, "allowed",
                                      persistent=True)
        elif decision == "deny":
            self._store.set_api_grant(req.plugin_id, req.api, "denied",
                                      persistent=True)
        elif decision == "once":  # 会话级授权（内存，重启失效）
            self._store.set_api_grant(req.plugin_id, req.api, "allowed",
                                      persistent=False)
        else:  # deny_once：仅本次拒绝，不落任何授权，下次调用仍会弹窗
            pass

        info(f"审批裁决: {req.plugin_id}.{req.api} -> {decision} "
             f"(req {req.id})")
        return req.to_dict()


def register(ctx, router):
    approvals = ctx.approvals

    @router.route("GET", "/api/approval/pending")
    def _pending(c, m, body):
        return api_ok({"pending": approvals.pending()})

    @router.route("GET", r"/api/approvals/([A-Za-z0-9]+)")
    def _status(c, m, body):
        req = approvals.get(m.group(1))
        if req is None:
            raise ApiError("not_found", "审批请求不存在", http=404)
        return api_ok(req.to_dict())

    @router.route("POST", r"/api/approvals/([A-Za-z0-9]+)/resolve")
    def _resolve(c, m, body):
        decision = (body or {}).get("decision")
        return api_ok(approvals.resolve(m.group(1), decision))