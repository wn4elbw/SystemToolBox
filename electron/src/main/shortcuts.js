/* 全局快捷键管理器（主进程）。
 *
 * 从后端 /api/shortcuts 拉取“当前生效”的快捷键（权限门控已在后端完成：
 * full 默认放行，approval/readonly 需在插件管理/快捷键管理中授权），
 * 用 Electron globalShortcut 注册；触发时 POST /api/shortcuts/trigger
 * 让后端调用插件回调并推事件给渲染层。
 */
"use strict";

const { globalShortcut } = require("electron");
const { fetchJson } = require("./fetch");

let timer = null;
let token = "";                     // 后端访问令牌
let registered = new Map();   // accelerator -> {pluginId, id}

async function sync(apiBase) {
  if (!apiBase) return;
  let list;
  try {
    const r = await fetchJson(`${apiBase}/api/shortcuts`,
                              { timeout: 5000, token });
    list = (r && r.data && r.data.shortcuts) || [];
  } catch (e) {
    return;    // 后端暂不可用：静默等待下一轮
  }

  const wanted = new Map();   // accelerator -> {pluginId, id}
  for (const s of list) {
    if (s && s.accelerator && !wanted.has(s.accelerator)) {
      wanted.set(s.accelerator, { pluginId: s.pluginId, id: s.id });
    }
  }

  // 注销不再生效的
  for (const [acc, info] of registered) {
    if (!wanted.has(acc)) {
      try { globalShortcut.unregister(acc); } catch { /* ignore */ }
      registered.delete(acc);
    }
  }
  // 注册新增的
  for (const [acc, info] of wanted) {
    if (registered.has(acc)) continue;
    let ok = false;
    try { ok = globalShortcut.register(acc, () => fire(apiBase, info)); }
    catch (e) { ok = false; }
    if (ok) registered.set(acc, info);
  }
}

async function fire(apiBase, info) {
  try {
    await fetchJson(`${apiBase}/api/shortcuts/trigger`, {
      method: "POST",
      body: { pluginId: info.pluginId, id: info.id },
      timeout: 5000,
      token,
    });
  } catch (e) { /* 失败静默（插件侧会收到错误事件或日志） */ }
}

function start(apiBase, tk) {
  stop();
  token = tk || "";
  sync(apiBase);
  timer = setInterval(() => sync(apiBase), 3000);
}

function stop() {
  if (timer) { clearInterval(timer); timer = null; }
  try { globalShortcut.unregisterAll(); } catch { /* ignore */ }
  registered.clear();
}

module.exports = { start, stop };