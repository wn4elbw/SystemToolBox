/* 叠加层窗口 preload：暴露 stbOv.onCfg 供页面订阅主进程下推的配置变化。
 * 透明度的主进程下推（webContents.send('overlay:cfg')）让滑块改动
 * 即时反映到叠加层窗口，无需等页面轮询。
 */
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("stbOv", {
  onCfg: (cb) => {
    ipcRenderer.on("overlay:cfg", (_e, cfg) => {
      try { cb(cfg || {}); } catch (e) { /* ignore */ }
    });
  },
});
