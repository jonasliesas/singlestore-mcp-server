// "Load into this session" / "Unload" for caslib tables (Schema Explorer's CAS source, SQL Editor's CAS tree).
// Server side: sas_workspace.py (cas_load_info / cas_load_table / cas_unload_table / cas_reload_tables) and
// cas_loads.py. table.loadTable into the Workspace's own CAS session, session scope only (never promoted, the caslib
// unchanged): SpeedyStore caslibs (SS) keep the table live in SingleStore, other caslibs copy it into CAS memory.
// Usage: S2CasLoad.open({ caslib, table, connection, onDone(result) }), S2CasLoad.unload({ caslib, table, connection, onDone }),
// S2CasLoad.reloadBar(lost, onDone, connection) – the "load them again" line after the CAS session restarted.
// Inlined as a vendor block, so it runs before shared.js: S2 is read when it's used.
(() => {
  const CL = (globalThis.S2CasLoad = {});
  const CSS = `
.s2-cl-back { position: fixed; inset: 0; z-index: 80; background: rgba(0,0,0,.35); display: flex; align-items: center; justify-content: center; }
.s2-cl { background: var(--s2-bg); color: var(--s2-text); border: 1px solid var(--s2-border); border-radius: var(--s2-radius);
  box-shadow: 0 10px 40px rgba(0,0,0,.3); width: min(620px, 94vw); max-height: 90vh; overflow: auto; display: flex; flex-direction: column; gap: 10px;
  padding: 12px 14px; font-size: var(--s2-fs); }
.s2-cl h2 { display: flex; align-items: center; gap: 8px; margin: 0; font-size: var(--font-text-md-size, 14px); overflow-wrap: anywhere; }
.s2-cl dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0; }
.s2-cl dt { color: var(--s2-text-2); }
.s2-cl dd { margin: 0; overflow-wrap: anywhere; }
.s2-cl label.f { display: flex; flex-direction: column; gap: 3px; }
.s2-cl label.f .s2-input { width: 100%; font-family: var(--s2-mono); font-size: var(--s2-fs-xs); }
.s2-cl .s2-cl-box { border-radius: var(--s2-radius-sm); padding: 8px 10px; }
.s2-cl .s2-cl-box.warn { background: var(--s2-warn-bg); border: 1px solid var(--s2-warn); }
.s2-cl .s2-cl-box.ok { background: var(--s2-ok-bg); border: 1px solid var(--s2-ok); }
.s2-cl .s2-cl-box.info { background: var(--s2-info-bg); border: 1px solid var(--s2-info); }
.s2-cl .s2-cl-foot { display: flex; gap: 8px; align-items: center; justify-content: flex-end; flex-wrap: wrap; border-top: 1px solid var(--s2-border-2); padding-top: 10px; }
.s2-cl .s2-cl-foot .note { margin-right: auto; color: var(--s2-text-2); font-size: var(--s2-fs-xs); }
.s2-cl-reload { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 6px 8px; margin: 4px 0; border: 1px solid var(--s2-info);
  background: var(--s2-info-bg); border-radius: var(--s2-radius-sm); font-size: var(--s2-fs-xs); }
`;
  function style() {
    if (document.getElementById("s2-cl-css")) return;
    const el = document.createElement("style");
    el.id = "s2-cl-css";
    el.textContent = CSS;
    document.head.append(el);
  }
  const errText = (e) => String(e?.message ?? e).replace(/^Error executing tool [\w-]+:\s*/, "");
  const fmtN = (n) => (n == null ? "?" : Number(n).toLocaleString("en-US"));

  CL.open = async function ({ caslib, table, connection = null, onDone = null } = {}) {
    const cx = connection ? { connection } : {};
    const S2 = globalThis.S2;
    const { h } = S2;
    style();
    const st = { info: null, err: null, busy: false, result: null, sure: false, where: "", columns: "" };
    const body = h("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } });
    const foot = h("div", { class: "s2-cl-foot" });
    const panel = h("div", { class: "s2-cl", "aria-labelledby": "s2-cl-title", "data-cas-load": `${caslib}.${table}` },
      h("h2", { id: "s2-cl-title" }, `Load ${caslib}.${table} into this session`, S2.engineChip("cas", S2.connections?.().viya ?? null, {}),
        h("span", { style: { flex: "1" } }),
        h("button", { class: "s2-btn ghost", "aria-label": "Close", title: "Close (Esc)", onclick: () => close() }, "✕")),
      body, foot);
    const back = h("div", { class: "s2-cl-back" }, panel);
    document.body.append(back);
    const closeModal = S2.modal(panel, { label: `Load ${caslib}.${table} into this CAS session`, onClose: () => teardown() });
    let closed = false;
    function teardown() { if (closed) return; closed = true; closeModal(); back.remove(); }
    function close() { if (st.busy) return; teardown(); }
    draw();
    try { st.info = await S2.callTool("cas_load_info", { caslib, table, ...cx }); } catch (e) { st.err = errText(e); }
    draw();

    function draw() {
      if (closed) return;
      if (st.err && !st.info) {
        body.replaceChildren(h("div", { class: "s2-cl-box warn", role: "alert" }, st.err));
        foot.replaceChildren(h("button", { class: "s2-btn", onclick: close }, "Close"));
        return;
      }
      if (!st.info) {
        body.replaceChildren(h("div", {}, h("span", { class: "s2-spinner" }), " Asking CAS about the table…"));
        foot.replaceChildren(h("button", { class: "s2-btn", onclick: close }, "Cancel"));
        return;
      }
      const i = st.info;
      if (st.result) return drawDone();
      const live = i.mode === "live";
      const src = i.source ?? {};
      body.replaceChildren(...[
        h("div", { class: `s2-cl-box ${live ? "ok" : "info"}`, "data-mode": i.mode },
          h("strong", {}, live ? "Live – queries run in SingleStore" : "Copied into CAS memory"), h("div", {}, i.expect)),
        h("dl", {},
          h("dt", {}, "Caslib"), h("dd", {}, `${i.caslib} (${i.type ?? "?"}${i.badge ? ` · ${i.badge}` : ""})`),
          h("dt", {}, "Source"), h("dd", {}, i.path),
          h("dt", {}, "Size"), h("dd", { "data-size": "" }, i.bytes != null ? `${i.size_text}${src.rows != null ? ` · ${fmtN(src.rows)} rows` : ""}${src.columns != null ? ` · ${src.columns} columns` : ""}`
            : src.rows != null ? `${fmtN(src.rows)} rows (CAS doesn't say how many bytes)` : "CAS can't tell (this caslib type doesn't report it)"),
          h("dt", {}, "Scope"), h("dd", {}, "This Workspace's CAS session only: not promoted, gone when the session ends; Unload drops it.")),
        i.loaded ? h("div", { class: "s2-cl-box warn" }, i.loaded.global ? `${i.caslib}.${i.table} is already loaded globally: queries see it as it is (it can't be replaced from here).`
          : `${i.caslib}.${i.table} is loaded in this session already: loading again replaces it.`) : null,
        h("label", { class: "f" }, h("span", {}, "Rows (optional WHERE, CAS / FedSQL syntax)"),
          h("input", { class: "s2-input", "aria-label": "WHERE for the load", placeholder: live ? "SpeedyStore caslibs ignore a WHERE on the load" : "e.g. Origin = 'Asia'",
            value: st.where, disabled: live, spellcheck: false, oninput: (e) => { st.where = e.target.value; } })),
        h("label", { class: "f" }, h("span", {}, "Columns (optional, comma-separated; empty = all)"),
          h("input", { class: "s2-input", "aria-label": "Columns for the load", placeholder: "e.g. Make, Model, MSRP", value: st.columns, spellcheck: false,
            oninput: (e) => { st.columns = e.target.value; } })),
        i.confirm ? h("div", { class: "s2-cl-box warn", role: "alert" }, h("strong", {}, `${i.confirm}.`),
          h("div", {}, h("label", {}, h("input", { type: "checkbox", checked: st.sure, "aria-label": "Load it anyway",
            onchange: (e) => { st.sure = e.target.checked; draw(); } }), " Load it anyway"))) : null,
        st.err ? h("div", { class: "s2-cl-box warn", role: "alert" }, st.err) : null,
      ].filter(Boolean));
      const blocked = i.loaded?.global || (i.confirm && !st.sure) || st.busy;
      foot.replaceChildren(
        h("span", { class: "note" }, "Nothing is written to the caslib's data source."),
        h("button", { class: "s2-btn", disabled: st.busy, onclick: close }, "Cancel"),
        h("button", { class: `s2-btn ${i.confirm ? "danger" : "primary"}`, disabled: blocked, onclick: load },
          st.busy ? h("span", { class: "s2-spinner" }) : null, st.busy ? " Loading…" : i.loaded && !i.loaded.global ? "Load again" : "Load into this session"));
    }
    async function load() {
      st.busy = true; st.err = null; draw();
      try {
        const res = await S2.callTool("cas_load_table", { caslib: st.info.caslib, table: st.info.path, where: st.where.trim() || null,
          columns: st.columns.trim() || null, confirmed: !!st.sure, ...cx });
        if (res.needs_confirmation) { st.info = { ...st.info, ...res, confirm: res.reason }; st.sure = false; }
        else { st.result = res; onDone?.(res); }
      } catch (e) { st.err = errText(e); }
      st.busy = false; draw();
    }
    function drawDone() {
      const r = st.result;
      body.replaceChildren(h("div", { class: `s2-cl-box ${r.live ? "ok" : "info"}`, role: "status", "data-loaded": r.live ? "live" : "copy" },
        h("strong", {}, `${r.caslib}.${r.table} loaded: `), r.message,
        h("div", {}, `${fmtN(r.rows)} rows · ${r.column_count ?? "?"} columns${r.memory ? ` · ${fmtN(r.memory)} bytes in CAS memory` : ""} · ${r.location ?? "?"}`),
        r.where_ignored ? h("div", {}, "The WHERE was not used: SpeedyStore caslibs ignore it on the load (put it in the query).") : null),
        h("div", { class: "s2-small s2-faint" }, `FedSQL in CAS can query it now: select * from ${r.caslib}.${r.table}.`));
      foot.replaceChildren(h("button", { class: "s2-btn primary", onclick: close }, "Close"));
      foot.querySelector("button")?.focus();
    }
  };

  CL.unload = async function ({ caslib, table, connection = null, onDone = null } = {}) {
    const S2 = globalThis.S2;
    try {
      const r = await S2.callTool("cas_unload_table", { caslib, table, ...(connection ? { connection } : {}) });
      S2.toast(r.dropped ? `Unloaded ${caslib}.${table} from this CAS session` : r.note, r.dropped ? "ok" : "");
      onDone?.(r);
    } catch (e) { S2.toast(`Couldn't unload ${caslib}.${table}: ${errText(e)}`, "err"); }
  };

  // After the CAS session restarted: the tables "Load into this session" had loaded, with Load again / Not now.
  CL.reloadBar = function (lost, onDone = null, connection = null) {
    const cx = connection ? { connection } : {};
    const S2 = globalThis.S2;
    const { h } = S2;
    style();
    if (!lost?.length) return null;
    const names = lost.map((x) => `${x.caslib}.${x.table}`);
    const bar = h("div", { class: "s2-cl-reload", role: "status", "data-cas-reload": "" },
      h("span", {}, `The CAS session restarted: ${names.length === 1 ? names[0] : `${names.length} tables`} loaded before ${names.length === 1 ? "is" : "are"} gone`,
        names.length > 1 ? ` (${names.slice(0, 4).join(", ")}${names.length > 4 ? ", …" : ""})` : "", "."),
      h("button", { class: "s2-btn primary", onclick: async (e) => {
        e.currentTarget.disabled = true;
        try {
          const r = await S2.callTool("cas_reload_tables", { ...cx });
          S2.toast(`Loaded ${r.reloaded.length} table(s) again${r.failed.length ? `; ${r.failed.length} failed: ${r.failed[0].error}` : ""}`, r.failed.length ? "err" : "ok");
        } catch (err) { S2.toast(`Couldn't load them again: ${errText(err)}`, "err"); }
        onDone?.();
      } }, "Load again"),
      h("button", { class: "s2-btn ghost", onclick: async () => { try { await S2.callTool("cas_reload_tables", { forget: true, ...cx }); } catch {} onDone?.(); } }, "Not now"));
    return bar;
  };
})();
