/* 系统信息叠加层窗口管理器（主进程）。
 *
 * 轮询后端插件 overlay/state（权限门控在后端），按配置创建/更新/销毁一个
 * 透明置顶无边框窗口：
 *  - enabled=false  -> 销毁窗口（若存在）
 *  - enabled=true   -> 创建窗口（加载 /plugin/overlay/web/overlay.html，页面
 *    自带 stbox 令牌自行轮询折线数据与半透明背景）
 *  - 应用 尺寸/位置（setBounds）、鼠标穿透（setIgnoreMouseEvents）。
 *
 * 视觉透明度由页面读取 state.opacity 用 rgba 背景渲染（Windows 上
 * transparent 窗口 + setOpacity 组合不可靠），主进程只管窗口几何与穿透。
 * 窗口被用户手动关闭（Alt+F4）时回写 enabled=false，保持前后端一致。
 */
"use strict";

const { BrowserWindow, screen } = require("electron");
const { fetchJson } = require("./fetch");

let timer = null;
let apiBase = "";
let token = "";
let win = null;
let lastSig = "";        // "x,y,w,h,pass" 签名，避免重复 setBounds

function stateUrl() {
  return `${apiBase}/api/rpc`;
}

async function readState() {
  const r = await fetchJson(stateUrl(), {
    method: "POST",
    timeout: 3000,
    token,
    body: { plugin: "overlay", api: "state", args: {} },
  });
  return r && r.ok ? r.data : null;
}

async function postSet(patch) {
  try {
    await fetchJson(stateUrl(), {
      method: "POST",
      timeout: 3000,
      token,
      body: { plugin: "overlay", api: "set", args: patch },
    });
  } catch (e) { /* 失败静默，下一轮轮询自愈 */ }
}

function defaultPos(w, h) {
  const wa = screen.getPrimaryDisplay().workArea;
  return {
    x: wa.x + wa.width - w - 24,
    y: wa.y + 24,
    w, h,
  };
}

function applyGeometry(s) {
  if (!win || win.isDestroyed()) return;
  const x = s.x != null ? Math.round(s.x) : null;
  const y = s.y != null ? Math.round(s.y) : null;
  const w = Math.max(180, Math.round(s.w || 320));
  const h = Math.max(90, Math.round(s.h || 180));
  const sig = `${x},${y},${w},${h},${s.passthrough}`;
  if (sig === lastSig) return;
  lastSig = sig;

  const b = win.getBounds();
  const want = { width: w, height: h };
  if (x != null) want.x = x;
  if (y != null) want.y = y;
  if (want.x !== b.x || want.y !== b.y ||
      want.width !== b.width || want.height !== b.height) {
    try { win.setBounds(want); } catch (e) { /* ignore */ }
  }
  try {
    win.setIgnoreMouseEvents(Boolean(s.passthrough), { forward: true });
  } catch (e) { /* ignore */ }
}

function destroy() {
  if (win && !win.isDestroyed()) win.destroy();
  win = null;
  lastSig = "";
}

async function sync() {
  if (!apiBase) return;
  let s = null;
  try { s = await readState(); } catch (e) { return; }   // 后端暂不可用
  if (!s) return;

  if (!s.enabled) {
    if (win && !win.isDestroyed()) destroy();
    return;
  }

  if (!win || win.isDestroyed()) {
    const w0 = Math.max(180, Math.round(s.w || 320));
    const h0 = Math.max(90, Math.round(s.h || 180));
    const p = defaultPos(w0, h0);
    console.log(`[overlay] creating window ${w0}x${h0}@(${s.x != null ? s.x : p.x},${s.y != null ? s.y : p.y})`);
    win = new BrowserWindow({
      width: w0,
      height: h0,
      x: s.x != null ? Math.round(s.x) : p.x,
      y: s.y != null ? Math.round(s.y) : p.y,
      frame: false,
      transparent: true,
      backgroundColor: "#00000000",
      resizable: true,                // 可拖边调整大小
      hasShadow: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: false,
      },
    });
    win.setAlwaysOnTop(true, "screen-saver");
    win.loadURL(`${apiBase}/plugin/overlay/overlay.html` +
                `?token=${encodeURIComponent(token)}`);
    win.on("closed", () => {
      win = null;
      lastSig = "";
      // 用户手动关闭窗口 -> 同步后端 enabled=false，避免下一轮立即重建
      postSet({ enabled: false });
    });
    // 用户拖动/缩放窗口后把新几何回写后端，供下次启动恢复
    win.on("moved", reportBounds);
    win.on("resized", reportBounds);
    if (win.setAlwaysOnTop) {
      win.showInactive();   // 不抢焦点地显示叠加层
    }
    lastSig = "";
  }

  applyGeometry(s);
}

function reportBounds() {
  if (!win || win.isDestroyed()) return;
  const b = win.getBounds();
  postSet({ x: b.x, y: b.y, w: b.width, h: b.height });
}

function start(base, tk) {
  stop();
  apiBase = base || "";
  token = tk || "";
  sync();
  timer = setInterval(sync, 1000);
}

function stop() {
  if (timer) { clearInterval(timer); timer = null; }
  destroy();
}

module.exports = { start, stop };