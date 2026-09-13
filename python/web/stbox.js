/* stbox.js — 插件页面共享 API 辅助（宿主注入，同源访问后端）。
 * 用法：<script src="/ui/stbox.js"></script>
 *   const r = await stbox.rpc("myplugin", "myapi", {k:1});
 *   if (!r.ok && r.error.code === "approval_required") { ... 或用 rpcWithApproval }
 * 安全：宿主以 ?token=<访问令牌> 加载页面，本文件自动把令牌附到所有请求头
 * （X-STB-Token）；未携带令牌的浏览器直接访问端口将一律 403。
 */
(function (global) {
  "use strict";

  const API = global.location.origin;

  // 访问令牌：宿主加载页面时通过 ?token= 传入
  const TOKEN = (new URLSearchParams(global.location.search || ""))
    .get("token") || "";
  const AUTH = TOKEN ? { "X-STB-Token": TOKEN } : {};

  function sleep(ms) {
    return new Promise((res) => setTimeout(res, ms));
  }

  /* 基础 RPC：返回 {ok:true,data} 或 {ok:false,error} */
  async function rpc(plugin, api, args) {
    const payload = { plugin: String(plugin), api: String(api), args: args || {} };
    let res;
    try {
      res = await fetch(API + "/api/rpc", {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, AUTH),
        body: JSON.stringify(payload),
      });
    } catch (e) {
      return { ok: false, error: { code: "network", message: String(e) } };
    }
    let body;
    try {
      body = await res.json();
    } catch (e) {
      return { ok: false, error: { code: "bad_response", message: String(e) } };
    }
    return body;
  }

  /* 带审批自动重试的 RPC：
   * 遇到 approval_required 时等待用户裁决；裁决为 once/always 后自动重试，
   * deny 则抛出包含 error 的异常。 */
  async function rpcWithApproval(plugin, api, args, opts) {
    opts = opts || {};
    const maxWaitMs = opts.maxWaitMs || 120000;
    const started = Date.now();
    for (;;) {
      const r = await rpc(plugin, api, args);
      if (r.ok) return r;
      if (r.error && r.error.code !== "approval_required") {
        throw new Error("[" + r.error.code + "] " + r.error.message);
      }
      const reqId = r.error && r.error.request && r.error.request.id;
      if (!reqId) throw new Error("[approval_required] " + r.error.message);
      if (opts.onPending) opts.onPending(r.error.request);
      // 轮询审批状态，直到裁决
      for (;;) {
        await sleep(500);
        if (Date.now() - started > maxWaitMs) {
          throw new Error("[approval_timeout] 等待审批超时");
        }
        let st;
        try {
          const res = await fetch(API + "/api/approvals/" + reqId,
                                  { headers: AUTH });
          st = await res.json();
        } catch (e) {
          continue;
        }
        const req = st && st.data;
        if (!req) continue;
        if (req.status === "resolved") {
          if (req.result === "deny") {
            throw new Error("[permission_denied] 权限被拒绝: " + api);
          }
          break; // once / always 已授权，重试原调用
        }
      }
    }
  }

  /* 字节数格式化 */
  function fmtBytes(n) {
    if (n == null) return "-";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n);
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? Math.round(v) : v.toFixed(1)) + " " + units[i];
  }

  /* 开机时长格式化 */
  function fmtUptime(sec) {
    sec = Number(sec) || 0;
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return d + "天 " + h + "时 " + m + "分";
  }

  /* 简易 DOM 工具 */
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), attrs[k]);
      else node.setAttribute(k, attrs[k]);
    }
    (children || []).forEach((c) => node.appendChild(
      typeof c === "string" ? document.createTextNode(c) : c));
    return node;
  }

  global.stbox = {
    api: API,
    token: TOKEN,
    rpc: rpc,
    rpcWithApproval: rpcWithApproval,
    fmtBytes: fmtBytes,
    fmtUptime: fmtUptime,
    el: el,
    sleep: sleep,
  };
})(window);