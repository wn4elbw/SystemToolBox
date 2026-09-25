/* SystemToolBox — Electron 主进程入口。
 *
 * 职责：
 *  - 启动时自动提权到管理员（runas 重启；STB_NO_ELEVATE=1 可跳过，用于开发）
 *  - 创建无边框窗口（frame:false，自绘标题栏）
 *  - 启动 / 管理 Python 后端子进程（HTTP 127.0.0.1:随机端口）
 *  - 提供窗口控制 IPC（最小化 / 最大化 / 关闭）
 */
"use strict";

const { app, BrowserWindow, ipcMain, nativeImage } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, execFile, execFileSync } = require("child_process");
const { startBackend } = require("./backend");
const shortcuts = require("./shortcuts");
const overlay = require("./overlay");

const ROOT = path.resolve(__dirname, "..", "..", "..");
const ICONS_DIR = path.join(__dirname, "..", "..", "icons");   // electron/icons
const DEFAULT_ICON = path.join(ICONS_DIR, "default.ico");
const CUSTOM_ICON = path.join(ICONS_DIR, "custom.ico");

function iconFilePath() {
  return fs.existsSync(CUSTOM_ICON) ? CUSTOM_ICON : DEFAULT_ICON;
}

let mainWindow = null;
let backend = null;
let apiBase = null;

/* ---------------- 自动提权到管理员 ---------------- */

function isElevated() {
  try {
    execFileSync("net", ["session"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function relaunchElevated() {
  /* 通过 UAC 以管理员身份重启自身；调用方需随后退出当前实例 */
  return new Promise((resolve) => {
    const args = process.argv.slice(1).map((a) => '"' + a + '"');
    const ps = "Start-Process -FilePath '" + process.execPath +
               "' -ArgumentList " + args.join(",") + " -Verb RunAs";
    const child = spawn("powershell", ["-NoProfile", "-Command", ps],
                        { windowsHide: true, stdio: "ignore" });
    child.on("error", () => resolve(false));
    child.on("spawn", () => resolve(true));
  });
}

async function ensureElevated() {
  if (isElevated()) {
    return true;
  }
  if (process.env.STB_NO_ELEVATE === "1") {
    console.log("[elevate] 跳过提权（STB_NO_ELEVATE=1），以普通权限运行");
    return false;
  }
  console.log("[elevate] 检测到非管理员，尝试 UAC 提权重启…");
  const ok = await relaunchElevated();
  if (ok) {
    app.quit();
    return true;      // 新实例接管
  }
  console.log("[elevate] UAC 提权被拒绝或失败，以普通权限继续（高级选项不可用）");
  return false;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 940,
    minHeight: 620,
    frame: false,                       // 不要原生标题栏（用自绘标题栏）
    thickFrame: true,                   // 保留系统标准窗口边框（可拖拽调整大小/阴影/动画）
    roundedCorners: true,               // Windows 11 保留圆角边框
    icon: iconFilePath(),               // 窗口图标（任务栏/Alt+Tab；custom 优先）
    backgroundColor: "#14161b",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "..", "preload", "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());

  // 最大化状态变化 -> 通知渲染层切换按钮图标
  const emitMax = () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("win:maximized-changed",
                                  mainWindow.isMaximized());
    }
  };
  mainWindow.on("maximize", emitMax);
  mainWindow.on("unmaximize", emitMax);
  mainWindow.on("closed", () => { mainWindow = null; });

  // 加载渲染层，附带后端地址与访问令牌（令牌校验在后端：无令牌的浏览器访问一律 403）
  mainWindow.loadFile(path.join(__dirname, "..", "renderer", "index.html"),
                      { query: { api: apiBase, token: backend.token || "" } });

  // 开发辅助：STB_SCREENSHOT=1 时把窗口截图存到 build/ui.png 后退出；
  // STB_SHOT_JS=... 可在截图前于页面执行一段 JS（如打开弹窗/切换分栏）
  if (process.env.STB_SCREENSHOT === "1") {
    mainWindow.webContents.once("did-finish-load", () => {
      setTimeout(async () => {
        try {
          const fs = require("fs");
          const js = process.env.STB_SHOT_JS;
          if (js) {
            await mainWindow.webContents.executeJavaScript(js);
            await new Promise((r) => setTimeout(r, 1500));
          }
          const img = await mainWindow.webContents.capturePage();
          fs.writeFileSync(path.join(ROOT, "build", "ui.png"), img.toPNG());
          console.log("[shot] saved build/ui.png");
        } catch (e) {
          console.log("[shot] failed:", e.message);
        }
        app.quit();
      }, 6000);
    });
  }
}

/* ---------------- 窗口控制 IPC ---------------- */

function registerIpc() {
  ipcMain.handle("win:minimize", () => {
    if (mainWindow) mainWindow.minimize();
  });
  ipcMain.handle("win:maximize-toggle", () => {
    if (!mainWindow) return false;
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
    return mainWindow.isMaximized();
  });
  ipcMain.handle("win:close", () => {
    if (mainWindow) mainWindow.close();
  });
  ipcMain.handle("win:is-maximized", () =>
    !!(mainWindow && mainWindow.isMaximized()));

  // 窗口外观：标题 / 图标
  ipcMain.handle("win:set-title", (_e, title) => {
    if (!mainWindow) return false;
    mainWindow.setTitle(String(title == null ? "" : title));
    return mainWindow.getTitle();
  });
  ipcMain.handle("win:set-icon", (_e, dataUrl) => {
    if (!mainWindow || typeof dataUrl !== "string") return false;
    try {
      const img = nativeImage.createFromDataURL(dataUrl);
      if (img.isEmpty()) return false;
      mainWindow.setIcon(img);
      return true;
    } catch {
      return false;
    }
  });

  // ---- 窗口图标文件管理 ----
  // 默认图标 default.ico 常驻目录且永不更改；用户选择的自定义图标
  // 缩放为 128x128 后写为 custom.ico，仅替换 custom 不影响默认图标。

  function iconToDataUrl(p) {
    const img = nativeImage.createFromPath(p);
    return img.isEmpty() ? "" : img.toDataURL();
  }

  ipcMain.handle("win:get-icon", () => {
    // 返回当前生效图标（自定义优先，否则默认）
    return { path: iconFilePath(), dataUrl: iconToDataUrl(iconFilePath()) };
  });

  ipcMain.handle("win:save-icon", (_e, dataUrl) => {
    if (typeof dataUrl !== "string") return { ok: false };
    try {
      const img = nativeImage.createFromDataURL(dataUrl);
      if (img.isEmpty()) return { ok: false };
      // 缩放为 128x128（等比例裁剪到正方形后缩放）
      const size = img.getSize();
      const side = Math.min(size.width, size.height);
      const cropped = nativeImage.createFromBuffer(
        img.toPNG(), { width: size.width, height: size.height });
      const sq = cropped.crop({
        x: Math.round((size.width - side) / 2),
        y: Math.round((size.height - side) / 2),
        width: side, height: side,
      });
      const resized = sq.resize({ width: 128, height: 128 });
      // 写 ICO：ICONDIR(6) + ICONDIRENTRY(16) + BITMAPINFOHEADER(40) + XOR + AND
      const bmp = resized.toBitmap();      // BGRA 自底向上
      const w = 128, h = 128;
      const andLen = Math.ceil(w / 8) * h;
      const header = Buffer.alloc(6);
      header.writeUInt16LE(0, 0);          // reserved
      header.writeUInt16LE(1, 2);          // type: icon
      header.writeUInt16LE(1, 4);          // count
      const entry = Buffer.alloc(16);
      entry.writeUInt8(w === 256 ? 0 : w, 0);
      entry.writeUInt8(h === 256 ? 0 : h, 1);
      entry.writeUInt8(0, 2);              // colors
      entry.writeUInt8(0, 3);              // reserved
      entry.writeUInt16LE(1, 4);           // planes
      entry.writeUInt16LE(32, 6);          // bit count
      const xorLen = w * h * 4;
      entry.writeUInt32LE(40 + xorLen + andLen, 8);
      entry.writeUInt32LE(22, 12);         // offset
      const bih = Buffer.alloc(40);
      bih.writeUInt32LE(40, 0);            // biSize
      bih.writeInt32LE(w, 4);              // biWidth
      bih.writeInt32LE(h * 2, 8);          // biHeight (XOR+AND)
      bih.writeUInt16LE(1, 12);            // biPlanes
      bih.writeUInt16LE(32, 14);           // biBitCount
      bih.writeUInt32LE(0, 16);            // compression
      bih.writeUInt32LE(xorLen, 20);       // sizeImage
      const andMask = Buffer.alloc(andLen);
      const ico = Buffer.concat([header, entry, bih, bmp, andMask]);
      fs.mkdirSync(ICONS_DIR, { recursive: true });
      fs.writeFileSync(CUSTOM_ICON, ico);
      if (mainWindow) {
        mainWindow.setIcon(nativeImage.createFromPath(CUSTOM_ICON));
      }
      return { ok: true, path: CUSTOM_ICON,
               dataUrl: iconToDataUrl(CUSTOM_ICON) };
    } catch (e) {
      console.log("[icon] 保存图标失败:", e.message);
      return { ok: false };
    }
  });

  ipcMain.handle("win:reset-icon", () => {
    try {
      if (fs.existsSync(CUSTOM_ICON)) fs.unlinkSync(CUSTOM_ICON);
      if (mainWindow) {
        mainWindow.setIcon(nativeImage.createFromPath(DEFAULT_ICON));
      }
      return { ok: true, path: DEFAULT_ICON,
               dataUrl: iconToDataUrl(DEFAULT_ICON) };
    } catch (e) {
      console.log("[icon] 重置图标失败:", e.message);
      return { ok: false };
    }
  });
}

/* ---------------- 窗口图标启动应用 ---------------- */

async function applyWindowIcon() {
  if (!mainWindow) return;
  try {
    const p = iconFilePath();
    const img = nativeImage.createFromPath(p);
    if (!img.isEmpty()) mainWindow.setIcon(img);
  } catch (e) {
    console.log("[icon] 应用图标失败:", e.message);
  }
}

/* ---------------- 生命周期 ---------------- */

async function main() {
  registerIpc();

  // 自动提权到管理员（UAC runas 重启）；STB_NO_ELEVATE=1 或用户拒绝时以普通权限继续
  const elevated = await ensureElevated();
  if (elevated && !isElevated()) {
    return;   // 已请求重启，本实例退出
  }

  // 启动 Python 后端
  backend = await startBackend({
    pythonDir: path.join(ROOT, "python"),
    debug: process.env.STB_DEBUG === "1",
  });
  apiBase = `http://${backend.host}:${backend.port}`;

  createWindow();
  applyWindowIcon();   // 应用窗口图标（custom.ico 优先，否则 default.ico；默认永不被改写）

  // 全局快捷键：轮询后端生效列表并注册（权限门控在后端）
  shortcuts.start(apiBase, backend.token || "");

  // 系统信息叠加层：轮询后端状态并管理透明置顶窗口
  overlay.start(apiBase, backend.token || "");
}

app.whenReady().then(main).catch((err) => {
  console.error("[main] 启动失败:", err);
  app.exit(1);
});

app.on("window-all-closed", () => {
  app.quit();
});

app.on("will-quit", () => {
  shortcuts.stop();
  overlay.stop();
  if (backend) backend.kill();
});