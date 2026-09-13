/* 主进程 HTTP 帮助函数（Electron 33 自带全局 fetch）。
 * 所有请求自动附带访问令牌（X-STB-Token），后端据此拒绝浏览器直接访问。 */
"use strict";

async function fetchJson(url, opts = {}) {
  const { timeout = 5000, method = "GET", body, headers = {}, token = "" } = opts;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const hdrs = {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { "X-STB-Token": token } : {}),
      ...headers,
    };
    const resp = await fetch(url, {
      method,
      signal: ctrl.signal,
      headers: hdrs,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) return null;
    return await resp.json();
  } finally {
    clearTimeout(timer);
  }
}

module.exports = { fetchJson };