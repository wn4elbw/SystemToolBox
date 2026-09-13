/* SystemToolBox — 渲染层逻辑
 *
 * 布局：自绘标题栏 / 左栏(主页+侧栏分组+底部设置/日志/退出/快捷键) / 顶栏 / 内容区
 * 职责：启动布局(冲突报错) / 插件导航 / 插件管理与详情(权限) / 快捷键管理 /
 *       设置 / 日志 / 审批弹窗 / 后端事件轮询(转发快捷键事件给插件页)
 */
"use strict";

(function () {
  const API = window.stb.apiBase.replace(/\/$/, "");
  const TOKEN = window.stb.token || "";   // 后端访问令牌
  const $ = (id) => document.getElementById(id);
  const iframe = $("frame");

  const PERM_LABEL = {
    readonly: "只读",
    approval: "审批",
    full: "全权",
  };

  let meta = null;          // /api/meta 快照
  let active = null;        // {kind:'plugin'|'home', pluginId?}
  let approvalQueue = [];   // 待处理审批 request id
  let currentRequest = null;
  let logsTimer = null;

  /* ---------------- 通用 ---------------- */

  async function api(method, path, body) {
    const opt = { method, headers: {} };
    // 所有请求附带访问令牌（后端校验：无令牌的浏览器访问一律 403）
    opt.headers["X-STB-Token"] = TOKEN;
    // GET/HEAD 不允许携带请求体（浏览器 fetch 限制），参数走查询串
    if (body !== undefined &&
        method.toUpperCase() !== "GET" && method.toUpperCase() !== "HEAD") {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(API + path, opt);
    } catch (e) {
      throw new Error("后端不可达: " + e.message + " (" + API + ")");
    }
    let data;
    try { data = await res.json(); } catch { data = {}; }
    if (!data.ok) {
      const err = data.error || {};
      const ex = new Error("[" + err.code + "] " + (err.message || res.status));
      ex.code = err.code;
      ex.request = err.request;
      throw ex;
    }
    return data.data;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function button(label, onClick, cls) {
    const b = document.createElement("button");
    b.className = "btn small" + (cls ? " " + cls : "");
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function metaCell(v, conflict) {
    const td = document.createElement("td");
    td.textContent = v;
    if (conflict) td.style.color = "var(--red)";
    return td;
  }

  /* ---------------- 标题栏 ---------------- */

  function setupTitlebar() {
    $("btn-min").addEventListener("click", () => window.stb.minimize());
    $("btn-max").addEventListener("click", async () => {
      await window.stb.maximizeToggle();
      syncMaxIcon(await window.stb.isMaximized());
    });
    $("btn-close").addEventListener("click", () => window.stb.close());
    window.stb.onMaximized(syncMaxIcon);
    window.stb.isMaximized().then(syncMaxIcon);
  }

  function syncMaxIcon(maximized) {
    $("btn-max").innerHTML = maximized ? "&#xE923;" : "&#xE922;";
  }

  /* ---------------- 启动 ---------------- */

  async function boot() {
    try {
      meta = await api("GET", "/api/meta");
    } catch (e) {
      $("sb-groups").innerHTML =
        '<div style="padding:10px;color:#ef5a5a">' + escapeHtml(e.message) + "</div>";
      return;
    }
    renderSidebar();
    renderTopbar(null);
    showStartupConflicts();
    loadTheme();          // 应用持久化的主题（不阻塞）
    loadWindowAppearance();   // 应用持久化的窗口标题/图标（含主进程推送）
    openHome();
    setInterval(pollApprovals, 1000);
    setInterval(pollEvents, 1500);
  }

  function showStartupConflicts() {
    const conflicts = (meta.conflicts || []);
    if (!conflicts.length) return;
    $("conflict-body").innerHTML =
      '<div class="err">以下插件因顶栏(topbar)已被其它插件占用而无法加载：</div>' +
      conflicts.map((c) =>
        '<div class="appr-item"><div class="appr-icon">⚠</div>' +
        '<div class="appr-meta"><b>' + escapeHtml(c.pluginName) +
        '</b>（' + escapeHtml(c.pluginId) + '）<br>' +
        '<span class="desc">顶栏 <span class="api">' + escapeHtml(c.topbar) +
        '</span> 已被「' + escapeHtml(c.existingPluginId) +
        '」占用 —— ' + escapeHtml(c.pluginId) + ' 将不加载</span></div></div>')
        .join("");
    $("modal-conflict").classList.remove("hidden");
  }

  /* ---------------- 左栏（平铺，无分组下拉） ---------------- */

  let sbFilter = "";

  function renderSidebar() {
    const host = $("sb-groups");
    host.innerHTML = "";
    const q = sbFilter.trim().toLowerCase();
    const sidebars = meta.sidebars || [];
    sidebars.forEach((sb) => {
      const group = document.createElement("div");
      group.className = "sb-group";

      const title = document.createElement("div");
      title.className = "sb-group-title";
      title.textContent = sb.title;
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = "(" + sb.plugins.length + ")";
      title.appendChild(count);
      group.appendChild(title);

      const items = document.createElement("div");
      items.className = "sb-items";
      const plugins = sb.plugins.filter((p) =>
        !q || p.name.toLowerCase().includes(q) ||
        p.id.toLowerCase().includes(q));
      plugins.forEach((p) => {
        const it = document.createElement("div");
        it.className = "sb-item";
        it.dataset.plugin = p.id;
        it.innerHTML =
          '<span>' + escapeHtml(p.name) + "</span>" +
          (p.status === "conflict"
            ? '<span class="dot-conflict hint-btn" data-hint-key="sidebar-conflict" title="点击查看说明">⚠</span>'
            : `<span class="badge">${PERM_LABEL[p.permission] || ""}</span>`);
        it.addEventListener("click", (e) => {
          // 点击冲突标记只弹说明，不打开插件
          if (e.target.classList && e.target.classList.contains("hint-btn")) {
            e.stopPropagation();
            showHint("sidebar-conflict");
            return;
          }
          openPlugin(p.id);
        });
        items.appendChild(it);
      });
      group.appendChild(items);
      if (plugins.length) host.appendChild(group);
    });
    markActivePlugin(active && active.pluginId);
  }

  function markActivePlugin(pluginId) {
    document.querySelectorAll(".sb-item").forEach((e) => {
      e.classList.toggle("active", e.dataset.plugin === pluginId);
    });
    $("btn-home").classList.toggle("active", !pluginId);
  }

  /* ---------------- 顶栏 / 内容区 ---------------- */

  function renderTopbar(p) {
    $("tb-plugin").textContent = "";
    if (!p) {
      $("tp-crumb").textContent = "主页";
      $("tp-title").textContent = "系统信息预览";
      $("tp-tag").textContent = "内置";
      $("tp-tag").className = "tp-tag";
      return;
    }
    // 面包屑：侧栏分组名 / 插件名
    let crumb = p.name;
    const sb = (meta.sidebars || []).find((s) =>
      s.plugins.some((x) => x.id === p.id));
    if (sb) crumb = sb.title + " / " + p.name;
    $("tp-crumb").textContent = crumb;
    $("tp-title").textContent = p.topbar || p.name;
    $("tp-tag").textContent = PERM_LABEL[p.permission] || p.permission;
    $("tp-tag").className = "tp-tag permission-" + p.permission;
    $("tb-plugin").textContent = p.name + (p.topbar ? " — " + p.topbar : "");
  }

  /* ---------------- 主题（预置切换，不提供配色自定义） ----------------
   * 后端 /api/theme(list) 提供固定预置集；外壳把主题变量应用到自身文档，
   * 插件页/主页由服务端在输出 HTML 时注入 :root 覆盖块自动跟随。 */

  function applyThemeColors(colors) {
    if (!colors) return;
    const root = document.documentElement;
    Object.keys(colors).forEach((k) => {
      root.style.setProperty("--" + k, colors[k]);
    });
  }

  /** 设置面板主题按钮高亮同步。 */
  function markSettingsTheme(curId) {
    document.querySelectorAll(".theme-pick").forEach((b) =>
      b.classList.toggle("active", b.dataset.id === curId));
  }

  async function loadTheme() {
    try {
      const r = await api("GET", "/api/theme");
      if (r && r.colors) applyThemeColors(r.colors);
    } catch (e) { /* 主题失败不阻塞启动 */ }
  }

  /* ---------------- 窗口外观（标题模板 / 图标文件） ----------------
   * 标题为模板：支持 %%time%%/%%date%%/%%perm%%/%%rand6%%/%%ver%%/%%name%%
   * 图标：用户自选图片经主进程缩放为 128x128 custom.ico（默认图标不改）。 */

  let titleTemplate = "";
  let titleCtx = { perm: "普通", version: "", name: "" };
  let titleTimer = null;

  function randAlphaNum(n) {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let s = "";
    for (let i = 0; i < n; i++) s += chars[Math.floor(Math.random() * chars.length)];
    return s;
  }

  function pad2(n) { return n < 10 ? "0" + n : "" + n; }

  function nowParts() {
    const d = new Date();
    return {
      time: pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" +
            pad2(d.getSeconds()),
      date: d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" +
            pad2(d.getDate()),
    };
  }

  /** 解析标题模板 → 显示文本。带时间的占位符每秒重算。 */
  function resolveTitleTemplate(tpl) {
    const p = nowParts();
    return String(tpl)
      .replace(/%%time%%/g, p.time)
      .replace(/%%date%%/g, p.date)
      .replace(/%%perm%%/g, titleCtx.perm || "")
      .replace(/%%rand6%%/g, () => randAlphaNum(6))
      .replace(/%%ver%%/g, titleCtx.version || "")
      .replace(/%%name%%/g, titleCtx.name || "");
  }

  function needsLiveTitle(tpl) {
    return /%%time%%|%%date%%|%%rand6%%/.test(tpl);
  }

  function applyWindowTitle() {
    const text = resolveTitleTemplate(titleTemplate);
    const t = $("tb-title");
    if (t) t.textContent = text || "SystemToolBox";
    if (window.stb && window.stb.setTitle) {
      window.stb.setTitle(text || "SystemToolBox").catch(() => {});
    }
    return text;
  }

  function startTitleClock() {
    stopTitleClock();
    if (titleTemplate && needsLiveTitle(titleTemplate)) {
      titleTimer = setInterval(applyWindowTitle, 1000);
    }
  }

  function stopTitleClock() {
    if (titleTimer) { clearInterval(titleTimer); titleTimer = null; }
  }

  /** 应用当前图标到标题栏（dataUrl 为 PNG/ICO 内容或空=默认）。 */
  function applyWindowIcon(dataUrl) {
    const img = $("tb-logo");
    if (img) {
      img.src = dataUrl || "";
      img.style.display = dataUrl ? "" : "none";
    }
  }

  async function loadWindowAppearance() {
    // 1) 图标：向主进程询问当前生效图标（custom 优先，否则 default）
    try {
      if (window.stb && window.stb.getIcon) {
        const r = await window.stb.getIcon();
        if (r && r.dataUrl) applyWindowIcon(r.dataUrl);
      }
    } catch (e) { /* 忽略 */ }
    // 2) 标题模板：拉取持久化模板与解析上下文，启动计时器
    try {
      const cfg = await api("GET", "/api/settings/window");
      if (cfg) {
        titleTemplate = String(cfg.title || "");
        titleCtx = {
          perm: cfg.perm || "普通",
          version: cfg.version || "",
          name: cfg.name || "",
        };
      }
    } catch (e) {
      titleTemplate = "SystemToolBox";
    }
    startTitleClock();
    applyWindowTitle();
  }

  async function switchTheme(id) {
    await api("POST", "/api/theme", { id });
    const r = await api("GET", "/api/theme");
    if (r && r.colors) applyThemeColors(r.colors);
    // 重载当前 iframe：服务端重新注入新主题的 :root 覆盖块
    if (iframe.src) iframe.src = iframe.src;
  }

  function openHome() {
    active = { kind: "home" };
    iframe.src = API + "/ui/home?token=" + encodeURIComponent(TOKEN);
    renderTopbar(null);
    markActivePlugin(null);
  }

  function openPlugin(pluginId) {
    const p = (meta.plugins || []).find((x) => x.id === pluginId);
    if (!p) return;
    if (p.status === "conflict") {
      alert("插件「" + p.name + "」顶栏冲突，未加载");
      return;
    }
    if (p.status === "disabled") {
      alert("插件「" + p.name + "」已停用" +
        (p.loadError ? "：" + p.loadError : ""));
      return;
    }
    if (p.status !== "ok") {
      alert("插件「" + p.name + "」加载失败：" + (p.loadError || p.status));
      return;
    }
    active = { kind: "plugin", pluginId };
    iframe.src = API + "/plugin/" + encodeURIComponent(pluginId) + "/" +
      "?token=" + encodeURIComponent(TOKEN);
    renderTopbar(p);
    markActivePlugin(pluginId);
  }

  // 后端事件轮询：把快捷键触发等事件转发给插件 iframe
  async function pollEvents() {
    let data;
    try { data = await api("GET", "/api/events"); } catch { return; }
    (data.events || []).forEach((ev) => {
      if (ev.type === "shortcut") {
        iframe.contentWindow.postMessage(
          { type: "stb-shortcut", pluginId: ev.pluginId, id: ev.id, data: ev.data },
          "*");
      }
    });
  }

  /* ---------------- 审批弹窗 ---------------- */

  async function pollApprovals() {
    try {
      const data = await api("GET", "/api/approval/pending");
      const fresh = (data.pending || [])
        .filter((r) => !approvalQueue.includes(r.id));
      if (fresh.length) {
        approvalQueue.push(...fresh.map((r) => r.id));
        showApproval(fresh[0]);
      }
    } catch { /* 静默 */ }
  }

  function showApproval(req) {
    currentRequest = req;
    $("approval-body").innerHTML =
      '<div class="appr-item"><div class="appr-icon">🔑</div>' +
      '<div class="appr-meta">' +
      "<b>" + escapeHtml(req.pluginName) + "</b>" +
      '（<span class="api">' + escapeHtml(req.pluginId) + "</span>）" +
      ' 请求调用接口 <span class="api">' + escapeHtml(req.api) + "</span><br>" +
      '<span class="desc">' + escapeHtml(req.description || "无说明") +
      " · 首次使用需授权，可在插件管理中取消或开放</span>" +
      "</div></div>";
    $("modal-approval").classList.remove("hidden");
  }

  async function resolveApproval(decision) {
    if (!currentRequest) return;
    const id = currentRequest.id;
    try {
      await api("POST", "/api/approvals/" + id + "/resolve",
                { decision: decision });
    } catch { /* ignore */ }
    approvalQueue = approvalQueue.filter((x) => x !== id);
    currentRequest = null;
    $("modal-approval").classList.add("hidden");
  }

  /* ---------------- 插件管理 ---------------- */

  async function openManager() {
    try {
      const data = await api("GET", "/api/manager/list");
      renderManager(data);
      $("modal-manager").classList.remove("hidden");
    } catch (e) { alert(e.message); }
  }

  function renderManager(data) {
    const warn = (data.scanWarnings || [])
      .map((w) => '<div class="warn">⚠ ' + escapeHtml(w) + "</div>").join("");
    const native = data.native;
    const natLine = native && native.available
      ? "✅ native_core.dll: " + escapeHtml(native.version || "") +
        " (" + escapeHtml(native.path || "") + ")"
      : '⚠ native_core.dll 未加载（驱动/服务管理接口不可用，可运行 scripts/build.ps1 编译）';
    $("mgr-status").innerHTML =
      '<div>' + natLine + "</div>" + warn +
      '<div>共 ' + data.plugins.length + " 个插件，冲突 " +
      data.conflicts.length + " 个（选择插件查看详情/权限）</div>";

    const tbody = $("mgr-table").querySelector("tbody");
    tbody.innerHTML = "";
    data.plugins.forEach((p) => {
      const tr = document.createElement("tr");

      const nameTd = document.createElement("td");
      nameTd.innerHTML = "<b>" + escapeHtml(p.name) + "</b><br>" +
        '<span style="color:var(--dim)">' + escapeHtml(p.id) +
        " v" + escapeHtml(p.version) + "</span>";
      tr.appendChild(nameTd);

      const stTd = document.createElement("td");
      const stMap = {
        ok: ["正常", "st-ok"], conflict: ["冲突", "st-conflict"],
        disabled: ["停用", "st-disabled"], error: ["加载失败", "st-error"],
      };
      const [stText, stCls] = stMap[p.status] || [p.status, "st-disabled"];
      stTd.innerHTML = '<span class="st ' + stCls + '">' + stText + "</span>" +
        (p.loadError ? '<div style="color:var(--amber);font-size:11px">' +
          escapeHtml(p.loadError) + "</div>" : "");
      tr.appendChild(stTd);

      tr.appendChild(metaCell(p.sidebar));
      tr.appendChild(metaCell(p.topbar || "—", p.status === "conflict"));

      const opTd = document.createElement("td");
      opTd.appendChild(button(
        p.status === "disabled" ? "启用" : "停用",
        async () => {
          await api("POST", "/api/manager/" + p.id +
                    (p.status === "disabled" ? "/enable" : "/disable"));
          await refreshMeta();
          renderManager(await api("GET", "/api/manager/list"));
        }));
      opTd.appendChild(button("详情", () => openGrants(p.id)));
      tr.appendChild(opTd);

      tbody.appendChild(tr);
    });
  }

  async function refreshMeta() {
    try { meta = await api("GET", "/api/meta"); } catch { return; }
    renderSidebar();
    if (active && active.kind === "plugin") {
      const p = (meta.plugins || []).find((x) => x.id === active.pluginId);
      if (p && p.status === "ok") renderTopbar(p);
      else { active = null; openHome(); }
    }
  }

  /* ---------------- 插件详情（选中插件后才有权限设置） ---------------- */

  async function openGrants(pluginId) {
    try {
      const data = await api("GET", "/api/manager/" + pluginId + "/grants");
      $("grants-plugin").textContent = "— " + data.plugin.name +
        " (" + pluginId + ")";
      $("grants-perm-tip").textContent =
        data.plugin.status === "ok"
          ? "（当前状态：正常）" : "（当前状态：" + data.plugin.status + "）";

      // 只读/全权时：下方授权列表灰色半透明覆盖、不允许编辑
      const overlay = $("grants-overlay");
      const overlayText = $("grants-overlay-text");
      if (data.effectivePermission === "readonly") {
        overlay.classList.remove("hidden");
        overlayText.textContent = "只读权限：管理接口均不可调用，无需授权（可先改为审批权限再编辑）";
      } else if (data.effectivePermission === "full") {
        overlay.classList.remove("hidden");
        overlayText.textContent = "全权权限：管理接口全部放行，无需授权（可先改为审批权限再编辑）";
      } else {
        overlay.classList.add("hidden");
      }

      const level = $("grants-level");
      level.value = data.effectivePermission;

      // API 授权表
      const tbody = $("grants-table").querySelector("tbody");
      tbody.innerHTML = "";
      data.apis.forEach((a) => {
        const tr = document.createElement("tr");
        tr.appendChild(metaCell(a.api));
        tr.appendChild(metaCell(a.permission === "admin" ? "管理" : "只读"));
        tr.appendChild(metaCell(a.description || ""));

        const gTd = document.createElement("td");
        if (a.permission === "readonly") {
          gTd.textContent = "始终放行";
          gTd.style.color = "var(--dim)";
        } else if (a.grant === "allowed") {
          gTd.innerHTML = '<span class="st st-ok">已授权' +
            (a.persistent ? "" : "（会话）") + "</span>";
        } else if (a.grant === "denied") {
          gTd.innerHTML = '<span class="st st-conflict">已拒绝</span>';
        } else {
          gTd.textContent = "未授权";
          gTd.style.color = "var(--dim)";
        }
        tr.appendChild(gTd);

        const opTd = document.createElement("td");
        if (a.permission === "admin") {
          // 开放/取消合并为单个按钮，按当前授权状态更换文案
          const allowed = a.grant === "allowed";
          opTd.appendChild(button(allowed ? "取消" : "开放", async () => {
            await api("POST", "/api/manager/" + pluginId + "/grants/" +
                      encodeURIComponent(a.api),
                      { action: allowed ? "deny" : "allow" });
            openGrants(pluginId);
          }));
          opTd.appendChild(button("重置", async () => {
            await api("POST", "/api/manager/" + pluginId + "/grants/" +
                      encodeURIComponent(a.api), { action: "reset" });
            openGrants(pluginId);
          }));
        } else {
          opTd.textContent = "—";
        }
        tr.appendChild(opTd);
        tbody.appendChild(tr);
      });

      // 快捷键授权（是否允许属于权限管理）
      renderShortcutGrants(pluginId, data.shortcuts);

      $("modal-grants").classList.remove("hidden");
    } catch (e) { alert(e.message); }
  }

  function renderShortcutGrants(pluginId, shortcuts, effectivePerm) {
    const host = $("grants-shortcuts");
    host.innerHTML = "";
    if (!shortcuts || !shortcuts.length) {
      host.innerHTML = '<div class="mgr-status" style="margin:4px 0 0">' +
        "本插件未申请快捷键</div>";
      return;
    }
    const note = document.createElement("div");
    note.className = "mgr-status";
    note.style.cssText = "margin:2px 0 6px";
    note.textContent = "快捷键的启用/改键/恢复默认请在「快捷键管理」中操作：";
    host.appendChild(note);
    shortcuts.forEach((s) => {
      const item = document.createElement("div");
      item.className = "sc-item sc-item-ro";
      item.innerHTML =
        '<span class="sc-desc" style="flex:0 0 130px;color:var(--accent)">' +
        escapeHtml(s.id) + "</span>" +
        '<span class="sc-desc" style="flex:1">' +
        escapeHtml(s.description || "") + "</span>" +
        '<span class="sc-key" style="margin-left:10px">' +
        escapeHtml(s.accelerator || "") + "</span>";
      host.appendChild(item);
    });
  }

  /* ---------------- 通用提示弹窗 ----------------
   * 所有提示语（说明文字 / 气泡 / 悬停 title）收敛为 “?” 按钮，
   * 点击后在此弹窗展示内容，界面不再直接显示大段提示文字。 */

  const HINTS = {};
  function regHint(key, html) { HINTS[key] = html; }
  function hintBtn(key) {
    return '<button type="button" class="hint-btn" data-hint-key="' +
           key + '" aria-label="查看说明">?</button>';
  }
  function showHint(key) {
    const html = HINTS[key] || "（无提示内容）";
    $("hint-body").innerHTML = html;
    $("modal-hint").classList.remove("hidden");
  }

  regHint("win-title",
    "窗口标题支持 %%占位符%% 模板：" +
    '<table class="hint-tbl"><tr><th>占位符</th><th>含义</th></tr>' +
    [["%%time%%", "当前时间 HH:MM:SS（每秒刷新）"],
     ["%%date%%", "当前日期 YYYY-MM-DD（每秒刷新）"],
     ["%%perm%%", "权限（管理员 / 普通）"],
     ["%%rand6%%", "随机 6 位（大小写字母+数字）"],
     ["%%ver%%", "软件版本"],
     ["%%name%%", "软件名称"]].map(([tk, ds]) =>
      "<tr><td><code>" + tk + "</code></td><td>" + ds + "</td></tr>").join("") +
    "</table>");
  regHint("sidebar-conflict",
    "该插件在顶栏与其他插件发生冲突，未加载到顶栏，但不影响侧栏使用。");
  regHint("logs",
    "后端运行日志：红色为错误、黄色为警告；“全选复制”复制全部内容，" +
    "“清空”会同时清空后端日志文件。");
  regHint("shortcuts",
    "插件申请的快捷键<strong>默认禁用</strong>；在此处开启开关后数秒内注册生效。" +
    "点击「更改键位」后直接按下新组合键（字母/数字需带 Ctrl/Alt/Shift，Esc 取消）。" +
    "「恢复默认」还原默认键位并禁用；「恢复全部默认」一键还原所有快捷键。");
  regHint("grants-note",
    "权限级别修改立即生效；API 与快捷键的授权可在此开放 / 取消 / 重置。" +
    "“允许一次”为会话级授权，重启后需重新确认。");

  /* ---------------- 设置（自带侧栏：系统信息 / 更改设置 / 关于） ---------------- */

  async function openSettings() {
    $("modal-settings").classList.remove("hidden");
    await Promise.all([renderPaneInfo(), renderPaneChange(), renderPaneAbout()]);
  }

  function setSettingsPane(name) {
    document.querySelectorAll(".set-nav-item").forEach((n) =>
      n.classList.toggle("active", n.dataset.pane === name));
    ["info", "change", "about"].forEach((p) =>
      $("pane-" + p).classList.toggle("hidden", p !== name));
  }

  async function renderPaneInfo() {
    const host = $("pane-info");
    host.innerHTML = '<div class="hint">加载中…</div>';
    try {
      const s = await api("GET", "/api/settings");
      const rows = [
        ["应用", s.app.name + " v" + s.app.version],
        ["原生层", s.native.available
          ? "已加载 " + (s.native.version || "") : "未编译（ctypes 兜底）"],
        ["原生路径", s.native.path || "—"],
        ["仓库根", s.dirs.root],
        ["Python 目录", s.dirs.pythonDir],
        ["数据目录", s.dirs.dataDir],
        ["插件目录", s.dirs.pluginsDir],
        ["日志文件", s.dirs.logFile],
        ["插件总数", String(s.counts.plugins)],
        ["生效快捷键", String(s.counts.shortcutsActive)],
      ];
      host.innerHTML =
        '<div class="set-pane-title">系统信息</div>' +
        '<div class="settings-body">' +
        rows.map(([k, v]) => '<div class="settings-row">' +
          '<span class="k">' + escapeHtml(k) + "</span>" +
          '<span class="v">' + escapeHtml(v) + "</span></div>").join("") +
        "</div>";
    } catch (e) {
      host.innerHTML = '<div class="hint" style="color:var(--red)">读取失败: ' +
        escapeHtml(e.message) + "</div>";
    }
  }

  async function renderPaneChange() {
    const host = $("pane-change");
    const themeList = (meta.theme && meta.theme.list) || [];
    host.innerHTML =
      '<div class="set-pane-title">更改设置</div>' +
      '<div class="settings-row" style="align-items:center;flex-wrap:wrap">' +
      '<span class="k">外观主题</span>' +
      themeList.map((t) =>
        '<button class="btn small theme-pick" data-id="' + escapeHtml(t.id) +
        '" data-name="' + escapeHtml(t.name) + '">' +
        '<span class="theme-dot" style="background:' +
        (t.preview || t.accent || "var(--accent)") + '"></span>' +
        "</button>").join("") +
      "</div>" +
      '<div class="settings-row" style="align-items:center;flex-wrap:wrap">' +
      '<span class="k">窗口标题</span>' +
      '<input type="text" id="set-win-title" class="win-title-input" maxlength="120" ' +
      'placeholder="SystemToolBox">' +
      '<button class="btn small" id="btn-win-title-save">保存标题</button>' +
      hintBtn("win-title") + "</div>" +
      '<div class="settings-row" style="align-items:center;flex-wrap:wrap">' +
      '<span class="k">窗口图标</span>' +
      '<img id="set-win-icon-preview" class="win-icon-preview" alt="当前图标">' +
      '<button class="btn small" id="btn-win-icon-pick">选择图片…</button>' +
      '<button class="btn small" id="btn-win-icon-reset">恢复默认图标</button>' +
      '<input type="file" id="set-win-icon-file" accept="image/*" style="display:none">' +
      "</div>" +
      '<div class="settings-row" style="align-items:center">' +
      '<span class="k">高级选项</span>' +
      '<label class="sw"><input type="checkbox" id="set-advanced"> ' +
      '<b>启用</b></label>' +
      '<button class="btn small" id="btn-driver-load">加载驱动</button>' +
      '<button class="btn small" id="btn-driver-unload">卸载驱动</button>' +
      "</div>" +
      '<div class="settings-row" style="align-items:center">' +
      '<span class="k">驱动状态</span>' +
      '<span id="drv-status" class="muted">查询中…</span></div>' +
      '<div class="settings-row" style="align-items:center">' +
      '<span class="k">插件授权</span>' +
      '<button class="btn small danger" id="set-grants-clear">清除所有插件授权</button>' +
      "</div>";

    // 窗口标题模板 / 图标（持久化 + 立即生效）
    const titleInput = $("set-win-title");
    let winCfg = { title: "", perm: "普通", version: "", name: "",
                   tokens: [] };
    try {
      winCfg = await api("GET", "/api/settings/window");
    } catch (e) { /* 默认值 */ }

    if (titleInput) {
      titleInput.value = winCfg.title || "SystemToolBox";
      $("btn-win-title-save").addEventListener("click", async () => {
        const v = titleInput.value.trim();
        if (!v) { alert("窗口标题不能为空"); return; }
        try {
          const r = await api("POST", "/api/settings/window", { title: v });
          titleTemplate = r.title;
          startTitleClock();
          applyWindowTitle();
          alert("窗口标题已保存: " + r.title);
        } catch (err) { alert("保存失败: " + err.message); }
      });
    }

    // 图标：预览当前生效图标 + 选择图片 + 恢复默认
    const iconPreview = $("set-win-icon-preview");
    const refreshIconPreview = () => {
      if (!iconPreview) return;
      if (window.stb && window.stb.getIcon) {
        window.stb.getIcon().then((r) => {
          if (r && r.dataUrl) {
            iconPreview.src = r.dataUrl;
            iconPreview.style.display = "";
          } else {
            iconPreview.style.display = "none";
          }
        }).catch(() => { iconPreview.style.display = "none"; });
      }
    };
    refreshIconPreview();
    $("btn-win-icon-pick").addEventListener("click", () => {
      $("set-win-icon-file").click();
    });
    $("set-win-icon-file").addEventListener("change", async (ev) => {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = "";
      if (!file) return;
      try {
        // 读为 dataURL（≤10MB 保护）
        if (file.size > 10 * 1024 * 1024) { alert("图片过大（限 10MB）"); return; }
        const dataUrl = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(fr.result);
          fr.onerror = () => rej(new Error("读取文件失败"));
          fr.readAsDataURL(file);
        });
        if (window.stb && window.stb.saveIcon) {
          const r = await window.stb.saveIcon(dataUrl);
          if (r && r.ok && r.dataUrl) {
            applyWindowIcon(r.dataUrl);
            refreshIconPreview();
            alert("图标已更新（128x128 已保存）");
          } else {
            alert("图标保存失败：无法解析该图片");
          }
        }
      } catch (err) { alert("选择图标失败: " + err.message); }
    });
    $("btn-win-icon-reset").addEventListener("click", async () => {
      if (!confirm("恢复为默认图标？自定义图标文件将被删除（默认图标不受影响）。")) return;
      try {
        if (window.stb && window.stb.resetIcon) {
          const r = await window.stb.resetIcon();
          if (r && r.ok && r.dataUrl) {
            applyWindowIcon(r.dataUrl);
            refreshIconPreview();
            alert("已恢复默认图标");
          }
        }
      } catch (err) { alert("恢复失败: " + err.message); }
    });

    // 主题预置：高亮当前项，点击切换（与顶栏色点联动）
    let curTheme = (meta.theme && meta.theme.current) || "dark";
    markSettingsTheme(curTheme);
    document.querySelectorAll(".theme-pick").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          curTheme = b.dataset.id;
          await switchTheme(b.dataset.id);
          markSettingsTheme(curTheme);
        } catch (err) {
          alert("主题切换失败: " + err.message);
        }
      });
    });

    $("set-grants-clear").addEventListener("click", async () => {
      if (!confirm("确定清除所有插件授权？之后插件调用管理接口需重新审批。")) return;
      const r = await api("POST", "/api/settings/grants/clear");
      alert("已清除 " + r.cleared + " 条授权，插件需重新授权。");
    });
    $("set-advanced").addEventListener("change", async (e) => {
      try {
        await api("POST", "/api/settings/advanced",
          { enabled: e.target.checked });
        await refreshDriverStatus();
      } catch (err) {
        e.target.checked = !e.target.checked;
        alert("高级选项设置失败: " + err.message);
      }
    });
    $("btn-driver-load").addEventListener("click", async () => {
      const b = $("btn-driver-load");
      b.textContent = "加载中…";
      try {
        const r = await api("POST", "/api/driver/load");
        await refreshDriverStatus();
        if (!r.ok) alert("驱动加载失败：" + r.message);
        else alert(r.message);
      } catch (err) {
        alert("驱动加载失败: " + err.message);
      } finally {
        b.textContent = "加载驱动";
      }
    });
    $("btn-driver-unload").addEventListener("click", async () => {
      try {
        const r = await api("POST", "/api/driver/unload");
        await refreshDriverStatus();
        alert(r.message);
      } catch (err) {
        alert("驱动卸载失败: " + err.message);
      }
    });
    refreshDriverStatus();
  }

  async function refreshDriverStatus() {
    const el = $("drv-status");
    if (!el) return;
    try {
      const st = await api("GET", "/api/driver/status");
      const on = $("set-advanced");
      if (on) on.checked = !!st.advanced;
      const svcTxt = st.service && st.service.exists
        ? ("服务:" + st.service.state) : "服务:未安装";
      const br = st.elevated ? "管理员✓" : "非管理员";
      el.textContent = "[" + st.state + "] " + svcTxt + " | " + br +
        (st.testSigning && st.testSigning.enabled ? " | 测试签名 开" : "") +
        (st.sysExists ? "" : " | ⚠ " + st.sysPath + " 不存在");
      el.title = st.error || "";
      if (st.error) el.classList.add("warn-text");
      else el.classList.remove("warn-text");
    } catch (e) {
      el.textContent = "查询失败: " + e.message;
    }
  }

  async function renderPaneAbout() {
    const host = $("pane-about");
    host.innerHTML = '<div class="hint">加载中…</div>';
    try {
      const s = await api("GET", "/api/settings");
      const ver = window.stb.versions || {};
      const native = s.native.available ? (s.native.version || "?") : "未编译";
      const envHtml =
        '<div class="about-block"><div class="h">软件本体</div>' +
        '<div class="about-ver"><span>SystemToolBox</span>' +
        '<span class="v">v' + escapeHtml(s.app.version) + "</span></div></div>" +
        '<div class="about-block"><div class="h">环境版本</div>' +
        '<div class="about-ver">' +
        '<span>Python <span class="v">' + escapeHtml(s.env.python) + "</span></span>" +
        '<span>Node <span class="v">v' + escapeHtml(ver.node || "?") + "</span></span>" +
        '<span>Electron <span class="v">v' + escapeHtml(ver.electron || "?") + "</span></span>" +
        '<span>Chromium <span class="v">v' + escapeHtml(ver.chrome || "?") + "</span></span>" +
        '<span>原生层 <span class="v">' + escapeHtml(native) + "</span></span>" +
        "</div></div>";
      const apis = (s.apis || []).map((g) =>
        '<div class="about-block"><div class="h">' +
        escapeHtml(g.pluginName) + " · " + escapeHtml(g.pluginId) +
        " v" + escapeHtml(g.version) +
        '（' + escapeHtml(g.permission) + "）</div>" +
        '<div class="about-apis">' +
        (g.apis || []).map((a) =>
          '<div class="about-api-line">' +
          '<span class="nm">' + escapeHtml(a.name) + "</span>" +
          '<span class="pm pm-' + escapeHtml(a.permission) + '">' +
          escapeHtml(a.permission) + "</span>" +
          '<span class="ver">v' + escapeHtml(g.version) + "</span>" +
          '<span class="ds">' + escapeHtml(a.description || "") + "</span>" +
          "</div>").join("") +
        "</div></div>").join("");
      host.innerHTML =
        '<div class="set-pane-title">关于</div>' + envHtml + apis;
    } catch (e) {
      host.innerHTML = '<div class="hint" style="color:var(--red)">读取失败: ' +
        escapeHtml(e.message) + "</div>";
    }
  }

  /* ---------------- 日志 ---------------- */

  async function loadLogs() {
    try {
      const d = await api("GET", "/api/logs");
      const body = $("logs-body");
      body.textContent = (d.lines || []).join("\n") || "（空）";
      body.scrollTop = body.scrollHeight;
    } catch (e) {
      $("logs-body").textContent = "读取日志失败: " + e.message;
    }
  }

  async function openLogs() {
    $("modal-logs").classList.remove("hidden");
    clearInterval(logsTimer);
    await loadLogs();
    logsTimer = setInterval(loadLogs, 3000);
  }

  function closeLogs() {
    $("modal-logs").classList.add("hidden");
    clearInterval(logsTimer);
    logsTimer = null;
  }

  /* ---------------- 快捷键管理 ---------------- */

  async function openShortcuts() {
    $("modal-shortcuts").classList.remove("hidden");
    await loadShortcuts();
  }

  let scCapturing = null;   // {tr, pluginId, id} 正在捕获新键位的行

  function scKeyCell(tr, s, onCaptured) {
    const td = document.createElement("td");
    const keySpan = document.createElement("span");
    keySpan.className = "sc-key";
    keySpan.textContent = s.accelerator || s.defaultAccelerator || "—";
    td.appendChild(keySpan);
    if (s.accelerator !== s.defaultAccelerator) {
      const mark = document.createElement("span");
      mark.className = "sc-changed";
      mark.textContent = "已改键";
      td.appendChild(mark);
    }
    return td;
  }

  function stopCapture() {
    if (!scCapturing) return;
    if (scCapturing.tr) {
      const td = scCapturing.tr.querySelector(".sc-capture");
      if (td) td.textContent = scCapturing.originalKey;
    }
    scCapturing = null;
  }

  function startCapture(tr, s, originalKey) {
    stopCapture();
    scCapturing = { tr, pluginId: s.pluginId, id: s.id, originalKey };
    const td = tr.querySelector(".sc-capture");
    td.textContent = "按下新组合键… (Esc 取消)";
    td.style.color = "var(--amber)";
  }

  function accFromEvent(e) {
    const parts = [];
    if (e.ctrlKey) parts.push("Ctrl");
    if (e.altKey) parts.push("Alt");
    if (e.shiftKey) parts.push("Shift");
    if (e.metaKey) parts.push("Super");
    let key = e.key;
    const map = {
      " ": "Space", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left",
      ArrowRight: "Right", Esc: "Escape", PageUp: "PageUp", PageDown: "PageDown",
      "(": "(", ")": ")", "+": "Plus", ",": "Comma", "-": "Minus",
      ".": "Period", "/": "Slash", "`": "`", ";": "Semicolon",
      "'": "Quote", "[": "[", "]": "]", "\\": "\\", "=": "=",
    };
    if (key in map) key = map[key];
    if (/^F([1-9]|1[0-9]|2[0-4])$/.test(key)) {
      parts.push(key);   // 功能键可裸用
    } else if (/^[A-Za-z0-9]$/.test(key)) {
      if (!parts.length) return null;   // 字母/数字必须有修饰键，避免劫持输入
      parts.push(key.toUpperCase());
    } else {
      const named = ["Enter", "Tab", "Delete", "Insert", "Home", "End",
                     "Backspace", "Space", "Escape", "CapsLock", "Up",
                     "Down", "Left", "Right", "PageUp", "PageDown",
                     "NumLock", "ScrollLock", "Pause"];
      if (named.includes(key)) parts.push(key);
      else return null;   // 其它键忽略（保持捕获状态）
    }
    return parts.join("+");
  }

  document.addEventListener("keydown", (e) => {
    if (!scCapturing) return;
    if (e.key === "Escape") { e.preventDefault(); stopCapture(); return; }
    if (e.repeat) { e.preventDefault(); return; }
    const acc = accFromEvent(e);
    if (!acc) { e.preventDefault(); return; }
    e.preventDefault();
    e.stopPropagation();
    const { pluginId, id } = scCapturing;
    api("POST", "/api/manager/" + pluginId + "/shortcuts/" +
        encodeURIComponent(id), { action: "bind", accelerator: acc })
      .then(() => { stopCapture(); loadShortcuts(); })
      .catch((err) => { stopCapture(); alert("改键失败: " + err.message); });
  });

  async function loadShortcuts() {
    const tbody = $("sc-table").querySelector("tbody");
    tbody.innerHTML = "";
    let list;
    try {
      const meta1 = await api("GET", "/api/meta");
      list = meta1.shortcuts || [];
      $("sc-none").style.display = list.length ? "none" : "block";
    } catch (e) {
      $("sc-none").style.display = "block";
      $("sc-none").textContent = "读取失败: " + e.message;
      return;
    }
    $("sc-none").style.display = list.length ? "none" : "block";
    list.forEach((s) => {
      const tr = document.createElement("tr");
      tr.appendChild(metaCell(s.pluginName + " (" + s.pluginId + ")"));
      const keyTd = scKeyCell(tr, s);
      keyTd.className = "sc-capture";
      tr.appendChild(keyTd);
      tr.appendChild(metaCell(s.description || ""));
      // 启用开关
      const st = document.createElement("td");
      const sw = document.createElement("label");
      sw.className = "sw";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!s.enabled;
      cb.addEventListener("change", async () => {
        cb.disabled = true;
        try {
          await api("POST", "/api/manager/" + s.pluginId + "/shortcuts/" +
                    encodeURIComponent(s.id),
                    { action: cb.checked ? "enable" : "disable" });
          loadShortcuts();
        } catch (err) {
          cb.checked = !cb.checked;
          alert("切换失败: " + err.message);
        } finally { cb.disabled = false; }
      });
      sw.appendChild(cb);
      sw.appendChild(document.createElement("b"));
      st.appendChild(sw);
      tr.appendChild(st);
      // 操作
      const op = document.createElement("td");
      op.style.cssText = "display:flex;gap:6px";
      op.appendChild(button("更改键位", () => {
        startCapture(tr, s, s.accelerator || s.defaultAccelerator || "—");
      }));
      op.appendChild(button("恢复默认", async () => {
        await api("POST", "/api/manager/" + s.pluginId + "/shortcuts/" +
                  encodeURIComponent(s.id), { action: "reset" });
        loadShortcuts();
      }));
      tr.appendChild(op);
      tbody.appendChild(tr);
    });
  }

  /* ---------------- 事件绑定 ---------------- */

  function bindEvents() {
    // 左栏
    $("btn-home").addEventListener("click", openHome);
    // 左栏插件搜索过滤（平铺列表即时筛选）
    const sbSearch = $("sb-search");
    if (sbSearch) {
      sbSearch.addEventListener("input", () => {
        sbFilter = sbSearch.value;
        renderSidebar();
      });
    }
    $("btn-manager").addEventListener("click", openManager);
    $("btn-mgr-close").addEventListener("click", () =>
      $("modal-manager").classList.add("hidden"));
    $("btn-settings").addEventListener("click", openSettings);
    $("btn-settings-close").addEventListener("click", () =>
      $("modal-settings").classList.add("hidden"));
    document.querySelectorAll(".set-nav-item").forEach((n) =>
      n.addEventListener("click", () => setSettingsPane(n.dataset.pane)));
    $("btn-logs").addEventListener("click", openLogs);
    $("btn-logs-close").addEventListener("click", closeLogs);
    $("btn-logs-refresh").addEventListener("click", loadLogs);
    // 一键全选复制：把全部日志文本复制到剪贴板（按钮短暂反馈）
    $("btn-logs-copy").addEventListener("click", () => {
      const text = $("logs-body").textContent || "";
      if (!text.trim()) return;
      const ok = window.stb.copyText(text);
      const btn = $("btn-logs-copy");
      const old = btn.textContent;
      btn.textContent = ok ? "已复制 ✓" : "复制失败";
      setTimeout(() => { btn.textContent = old; }, 1200);
    });
    $("btn-logs-clear").addEventListener("click", async () => {
      await api("POST", "/api/logs/clear");
      loadLogs();
    });
    $("btn-shortcuts").addEventListener("click", openShortcuts);
    $("btn-sc-close").addEventListener("click", () => {
      stopCapture();
      $("modal-shortcuts").classList.add("hidden");
    });
    $("btn-sc-refresh").addEventListener("click", loadShortcuts);
    $("btn-sc-reset-all").addEventListener("click", async () => {
      if (!confirm("确定将所有快捷键恢复默认（还原键位并全部禁用）？")) return;
      try {
        const r = await api("POST", "/api/manager/shortcuts/reset-all");
        alert(r.message || "已恢复默认");
      } catch (e) { alert("恢复失败: " + e.message); }
      loadShortcuts();
    });
    $("btn-exit").addEventListener("click", () => window.stb.close());

    // 插件管理
    $("btn-mgr-enable-all").addEventListener("click", async () => {
      await api("POST", "/api/manager/enable-all");
      await refreshMeta();
      renderManager(await api("GET", "/api/manager/list"));
    });
    $("btn-mgr-disable-all").addEventListener("click", async () => {
      await api("POST", "/api/manager/disable-all");
      await refreshMeta();
      renderManager(await api("GET", "/api/manager/list"));
    });
    $("btn-rescan").addEventListener("click", async () => {
      await api("POST", "/api/manager/rescan");
      await refreshMeta();
      renderManager(await api("GET", "/api/manager/list"));
    });
    $("btn-grants-close").addEventListener("click", () =>
      $("modal-grants").classList.add("hidden"));

    // 启动冲突
    $("btn-conflict-ok").addEventListener("click", () =>
      $("modal-conflict").classList.add("hidden"));

    // 审批
    $("btn-approval-once").addEventListener("click", () => resolveApproval("once"));
    $("btn-approval-always").addEventListener("click", () => resolveApproval("always"));
    $("btn-approval-deny-once").addEventListener("click", () => resolveApproval("deny_once"));
    $("btn-approval-deny").addEventListener("click", () => resolveApproval("deny"));

    // 插件详情：权限级别即时生效
    $("grants-level").addEventListener("change", async (e) => {
      const pid = $("grants-plugin").textContent.split("— ")[1];
      if (!pid) return;
      await api("POST", "/api/manager/" + pid + "/permission",
                { permission: e.target.value });
      await refreshMeta();
      openGrants(pid);
    });

    // 顶栏刷新
    $("btn-refresh").addEventListener("click", () => { iframe.src = iframe.src; });

    // 点击遮罩关闭（日志弹窗需额外清定时器）
    document.querySelectorAll(".modal").forEach((m) => {
      m.addEventListener("click", (e) => {
        if (e.target !== m) return;
        m.classList.add("hidden");
        if (m.id === "modal-logs") closeLogs();
      });
    });

    // 通用提示：所有 .hint-btn 点击 -> 弹窗展示对应提示
    document.addEventListener("click", (e) => {
      const btn = e.target.closest(".hint-btn");
      if (btn && btn.dataset.hintKey) {
        showHint(btn.dataset.hintKey);
      }
    });
    $("btn-hint-close").addEventListener("click", () =>
      $("modal-hint").classList.add("hidden"));
  }

  /* ---------------- 入口 ---------------- */
  setupTitlebar();
  bindEvents();
  boot();
})();