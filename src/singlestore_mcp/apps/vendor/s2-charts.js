/* S2Charts: charts for query results, drawn as plain SVG (no library, works offline).
 *
 *   const view = S2Charts.view(columns, rows, { settings, onChange })  -> HTMLElement
 *
 * Bar (grouped / stacked), line, area, scatter and pie. Picks sensible defaults (a date or text column
 * on X, numeric columns on Y), aggregates repeated X values (sum / avg / count / min / max), can split one
 * measure into series by a column, and downloads the chart as SVG. `settings` is kept by the caller so a
 * re-run of the same query keeps the chart; `onChange(settings)` reports every change.
 */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  // Harmonious, clearly distinct neighbours; starts with a SingleStore-like violet.
  const PALETTE = ["#7c3aed", "#0ea5e9", "#f59e0b", "#10b981", "#f43f5e", "#6366f1", "#14b8a6", "#ec4899", "#84cc16", "#64748b"];
  const GRID = "#eef1f4", AXIS = "#c9d1d9", TICK = "#6e7781";
  const MAX_SERIES = 10, MAX_CATEGORIES = 60, MAX_PIE = 10, MAX_POINTS = 5000;

  // ------------------------------------------------------------ small helpers
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "class") n.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid);
    return n;
  };
  const svg = (tag, attrs = {}, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  };
  const num = (v) => {
    if (v == null || v === "") return null;
    if (typeof v === "number") return Number.isFinite(v) ? v : null;
    if (typeof v === "boolean") return v ? 1 : 0;
    const s = String(v).trim();
    return /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(s) ? Number(s) : null;
  };
  const DATE_RE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$/;
  const toTime = (v) => {
    if (v == null) return null;
    const s = String(v).trim();
    if (!DATE_RE.test(s)) return null;
    const t = Date.parse(s.length === 10 ? s + "T00:00:00" : s.replace(" ", "T"));
    return Number.isNaN(t) ? null : t;
  };
  const compact = (v) => {
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(a >= 1e13 ? 0 : 1) + "T";
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "K";
    return Number.isInteger(v) ? String(v) : v.toFixed(a < 1 ? 3 : 2).replace(/\.?0+$/, "");
  };
  const full = (v) => (v == null ? "–" : typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v));
  const dateLabel = (t, span) => {
    const d = new Date(t);
    if (span < 2 * 86400000) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (span < 400 * 86400000) return d.toLocaleDateString([], { month: "short", day: "numeric" });
    return d.toLocaleDateString([], { year: "numeric", month: "short" });
  };
  function niceScale(lo, hi, ticks = 5) {
    if (lo === hi) { hi = lo === 0 ? 1 : lo + Math.abs(lo) * 0.1; lo = lo === 0 ? 0 : lo - Math.abs(lo) * 0.1; }
    const raw = (hi - lo) / ticks;
    const p = 10 ** Math.floor(Math.log10(raw));
    const f = raw / p;
    const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
    return { lo: Math.floor(lo / step) * step, hi: Math.ceil(hi / step) * step, step };
  }

  // ------------------------------------------------------------ column analysis and defaults
  function profile(columns, rows) {
    const sample = rows.slice(0, 2000);
    return columns.map((c) => {
      let n = 0, nums = 0, dates = 0;
      const distinct = new Set();
      for (const r of sample) {
        const v = r[c];
        if (v == null || v === "") continue;
        n++;
        if (num(v) != null) nums++;
        else if (toTime(v) != null) dates++;
        if (distinct.size < 500) distinct.add(String(v));
      }
      const type = n && nums === n ? "number" : n && dates === n ? "date" : "text";
      return { name: c, type, distinct: distinct.size };
    });
  }

  function defaults(cols) {
    const numeric = cols.filter((c) => c.type === "number");
    const date = cols.find((c) => c.type === "date");
    const text = cols.find((c) => c.type === "text");
    // An id-like first numeric column with many values is a poor measure; prefer it as X if nothing else fits.
    const x = date ?? text ?? (numeric.length > 1 ? numeric[0] : null) ?? cols[0];
    const ys = numeric.filter((c) => c !== x).slice(0, 3).map((c) => c.name);
    return {
      type: date ? "line" : x?.type === "number" && numeric.length > 1 ? "scatter" : "bar",
      x: x?.name ?? null, ys, agg: "auto", split: "", stacked: false, sort: "auto",
    };
  }

  // ------------------------------------------------------------ data shaping
  // -> { xKind: "category"|"time"|"number", cats: [x...], series: [{name, values: [v or null per x]}], notes: [] }
  function shape(cols, rows, s) {
    const notes = [];
    const xCol = cols.find((c) => c.name === s.x);
    if (!xCol) return { error: "Pick a column for X." };
    const ys = s.ys.filter((y) => cols.some((c) => c.name === y));
    if (!ys.length && !(s.agg === "count")) return { error: "Pick at least one numeric column for Y (or Count rows)." };
    const xKind = s.type === "pie" || xCol.type === "text" ? "category"
      : xCol.type === "date" ? (s.type === "bar" ? "category" : "time") : (s.type === "bar" ? "category" : "number");
    const keyOf = (v) => (xKind === "time" ? toTime(v) : xKind === "number" ? num(v) : v == null ? "(null)" : String(v));
    // Repeated X values are aggregated; "auto" sums when they repeat (count when there's no Y).
    const counts = new Map();
    for (const r of rows) { const k = keyOf(r[s.x]); counts.set(k, (counts.get(k) ?? 0) + 1); }
    const repeats = [...counts.values()].some((n) => n > 1);
    const agg = s.agg === "auto" ? (ys.length ? (repeats || s.split ? "sum" : "none") : "count") : s.agg;
    if (s.agg === "auto" && agg === "sum" && repeats) notes.push("X values repeat, so Y is summed per X (change it under Aggregate).");
    const measures = agg === "count" ? ["count"] : ys;
    const splitCol = s.split && s.type !== "pie" && s.type !== "scatter" ? s.split : "";
    // group: x -> seriesName -> [values]
    const groups = new Map();
    const order = [];
    for (const r of rows) {
      const k = keyOf(r[s.x]);
      if (k == null) continue;
      if (!groups.has(k)) { groups.set(k, new Map()); order.push(k); }
      const g = groups.get(k);
      for (const m of measures) {
        const name = splitCol ? `${r[splitCol] ?? "(null)"}` + (measures.length > 1 ? ` · ${m}` : "") : m;
        if (!g.has(name)) g.set(name, []);
        g.get(name).push(m === "count" ? 1 : num(r[m]));
      }
    }
    const reduce = (vals) => {
      const v = vals.filter((x) => x != null);
      if (agg === "count") return vals.length;
      if (!v.length) return null;
      if (agg === "none" || agg === "sum") return agg === "none" ? v[v.length - 1] : v.reduce((a, b) => a + b, 0);
      if (agg === "avg") return v.reduce((a, b) => a + b, 0) / v.length;
      if (agg === "min") return Math.min(...v);
      if (agg === "max") return Math.max(...v);
      return v[0];
    };
    if (agg === "none" && repeats && s.type !== "scatter") notes.push("X values repeat; only the last value per X is shown. Choose an aggregate to combine them.");
    let cats = order;
    if (xKind !== "category") cats = [...order].sort((a, b) => a - b);
    const totals = new Map();
    let names = [];
    for (const k of cats) for (const [name, vals] of groups.get(k)) {
      const v = reduce(vals);
      if (!totals.has(name)) { totals.set(name, 0); names.push(name); }
      totals.set(name, totals.get(name) + Math.abs(v ?? 0));
    }
    if (names.length > MAX_SERIES) {
      notes.push(`${names.length} series; showing the ${MAX_SERIES} largest.`);
      names = [...names].sort((a, b) => totals.get(b) - totals.get(a)).slice(0, MAX_SERIES);
    }
    let series = names.map((name) => ({ name, values: cats.map((k) => { const vals = groups.get(k).get(name); return vals ? reduce(vals) : null; }) }));
    // Sorting categories: by value (largest first) or as returned / alphabetically.
    if (xKind === "category") {
      const sortBy = s.sort === "auto" ? (s.type === "pie" || (xCol.type === "text" && !s.split && cats.length > 1 && agg !== "none") ? "value" : "none") : s.sort;
      if (sortBy !== "none") {
        const idx = cats.map((_, i) => i);
        const total = (i) => series.reduce((a, sr) => a + (sr.values[i] ?? 0), 0);
        idx.sort(sortBy === "value" ? (a, b) => total(b) - total(a) : (a, b) => String(cats[a]).localeCompare(String(cats[b]), undefined, { numeric: true }));
        cats = idx.map((i) => cats[i]);
        series = series.map((sr) => ({ ...sr, values: idx.map((i) => sr.values[i]) }));
      }
      const max = s.type === "pie" ? MAX_PIE : MAX_CATEGORIES;
      if (cats.length > max) {
        if (s.type === "pie") {
          const restIdx = cats.map((_, i) => i).slice(max - 1);
          series = series.map((sr) => ({ ...sr, values: [...sr.values.slice(0, max - 1), restIdx.reduce((a, i) => a + (sr.values[i] ?? 0), 0)] }));
          cats = [...cats.slice(0, max - 1), `Other (${restIdx.length})`];
        } else {
          notes.push(`${cats.length} categories; showing the first ${max}.`);
          cats = cats.slice(0, max);
          series = series.map((sr) => ({ ...sr, values: sr.values.slice(0, max) }));
        }
      }
    } else if (cats.length > MAX_POINTS) {
      notes.push(`${cats.length} points; showing the first ${MAX_POINTS}.`);
      cats = cats.slice(0, MAX_POINTS);
      series = series.map((sr) => ({ ...sr, values: sr.values.slice(0, MAX_POINTS) }));
    }
    return { xKind, cats, series, agg, notes };
  }

  // ------------------------------------------------------------ drawing
  function draw(host, data, s, hidden, tip) {
    const W = Math.max(360, host.clientWidth || 720);
    const pie = s.type === "pie";
    const H = pie ? Math.min(420, Math.max(280, W * 0.45)) : Math.min(460, Math.max(260, W * 0.42));
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "font-family": "system-ui, Segoe UI, sans-serif", "font-size": "11" });
    const visible = data.series.map((sr, i) => ({ ...sr, color: PALETTE[i % PALETTE.length] })).filter((sr) => !hidden.has(sr.name));
    root.setAttribute("aria-label", `${s.type} chart of ${data.series.map((x) => x.name).join(", ")} by ${s.x}`);
    if (!visible.length) { root.append(svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle", fill: "currentColor" }, "All series hidden.")); return root; }
    if (pie) return drawPie(root, W, H, data, visible[0], tip);

    const longest = Math.max(...data.cats.map((c) => String(data.xKind === "time" ? "" : c).length), 4);
    const rotate = data.xKind === "category" && data.cats.length > 8 && longest > 4;
    const m = { l: 56, r: 14, t: 12, b: rotate ? Math.min(110, 18 + longest * 5.5) : 30 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    // Y range (stacked bars / areas add up)
    let lo = 0, hi = 0;
    const stacked = s.stacked && (s.type === "bar" || s.type === "area") && visible.length > 1;
    if (stacked) {
      data.cats.forEach((_, i) => {
        let pos = 0, neg = 0;
        for (const sr of visible) { const v = sr.values[i] ?? 0; if (v >= 0) pos += v; else neg += v; }
        hi = Math.max(hi, pos); lo = Math.min(lo, neg);
      });
    } else {
      for (const sr of visible) for (const v of sr.values) if (v != null) { hi = Math.max(hi, v); lo = Math.min(lo, v); }
      if (s.type === "scatter" || s.type === "line") {
        const vals = visible.flatMap((sr) => sr.values.filter((v) => v != null));
        if (vals.length) { const mn = Math.min(...vals), mx = Math.max(...vals); if (mn > 0 && mn > (mx - mn)) lo = mn; else lo = Math.min(0, mn); hi = mx; }
      }
    }
    const ys = niceScale(lo, hi);
    const y = (v) => m.t + ph - ((v - ys.lo) / (ys.hi - ys.lo)) * ph;
    for (let v = ys.lo; v <= ys.hi + ys.step / 2; v += ys.step) {
      root.append(svg("line", { x1: m.l, x2: m.l + pw, y1: y(v), y2: y(v), stroke: Math.abs(v) < 1e-12 ? AXIS : GRID, "stroke-width": 1, "shape-rendering": "crispEdges" }));
      root.append(svg("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end", fill: TICK }, compact(v)));
    }
    // X scale
    let xPos, band = 0;
    if (data.xKind === "category") {
      band = pw / data.cats.length;
      xPos = (i) => m.l + band * (i + 0.5);
      const every = Math.max(1, Math.ceil(data.cats.length / Math.max(1, Math.floor(pw / (rotate ? 14 : 70)))));
      data.cats.forEach((c, i) => {
        if (i % every) return;
        const label = String(c).length > 24 ? String(c).slice(0, 23) + "…" : String(c);
        const t = svg("text", { x: xPos(i), y: H - m.b + 14, "text-anchor": rotate ? "end" : "middle", fill: TICK }, label);
        if (rotate) t.setAttribute("transform", `rotate(-40 ${xPos(i)} ${H - m.b + 10})`);
        root.append(t);
      });
    } else {
      const lo2 = data.cats[0], hi2 = data.cats[data.cats.length - 1];
      const xs = data.xKind === "number" ? niceScale(lo2, hi2, 6) : { lo: lo2, hi: hi2 === lo2 ? lo2 + 1 : hi2 };
      xPos = (i) => m.l + ((data.cats[i] - xs.lo) / ((xs.hi - xs.lo) || 1)) * pw;
      const ticks = data.xKind === "number"
        ? Array.from({ length: Math.round((xs.hi - xs.lo) / xs.step) + 1 }, (_, k) => xs.lo + k * xs.step)
        : Array.from({ length: 6 }, (_, k) => xs.lo + ((xs.hi - xs.lo) * k) / 5);
      for (const t of ticks) {
        const px = m.l + ((t - xs.lo) / ((xs.hi - xs.lo) || 1)) * pw;
        root.append(svg("text", { x: px, y: H - m.b + 16, "text-anchor": "middle", fill: TICK },
          data.xKind === "time" ? dateLabel(t, xs.hi - xs.lo) : compact(t)));
      }
    }
    root.append(svg("line", { x1: m.l, x2: m.l + pw, y1: m.t + ph, y2: m.t + ph, stroke: AXIS, "shape-rendering": "crispEdges" }));

    // series
    if (s.type === "bar") {
      const inner = band * 0.8, gap = band * 0.1;
      const base = new Array(data.cats.length).fill(0), baseNeg = new Array(data.cats.length).fill(0);
      visible.forEach((sr, si) => {
        sr.values.forEach((v, i) => {
          if (v == null) return;
          let x0, w, y0, y1;
          if (stacked) {
            x0 = m.l + band * i + gap; w = inner;
            const b = v >= 0 ? base[i] : baseNeg[i];
            y0 = y(b + v); y1 = y(b);
            if (v >= 0) base[i] += v; else baseNeg[i] += v;
          } else {
            w = inner / visible.length; x0 = m.l + band * i + gap + w * si;
            y0 = y(Math.max(0, v)); y1 = y(Math.min(0, v));
          }
          root.append(svg("rect", { x: x0, y: Math.min(y0, y1), width: Math.max(1, w - 1), height: Math.max(0.5, Math.abs(y1 - y0)), fill: sr.color, rx: 2.5 }));
        });
      });
    } else if (s.type === "line" || s.type === "area") {
      const base = new Array(data.cats.length).fill(0);
      visible.forEach((sr) => {
        let d = "", pen = false;
        const pts = [];
        sr.values.forEach((v, i) => {
          if (v == null) { pen = false; return; }
          const val = stacked ? (base[i] += v) : v;
          pts.push([xPos(i), y(val), i]);
          d += `${pen ? "L" : "M"}${xPos(i).toFixed(1)},${y(val).toFixed(1)}`;
          pen = true;
        });
        if (s.type === "area" && pts.length) {
          const bottom = stacked ? pts.map(([px, , i]) => [px, y(base[i] - (sr.values[i] ?? 0))]).reverse() : [[pts[pts.length - 1][0], y(Math.max(ys.lo, 0))], [pts[0][0], y(Math.max(ys.lo, 0))]];
          const gid = `s2c-g${Math.random().toString(36).slice(2, 8)}`;
          const grad = svg("linearGradient", { id: gid, x1: "0", y1: "0", x2: "0", y2: "1" });
          grad.append(svg("stop", { offset: "0%", "stop-color": sr.color, "stop-opacity": "0.35" }), svg("stop", { offset: "100%", "stop-color": sr.color, "stop-opacity": "0.04" }));
          const defs = svg("defs"); defs.append(grad); root.append(defs);
          root.append(svg("path", { d: `M${pts.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join("L")}L${bottom.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join("L")}Z`, fill: `url(#${gid})`, stroke: "none" }));
        }
        root.append(svg("path", { d, fill: "none", stroke: sr.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
        if (pts.length <= 60) for (const [px, py] of pts) root.append(svg("circle", { cx: px, cy: py, r: 2.6, fill: sr.color }));
      });
    } else if (s.type === "scatter") {
      visible.forEach((sr) => sr.values.forEach((v, i) => {
        if (v != null) root.append(svg("circle", { cx: xPos(i), cy: y(v), r: data.cats.length > 1000 ? 1.8 : 3.2, fill: sr.color, "fill-opacity": 0.7 }));
      }));
    }

    // hover: nearest X shows every visible series' value
    const cross = svg("line", { y1: m.t, y2: m.t + ph, stroke: "#afb8c1", "stroke-dasharray": "3 3", visibility: "hidden" });
    root.append(cross);
    const hit = svg("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent" });
    hit.addEventListener("pointermove", (e) => {
      const rect = root.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * W;
      let best = 0, bd = Infinity;
      for (let i = 0; i < data.cats.length; i++) { const dd = Math.abs(xPos(i) - px); if (dd < bd) { bd = dd; best = i; } }
      cross.setAttribute("x1", xPos(best)); cross.setAttribute("x2", xPos(best)); cross.setAttribute("visibility", "visible");
      const xLabel = data.xKind === "time" ? new Date(data.cats[best]).toLocaleString() : full(data.cats[best]);
      tip.show(e, xLabel, visible.map((sr) => [sr.color, sr.name, sr.values[best]]));
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hide(); });
    root.append(hit);
    return root;
  }

  function drawPie(root, W, H, data, sr, tip) {
    const cx = W / 2, cy = H / 2, r = Math.min(W, H) / 2 - 16, ri = r * 0.55;
    const vals = sr.values.map((v) => Math.max(0, v ?? 0));
    const total = vals.reduce((a, b) => a + b, 0) || 1;
    let a0 = -Math.PI / 2;
    vals.forEach((v, i) => {
      if (!v) return;
      const a1 = a0 + (v / total) * Math.PI * 2;
      const large = a1 - a0 > Math.PI ? 1 : 0;
      const p = (rad, a) => `${(cx + rad * Math.cos(a)).toFixed(2)},${(cy + rad * Math.sin(a)).toFixed(2)}`;
      const color = PALETTE[i % PALETTE.length];
      const d = vals.filter(Boolean).length === 1
        ? `M${cx - r},${cy}A${r},${r} 0 1 1 ${cx + r},${cy}A${r},${r} 0 1 1 ${cx - r},${cy}M${cx - ri},${cy}A${ri},${ri} 0 1 0 ${cx + ri},${cy}A${ri},${ri} 0 1 0 ${cx - ri},${cy}Z`
        : `M${p(r, a0)}A${r},${r} 0 ${large} 1 ${p(r, a1)}L${p(ri, a1)}A${ri},${ri} 0 ${large} 0 ${p(ri, a0)}Z`;
      const slice = svg("path", { d, fill: color, stroke: "#fff", "stroke-width": 1.5, "fill-rule": "evenodd" });
      slice.addEventListener("pointermove", (e) => tip.show(e, full(data.cats[i]), [[color, sr.name, v], [null, "share", `${((v / total) * 100).toFixed(1)} %`]]));
      slice.addEventListener("pointerleave", () => tip.hide());
      root.append(slice);
      if ((a1 - a0) > 0.25) {
        const mid = (a0 + a1) / 2, lr = (r + ri) / 2;
        root.append(svg("text", { x: cx + lr * Math.cos(mid), y: cy + lr * Math.sin(mid) + 4, "text-anchor": "middle", fill: "#fff", "font-weight": "600", "pointer-events": "none" },
          `${Math.round((v / total) * 100)}%`));
      }
      a0 = a1;
    });
    root.append(svg("text", { x: cx, y: cy - 2, "text-anchor": "middle", "font-size": "16", "font-weight": "600", fill: "#1f2328" }, compact(total)));
    root.append(svg("text", { x: cx, y: cy + 15, "text-anchor": "middle", fill: TICK }, sr.name));
    return root;
  }

  // ------------------------------------------------------------ the view
  function view(columns, rows, { settings, onChange } = {}) {
    const cols = profile(columns, rows);
    const sig = columns.join("\u0001");
    let s = settings && settings.sig === sig ? settings : { ...defaults(cols), sig };
    const hidden = new Set();
    const box = el("div", { class: "s2c" });
    const controls = el("div", { class: "s2c-controls" });
    const plot = el("div", { class: "s2c-plot" });
    const legend = el("div", { class: "s2c-legend" });
    const notes = el("div", { class: "s2c-notes" });
    const tipEl = el("div", { class: "s2c-tip", hidden: true });
    const tip = {
      show(e, title, rowsOut) {
        tipEl.replaceChildren(el("div", { class: "s2c-tip-t" }, title),
          ...rowsOut.map(([color, name, v]) => el("div", { class: "s2c-tip-r" },
            color ? el("span", { class: "s2c-key", style: { background: color } }) : el("span", { class: "s2c-key" }),
            el("span", { class: "n" }, name), el("strong", {}, typeof v === "number" ? full(v) : v ?? "–"))));
        tipEl.hidden = false;
        const r = plot.getBoundingClientRect();
        let left = e.clientX - r.left + 14;
        if (left + tipEl.offsetWidth > r.width) left = e.clientX - r.left - tipEl.offsetWidth - 14;
        tipEl.style.left = `${Math.max(0, left)}px`;
        tipEl.style.top = `${Math.max(0, e.clientY - r.top - 10)}px`;
      },
      hide() { tipEl.hidden = true; },
    };
    box.append(controls, el("div", { class: "s2c-stage" }, plot, tipEl), legend, notes);

    const update = (patch) => { s = { ...s, ...patch }; onChange?.(s); render(); };
    const select = (label, value, options, onSel, title) => el("label", { class: "s2c-field", title },
      label, el("select", { onchange: (e) => onSel(e.target.value) },
        options.map(([v, t]) => el("option", { value: v, selected: v === value }, t))));

    function renderControls() {
      const numeric = cols.filter((c) => c.type === "number");
      const yBox = el("span", { class: "s2c-ys" }, numeric.filter((c) => c.name !== s.x).map((c) => el("label", { class: "s2c-chip" },
        el("input", { type: "checkbox", checked: s.ys.includes(c.name), onchange: (e) => {
          const ys = e.target.checked ? [...s.ys, c.name] : s.ys.filter((y) => y !== c.name);
          update({ ys: s.type === "pie" ? ys.slice(-1) : ys });
        } }), c.name)));
      // (replaceChildren would turn null slots into the text "null")
      controls.replaceChildren(...[
        el("span", { class: "s2c-seg", role: "group", "aria-label": "Chart type" },
          [["bar", "Bar"], ["line", "Line"], ["area", "Area"], ["scatter", "Scatter"], ["pie", "Pie"]].map(([t, label]) =>
            el("button", { type: "button", "aria-pressed": String(s.type === t), onclick: () => update({ type: t, ys: t === "pie" ? s.ys.slice(0, 1) : s.ys }) }, label))),
        select("X", s.x, cols.map((c) => [c.name, `${c.name}${c.type === "date" ? " (date)" : c.type === "number" ? " (#)" : ""}`]),
          (x) => update({ x, ys: s.ys.filter((y) => y !== x) })),
        el("span", { class: "s2c-field" }, "Y", numeric.length ? yBox : el("span", { class: "s2c-faint" }, "no numeric columns")),
        select("Aggregate", s.agg, [["auto", "Auto"], ["none", "None"], ["sum", "Sum"], ["avg", "Average"], ["count", "Count rows"], ["min", "Min"], ["max", "Max"]],
          (agg) => update({ agg }), "How to combine rows with the same X value"),
        s.type !== "pie" && s.type !== "scatter"
          ? select("Split by", s.split, [["", "—"], ...cols.filter((c) => c.name !== s.x && c.type !== "number").map((c) => [c.name, c.name])],
              (split) => update({ split }), "One series per value of this column") : null,
        s.type === "bar" || s.type === "area"
          ? el("label", { class: "s2c-field" }, el("input", { type: "checkbox", checked: s.stacked, onchange: (e) => update({ stacked: e.target.checked }) }), "Stacked") : null,
        s.type === "bar" || s.type === "pie"
          ? select("Sort", s.sort, [["auto", "Auto"], ["value", "Largest first"], ["label", "By label"], ["none", "As returned"]], (sort) => update({ sort })) : null,
        el("span", { class: "s2c-spacer" }),
        el("button", { type: "button", class: "s2c-btn", title: "Download the chart as an SVG image", onclick: download }, "Download SVG"),
      ].filter(Boolean));
    }

    let data = null;
    function render() {
      renderControls();
      data = shape(cols, rows, s);
      if (data.error) { plot.replaceChildren(el("div", { class: "s2c-empty" }, data.error)); legend.replaceChildren(); notes.replaceChildren(); return; }
      if (!data.cats.length || !data.series.length) { plot.replaceChildren(el("div", { class: "s2c-empty" }, "Nothing to chart: no rows with a value for X.")); legend.replaceChildren(); notes.replaceChildren(); return; }
      plot.replaceChildren(draw(plot, data, s, hidden, tip));
      const pie = s.type === "pie";
      const items = pie ? data.cats.map((c, i) => [String(c), PALETTE[i % PALETTE.length], false])
        : data.series.map((sr, i) => [sr.name, PALETTE[i % PALETTE.length], true]);
      legend.replaceChildren(...items.map(([name, color, toggle]) => el(toggle ? "button" : "span", {
        type: toggle ? "button" : null, class: "s2c-leg", "aria-pressed": toggle ? String(!hidden.has(name)) : null,
        title: toggle ? "Show / hide this series" : null,
        onclick: toggle ? () => { hidden.has(name) ? hidden.delete(name) : hidden.add(name); render(); } : null,
      }, el("span", { class: "s2c-key", style: { background: color } }), name)));
      const aggNote = data.agg !== "none" && data.agg !== "count" ? `Y = ${data.agg} per ${s.x}` : data.agg === "count" ? `rows per ${s.x}` : null;
      notes.replaceChildren(...[aggNote, ...data.notes, rows.length >= 1000 ? "Charted from the rows shown (the result may be truncated)." : null]
        .filter(Boolean).map((t) => el("span", {}, t)));
    }

    function download() {
      const node = plot.querySelector("svg");
      if (!node) return;
      const copy = node.cloneNode(true);
      copy.setAttribute("xmlns", NS);
      // Legend under the chart, so the image explains itself.
      const W = Number(copy.getAttribute("width")), H = Number(copy.getAttribute("height"));
      const names = s.type === "pie" ? data.cats.map(String) : data.series.filter((sr) => !hidden.has(sr.name)).map((sr) => sr.name);
      let x = 10, y = H + 18;
      const leg = [];
      names.forEach((name, i) => {
        const w = 18 + name.length * 6.5;
        if (x + w > W) { x = 10; y += 18; }
        leg.push(svg("rect", { x, y: y - 9, width: 10, height: 10, rx: 2, fill: PALETTE[(s.type === "pie" ? i : data.series.findIndex((sr) => sr.name === name)) % PALETTE.length] }),
          svg("text", { x: x + 14, y, fill: "#1f2328" }, name));
        x += w + 10;
      });
      const total = y + 10;
      copy.setAttribute("height", total);
      copy.setAttribute("viewBox", `0 0 ${W} ${total}`);
      copy.insertBefore(svg("rect", { width: W, height: total, fill: "#ffffff" }), copy.firstChild);
      for (const n of leg) copy.append(n);
      for (const n of copy.querySelectorAll("rect[fill='transparent'], line[visibility]")) n.remove();
      const text = new XMLSerializer().serializeToString(copy);
      const name = `chart-${s.type}-${(s.x || "x").replace(/[^\w-]+/g, "_")}.svg`;
      if (globalThis.S2?.download) S2.download(name, text, "image/svg+xml");
      else { const a = el("a", { href: URL.createObjectURL(new Blob([text], { type: "image/svg+xml" })), download: name }); document.body.append(a); a.click(); a.remove(); }
    }

    new ResizeObserver(() => { if (data && !data.error) { const node = plot.querySelector("svg"); if (node && Math.abs(Number(node.getAttribute("width")) - plot.clientWidth) > 8) plot.replaceChildren(draw(plot, data, s, hidden, tip)); } }).observe(plot);
    render();
    onChange?.(s);
    return box;
  }

  const css = `
.s2c { display: flex; flex-direction: column; gap: 8px; padding: 8px 10px; min-height: 0; }
.s2c-controls { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; font-size: 12px; }
.s2c-field { display: inline-flex; align-items: center; gap: 5px; color: var(--s2-text-2, #57606a); }
.s2c-field select { font: inherit; padding: 2px 4px; border: 1px solid var(--s2-border, #d0d7de); border-radius: 4px; background: var(--s2-bg, #fff); color: var(--s2-text, #1f2328); max-width: 220px; }
.s2c-ys { display: inline-flex; flex-wrap: wrap; gap: 4px; }
.s2c-chip { display: inline-flex; align-items: center; gap: 3px; border: 1px solid var(--s2-border-2, #d8dee4); border-radius: 10px; padding: 0 7px 0 3px; color: var(--s2-text, #1f2328); }
.s2c-seg { display: inline-flex; border: 1px solid var(--s2-border, #d0d7de); border-radius: 5px; overflow: hidden; }
.s2c-seg button { border: 0; background: transparent; padding: 3px 9px; font: inherit; cursor: pointer; color: var(--s2-text-2, #57606a); }
.s2c-seg button + button { border-left: 1px solid var(--s2-border, #d0d7de); }
.s2c-seg button[aria-pressed="true"] { background: var(--s2-bg-3, #eaeef2); color: var(--s2-text, #1f2328); font-weight: 600; }
.s2c-btn { border: 1px solid var(--s2-border, #d0d7de); background: var(--s2-bg, #fff); color: var(--s2-text, #1f2328); border-radius: 5px; padding: 3px 9px; font: inherit; cursor: pointer; }
.s2c-spacer { flex: 1; }
.s2c-faint { color: var(--s2-text-3, #8c959f); }
.s2c-stage { position: relative; }
.s2c-plot { width: 100%; background: #fff; border-radius: 6px; color: #1f2328; }
.s2c-plot svg { display: block; width: 100%; height: auto; }
.s2c-empty { padding: 40px 10px; text-align: center; color: #57606a; font-size: 13px; }
.s2c-legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; }
.s2c-leg { display: inline-flex; align-items: center; gap: 6px; border: 0; background: none; padding: 0; font: inherit; color: var(--s2-text-2, #57606a); cursor: default; }
button.s2c-leg { cursor: pointer; }
button.s2c-leg[aria-pressed="false"] { text-decoration: line-through; opacity: .5; }
.s2c-key { display: inline-block; width: 10px; height: 10px; border-radius: 2px; flex: none; }
.s2c-notes { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 11px; color: var(--s2-text-3, #8c959f); }
.s2c-tip { position: absolute; pointer-events: none; z-index: 5; background: var(--s2-bg, #fff); color: var(--s2-text, #1f2328); border: 1px solid var(--s2-border, #d0d7de); border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,.15); padding: 6px 9px; font-size: 12px; min-width: 120px; max-width: 320px; }
.s2c-tip[hidden] { display: none; }
.s2c-tip-t { color: var(--s2-text-3, #8c959f); margin-bottom: 3px; }
.s2c-tip-r { display: flex; gap: 7px; align-items: center; line-height: 1.6; }
.s2c-tip-r .n { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
`;
  const style = document.createElement("style");
  style.textContent = css;
  (document.head || document.documentElement).append(style);

  globalThis.S2Charts = { view, profile, defaults, shape };
})();
