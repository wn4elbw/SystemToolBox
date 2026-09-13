/* preload.js — 渲染层与主进程的桥（contextBridge）。
 * 暴露 window.stb：
 *   - apiBase: 后端 HTTP 地址
 *   - token:   后端访问令牌（所有请求附带，浏览器直接访问端口一律 403）
 *   - platform: 平台标识
 *   - 窗口控制: minimize / maximizeToggle / close / isMaximized
 *   - onMaximized(cb): 最大化状态变化订阅
 */
"use strict";

const { contextBridge, ipcRenderer, clipboard } = require("electron");

const params = new URLSearchParams(window.location.search || "");

contextBridge.exposeInMainWorld("stb", {
  apiBase: params.get("api") || "http://127.0.0.1:0",
  token: params.get("token") || "",
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  /** 复制文本到剪贴板（日志“全选复制”等） */
  copyText: (text) => {
    try {
      clipboard.writeText(String(text == null ? "" : text));
      return true;
    } catch {
      return false;
    }
  },
  minimize: () => ipcRenderer.invoke("win:minimize"),
  maximizeToggle: () => ipcRenderer.invoke("win:maximize-toggle"),
  close: () => ipcRenderer.invoke("win:close"),
  isMaximized: () => ipcRenderer.invoke("win:is-maximized"),
  setTitle: (title) => ipcRenderer.invoke("win:set-title", title),
  setIcon: (dataUrl) => ipcRenderer.invoke("win:set-icon", dataUrl),
  getIcon: () => ipcRenderer.invoke("win:get-icon"),
  saveIcon: (dataUrl) => ipcRenderer.invoke("win:save-icon", dataUrl),
  resetIcon: () => ipcRenderer.invoke("win:reset-icon"),
  onMaximized: (cb) => {
    const listener = (_e, maximized) => cb(maximized);
    ipcRenderer.on("win:maximized-changed", listener);
    return () => ipcRenderer.removeListener("win:maximized-changed", listener);
  },
});