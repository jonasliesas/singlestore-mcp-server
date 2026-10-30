// "Load file into table…" (Schema Explorer, SQL Editor tree): a wizard in a modal dialog. Server side: apps/load_file.py.
// The MCP server runs on this computer and reads the file itself (no browser upload):
//   1. pick a data file (.csv .tsv .txt …) on This computer        load_file_browse
//   2. how to read it, first rows, target (new table + editable    load_file_preview / load_file_target
//      CREATE TABLE, or append with the column mapping)
//   3. confirm: size, rows, where it goes ("Runs on SingleStore · <connection>"); above the size limit the server
//      route (CREATE PIPELINE template) is recommended and "Load anyway" needs a second confirmation   load_file_pipeline
//   4. progress, Cancel, bad rows, row-count check, Open in SQL Editor                   load_file_start / _status / _cancel
// Usage: S2LoadFile.open({ database, table, onDone(status), onOpenSql(sql, database) }). Inlined as a vendor block, so it
// runs before shared.js: S2 is read when the wizard opens.
(() => {
  const LF = (globalThis.S2LoadFile = {});
  const CSS = `
.s2-lf-back { position: fixed; inset: 0; z-index: 80; background: rgba(0,0,0,.35); display: flex; align-items: center; justify-content: center; }
.s2-lf { background: var(--s2-bg); color: var(--s2-text); border: 1px solid var(--s2-border); border-radius: var(--s2-radius);
  box-shadow: 0 10px 40px rgba(0,0,0,.3); width: min(1180px, 94vw); height: 90vh; display: flex; flex-direction: column; padding: 12px 14px; font-size: var(--s2-fs); }
.s2-lf h2 { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; font-size: var(--font-text-md-size, 14px); }
.s2-lf .s2-lf-body { flex: 1; min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 10px; }
.s2-lf .s2-lf-foot { display: flex; gap: 8px; align-items: center; justify-content: flex-end; padding-top: 10px; border-top: 1px solid var(--s2-border-2); margin-top: 8px; flex-wrap: wrap; }
.s2-lf .s2-lf-foot .s2-lf-note { margin-right: auto; color: var(--s2-text-2); font-size: var(--s2-fs-xs); }
.s2-lf .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.s2-lf fieldset { border: 1px solid var(--s2-border-2); border-radius: var(--s2-radius-sm); padding: 8px 10px; margin: 0; }
.s2-lf legend { padding: 0 4px; color: var(--s2-text-2); font-size: var(--s2-fs-xs); }
.s2-lf label { display: inline-flex; align-items: center; gap: 4px; }
.s2-lf .s2-lf-file { font-family: var(--s2-mono); overflow-wrap: anywhere; }
.s2-lf textarea.s2-lf-ddl, .s2-lf textarea.s2-lf-pipe { width: 100%; font-family: var(--s2-mono); font-size: var(--s2-fs-xs); min-height: 170px; resize: vertical; }
.s2-lf .s2-fb { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.s2-lf .s2-fb .list { flex: 1; min-height: 160px; max-height: none; overflow: auto; border: 1px solid var(--s2-border-2); border-radius: var(--s2-radius-sm); }
.s2-lf .s2-fb .item { display: flex; gap: 10px; width: 100%; border: 0; background: none; text-align: left; padding: 4px 8px; cursor: pointer; }
.s2-lf .s2-fb .item:hover { background: var(--s2-bg-2); }
.s2-lf .s2-fb .item .n { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.s2-lf .s2-fb .item .m { color: var(--s2-text-3); font-size: var(--s2-fs-xs); }
.s2-lf .s2-fb .row + .row, .s2-lf .s2-fb .list + .row { margin-top: 6px; }
.s2-lf .s2-lf-box { border-radius: var(--s2-radius-sm); padding: 8px 10px; }
.s2-lf .s2-lf-box.warn { background: var(--s2-warn-bg); color: var(--s2-text); border: 1px solid var(--s2-warn); }
.s2-lf .s2-lf-box.ok { background: var(--s2-ok-bg); border: 1px solid var(--s2-ok); }
.s2-lf .s2-lf-box.info { background: var(--s2-info-bg); border: 1px solid var(--s2-info); }
.s2-lf .s2-lf-box.err { background: var(--s2-err-bg); border: 1px solid var(--s2-err); }
.s2-lf dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0; }
.s2-lf dt { color: var(--s2-text-2); }
.s2-lf dd { margin: 0; overflow-wrap: anywhere; }
.s2-lf progress { width: 100%; height: 14px; }
.s2-lf .s2-lf-mono { font-family: var(--s2-mono); font-size: var(--s2-fs-xs); }
.s2-lf .s2-lf-miss { color: var(--s2-warn); }
.s2-lf .s2-lf-req { color: var(--s2-err); }
`;
  function style() {
    if (document.getElementById("s2-lf-css")) return;
    const el = document.createElement("style");
    el.id = "s2-lf-css";
    el.textContent = CSS;
    document.head.append(el);
  }
  const errText = (e) => String(e?.message ?? e).replace(/^Error executing tool [\w-]+:\s*/, "");
  const PHASES = { starting: "Starting…", connecting: "Connecting to SingleStore…", creating: "Creating the table…",
    checking: "Checking every row on this computer (nothing is sent yet)…", loading: "Loading…", counting: "Checking the row count…" };
  const DELIM_LABEL = { ",": "Comma ,", ";": "Semicolon ;", "\t": "Tab", "|": "Pipe |" };

  LF.open = async function ({ database, table = null, onDone = null, onOpenSql = null } = {}) {
    const S2 = globalThis.S2;
    const { h } = S2;
    style();
    const st = { step: "file", path: null, pv: null, ov: {}, formats: {}, mode: table ? "append" : "new", database, table,
      ddl: null, ddlEdited: false, mapping: null, skip: false, large: false, job: null, pipe: null, seq: 0, busy: false, err: null,
      info: null };
    const conn = () => S2.connections?.().s2 ?? null;
    const body = h("div", { class: "s2-lf-body" });
    const foot = h("div", { class: "s2-lf-foot" });
    const chipSlot = h("span", {});
    const panel = h("div", { class: "s2-lf", "aria-labelledby": "s2-lf-title" },
      h("h2", { id: "s2-lf-title" }, "Load a file into a table", chipSlot, h("span", { class: "s2-spacer", style: { flex: "1" } }),
        h("button", { class: "s2-btn ghost", "aria-label": "Close", title: "Close (Esc)", onclick: () => tryClose() }, "✕")),
      body, foot);
    const back = h("div", { class: "s2-lf-back" }, panel);
    document.body.append(back);
    const running = () => st.job && !st.job.done;
    // Escape while loading doesn't close (the panel sees the key before S2.modal's document listener).
    panel.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && running()) { e.preventDefault(); S2.toast("The file is being loaded: Cancel it first.", "err"); }
    });
    const closeModal = S2.modal(panel, { label: "Load a file into a table", onClose: () => teardown() });
    let closed = false;
    function teardown() { if (closed) return; closed = true; closeModal(); back.remove(); }
    function tryClose() {
      if (running()) { S2.toast("The file is being loaded: Cancel it first.", "err"); return; }
      teardown();
    }
    S2.onConnections?.(() => drawChip());
    function drawChip() { chipSlot.replaceChildren(S2.engineChip("s2", conn(), {})); }
    drawChip();

    try { st.info = await S2.callTool("load_file_info", {}); } catch (e) { st.info = { available: false, reason: errText(e) }; }
    draw();

    // Redraws keep the field being typed in focused (a table name or NULL marker re-reads the preview).
    function draw() {
      if (closed) return;
      const a = document.activeElement;
      const label = a && panel.contains(a) && a.matches("input, select, textarea") ? a.getAttribute("aria-label") : null;
      const caret = label && typeof a.selectionStart === "number" ? [a.selectionStart, a.selectionEnd] : null;
      if (!st.info?.available) drawUnavailable();
      else if (st.step === "file") drawFile();
      else if (st.step === "options") drawOptions();
      else if (st.step === "confirm") drawConfirm();
      else drawProgress();
      if (!label) return;
      const again = [...panel.querySelectorAll("input, select, textarea")].find((el) => el.getAttribute("aria-label") === label);
      if (again && again !== document.activeElement) {
        again.focus();
        if (caret) try { again.setSelectionRange(caret[0], caret[1]); } catch {}
      }
    }

    function drawUnavailable() {
      body.replaceChildren(h("div", { class: "s2-lf-box warn", role: "alert" }, st.info?.reason ?? "Loading a file isn't available here."));
      foot.replaceChildren(h("button", { class: "s2-btn", onclick: teardown }, "Close"));
    }

    // ---- 1. the file
    function drawFile() {
      const box = h("div", { class: "s2-lf-browser", style: { display: "flex", flexDirection: "column", flex: "1", minHeight: "0" } });
      body.replaceChildren(
        h("div", { class: "row" }, h("span", { class: "s2-btn ghost primary", role: "tab", "aria-selected": "true",
          title: "Files on this computer: Studio reads the file here and sends the rows to SingleStore" }, "This computer"),
          h("span", { class: "s2-muted s2-small" }, `Delimited text: ${(st.info.suffixes ?? []).join(" ")}. Target: ${database}${table ? `.${table}` : ""}`)),
        box);
      S2.fileBrowser(box, {
        kind: "data", mode: "open", storageKey: "s2-browse-load", manage: false,
        empty: "No .csv, .tsv or .txt files or folders here",
        list: (path) => S2.callTool("load_file_browse", { path }),
        onOpen: (path) => { st.path = path; st.ov = {}; st.formats = {}; st.ddl = null; st.ddlEdited = false; st.mapping = null; st.step = "options"; refresh(); },
        onClose: teardown,
      });
      foot.replaceChildren(h("span", { class: "s2-lf-note" }, "Pick a file: nothing is read until you do, nothing is written until you confirm."),
        h("button", { class: "s2-btn", onclick: teardown }, "Cancel"));
    }

    // ---- 2. options, preview and target
    let timer = null;
    function later(fn, ms = 350) { clearTimeout(timer); timer = setTimeout(fn, ms); }
    async function refresh() {
      const mine = ++st.seq;
      st.busy = true; st.err = null;
      draw();
      try {
        const args = { path: st.path, database, table: st.table, ...st.ov };
        if (Object.keys(st.formats).length) args.formats = st.formats;
        const pv = await S2.callTool("load_file_preview", args);
        if (mine !== st.seq) return;
        st.pv = pv;
        if (!st.table) st.table = pv.suggested_table;
        const exists = Boolean(pv.target?.exists);
        st.mode = exists ? "append" : "new";
        if (!st.ddlEdited || !st.ddl) st.ddl = pv.create_sql;
        st.mapping = null;
      } catch (e) {
        if (mine !== st.seq) return;
        st.err = errText(e);
      }
      st.busy = false;
      draw();
    }
    async function refreshTarget() {
      // Mapping changes only: the table's columns against the file's.
      const mine = ++st.seq;
      try {
        const t = await S2.callTool("load_file_target", { database, table: st.table, columns: st.pv.columns, mapping: st.mapping });
        if (mine !== st.seq) return;
        st.pv = { ...st.pv, target: t };
      } catch (e) { if (mine === st.seq) st.err = errText(e); }
      draw();
    }
    function setOv(key, value) { st.ov[key] = value; later(refresh); }

    function readOptions(pv) {
      const o = pv.options;
      const encs = pv.encodings ?? {};
      const enc = h("select", { class: "s2-input", "aria-label": "Encoding", value: o.encoding, onchange: (e) => setOv("encoding", e.target.value) },
        Object.entries(encs).map(([k, v]) => h("option", { value: k }, `${v}${k === pv.detected.encoding ? " (detected)" : ""}`)));
      const known = Object.keys(DELIM_LABEL);
      const delim = h("select", { class: "s2-input", "aria-label": "Delimiter", value: known.includes(o.delimiter) ? o.delimiter : "other",
        onchange: (e) => { if (e.target.value !== "other") setOv("delimiter", e.target.value); else { other.hidden = false; other.focus(); } } },
        known.map((k) => h("option", { value: k }, `${DELIM_LABEL[k]}${k === pv.detected.delimiter ? " (detected)" : ""}`)), h("option", { value: "other" }, "Other…"));
      const other = h("input", { class: "s2-input", style: { width: "4em" }, maxlength: 1, "aria-label": "Other delimiter", hidden: known.includes(o.delimiter),
        value: known.includes(o.delimiter) ? "" : o.delimiter, oninput: (e) => { if (e.target.value.length === 1) setOv("delimiter", e.target.value); } });
      const quote = h("select", { class: "s2-input", "aria-label": "Quote", value: o.quote ?? "", onchange: (e) => setOv("quote", e.target.value) },
        h("option", { value: '"' }, 'Double quote "'), h("option", { value: "'" }, "Single quote '"), h("option", { value: "" }, "None"));
      const markers = (o.null_markers ?? []);
      const emptyNull = h("input", { type: "checkbox", checked: markers.includes(""), "aria-label": "Empty value is NULL",
        onchange: (e) => setOv("null_markers", [...(e.target.checked ? [""] : []), ...markers.filter((m) => m !== "")]) });
      const others = h("input", { class: "s2-input", style: { width: "12em" }, "aria-label": "Other null markers", placeholder: "e.g. . NULL NA",
        value: markers.filter((m) => m !== "").join(" "), title: "Values that mean NULL, separated by spaces (SAS writes . for a missing number)",
        oninput: (e) => setOv("null_markers", [...(emptyNull.checked ? [""] : []), ...e.target.value.split(/\s+/).filter(Boolean)]) });
      return h("fieldset", {}, h("legend", {}, "How to read the file"),
        h("div", { class: "row" },
          h("label", {}, "Encoding ", enc), h("label", {}, "Delimiter ", delim, other), h("label", {}, "Quote ", quote),
          h("label", { title: "The first line holds the column names" }, h("input", { type: "checkbox", checked: o.header, "aria-label": "First line is a header",
            onchange: (e) => setOv("header", e.target.checked) }), "First line is a header"),
          h("label", {}, emptyNull, "Empty = NULL"), h("label", {}, "NULL markers ", others),
          h("label", { title: "Numbers like 12,5 (Excel in Norway, Germany …): the comma is the decimal point" },
            h("input", { type: "checkbox", checked: o.decimal_comma, "aria-label": "Decimal comma", onchange: (e) => setOv("decimal_comma", e.target.checked) }), "Decimal comma")),
        h("div", { class: "s2-small s2-muted" }, `Detected: ${pv.detected.encoding_note}. `,
          pv.replaced_chars ? h("span", { class: "s2-lf-miss" }, `${pv.replaced_chars} character(s) can't be read as ${encs[o.encoding]}: try Windows-1252. `) : null,
          pv.bad_width_in_sample ? h("span", { class: "s2-lf-miss" }, `${pv.bad_width_in_sample} row(s) in the sample don't have ${pv.width} fields. `) : null));
    }

    function columnsBox(pv) {
      const rows = pv.columns.map((c) => {
        const fmts = (pv.formats ?? []).filter((f) => f.kind === c.kind).map((f) => f.name);
        const fmt = ["date", "datetime", "time"].includes(c.kind)
          ? h("select", { class: "s2-input", "aria-label": `Format of ${c.name}`, value: st.formats[c.name] ?? c.format,
              onchange: (e) => { st.formats[c.name] = e.target.value; later(refresh, 50); } },
              fmts.map((f) => h("option", { value: f }, `${f}${(c.format_choices ?? []).includes(f) ? "" : " (doesn't fit the sample)"}`)))
          : h("span", { class: "s2-muted" }, "–");
        return h("tr", {}, h("td", {}, c.name), h("td", { class: "s2-muted" }, c.source !== c.name ? c.source : ""), h("td", { class: "s2-lf-mono" }, c.sql_type),
          h("td", {}, fmt, (c.format_choices ?? []).length > 1 ? h("span", { class: "s2-lf-miss", title: (c.format_choices ?? []).join(", ") }, " ambiguous: check") : null),
          h("td", { class: "num" }, c.nulls ? S2.fmt.number(c.nulls) : ""));
      });
      return h("details", { open: true }, h("summary", {}, `Columns (${pv.columns.length}) – types suggested from the first ${S2.fmt.number(pv.sample_rows)} row(s)`),
        h("div", { class: "s2-table-wrap", style: { maxHeight: "200px" } }, h("table", { class: "s2-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Header text"), h("th", {}, "Suggested type"), h("th", {}, "Date / time format"), h("th", { class: "num" }, "NULLs in sample"))),
          h("tbody", {}, rows))));
    }

    function previewBox(pv) {
      const names = pv.columns.map((c) => c.name);
      const objs = pv.rows.map((r) => Object.fromEntries(names.map((n, i) => [n, r[i] ?? null])));
      return h("details", { open: true }, h("summary", {}, `First ${S2.fmt.number(pv.rows.length)} row(s), as read`),
        S2.dataTable(names, objs, { maxHeight: "220px", data: true }));
    }

    function targetBox(pv) {
      const t = pv.target ?? {};
      const name = h("input", { class: "s2-input", value: st.table ?? "", "aria-label": "Table name", spellcheck: false,
        oninput: (e) => { st.table = e.target.value.trim(); if (!st.ddlEdited) st.ddl = null; later(refresh, 500); } });
      const head = h("div", { class: "row" }, h("strong", {}, "Into "), h("span", { class: "s2-lf-mono" }, `${database}.`), name,
        st.busy ? h("span", { class: "s2-spinner" }) : null,
        h("span", { class: `s2-badge ${t.exists ? "info" : "plain"}` }, t.exists ? "existing table: rows are added" : "new table"));
      if (t.error) return h("fieldset", {}, h("legend", {}, "Target"), head, h("div", { class: "s2-lf-box err" }, t.error));
      if (!t.exists) {
        const ta = h("textarea", { class: "s2-input s2-lf-ddl", spellcheck: false, "aria-label": "CREATE TABLE script", value: st.ddl ?? "",
          oninput: (e) => { st.ddl = e.target.value; st.ddlEdited = true; reset.hidden = false; } });
        const reset = h("button", { class: "s2-btn ghost", hidden: !st.ddlEdited, onclick: () => { st.ddlEdited = false; st.ddl = pv.create_sql; draw(); } }, "Back to the suggestion");
        return h("fieldset", {}, h("legend", {}, "Target"), head,
          h("div", { class: "row s2-small s2-muted" }, "Columnstore (SORT KEY", pv.sort_key ? ` on ${pv.sort_key}` : " – no date column", "), ",
            pv.shard_key ? `SHARD KEY on ${pv.shard_key} (unique in the sample)` : "keyless sharding (no unique id column found)",
            ". Edit the script as you like: the columns are matched by name when loading.", reset),
          ta);
      }
      const m = t.mapping ?? { pairs: [], skipped_source: [], required_missing: [] };
      const srcNames = pv.columns.map((c) => c.name);
      const rows = m.pairs.map((p) => h("tr", {},
        h("td", {}, p.target), h("td", { class: "s2-lf-mono" }, p.target_type),
        h("td", {}, h("select", { class: "s2-input", "aria-label": `File column for ${p.target}`, value: p.source ?? "",
          onchange: (e) => { st.mapping = Object.fromEntries(m.pairs.map((x) => [x.target, x.source])); st.mapping[p.target] = e.target.value || null; refreshTarget(); } },
          h("option", { value: "" }, "— not loaded (default / NULL) —"), srcNames.map((n) => h("option", { value: n }, n)))),
        h("td", {}, p.required ? h("span", { class: "s2-lf-req" }, "required: NOT NULL without a default") : !p.source ? h("span", { class: "s2-lf-miss" }, "no file column") : "")));
      return h("fieldset", {}, h("legend", {}, "Target"), head,
        h("div", { class: "s2-small s2-muted" }, `${t.columns?.length ?? 0} column(s); file columns are matched by name (any case).`),
        h("div", { class: "s2-table-wrap", style: { maxHeight: "220px" } }, h("table", { class: "s2-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Table column"), h("th", {}, "Type"), h("th", {}, "From file column"), h("th", {}, ""))),
          h("tbody", {}, rows))),
        m.skipped_source.length ? h("div", { class: "s2-small s2-lf-miss" }, `File column(s) not loaded: ${m.skipped_source.join(", ")}`) : null);
    }

    function canGoOn() {
      const pv = st.pv, t = pv?.target;
      if (!pv || st.busy || !st.table || !t || t.error) return false;
      if (t.exists) return Boolean(t.mapping?.pairs?.some((p) => p.source)) && !t.mapping.required_missing.length;
      return Boolean(st.ddl?.trim());
    }

    function drawOptions() {
      const pv = st.pv;
      const fileLine = h("div", { class: "row" }, h("span", { class: "s2-lf-file" }, st.path),
        pv ? h("span", { class: "s2-muted" }, `${S2.fmt.bytes(pv.size)} · about ${S2.fmt.number(pv.estimated_rows)} row(s)`) : null,
        h("button", { class: "s2-btn ghost", onclick: () => { st.step = "file"; st.pv = null; draw(); } }, "Other file…"));
      if (!pv) {
        body.replaceChildren(fileLine, st.err ? h("div", { class: "s2-lf-box err", role: "alert" }, st.err)
          : h("div", { class: "s2-empty" }, h("span", { class: "s2-spinner" }), " Reading the start of the file…"));
      } else {
        body.replaceChildren(...[fileLine, st.err ? h("div", { class: "s2-lf-box err", role: "alert" }, st.err) : null,
          readOptions(pv), previewBox(pv), columnsBox(pv), targetBox(pv)].filter(Boolean));
      }
      const next = h("button", { class: "s2-btn primary", disabled: !canGoOn(), onclick: () => { st.step = "confirm"; st.skip = false; st.large = false; st.pipe = null; draw(); } }, "Next: review…");
      foot.replaceChildren(h("span", { class: "s2-lf-note" }, "Nothing has been written yet."),
        h("button", { class: "s2-btn", onclick: teardown }, "Cancel"), next);
    }

    // ---- 3. confirm
    async function showPipeline(kind) {
      const pv = st.pv;
      try {
        const r = await S2.callTool("load_file_pipeline", { database, table: st.table, columns: pv.columns.map((c) => ({ ...c, format: st.formats[c.name] ?? c.format })),
          options: pv.options, file_name: pv.name, create_sql: pv.target?.exists ? null : st.ddl });
        st.pipe = { kind, sql: r[kind] };
      } catch (e) { st.pipe = { kind, sql: `-- ${errText(e)}` }; }
      draw();
    }
    function openSql(sql) {
      if (onOpenSql) { onOpenSql(sql, database); return; }
      if (S2.embedded()) { S2.openView("sql:new", { sql, database }); return; }
      S2.copy(sql, "SQL copied: paste it in the SQL Editor");
    }
    function drawConfirm() {
      const pv = st.pv, t = pv.target;
      const over = pv.over_limit;
      const mb = S2.fmt.bytes(pv.size);
      const skip = h("label", {}, h("input", { type: "checkbox", checked: st.skip, onchange: (e) => { st.skip = e.target.checked; } }),
        "Skip rows that can't be loaded (they're listed afterwards); otherwise any bad row stops the load before anything is written");
      const facts = h("dl", {},
        h("dt", {}, "File"), h("dd", { class: "s2-lf-file" }, st.path),
        h("dt", {}, "Size"), h("dd", {}, `${mb} (${S2.fmt.number(pv.size)} bytes)`),
        h("dt", {}, "Rows"), h("dd", {}, `about ${S2.fmt.number(pv.estimated_rows)}${pv.whole_file_in_sample ? "" : " (estimated from the start of the file)"}`),
        h("dt", {}, "Read as"), h("dd", {}, `${pv.encodings[pv.options.encoding]}, ${DELIM_LABEL[pv.options.delimiter] ?? JSON.stringify(pv.options.delimiter)}${pv.options.header ? ", header line" : ""}`),
        h("dt", {}, "Runs on"), h("dd", {}, S2.engineChip("s2", conn(), {})),
        h("dt", {}, "Into"), h("dd", {}, h("span", { class: "s2-lf-mono" }, `${database}.${st.table}`), " ",
          h("span", { class: `s2-badge ${t.exists ? "info" : "plain"}` }, t.exists ? "add to the existing table" : "new table (CREATE TABLE first)")),
        h("dt", {}, "How"), h("dd", {}, "LOAD DATA LOCAL INFILE, all or nothing (INSERTs in one transaction if the cluster doesn't allow local infile)"));
      const parts = [
        h("div", { class: "s2-lf-box info" }, h("strong", {}, "The data goes from this computer to SingleStore. "),
          "Studio reads the file here and sends its rows over your network connection to the cluster."),
        facts, skip];
      if (over) {
        const anyway = h("input", { type: "checkbox", checked: st.large, "aria-label": "Load it through this computer anyway",
          onchange: (e) => { st.large = e.target.checked; draw(); } });
        parts.push(h("div", { class: "s2-lf-box warn", role: "alert" },
          h("div", {}, h("strong", {}, `${mb} is over the ${pv.limit_mb} MB limit for loading through this computer. `),
            "Recommended: the server route – upload the file to S3 or Azure blob storage and let a pipeline load it on the cluster's side."),
          h("div", { class: "row", style: { marginTop: "6px" } },
            h("button", { class: "s2-btn", onclick: () => showPipeline("s3") }, "Pipeline template: S3"),
            h("button", { class: "s2-btn", onclick: () => showPipeline("azure") }, "Pipeline template: Azure blob")),
          st.pipe ? h("div", {}, h("textarea", { class: "s2-input s2-lf-pipe", readonly: true, spellcheck: false, "aria-label": "CREATE PIPELINE template", value: st.pipe.sql }),
            h("div", { class: "row" }, h("button", { class: "s2-btn", onclick: () => S2.copy(st.pipe.sql, "Pipeline template copied") }, "Copy"),
              h("button", { class: "s2-btn", onclick: () => openSql(st.pipe.sql) }, "Open in SQL Editor"),
              h("span", { class: "s2-small s2-muted" }, "Fill in the <placeholders> yourself: no credentials are put in here."))) : null,
          h("label", { style: { marginTop: "6px" } }, anyway, `I understand: load ${mb} through this computer anyway`)));
      }
      body.replaceChildren(...parts);
      const go = h("button", { class: "s2-btn primary", disabled: over && !st.large, onclick: () => start(st.skip) },
        over ? "Load anyway" : `Load ${mb} into ${st.table}`);
      foot.replaceChildren(h("span", { class: "s2-lf-note" }, "This is the confirmation: nothing is written before you click Load."),
        h("button", { class: "s2-btn", onclick: () => { st.step = "options"; draw(); } }, "Back"), go);
      go.focus();
    }

    // ---- 4. load and follow it
    async function start(skipBad) {
      const pv = st.pv;
      st.step = "progress";
      st.cancelling = false;
      st.job = { phase: "starting", done: false };
      draw();
      try {
        const formats = Object.fromEntries(pv.columns.filter((c) => c.format || st.formats[c.name]).map((c) => [c.name, st.formats[c.name] ?? c.format]));
        st.job = await S2.callTool("load_file_start", {
          path: st.path, database, table: st.table, mode: pv.target.exists ? "append" : "new", options: pv.options, columns: pv.columns,
          width: pv.width, expected_size: pv.size, confirmed: true, confirmed_large: Boolean(pv.over_limit && st.large),
          create_sql: pv.target.exists ? null : st.ddl, mapping: pv.target.exists ? st.mapping : null, formats, skip_bad: Boolean(skipBad) });
      } catch (e) {
        st.job = { phase: "error", done: true, error: errText(e), result: {} };
        draw();
        return;
      }
      poll();
    }
    async function poll() {
      while (st.job && !st.job.done && !closed) {
        try { st.job = await S2.callTool("load_file_status", { job_id: st.job.job_id, wait_ms: 800 }); }
        catch (e) { await new Promise((r) => setTimeout(r, 1500)); }
        draw();
      }
      if (st.job?.phase === "done") onDone?.(st.job);
    }
    function badList(j) {
      if (!j.bad?.length) return null;
      const rows = j.bad.map((b) => ({ Line: b.line, Reason: b.reason, Where: b.where === "cluster" ? "SingleStore" : "file", Text: b.text ?? "" }));
      return h("details", { open: true }, h("summary", {}, `${S2.fmt.number(j.bad_count)} bad row(s)${j.bad_count > j.bad.length ? ` (first ${j.bad.length} shown)` : ""}`),
        S2.dataTable(["Line", "Reason", "Where", "Text"], rows, { maxHeight: "240px", data: true }));
    }
    function drawProgress() {
      const j = st.job ?? {};
      const mbDone = j.phase === "checking" ? j.bytes_done : j.phase === "loading" && j.rows_total ? (j.total_bytes * j.rows_sent) / j.rows_total : null;
      const parts = [h("div", { class: "row" }, h("span", { class: "s2-lf-file" }, st.path), "→", h("span", { class: "s2-lf-mono" }, `${database}.${st.table}`))];
      const foot2 = [];
      if (!j.done) {
        parts.push(h("div", { role: "status" }, h("span", { class: "s2-spinner" }), " ", PHASES[j.phase] ?? "Working…"),
          h("progress", { max: 100, value: j.pct ?? 0, "aria-label": "Progress" }),
          h("div", { class: "s2-small s2-muted" }, [
            j.pct != null ? `${j.pct} %` : null,
            j.phase === "checking" ? `${S2.fmt.number(j.rows_read)} row(s) read` : j.phase === "loading" ? `${S2.fmt.number(j.rows_sent)} of ${S2.fmt.number(j.rows_total)} row(s) sent` : null,
            mbDone != null ? `${S2.fmt.bytes(mbDone)} of ${S2.fmt.bytes(j.total_bytes)}` : null,
            j.bad_count ? `${S2.fmt.number(j.bad_count)} bad row(s) so far` : null,
            j.elapsed_s != null ? S2.fmt.duration(j.elapsed_s) : null].filter(Boolean).join(" · ")),
          j.message ? h("div", { class: "s2-small" }, j.message) : null);
        foot2.push(h("button", { class: "s2-btn danger", disabled: !j.job_id || st.cancelling, onclick: async () => {
          st.cancelling = true; draw();
          try { await S2.callTool("load_file_cancel", { job_id: j.job_id }); } catch (e) { S2.toast(errText(e), "err"); }
        } }, st.cancelling ? "Cancelling…" : "Cancel"));
      } else if (j.phase === "done") {
        const r = j.result ?? {};
        parts.push(h("div", { class: "s2-lf-box ok", role: "status" }, h("strong", {}, "✓ "), j.message),
          h("dl", {}, h("dt", {}, "Method"), h("dd", {}, j.method === "insert" ? "batched INSERTs in one transaction" : "LOAD DATA LOCAL INFILE"),
            h("dt", {}, "Rows now"), h("dd", {}, `${S2.fmt.number(r.count_after)}${r.count_before ? ` (was ${S2.fmt.number(r.count_before)})` : ""} – `,
              r.check_ok ? h("span", { class: "s2-badge ok" }, "row count checked") : h("span", { class: "s2-badge warn" }, "row count differs")),
            h("dt", {}, "Time"), h("dd", {}, S2.fmt.duration(j.elapsed_s))),
          badList(j));
        foot2.push(h("button", { class: "s2-btn", onclick: () => openSql(r.sql) }, "Open in SQL Editor"));
      } else if (j.phase === "bad_rows") {
        parts.push(h("div", { class: "s2-lf-box warn", role: "alert" }, j.message), badList(j));
        foot2.push(h("button", { class: "s2-btn", onclick: () => { st.job = null; st.step = "options"; draw(); } }, "Back to the options"),
          h("button", { class: "s2-btn primary", onclick: () => start(true) }, `Skip these ${S2.fmt.number(j.bad_count)} row(s) and load the rest`));
      } else if (j.phase === "cancelled") {
        parts.push(h("div", { class: "s2-lf-box info", role: "status" }, j.message ?? "Cancelled."));
        foot2.push(h("button", { class: "s2-btn", onclick: () => { st.job = null; st.step = "confirm"; draw(); } }, "Back"));
      } else if (j.phase === "uncertain") {
        parts.push(h("div", { class: "s2-lf-box warn", role: "alert" }, j.error));
        foot2.push(h("button", { class: "s2-btn", onclick: () => openSql(`SELECT COUNT(*) FROM \`${database}\`.\`${st.table}\`;`) }, "Count the rows in the SQL Editor"));
      } else {
        parts.push(h("div", { class: "s2-lf-box err", role: "alert" }, j.error ?? "The load failed.",
          j.result?.table_removed ? " The new table was removed again." : ""), badList(j));
        foot2.push(h("button", { class: "s2-btn", onclick: () => { st.job = null; st.step = "options"; draw(); } }, "Back to the options"));
      }
      body.replaceChildren(...parts.filter(Boolean));
      foot.replaceChildren(h("span", { class: "s2-lf-note" }, j.done ? "" : "Closing waits for the load: Cancel stops it, and nothing is kept."),
        ...foot2, h("button", { class: "s2-btn", disabled: !j.done, onclick: teardown }, "Close"));
    }
  };
})();
