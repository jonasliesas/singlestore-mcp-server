// Shared helpers for all SingleStore MCP apps, built on the official
// @modelcontextprotocol/ext-apps `App` class (inlined before this script and
// published as globalThis.McpExtApps). Exposed as globalThis.S2.
const X = globalThis.McpExtApps;
const S2 = (globalThis.S2 = {});

S2.app = null;

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
    onHostContext?.(ctx);
  };
  app.onhostcontextchanged = applyContext;
  if (onToolInput) app.ontoolinput = (p) => onToolInput(p?.arguments ?? {});
  if (onToolResult) app.ontoolresult = (result) => onToolResult(result);
  app.ontoolcancelled = (p) => onToolCancelled?.(p);
  app.onteardown = async () => ({});
  await app.connect(new X.PostMessageTransport(window.parent, window.parent));
  S2.app = app;
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

// Header button; null when the host doesn't offer fullscreen.
S2.fullscreenButton = function () {
  if (!S2.canFullscreen() || S2.inBrowserView()) return null;
  const full = S2.displayMode === "fullscreen";
  return S2.h("button", {
    class: "s2-btn ghost", onclick: () => S2.toggleFullscreen(),
    title: full ? "Exit full screen (Esc)" : "Full screen", "aria-label": full ? "Exit full screen" : "Full screen",
  }, full ? "⤡ Exit full screen" : "⤢ Full screen");
};

async function copyText(text, input) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      input.focus();
      input.select();
      return document.execCommand("copy");
    } catch {
      return false;
    }
  }
}

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
  if (!S2.app || S2.inBrowserView()) return null;
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
  if (!S2.inBrowserView()) S2.updateModelContext(`Browser link for the user's current ${tool} view: ${url}`);
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
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value == null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
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

S2.toast = function (message, kind = "") {
  let host = document.getElementById("s2-toasts");
  if (!host) document.body.append((host = S2.h("div", { id: "s2-toasts" })));
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

S2.errorBox = (err) => S2.h("div", { class: "s2-error" }, err?.message ?? String(err));

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
S2.dataTable = function (columns, rows, opts = {}) {
  const numeric = Object.fromEntries(columns.map((c) => [c, S2.isNumericColumn(rows, c)]));
  let sortCol = null;
  let sortDir = 1;
  const tbody = S2.h("tbody");
  const headCells = columns.map((col) =>
    S2.h("th", {
      class: `sortable${numeric[col] ? " num" : ""}`, title: col,
      onclick: () => { sortDir = sortCol === col ? -sortDir : 1; sortCol = col; draw(); },
    }, col, S2.h("span", { class: "s2-sort" })));
  const compare = (a, b) => {
    const x = a[sortCol], y = b[sortCol];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    if (numeric[sortCol]) return (Number(x) - Number(y)) * sortDir;
    return String(x).localeCompare(String(y), undefined, { numeric: true }) * sortDir;
  };
  function draw() {
    const sorted = sortCol ? [...rows].sort(compare) : rows;
    headCells.forEach((th, i) => {
      th.querySelector(".s2-sort").textContent = columns[i] === sortCol ? (sortDir > 0 ? "▲" : "▼") : "";
    });
    tbody.replaceChildren(...sorted.map((row) => S2.h("tr", {
      style: opts.onRowClick ? { cursor: "pointer" } : null,
      onclick: opts.onRowClick ? () => opts.onRowClick(row) : null,
    }, columns.map((col) => {
      const v = row[col];
      const custom = opts.render?.[col];
      return S2.h("td", { class: `${numeric[col] ? "num" : ""}${v == null ? " null" : ""}`, title: S2.cellText(v) },
        custom ? custom(v, row) : S2.cellText(v));
    }))));
  }
  draw();
  return S2.h("div", { class: "s2-table-wrap", style: { maxHeight: opts.maxHeight ?? "420px" } },
    S2.h("table", { class: "s2-table" }, S2.h("thead", {}, S2.h("tr", {}, headCells)), tbody));
};
