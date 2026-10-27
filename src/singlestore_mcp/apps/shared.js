// Shared helpers for all SingleStore MCP apps, built on the official
// @modelcontextprotocol/ext-apps `App` class (inlined before this script and
// published as globalThis.McpExtApps). Exposed as globalThis.S2.
const X = globalThis.McpExtApps;
const S2 = (globalThis.S2 = {});

S2.app = null;

// An uncaught error in an app shows as a banner in the page, not a blank view (apps run in sandboxed
// frames whose console the user can't see). Errors from the host connection itself are left alone.
function showPageError(err) {
  const msg = String(err?.stack || err?.message || err || "Unknown error");
  if (/ProtocolError|Method not found/.test(msg) && !S2.app) return;
  // Browsers report a resize that changes layout within the same frame this way; harmless, nothing broke.
  if (/^ResizeObserver loop/.test(msg)) return;
  let box = document.getElementById("s2-page-error");
  if (!box) {
    box = document.createElement("div");
    box.id = "s2-page-error";
    box.setAttribute("role", "alert");
    box.style.cssText = "position:fixed;left:8px;right:8px;bottom:8px;z-index:9999;background:#fff1f0;color:#a40e26;" +
      "border:1px solid #ffb3b8;border-radius:6px;padding:8px 10px;font:12px/1.4 system-ui,sans-serif;white-space:pre-wrap;max-height:40vh;overflow:auto";
    (document.body || document.documentElement).append(box);
  }
  box.textContent = `This view hit an error (build ${S2.build}): ${msg.slice(0, 1200)}`;
}
window.addEventListener("error", (e) => showPageError(e.error ?? e.message));
window.addEventListener("unhandledrejection", (e) => showPageError(e.reason));
// Short hash of this page's build (see _core.build_page); shown in tooltips.
S2.build = document.querySelector('meta[name="s2-build"]')?.content ?? "dev";

// Connect to the host. Handlers are installed before connecting because the
// host may deliver tool input/result immediately after initialization.
S2.connect = async function ({ name, onToolInput, onToolResult, onToolCancelled, onHostContext } = {}) {
  const app = new X.App({ name, version: "0.2.0" }, { availableDisplayModes: ["inline", "fullscreen"] }, { autoResize: true });
  const applyContext = (ctx) => {
    if (!ctx) return;
    if (ctx.theme) {
      X.applyDocumentTheme(ctx.theme);
      document.documentElement.style.colorScheme = ctx.theme;
    }
    if (ctx.styles?.variables) X.applyHostStyleVariables(ctx.styles.variables);
    if (ctx.styles?.css?.fonts) X.applyHostFonts(ctx.styles.css.fonts);
    if (ctx.displayMode) setDisplayMode(ctx.displayMode);
    if ("s2Presenter" in ctx) S2.presenter.set(Boolean(ctx.s2Presenter));
    onHostContext?.(ctx);
  };
  app.onhostcontextchanged = applyContext;
  if (onToolInput) app.ontoolinput = (p) => onToolInput(p?.arguments ?? {});
  if (onToolResult) app.ontoolresult = (result) => onToolResult(result);
  app.ontoolcancelled = (p) => onToolCancelled?.(p);
  app.onteardown = async () => ({});
  await app.connect(new X.PostMessageTransport(window.parent, window.parent));
  S2.app = app;
  if (S2.embedded()) document.documentElement.dataset.embedded = "";
  if (activeConn.listeners.size) S2.activeConnections();
  applyContext(app.getHostContext());
  return app;
};

S2.resultText = function (result) {
  return (result?.content ?? [])
    .filter((c) => c.type === "text")
    .map((c) => c.text)
    .join("\n");
};

// structuredContent of a tool result; throws with the tool's error text on failure.
S2.resultData = function (result) {
  if (!result) throw new Error("Empty tool result");
  if (result.isError) {
    const text = S2.resultText(result).replace(/^Error executing tool [\w-]+: /, "");
    throw new Error(text || "Tool call failed");
  }
  return result.structuredContent ?? {};
};

// Call one of this server's tools through the host.
S2.callTool = async function (name, args = {}) {
  if (!S2.app) throw new Error("Not connected to host");
  return S2.resultData(await S2.app.callServerTool({ name, arguments: args }));
};

S2.hostCan = (capability) => Boolean(S2.app?.getHostCapabilities?.()?.[capability]);

// ---- Display mode (inline / fullscreen) ----
// The current mode is mirrored on <html data-display-mode="..."> so app CSS
// can give fullscreen layouts more height. Set S2.onDisplayModeChange to
// re-render when it changes.
S2.displayMode = "inline";
S2.onDisplayModeChange = null;

function setDisplayMode(mode) {
  if (!mode || mode === S2.displayMode) return;
  S2.displayMode = mode;
  document.documentElement.dataset.displayMode = mode;
  S2.onDisplayModeChange?.(mode);
}

S2.canFullscreen = () => (S2.app?.getHostContext?.()?.availableDisplayModes ?? []).includes("fullscreen");

S2.toggleFullscreen = async function () {
  const want = S2.displayMode === "fullscreen" ? "inline" : "fullscreen";
  try {
    const res = await S2.app.requestDisplayMode({ mode: want });
    setDisplayMode(res?.mode ?? want);
  } catch (e) {
    S2.toast(`Couldn't switch display mode: ${e.message ?? e}`, "err");
  }
};

// True when running in the server's own full-window browser page.
S2.inBrowserView = () => S2.app?.getHostVersion?.()?.name === "singlestore-browser-view";

// ---- Embedded in the SingleStore Workspace ----
// The workspace hosts the apps as views; it owns the full-screen and
// browser buttons, tells views when they're hidden, and handles hand-offs
// between views (custom "s2-*" postMessages, only to/from the parent).
S2.embedded = () => S2.app?.getHostVersion?.()?.name === "singlestore-workspace";
let embeddedHidden = false;
S2.isHidden = () => document.hidden || embeddedHidden;
// Ask the workspace to switch view, e.g. S2.openView("sql", {sql, database, run: true}).
S2.openView = (view, args = {}) => window.parent.postMessage({ type: "s2-open-view", view, ...args }, "*");
window.addEventListener("message", (ev) => {
  if (ev.source !== window.parent || !S2.embedded()) return;
  const msg = ev.data;
  if (msg?.type === "s2-visibility") embeddedHidden = Boolean(msg.hidden);
  else if (msg?.type === "s2-set-sql") window.dispatchEvent(new CustomEvent("s2:set-sql", { detail: msg }));
  else if (msg?.type === "s2-connection-changed") window.dispatchEvent(new CustomEvent("s2:connection-changed", { detail: msg }));
  else if (msg?.type === "s2-sas-connection-changed") window.dispatchEvent(new CustomEvent("s2:sas-connection-changed", { detail: msg }));
  else if (msg?.type === "s2-files-changed") window.dispatchEvent(new CustomEvent("s2:files-changed", { detail: msg.change }));
  else if (msg?.type === "s2-presenter") S2.presenter.set(Boolean(msg.on));
  // The cluster / SAS Viya answer again after a network break (the Workspace's connection-health line).
  else if (msg?.type === "s2-reconnected") window.dispatchEvent(new CustomEvent("s2:reconnected", { detail: msg }));
});

// A file or folder was renamed or deleted in a file browser: {from, to (null when deleted), folder, version,
// old_version (a renamed SAS Viya file's new and old version)}, where from / to are what an editor keeps as its
// file's path (a local path, or a SAS Viya ref). This view hears it at once
// ("s2:files-changed"); in the Workspace the other views hear it through the workspace.
S2.filesChanged = (change) => {
  window.dispatchEvent(new CustomEvent("s2:files-changed", { detail: change }));
  if (S2.embedded()) window.parent.postMessage({ type: "s2-files-changed", change }, "*");
};
// Where `path` is after the change: the new path, "" when it was deleted, undefined when the change doesn't touch it
// (a folder's change applies to the files below it). Local Windows paths compare case-insensitively.
S2.movedPath = (path, change) => {
  if (!path || !change?.from) return undefined;
  const local = !/^sas-(content|server):\/\//.test(change.from);
  const win = local && /^([a-zA-Z]:[\\/]|\\\\)/.test(change.from);
  const norm = (p) => { let x = String(p); if (local) x = x.replace(/[\\/]+$/, ""); return win ? x.replace(/\//g, "\\").toLowerCase() : x; };
  const p = norm(path), o = norm(change.from);
  if (p === o) return change.to || "";
  const sep = win ? "\\" : "/";
  if (change.folder && p.startsWith(o + sep)) return change.to ? change.to.replace(/[\\/]+$/, "") + String(path).slice(o.length) : "";
  return undefined;
};

// Header button; null when the host doesn't offer fullscreen.
S2.fullscreenButton = function () {
  if (!S2.canFullscreen() || S2.inBrowserView() || S2.embedded()) return null;
  const full = S2.displayMode === "fullscreen";
  return S2.h("button", {
    class: "s2-btn ghost", onclick: () => S2.toggleFullscreen(),
    title: full ? "Exit full screen (Esc)" : "Full screen", "aria-label": full ? "Exit full screen" : "Full screen",
  }, full ? "⤡ Exit full screen" : "⤢ Full screen");
};

// The Clipboard API is often refused inside sandboxed app frames (the host has to delegate
// clipboard-write to every frame level), so fall back to selecting the text and execCommand("copy").
async function copyText(text, input) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const temp = !input;
    try {
      if (temp) {
        input = Object.assign(document.createElement("textarea"), { value: text, readOnly: true });
        input.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
        document.body.append(input);
      }
      input.focus();
      input.select();
      return document.execCommand("copy");
    } catch {
      return false;
    } finally {
      if (temp) input?.remove();
    }
  }
}

// Copy text and say whether it worked.
S2.copy = async function (text, what = "Copied") {
  if (await copyText(text)) S2.toast(what, "ok");
  else S2.toast("Couldn't copy: select the text and press Ctrl+C", "err");
};

// Shown when the host won't open links: the URL stays on screen until closed.
function showLinkPanel(url) {
  document.getElementById("s2-linkpanel")?.remove();
  const input = S2.h("input", {
    class: "s2-input s2-mono", readOnly: true, value: url, "aria-label": "Browser link",
    onfocus: (e) => e.target.select(), onclick: (e) => e.target.select(),
  });
  const panel = S2.h("div", { id: "s2-linkpanel", class: "s2-linkpanel", role: "status" },
    S2.h("div", { class: "s2-linkpanel-text" }, "Claude can't open your browser from here. Open this link in your browser:"),
    S2.h("div", { class: "s2-linkpanel-row" },
      input,
      S2.h("button", { class: "s2-btn primary", onclick: async () =>
        S2.toast((await copyText(url, input)) ? "Link copied" : "Couldn't copy: select the link and press Ctrl+C", "") }, "Copy"),
      S2.h("button", { class: "s2-btn", title: "Post the link as a message in the chat, where you can click it",
        onclick: () => S2.sendPrompt(`Browser link for this view (open it in your browser): ${url}`) }, "Put link in chat"),
      S2.h("button", { class: "s2-btn ghost", "aria-label": "Close", onclick: () => panel.remove() }, "✕")));
  document.body.prepend(panel);
  input.focus();
}

// Header button that reopens this app, with its current arguments, full-window
// in the user's browser. `getArgs` returns the tool arguments for the current view.
S2.openInBrowserButton = function (tool, getArgs) {
  if (!S2.app || S2.inBrowserView() || S2.embedded()) return null;
  return S2.h("button", {
    class: "s2-btn ghost", title: "Open this view full-window in your browser",
    onclick: async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        await S2.openAppInBrowser(tool, getArgs());
      } catch (err) {
        S2.toast(`Couldn't open in browser: ${err.message}`, "err");
      } finally {
        btn.disabled = false;
      }
    },
  }, "↗ Open in browser");
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && S2.displayMode === "fullscreen" && !e.defaultPrevented) S2.toggleFullscreen();
});

// Put a user message into the chat, e.g. "Explain this table".
// Returns true if the host accepted the message. `quiet` skips the error toast
// (for callers with their own fallback).
S2.sendPrompt = async function (text, { quiet = false } = {}) {
  try {
    const res = await S2.app.sendMessage({ role: "user", content: [{ type: "text", text }] });
    if (res?.isError) throw new Error("rejected by host");
    return true;
  } catch {
    if (!quiet) S2.toast(S2.inBrowserView() ? "Asking Claude only works inside Claude" : "Couldn't send the message to the chat", "err");
    return false;
  }
};

// Open an app view (tool + arguments) full-window in the user's browser; if the
// host won't open links, show the link with Copy / "Put link in chat" instead.
S2.openAppInBrowser = async function (tool, args) {
  const clean = Object.fromEntries(Object.entries(args ?? {}).filter(([, v]) => v != null && v !== ""));
  const { url } = await S2.callTool("browser_link", { tool, arguments: clean });
  let opened = false;
  try { opened = !(await S2.app.openLink({ url }))?.isError; } catch { opened = false; }
  if (!opened) showLinkPanel(url);
};

// Tell the model what the user is currently looking at, without a chat message.
S2.updateModelContext = async function (text) {
  try {
    await S2.app.updateModelContext({ content: [{ type: "text", text }] });
  } catch {
    // Optional host feature; nothing to do if unsupported.
  }
};

// Save text as a file via the host (sandboxed iframes usually can't download directly).
S2.download = async function (filename, text, mimeType = "text/plain") {
  if (S2.hostCan("downloadFile")) {
    const res = await S2.app.downloadFile({
      contents: [{ type: "resource", resource: { uri: `file:///${filename}`, mimeType, text } }],
    });
    if (!res?.isError) return true;
  }
  try {
    const a = S2.h("a", { href: URL.createObjectURL(new Blob([text], { type: mimeType })), download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    return true;
  } catch {
    S2.toast("This host doesn't allow file downloads", "err");
    return false;
  }
};

// Tiny DOM builder: S2.h("button", {class: "s2-btn", onclick}, "Label")
S2.h = function (tag, props, ...children) {
  const el = document.createElement(tag);
  let initialValue;
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value == null || value === false) continue;
    // value is a property, set after the children: a <textarea> ignores a value attribute, and a <select>
    // can only take a value once its options are there.
    if (key === "value") initialValue = value;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  if (initialValue !== undefined) el.value = initialValue;
  return el;
};

S2.fmt = {
  number(n, digits = 0) {
    if (n == null || n === "") return "–";
    const v = Number(n);
    return Number.isFinite(v) ? v.toLocaleString(undefined, { maximumFractionDigits: digits }) : String(n);
  },
  bytes(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "–";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let x = v;
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
    return `${x.toFixed(x >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  },
  duration(seconds) {
    const s = Number(seconds);
    if (!Number.isFinite(s)) return "–";
    if (s < 1) return `${Math.round(s * 1000)} ms`;
    if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
    return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  },
  // Accepts ISO strings (server-local, no zone), Date, or unix seconds.
  toDate(v) {
    if (v == null || v === "") return null;
    if (v instanceof Date) return v;
    if (typeof v === "number") return new Date(v * 1000);
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? null : d;
  },
  dateTime(v) {
    const d = S2.fmt.toDate(v);
    return d ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–";
  },
  relTime(v) {
    const d = S2.fmt.toDate(v);
    if (!d) return "–";
    const diff = (Date.now() - d.getTime()) / 1000;
    const abs = Math.abs(diff);
    const unit = abs < 60 ? ["second", 1] : abs < 3600 ? ["minute", 60] : abs < 86400 ? ["hour", 3600] : ["day", 86400];
    return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(-Math.round(diff / unit[1]), unit[0]);
  },
};

// The toast container is there from the start, so screen readers announce the first message too.
function toastHost() {
  let host = document.getElementById("s2-toasts");
  if (!host && document.body) document.body.append((host = S2.h("div", { id: "s2-toasts", "aria-live": "polite" })));
  return host;
}
if (document.body) toastHost(); else document.addEventListener("DOMContentLoaded", toastHost);
S2.toast = function (message, kind = "") {
  const host = toastHost();
  const el = S2.h("div", { class: `s2-toast ${kind}`, role: "status" }, message);
  host.append(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 3500);
};

// Inline confirmation (window.confirm is blocked in sandboxed iframes).
// Temporarily replaces `anchor` with "message [Cancel] [Confirm]".
S2.confirmInline = function (anchor, message, confirmLabel = "Confirm") {
  return new Promise((resolve) => {
    const done = (answer) => { chip.replaceWith(anchor); resolve(answer); };
    const chip = S2.h("span", { class: "s2-confirm" }, message,
      S2.h("button", { class: "s2-btn ghost", onclick: () => done(false) }, "Cancel"),
      S2.h("button", { class: "s2-btn danger", onclick: () => done(true) }, confirmLabel));
    anchor.replaceWith(chip);
    chip.querySelector(".danger").focus();
  });
};

// Status words in sentence case for badges ("BASE TABLE" -> "Base table", "OPEN" -> "Open"); mixed-case text stays.
S2.sentence = (text) => { const t = String(text ?? ""); return /^[A-Z][A-Z0-9 _-]*$/.test(t) ? t[0] + t.slice(1).toLowerCase().replace(/_/g, " ") : t; };

S2.errorBox = (err) => S2.h("div", { class: "s2-error" }, err?.message ?? String(err));

// The Workspace's SAS session (sas_prewarm), for the SAS views' header: "SAS session: starting… / ready / not
// signed in / unreachable". ``watch.refresh()`` asks the server (``getArgs()``: {connection, start}) and, while the
// session is starting, asks again every 2.5 s; ``onChange(status)`` redraws.
// ---- Where code runs: SingleStore, SAS Compute, CAS or Python on this computer.
// S2.engineChip(engine, connection): a labelled chip (icon + "SingleStore · prod"; "S2" on narrow widths, the full
// text in the tooltip). S2.runContent(engine, connection) is the inside of a Run button ("Run on SingleStore · prod"),
// S2.runningText(engine) the status line while it runs, S2.pushdownChip(verdict) what reached SingleStore.
const ICONS = {
  s2: '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8v-13"/><path d="M4.5 12c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8"/>',
  compute: '<rect x="3" y="4" width="18" height="12" rx="2"/><line x1="8" y1="20" x2="16" y2="20"/><line x1="12" y1="16" x2="12" y2="20"/>',
  cas: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/><path d="M9.5 2.5v4M14.5 2.5v4M9.5 17.5v4M14.5 17.5v4M2.5 9.5h4M2.5 14.5h4M17.5 9.5h4M17.5 14.5h4"/>',
  python: '<polyline points="5 16 10 11 5 6"/><line x1="12" y1="18" x2="19" y2="18"/>',
  sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>',
  present: '<rect x="3" y="4" width="18" height="12" rx="1.5"/><line x1="12" y1="16" x2="12" y2="20"/><line x1="8" y1="20" x2="16" y2="20"/><path d="M9 10l2 2 4-4"/>',
};
S2.icon = function (name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "2"); s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = ICONS[name] ?? ""; // static, trusted markup
  return s;
};
S2.ENGINES = {
  s2: { label: "SingleStore", short: "S2", prep: "on" },
  compute: { label: "SAS Compute", short: "Compute", prep: "in" },
  cas: { label: "CAS", short: "CAS", prep: "in" },
  python: { label: "Python (local)", short: "Python", prep: "in" },
};
// The engine a language runs in: SingleStore SQL on SingleStore; PROC SQL, FedSQL and plain SAS code in SAS Compute;
// FedSQL in CAS in CAS; notebook Python on this computer.
S2.engineOf = (lang) => (lang === "cas" ? "cas" : !lang || lang === "singlestore" || lang === "sql" ? "s2" : lang === "python" ? "python" : "compute");
S2.engineText = (engine, connection) => `${(S2.ENGINES[engine] ?? S2.ENGINES.s2).label}${connection ? ` · ${connection}` : ""}`;
S2.engineChip = function (engine, connection, { title, verb = "Runs" } = {}) {
  const e = S2.ENGINES[engine] ?? S2.ENGINES.s2;
  const full = `${verb} ${e.prep} ${e.label}${connection ? ` (connection ${connection})` : ""}`;
  return S2.h("span", { class: "s2-engine", "data-engine": engine in S2.ENGINES ? engine : "s2", "aria-label": full, title: title ?? full },
    S2.icon(engine), S2.h("span", { class: "long", "aria-hidden": "true" }, e.label, connection ? [" · ", S2.h("span", { class: "conn" }, connection)] : null),
    S2.h("span", { class: "short", "aria-hidden": "true" }, e.short));
};
S2.runText = (engine, connection) => `Run ${(S2.ENGINES[engine] ?? S2.ENGINES.s2).prep} ${S2.engineText(engine, connection)}`;
S2.runContent = function (engine, connection, verb = "Run") {
  const e = S2.ENGINES[engine] ?? S2.ENGINES.s2;
  return [S2.h("span", { class: "long" }, `${verb} ${e.prep} ${e.label}`, connection ? [" · ", S2.h("span", { class: "conn" }, connection)] : null),
    S2.h("span", { class: "short" }, `${verb} · ${e.short}`)];
};
S2.runningText = (engine) => { const e = S2.ENGINES[engine] ?? S2.ENGINES.s2; return `Running ${e.prep} ${e.label}…`; };
const PUSHDOWN = { full: "full", partly: "partial", pulled: "none" };
S2.pushdownChip = function (verdict) {
  if (!PUSHDOWN[verdict]) return null;
  return S2.h("span", { class: "s2-pushdown", "data-v": verdict,
    title: verdict === "full" ? "SingleStore did the work; SAS only received the result."
      : verdict === "partly" ? "Part of the work (e.g. the WHERE clause or the column list) ran in SingleStore; SAS did the rest."
      : "Only a plain table read reached SingleStore: the rows were read into SAS and processed there." },
    "→ pushed down to SingleStore: ", S2.h("b", {}, PUSHDOWN[verdict]));
};
// The active SingleStore and SAS Viya connections (names), read once per page and again after a switch.
// S2.onConnections(fn) calls fn({s2, viya}) now (when known) and after every change.
const activeConn = { s2: undefined, viya: undefined, loading: null, listeners: new Set() };
S2.activeConnections = function (force = false) {
  if (activeConn.loading && !force) return activeConn.loading;
  activeConn.loading = Promise.all([
    S2.callTool("connections_state", {}).then((r) => r.active ?? null).catch(() => null),
    S2.callTool("sas_connections_state", {}).then((r) => r.active ?? null).catch(() => null),
  ]).then(([s2, viya]) => {
    activeConn.s2 = s2; activeConn.viya = viya;
    for (const fn of activeConn.listeners) { try { fn(activeConn); } catch { /* a view's redraw */ } }
    return activeConn;
  });
  return activeConn.loading;
};
S2.onConnections = function (fn) {
  activeConn.listeners.add(fn);
  if (activeConn.s2 !== undefined) fn(activeConn); else if (S2.app) S2.activeConnections();
};
S2.connections = () => activeConn;
window.addEventListener("s2:connection-changed", () => { if (activeConn.loading) S2.activeConnections(true); });
window.addEventListener("s2:sas-connection-changed", () => { if (activeConn.loading) S2.activeConnections(true); });

// "Ask Claude": one name, one style and a small sparkle everywhere (the title says what is asked).
S2.askClaudeButton = function (props = {}, label = "Ask Claude") {
  const { class: cls, ...rest } = props;
  return S2.h("button", { ...rest, class: `s2-btn s2-ask${cls ? ` ${cls}` : ""}` }, S2.icon("sparkle"), label);
};

// A modal dialog over the page: role=dialog + aria-modal, focus moved into it (``focus``: an element or a function
// returning one; else its first field), Tab kept inside, Escape anywhere closes it (unless a field inside already
// handled the key: preventDefault). Returns close(), which also gives the focus back to where it was.
S2.modal = function (panel, { label, onClose, focus } = {}) {
  const before = document.activeElement;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  if (label) panel.setAttribute("aria-label", label);
  if (!panel.hasAttribute("tabindex")) panel.setAttribute("tabindex", "-1");
  const focusables = () => [...panel.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter((el) => !el.disabled && el.getClientRects().length > 0);
  const onKey = (e) => {
    if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); e.stopPropagation(); close(); onClose?.(); return; }
    if (e.key !== "Tab") return;
    const els = focusables();
    if (!els.length) { e.preventDefault(); panel.focus(); return; }
    const first = els[0], last = els[els.length - 1];
    if (!panel.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  // Focus that lands behind the dialog (a click on the page) comes back into it.
  const onFocus = (e) => { if (open && panel.isConnected && !panel.hidden && !panel.contains(e.target)) (focusables()[0] ?? panel).focus(); };
  document.addEventListener("keydown", onKey);
  document.addEventListener("focusin", onFocus);
  let open = true;
  function close() {
    if (!open) return;
    open = false;
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("focusin", onFocus);
    panel.removeAttribute("aria-modal");
    panel.removeAttribute("role");
    if (before && before.isConnected && before !== document.body && typeof before.focus === "function") before.focus();
  }
  const first = () => { const el = typeof focus === "function" ? focus() : focus; (el ?? focusables().find((x) => x.matches("input, select")) ?? focusables()[0] ?? panel).focus(); };
  // The dialog's content may be drawn right after this call (a file browser): focus the panel now, the field next frame.
  panel.focus();
  requestAnimationFrame(() => { if (open && (document.activeElement === panel || !panel.contains(document.activeElement))) first(); });
  return close;
};

// ---- Presenter mode
// One switch in the Workspace (remembered per browser): bigger text (shared.css) and neutral stand-ins for
// identifying metadata in every view: connection names ("Connection A"), hosts and IPs ("host-1"), user names and
// e-mail addresses ("user1"), SAS Viya addresses ("viya-1"). The server hands out the labels (presenter_labels), so a
// value gets the same label in every view. Only the text on screen changes (text, tooltips, labels); the views' data
// and what they send stay real. Not masked: editors (the SQL and code you type) and query results, where only a cell
// that is exactly one of the known values is replaced. Form fields holding such a value are blurred until focused.
const pres = { on: false, map: new Map(), re: null, pending: new Map(), timer: null, observer: null, ready: null,
  texts: new Map(), attrs: new Map(), fields: new Set() };
const MASK_ATTRS = ["title", "aria-label", "placeholder", "alt"];
const NO_MASK = "script, style, textarea, .cm-editor, [data-s2-nomask]";
const RX_EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/g;
const RX_IP = /(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])/g;
const RX_FQDN = /(?<![\w.@-])[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\.(?:com|net|org|io|local|internal|cloud|corp|lan|intra|no|se|dk|fi|uk|de|fr|nl|eu|us|ca|au|ch|be|at|it|es|pl|biz|info|dev|app|ai)(?![\w-])/gi;
const PUBLIC_HOSTS = /(^|\.)(singlestore\.com|singlestore\.io|documentation\.sas\.com|support\.sas\.com|communities\.sas\.com|github\.com|microsoft\.com|python\.org|pypi\.org|jupyter\.org|anthropic\.com|claude\.ai)$/i;
const LOCAL_IP = /^(127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.255)$/;
// host:port where the host isn't a domain name (Kubernetes service names, short host names): "node-x.svc-y:3306".
const RX_HOSTPORT = /(?<![\w.@/-])([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?=:\d{2,5}(?!\d))/gi;
const IS_LABEL = /^(host|viya)-\d+$|^user\d+$/i;
const escRe = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function presLabel(value, kind) {
  const k = pres.map.get(value.toLowerCase());
  if (k) return k.label;
  pres.pending.set(value.toLowerCase(), { value, kind });
  clearTimeout(pres.timer);
  pres.timer = setTimeout(fetchLabels, 40);
  return "•••";
}
function maskString(s, exact) {
  if (!s || !/[A-Za-z0-9]/.test(s)) return s;
  if (exact) {
    const k = pres.map.get(s.trim().toLowerCase());
    return k ? s.replace(s.trim(), k.label) : s;
  }
  let out = pres.re ? s.replace(pres.re, (m) => pres.map.get(m.toLowerCase())?.label ?? m) : s;
  out = out.replace(RX_EMAIL, (m) => presLabel(m, "user"));
  out = out.replace(RX_IP, (m) => (LOCAL_IP.test(m) ? m : presLabel(m, "host")));
  out = out.replace(RX_FQDN, (m) => (PUBLIC_HOSTS.test(m) ? m : presLabel(m, "host")));
  out = out.replace(RX_HOSTPORT, (m) => (IS_LABEL.test(m) || /^localhost$/i.test(m) || !/[.-]/.test(m) || !/[a-z]/i.test(m.replace(/^\d{4}-\d\d-\d\dT/i, "")) ? m : presLabel(m, "host")));
  return out;
}
function setLabels(items) {
  for (const x of items ?? []) pres.map.set(String(x.value).toLowerCase(), x);
  const terms = [...pres.map.values()].map((x) => x.value).sort((a, b) => b.length - a.length);
  // Whole words, where _ also separates: "db_jonas_1572b" names a user too.
  pres.re = terms.length ? new RegExp(`(?<![A-Za-z0-9])(?:${terms.map(escRe).join("|")})(?![A-Za-z0-9])`, "gi") : null;
}
async function fetchLabels() {
  const values = [...pres.pending.values()];
  pres.pending.clear();
  try { setLabels((await S2.callTool("presenter_labels", values.length ? { values } : {})).labels); }
  catch { return; }
  if (pres.on) remaskAll();
}
const skipped = (el) => !el || Boolean(el.closest(NO_MASK));
function maskText(node) {
  const parent = node.parentElement;
  if (skipped(parent)) return;
  const rec = pres.texts.get(node);
  const orig = rec && node.data === rec.masked ? rec.orig : node.data;
  const masked = maskString(orig, Boolean(parent.closest("[data-s2-data]")));
  if (masked !== orig) {
    pres.texts.set(node, { orig, masked });
    if (node.data !== masked) node.data = masked;
  } else if (rec) pres.texts.delete(node);
}
function maskAttr(el, name) {
  const v = el.getAttribute(name);
  if (v == null || skipped(el)) return;
  let recs = pres.attrs.get(el);
  const rec = recs?.[name];
  const orig = rec && v === rec.masked ? rec.orig : v;
  const masked = maskString(orig, Boolean(el.closest("[data-s2-data]")));
  if (masked !== orig) {
    if (!recs) pres.attrs.set(el, (recs = {}));
    recs[name] = { orig, masked };
    if (v !== masked) el.setAttribute(name, masked);
  } else if (rec) delete recs[name];
}
function maskField(el) {
  if (el.closest(".cm-editor") || ["password", "checkbox", "radio", "number"].includes(el.type)) return;
  const v = el.value ?? "";
  const hit = Boolean(v) && maskString(v, false) !== v;
  el.classList.toggle("s2-masked-field", hit);
  if (hit) pres.fields.add(el); else pres.fields.delete(el);
}
function maskTree(root) {
  if (root.nodeType === Node.TEXT_NODE) { maskText(root); return; }
  if (root.nodeType !== Node.ELEMENT_NODE || root.closest(NO_MASK)) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.nodeType === Node.ELEMENT_NODE && n.matches(NO_MASK) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  for (let n = walker.currentNode; n; n = walker.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) { maskText(n); continue; }
    for (const a of MASK_ATTRS) if (n.hasAttribute(a)) maskAttr(n, a);
    if (n.tagName === "INPUT") maskField(n);
  }
}
function remaskAll() {
  for (const [node, rec] of [...pres.texts]) {
    if (!node.isConnected) { pres.texts.delete(node); continue; }
    if (node.data === rec.masked) { pres.texts.delete(node); node.data = rec.orig; }
  }
  for (const [el, recs] of [...pres.attrs]) {
    if (!el.isConnected) { pres.attrs.delete(el); continue; }
    for (const [name, rec] of Object.entries(recs)) if (el.getAttribute(name) === rec.masked) { delete recs[name]; el.setAttribute(name, rec.orig); }
  }
  if (document.body) maskTree(document.body);
}
function unmaskAll() {
  for (const [node, rec] of pres.texts) if (node.data === rec.masked) node.data = rec.orig;
  for (const [el, recs] of pres.attrs) for (const [name, rec] of Object.entries(recs)) if (el.getAttribute(name) === rec.masked) el.setAttribute(name, rec.orig);
  for (const el of pres.fields) el.classList.remove("s2-masked-field");
  pres.texts.clear(); pres.attrs.clear(); pres.fields.clear();
}
function onPresInput(e) { if (pres.on && e.target?.tagName === "INPUT") maskField(e.target); }
function onPresMutations(records) {
  for (const r of records) {
    if (r.type === "characterData") maskText(r.target);
    else if (r.type === "attributes") maskAttr(r.target, r.attributeName);
    else for (const n of r.addedNodes) maskTree(n);
  }
}
S2.presenter = {
  get on() { return pres.on; },
  // What the screen shows for this text (the text itself when presenter mode is off).
  mask: (text) => (pres.on ? maskString(String(text ?? ""), false) : text),
  // Resolves once the labels are in and the page is masked.
  ready: () => pres.ready ?? Promise.resolve(),
  set(on) {
    on = Boolean(on);
    if (on === pres.on) return;
    pres.on = on;
    const root = document.documentElement;
    if (!on) {
      delete root.dataset.presenter;
      delete root.dataset.presenterLoading;
      pres.observer?.disconnect(); pres.observer = null;
      document.removeEventListener("input", onPresInput, true);
      unmaskAll();
      pres.ready = null;
      return;
    }
    root.dataset.presenter = "";
    // Nothing identifying shows before the labels are in (one local call): the page is hidden until then.
    root.dataset.presenterLoading = "";
    const show = () => { delete root.dataset.presenterLoading; };
    const failsafe = setTimeout(show, 3000);
    pres.observer = new MutationObserver(onPresMutations);
    pres.observer.observe(root, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: MASK_ATTRS });
    document.addEventListener("input", onPresInput, true);
    if (document.body) maskTree(document.body);  // e-mails, IPs and host names at once
    pres.ready = (async () => {
      try { setLabels((await S2.callTool("presenter_labels", {})).labels); } catch { /* e-mails, IPs and host names only */ }
      if (pres.on) remaskAll();
      clearTimeout(failsafe); show();
    })();
  },
};

const SAS_SESSION = {
  starting: ["info", "starting…"], ready: ["ok", "ready"], idle: ["plain", "starts with the first run"],
  off: ["plain", "starts with the first run"], not_signed_in: ["warn", "not signed in"],
  unreachable: ["warn", "SAS Viya unreachable"], error: ["err", "couldn't start"],
};
S2.sasSessionBadge = function (st) {
  if (!st?.state) return null;
  const [kind, text] = SAS_SESSION[st.state] ?? ["plain", st.state];
  const secs = (ms) => `${Math.round(ms / 1000)} s`;
  const title = st.state === "ready"
    ? `The SAS session${st.connection ? ` on ${st.connection}` : ""} is running${st.ms ? ` (started in the background in ${secs(st.ms)})` : ""}`
      + `${st.startup_errors ? `; the startup code had ${st.startup_errors} error(s)` : ""}.`
    : st.state === "starting" ? "Starting the SAS session in the background (SingleStore library and startup code included): the first run waits for it, not for a new one."
    : st.note || (st.state === "idle" || st.state === "off" ? "No SAS session yet: the first SAS run starts one." : "");
  return S2.h("span", { class: `s2-badge ${kind} s2-sas-session`, "data-state": st.state, title }, `SAS session: ${text}`);
};
S2.sasSessionWatch = function (getArgs, onChange) {
  let timer = null, seq = 0;
  const watch = { status: null };
  watch.refresh = async function () {
    clearTimeout(timer); timer = null;
    const mine = ++seq;
    let st = null;
    try { st = await S2.callTool("sas_prewarm", getArgs()); } catch { st = null; }
    if (mine !== seq) return;
    watch.status = st;
    onChange(st);
    if (st?.state === "starting") timer = setTimeout(() => watch.refresh(), 2500);
  };
  return watch;
};

S2.isNumericColumn = function (rows, col) {
  let seen = 0;
  for (const row of rows) {
    const v = row[col];
    if (v == null || v === "") continue;
    if (typeof v === "number") { seen++; continue; }
    if (typeof v === "string" && /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(v.trim())) { seen++; continue; }
    return false;
  }
  return seen > 0;
};

S2.cellText = (v) => (v == null ? "NULL" : typeof v === "object" ? JSON.stringify(v) : String(v));

// Basic sortable table. opts.onRowClick(row) optional; opts.render[col](value,row) optional.
// For a result that arrives in pages: the returned element has appendRows(rows) (adds rows at the end, or
// re-sorts when sorted), and opts.beforeSort() (async, optional) runs before a sort, e.g. to load every row.
S2.dataTable = function (columns, rows, opts = {}) {
  rows = [...rows];
  const numeric = Object.fromEntries(columns.map((c) => [c, S2.isNumericColumn(rows, c)]));
  let sortCol = null;
  let sortDir = 1;
  const tbody = S2.h("tbody");
  const headCells = columns.map((col) =>
    S2.h("th", {
      class: `sortable${numeric[col] ? " num" : ""}`, title: col,
      onclick: async () => {
        if (opts.beforeSort) await opts.beforeSort();
        sortDir = sortCol === col ? -sortDir : 1; sortCol = col; draw();
      },
    }, col, S2.h("span", { class: "s2-sort" })));
  const compare = (a, b) => {
    const x = a[sortCol], y = b[sortCol];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    if (numeric[sortCol]) return (Number(x) - Number(y)) * sortDir;
    return String(x).localeCompare(String(y), undefined, { numeric: true }) * sortDir;
  };
  const tr = (row) => S2.h("tr", {
    style: opts.onRowClick ? { cursor: "pointer" } : null,
    onclick: opts.onRowClick ? () => opts.onRowClick(row) : null,
  }, columns.map((col) => {
    const v = row[col];
    const custom = opts.render?.[col];
    return S2.h("td", { class: `${numeric[col] ? "num" : ""}${v == null ? " null" : ""}`, title: S2.cellText(v) },
      custom ? custom(v, row) : S2.cellText(v));
  }));
  function draw() {
    const sorted = sortCol ? [...rows].sort(compare) : rows;
    headCells.forEach((th, i) => {
      th.querySelector(".s2-sort").textContent = columns[i] === sortCol ? (sortDir > 0 ? "▲" : "▼") : "";
    });
    tbody.replaceChildren(...sorted.map(tr));
  }
  draw();
  // opts.data: the rows are query results (presenter mode replaces only cells that are exactly a known value).
  const wrap = S2.h("div", { class: "s2-table-wrap", style: { maxHeight: opts.maxHeight ?? "420px" }, "data-s2-data": opts.data ? "" : null },
    S2.h("table", { class: "s2-table" }, S2.h("thead", {}, S2.h("tr", {}, headCells)), tbody));
  wrap.appendRows = (more) => {
    if (!more?.length) return;
    rows.push(...more);
    if (sortCol) draw();
    else tbody.append(...more.map(tr));
  };
  wrap.rowCount = () => rows.length;
  return wrap;
};

// File browser shared by the SQL Editor, the Notebook and the Security script basket: folders to
// navigate, shortcuts (SQL folder, Documents, Desktop, Downloads, Home) and
// the files of one kind. Server side: files_browse (folders inside the
// user's home folder and SINGLESTORE_MCP_FILE_ROOTS).
//   S2.fileBrowser(container, { kind: "sql"|"notebook", mode: "open"|"save",
//     start, name, onOpen(path, file), onSave(path, overwrite, listing) -> {exists}, onClose, extra: [buttons] })
// Other places (SAS Content / SAS Server, see S2.fileLocations) pass their own
//   list(path, showAll) -> listing (files_browse's shape), storageKey, showAll (an "All files" box),
//   mkdir(parent, name) (a "New folder" button) and empty (text for an empty folder).
// Rename / Delete (kind "sql" and "notebook", and SAS Viya listings, which say `manage`): one item at a time in
// the folder shown, deleting a folder only when it's empty and after typing its name; files_rename / files_delete
// or viya_files_rename / viya_files_delete. Afterwards S2.filesChanged tells the editors that have the file open
// ({from, to, folder}; opts.onChanged(result) too). opts.manage = false turns it off.
S2.fileBrowser = function (box, opts) {
  const { h } = S2;
  box.classList.add("s2-fb");
  const key = opts.storageKey ?? `s2-browse-${opts.kind}`;
  let listing = null, error = null, allFiles = false, loading = false;
  let folder = opts.start || (() => { try { return localStorage.getItem(key); } catch { return null; } })() || null;
  const filter = h("input", { class: "s2-input", type: "search", placeholder: "Filter…", "aria-label": "Filter", oninput: () => draw() });
  const nameInput = opts.mode === "save"
    ? h("input", { class: "s2-input", value: opts.name || "", spellcheck: false, "aria-label": "File name",
        onkeydown: (e) => { if (e.key === "Enter") save(false); if (e.key === "Escape") { e.preventDefault(); opts.onClose?.(); } } })
    : null;
  const message = h("div", { class: "row" });
  const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : new Date(t * 1000).toLocaleDateString(); };
  const sep = () => (listing?.path?.includes("\\") ? "\\" : "/");
  const errText = (e) => String(e?.message ?? e).replace(/^Error executing tool [\w-]+:\s*/, "");
  const baseName = (p) => String(p ?? "").split(/[\\/]/).pop();
  const warn = (text, ...buttons) => message.replaceChildren(h("span", { class: "warn" }, text), ...buttons,
    h("button", { class: "s2-btn ghost", onclick: () => message.replaceChildren() }, buttons.length ? "Cancel" : "OK"));
  const canManage = () => Boolean(listing) && (opts.manage ?? (opts.list ? listing.manage === true : ["sql", "notebook"].includes(opts.kind)));

  // Only the latest navigation counts: a slower answer for a folder clicked earlier (e.g. My Folder, then a folder
  // in it before My Folder had loaded) must not replace the list afterwards.
  let navSeq = 0;
  async function go(path) {
    const mine = ++navSeq;
    loading = true;
    draw();
    try {
      const res = opts.list ? await opts.list(path, allFiles) : await S2.callTool("files_browse", { path, kind: opts.kind });
      if (mine !== navSeq) return;
      listing = res;
      folder = listing.path; error = null;
      try { localStorage.setItem(key, folder); } catch {}
    } catch (e) {
      if (mine !== navSeq) return;
      error = errText(e);
      if (!listing && path) return go(null);  // remembered folder gone: start at the SQL folder
    }
    loading = false;
    filter.value = "";
    draw();
  }
  function newFolder() {
    const input = h("input", { class: "s2-input", placeholder: "Folder name", spellcheck: false, "aria-label": "New folder name",
      onkeydown: (e) => { if (e.key === "Enter") create(); if (e.key === "Escape") { e.preventDefault(); message.replaceChildren(); } } });
    async function create() {
      const name = input.value.trim();
      if (!name || !listing) return;
      message.replaceChildren(h("span", { class: "s2-spinner" }), ` Creating ${name}…`);
      try {
        const res = await opts.mkdir(listing.path, name);
        message.replaceChildren();
        go(res?.path ?? listing.path);
      } catch (e) {
        message.replaceChildren(h("span", { class: "warn" }, `Couldn't create the folder: ${errText(e)}`));
      }
    }
    message.replaceChildren(input, h("button", { class: "s2-btn", onclick: create }, "Create folder"),
      h("button", { class: "s2-btn ghost", onclick: () => message.replaceChildren() }, "Cancel"));
    input.focus();
  }
  // Rename / Delete: the tool of the listing's location; a file's listed modified time lets the server refuse a
  // file changed since (conflict).
  function manageCall(op, item, extra = {}) {
    const viya = listing.location;
    const args = viya ? { location: viya, path: item.path, ...extra } : { path: item.path, kind: opts.kind, ...extra };
    if (!item.folder && item.modified) args.expected_modified = item.modified;
    return S2.callTool(viya ? `viya_files_${op}` : `files_${op}`, args);
  }
  function refused(res, item, verb) {
    const who = [res.modified_by && `by ${res.modified_by}`, res.modified && ago(res.modified)].filter(Boolean).join(", ");
    if (res.exists) warn(`${baseName(res.new_path)} already exists here: pick another name.`);
    else if (res.conflict) warn(`${item.name} changed since the list was read${who ? ` (${who})` : ""}: nothing ${verb}.`,
      h("button", { class: "s2-btn", onclick: () => { message.replaceChildren(); go(listing.path); } }, "Refresh"));
    else if (res.not_empty) warn(`${item.name} isn't empty: only empty folders can be deleted here.`);
    else message.replaceChildren();
  }
  function changed(res, text) {
    message.replaceChildren();
    S2.toast(text, "ok");
    S2.filesChanged({ from: res.from_ref, to: res.renamed ? res.to_ref : null, folder: Boolean(res.folder),
      ...(res.version ? { version: res.version, old_version: res.old_version } : {}) });
    opts.onChanged?.(res);
    go(listing.path);
  }
  function startRename(item) {
    const what = item.folder ? "folder" : "file";
    const input = h("input", { class: "s2-input", value: item.name, spellcheck: false, "aria-label": `New name for ${item.name}`,
      onkeydown: (e) => { if (e.key === "Enter") rename(); if (e.key === "Escape") { e.preventDefault(); message.replaceChildren(); } } });
    async function rename() {
      const name = input.value.trim();
      if (!name || name === item.name) { message.replaceChildren(); return; }
      message.replaceChildren(h("span", { class: "s2-spinner" }), ` Renaming ${item.name}…`);
      try {
        const res = await manageCall("rename", item, { new_name: name });
        if (res.renamed) changed(res, `Renamed ${item.name} to ${baseName(res.new_path)}`);
        else refused(res, item, "renamed");
      } catch (e) { warn(`Couldn't rename ${item.name}: ${errText(e)}`); }
    }
    message.replaceChildren(h("span", { class: "s2-small" }, `Rename ${what} ${item.name} to`), input,
      h("button", { class: "s2-btn primary", onclick: rename }, "Rename"),
      h("button", { class: "s2-btn ghost", onclick: () => message.replaceChildren() }, "Cancel"));
    input.focus();
    const dot = item.folder ? -1 : item.name.lastIndexOf(".");
    try { input.setSelectionRange(0, dot > 0 ? dot : item.name.length); } catch {}
  }
  function startDelete(item) {
    async function remove() {
      message.replaceChildren(h("span", { class: "s2-spinner" }), ` Deleting ${item.name}…`);
      try {
        const res = await manageCall("delete", item);
        if (res.deleted) changed(res, `Deleted ${item.name}`);
        else refused(res, item, "deleted");
      } catch (e) { warn(`Couldn't delete ${item.name}: ${errText(e)}`); }
    }
    const where = listing.label ? ` from ${listing.label}` : "";
    if (!item.folder) {
      const btn = h("button", { class: "s2-btn danger", onclick: remove }, "Delete");
      warn(`Delete ${item.name}${where}? This can't be undone.`, btn);
      btn.focus();
      return;
    }
    // A folder: type its name to confirm (and it must be empty).
    const btn = h("button", { class: "s2-btn danger", disabled: true, onclick: remove }, "Delete folder");
    const input = h("input", { class: "s2-input", spellcheck: false, placeholder: item.name, "aria-label": `Type ${item.name} to confirm`,
      oninput: () => { btn.disabled = input.value !== item.name; },
      onkeydown: (e) => { if (e.key === "Enter" && input.value === item.name) remove(); if (e.key === "Escape") { e.preventDefault(); message.replaceChildren(); } } });
    warn(`Delete the empty folder ${item.name}${where}? Type its name to confirm:`, input, btn);
    input.focus();
  }
  async function save(overwrite) {
    let name = nameInput.value.trim();
    if (!name) return;
    const base = listing.path.endsWith(sep()) ? listing.path : `${listing.path}${sep()}`;
    const full = /^([a-zA-Z]:[\/]|[\/])/.test(name) ? name : `${base}${name}`;
    message.replaceChildren(h("span", { class: "s2-spinner" }), " Saving…");
    try {
      const res = await opts.onSave(full, overwrite, listing);
      if (res?.exists) {
        message.replaceChildren(h("span", { class: "warn" }, `${name} already exists here.`),
          h("button", { class: "s2-btn", onclick: () => save(true) }, "Replace it"),
          h("button", { class: "s2-btn ghost", onclick: () => message.replaceChildren() }, "Pick another name"));
      } else message.replaceChildren();
    } catch (e) {
      message.replaceChildren(h("span", { class: "warn" }, `Couldn't save: ${errText(e)}`));
    }
  }
  function row(btn, item) {
    if (!canManage()) return btn;
    return h("div", { class: "s2-fb-row" }, btn, h("span", { class: "s2-fb-acts" },
      h("button", { class: "s2-btn ghost", title: `Rename ${item.name}`, "aria-label": `Rename ${item.name}`, onclick: () => startRename(item) }, "Rename"),
      h("button", { class: "s2-btn ghost danger", title: item.folder ? `Delete ${item.name} (only an empty folder)` : `Delete ${item.name}`,
        "aria-label": `Delete ${item.name}`, onclick: () => startDelete(item) }, "Delete")));
  }
  function draw() {
    // replaceChildren() detaches the filter and drops its focus: remember it (and the caret) and put it back,
    // so typing in the filter doesn't jump to the file name.
    const filterFocused = document.activeElement === filter;
    const caret = filterFocused ? filter.selectionStart : null;
    const q = filter.value.trim().toLowerCase();
    const match = (n) => !q || n.toLowerCase().includes(q);
    const items = [];
    if (listing?.parent) items.push(h("button", { class: "item", onclick: () => go(listing.parent) }, h("span", { class: "n" }, "↑ .."), h("span", { class: "m" }, "up")));
    for (const f of listing?.folders ?? []) if (match(f.name)) items.push(row(h("button", { class: "item", onclick: () => go(f.path) }, h("span", { class: "n" }, `📁 ${f.name}`), h("span", { class: "m" }, "")), { ...f, folder: true }));
    for (const f of listing?.files ?? []) if (match(f.name)) items.push(row(h("button", { class: "item",
      title: f.path,
      onclick: () => (opts.mode === "open" ? opts.onOpen(f.path, f) : (nameInput.value = f.name, nameInput.focus())),
      ondblclick: () => (opts.mode === "save" ? save(false) : null) },
      h("span", { class: "n" }, `${opts.kind === "notebook" ? "📓" : "📄"} ${f.name}`), h("span", { class: "m" }, [f.size != null ? S2.fmt.bytes(f.size) : null, f.modified ? ago(f.modified) : null].filter(Boolean).join(" · "))), f));
    const empty = opts.empty ?? `No ${opts.kind === "notebook" ? ".ipynb" : ".sql"} files or folders here`;
    if (!items.length) items.push(h("div", { class: "item m" }, error ?? (listing ? (q ? "Nothing matches" : allFiles ? "No files or folders here" : empty) : "Loading…")));
    const shortcuts = (listing?.shortcuts ?? []).map((s) => h("button", { class: `s2-btn ghost${s.path === listing?.path ? " primary" : ""}`, title: s.path, onclick: () => go(s.path) }, s.label));
    box.replaceChildren(...[
      h("div", { class: "row" }, h("strong", {}, opts.mode === "open" ? "Open" : "Save as"), ...shortcuts, ...(opts.mode === "open" ? opts.extra ?? [] : []), h("span", { class: "s2-spacer" }),
        h("button", { class: "s2-btn ghost", onclick: () => opts.onClose?.() }, opts.mode === "open" ? "Close" : "Cancel")),
      h("div", { class: "row" }, h("span", { class: "s2-muted", title: listing?.path ?? "", style: { fontFamily: "var(--s2-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "60%" } }, listing?.path ?? "…"), filter,
        loading && listing ? h("span", { class: "s2-spinner", title: "Loading…" }) : null,
        opts.showAll ? h("label", { class: "s2-small", title: "Show every file, not only the ones this editor opens" },
          h("input", { type: "checkbox", checked: allFiles, onchange: (e) => { allFiles = e.target.checked; go(listing?.path ?? folder); } }), " All files") : null,
        opts.mkdir && listing ? h("button", { class: "s2-btn ghost", title: "Create a folder here", onclick: newFolder }, "New folder") : null),
      h("div", { class: "list" }, ...items),
      nameInput ? h("div", { class: "row" }, nameInput, h("button", { class: "s2-btn primary", onclick: () => save(false) }, "Save"), ...(opts.mode === "save" ? opts.extra ?? [] : [])) : null,
      error && listing ? h("div", { class: "row" }, h("span", { class: "warn" }, error)) : null,
      message].filter(Boolean));
    if (filterFocused) {
      filter.focus();
      try { filter.setSelectionRange(caret, caret); } catch {}
    } else if (nameInput && !box.contains(document.activeElement)) nameInput.focus();
  }
  draw();
  go(folder);
};

// Where a file lives: this computer, SAS Content or the SAS server (the active SAS Viya connection).
S2.FILE_LOCATIONS = [
  { id: "local", label: "This computer", title: "Files on this computer (the SQL folder and your user folder)" },
  { id: "content", label: "SAS Content", title: "SAS Content on the active SAS Viya connection: My Folder, Public and other folders you can see" },
  { id: "server", label: "SAS Server", title: "The SAS Compute server's file system (as in SAS Studio's SAS Server), through the Workspace's SAS session" },
];
// "content" / "server" for a SAS Viya file ref, else null; refPath: the path inside the location.
S2.viyaLocation = (p) => (/^sas-content:\/\//.test(p ?? "") ? "content" : /^sas-server:\/\//.test(p ?? "") ? "server" : null);
S2.refPath = (p) => String(p ?? "").replace(/^sas-(content|server):\/\/[^/]*/, "");

// Open / Save as with a location bar (This computer / SAS Content / SAS Server) above S2.fileBrowser, as in the SQL
// Editor. A file on Viya is named by its ref (sas-content://<connection>/<path>).
//   S2.fileLocations(container, { kind: "sql"|"notebook", mode: "open"|"save", name, start (path or ref of the open
//     file), onOpen(pathOrRef, file), onSave(pathOrRef, overwrite, location) -> {exists}, onClose, onChanged,
//     extra(location) -> [buttons] })
S2.fileLocations = function (box, opts) {
  const { h } = S2;
  const key = `s2-file-loc-${opts.kind}`;
  const dirOf = (p) => (p ? p.replace(/[\\/][^\\/]*$/, "") : null);
  const remembered = () => { try { return localStorage.getItem(key); } catch { return null; } };
  const startLoc = S2.viyaLocation(opts.start) ?? (opts.start ? "local" : null) ?? remembered() ?? "local";
  function show(loc) {
    if (!S2.FILE_LOCATIONS.some((l) => l.id === loc)) loc = "local";
    try { localStorage.setItem(key, loc); } catch {}
    const browser = h("div", { class: "s2-fb-browser" });
    const bar = h("div", { class: "row s2-fb-locs", role: "tablist", "aria-label": "Location" },
      S2.FILE_LOCATIONS.map((l) => h("button", { class: `s2-btn ghost${l.id === loc ? " primary" : ""}`, role: "tab", "aria-selected": String(l.id === loc),
        title: l.title, onclick: () => show(l.id) }, l.label)));
    box.replaceChildren(bar, browser);
    const extra = opts.extra?.(loc) ?? [];
    const common = { kind: opts.kind, mode: opts.mode, name: opts.name, onClose: opts.onClose, onChanged: opts.onChanged };
    if (loc === "local") {
      S2.fileBrowser(browser, { ...common, start: S2.viyaLocation(opts.start) ? null : dirOf(opts.start),
        onOpen: (path, f) => opts.onOpen(path, f), onSave: (path, overwrite) => opts.onSave(path, overwrite, "local"), extra });
      return;
    }
    const exts = opts.kind === "notebook" ? ".ipynb" : ".sql or .sas";
    S2.fileBrowser(browser, {
      ...common, storageKey: `s2-browse-viya-${loc}${opts.kind === "sql" ? "" : `-${opts.kind}`}`,
      start: S2.viyaLocation(opts.start) === loc ? dirOf(S2.refPath(opts.start)) : null,
      showAll: true, empty: `No ${exts} files or folders here`,
      list: (path, all) => S2.callTool("viya_files_browse", { location: loc, path, show_all: all, kind: opts.kind }),
      mkdir: opts.mode === "save" ? (parent, name) => S2.callTool("viya_files_mkdir", { location: loc, parent, name }) : null,
      onOpen: (path, f) => opts.onOpen(f?.ref ?? path, f),
      onSave: (path, overwrite, listing) => opts.onSave(listing.ref_prefix + path, overwrite, loc),
      extra: [...extra, ...(S2.embedded() ? [h("button", { class: "s2-btn ghost", title: "Sign in to SAS Viya or switch the active SAS Viya connection", onclick: () => S2.openView("connections") }, "Connections…")] : [])],
    });
  }
  show(startLoc);
};
