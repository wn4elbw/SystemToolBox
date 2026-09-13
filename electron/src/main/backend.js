/* 后端进程管理：spawn Python 后端，等待就绪（STB_READY）。 */
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..", "..");

/** 查找 python：优先 runtime/ 便携版 -> 环境变量 -> PATH。
 *  PATH 上的 "python"/"py" 可能是 WindowsApps 别名（GUI 进程里 spawn 会
 *  ENOENT），因此用 sys.executable 解析出真实 exe 路径。 */
function findPython() {
  const candidates = [
    process.env.STB_PYTHON,
    path.join(ROOT, "runtime", "python", "python.exe"),
    path.join(ROOT, "runtime", "python", "pythonw.exe"),
    "python",
    "py",
  ].filter(Boolean);

  for (const c of candidates) {
    if (c === "python" || c === "py") {
      try {
        const r = require("child_process").spawnSync(
          c, ["-c", "import sys;print(sys.executable)"],
          { timeout: 8000, windowsHide: true });
        if (r.status === 0 && r.stdout) {
          const real = r.stdout.toString("utf8").trim();
          if (real && fs.existsSync(real)) return real;
        }
      } catch { /* continue */ }
      continue;
    }
    if (fs.existsSync(c)) return c;
  }
  return null;
}

/**
 * 启动后端。
 * @returns {Promise<{host:string, port:number, proc:import('child_process').ChildProcess, kill:()=>void}>}
 */
async function startBackend({ pythonDir, debug = false } = {}) {
  const py = findPython();
  if (!py) throw new Error("未找到 Python 解释器（可设置 STB_PYTHON 或放入 runtime/python/）");

  const args = ["-m", "app.main", "--port", "0"];
  if (debug) args.push("--debug");

  const proc = spawn(py, args, {
    cwd: pythonDir,
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });

  const backend = {
    host: "127.0.0.1",
    port: 0,
    token: "",
    proc,
    debug,
    kill() {
      if (proc && !proc.killed) {
        try { proc.kill(); } catch { /* ignore */ }
      }
    },
  };

  return await new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error("后端启动超时（未收到 STB_READY）"));
      }
    }, 20000);

    const onData = (chunk) => {
      const text = chunk.toString("utf8");
      if (debug) process.stdout.write("[backend] " + text);
      const m = text.match(/STB_READY host=(\S+) port=(\d+)(?:\s+token=(\S+))?/);
      if (m && !settled) {
        settled = true;
        clearTimeout(timer);
        backend.host = m[1];
        backend.port = parseInt(m[2], 10);
        backend.token = m[3] || "";
        proc.stdout.off("data", onData);
        proc.stderr.off("data", onErr);
        resolve(backend);
      }
    };
    const onErr = (chunk) => {
      const text = chunk.toString("utf8");
      if (debug) process.stderr.write("[backend:err] " + text);
    };

    proc.stdout.on("data", onData);
    proc.stderr.on("data", onErr);
    proc.on("error", (e) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(e);
      }
    });
    proc.on("exit", (code) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new Error(`后端进程提前退出 (code=${code})`));
      }
    });
  });
}

module.exports = { startBackend, findPython };