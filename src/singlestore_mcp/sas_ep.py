"""What drives the runtime of a SAS Embedded Process (EP) call, for the Query History's tuning panel.

Verified on SingleStore 9.0 with SAS EP:

- SAS calls ``SELECT status, message FROM <db>.sasepFunction('RUNDATASTEP … inputDatabase="…" inputTable="…"
  outputDatabase="…" outputTable="…" externalFunction="h…_<db>"')``. ``sasepFunction`` is an EXTERNAL FUNCTION
  ``AS COLLOCATED SERVICE '/opt/sas/sasep.sock'`` (SHOW CREATE FUNCTION), so EXPLAIN shows only an opaque
  ``GeneratedTable``, and the call's plan cache statistics don't include the time spent in the EP.
- The EP then runs a generated statement: ``CREATE TABLE <out> AS SELECT sasepin.… , sasepout.… FROM <in> AS sasepin
  JOIN <db>.h…(sasepin.…) AS sasepout`` — a join with a per-job function that is dropped afterwards. Like any
  distributed statement it runs on the leaves, one task per partition, so the largest partition and the number of
  partitions per leaf core shape the elapsed time.
- ``EP.sasep…`` tables (``INSERT INTO EP.sasepv5 …`` / ``DELETE FROM EP.sasepv5 …``) are the EP's bookkeeping.
- SAS VA / CAS queries use per-job EP *format functions*: ``… FROM (SELECT * FROM db.t) AS t JOIN
  EP.h<32 hex>_EP(cols) AS ep``, also inside CTEs, and for CAS parallel loads one statement per partition
  (``… WHERE partition_id() = N``). The function's name changes every run (history_store.normalize ignores it),
  so each run is a new plan, used once, and soon gone from the plan cache.
- On this cluster most *traced* EP calls of both kinds take 5.0–5.4 s whatever they read: a fixed wait, which the card
  points out by comparing a run with every other EP call in the history. Measured on 9.0 (2026-10, read-only):
  the same sasepFunction job takes ~0.24 s on some days and ~5.25 s on others (a +5.0 s step); in the slow runs the
  EP sends its generated statement 5.2 s after the call started and everything after that takes < 50 ms; the plan
  cache counts the 5.25 s as execution time but only ~3 ms CPU and 0 queued; concurrent EP calls that started up to
  3.6 s apart finish within ~0.2 s of each other (one shared wait). The trace keeps only calls over its threshold
  (1 s here), so fast EP calls are missing from it: the plan cache's EP.sasepv5 registrations count all of them.

Sources: TABLE_STATISTICS (rows per master partition), COLUMNAR_SEGMENTS (compressed bytes per partition),
DISTRIBUTED_PARTITIONS (partitions per leaf), MV_SYSINFO_CPU (leaf cores, with the container's CFS quota),
SHOW CREATE TABLE (shard / sort key), OPTIMIZER_STATISTICS (column cardinality), MV_TRACE_EVENTS and MV_PLANCACHE
(the generated statement, earlier runs). All read-only; nothing here runs the EP (and it's never PROFILEd).
"""

from __future__ import annotations

import bisect
import datetime as _dt
import re
import statistics
from typing import Any

from . import history_store
from .db import db, quote_identifier

MARKER = "/* s2-query-history */"

_CALL = re.compile(r"sasepFunction`?\s*\(\s*'(.*)'\s*\)", re.S | re.I)
_PARAM = re.compile(r"(\w+)\s*=\s*\\?\"((?:[^\"\\]|\\.)*?)\\?\"")
_BOOKKEEPING = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+`?EP`?\s*\.\s*`?sasep", re.I)
_SECRET = re.compile(r"password|secret|token", re.I)


# SAS VA / CAS queries call per-job EP format functions: … FROM (SELECT * FROM db.t) AS t JOIN EP.h<hex>_EP(cols) AS ep
_FORMAT_FN = re.compile(r"`?EP`?\s*\.\s*`?(h[0-9A-F]{32}_EP)`?\s*\(([^()]*)\)", re.I)
_PARTITION = re.compile(r"\bpartition_id\(\)\s*=\s*(\d+)", re.I)
_CLAUSE_END = re.compile(r"(GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|WINDOW|OPTION)\b", re.I)
_FILTER_COL = re.compile(r"(?:`?\w+`?\.)?`?([A-Za-z_]\w*)`?\s*(?:=|<>|!=|<=|>=|<|>|\bIN\b|\bBETWEEN\b|\bLIKE\b|\bIS\b)", re.I)


def _where_clauses(text: str) -> list[str]:
    """Bodies of the statement's WHERE clauses (parentheses balanced), without trivial "1 = 1" parts."""
    out = []
    for m in re.finditer(r"\bWHERE\b", text, re.I):
        i, depth, start = m.end(), 0, m.end()
        while i < len(text):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and _CLAUSE_END.match(text, i) and (i == 0 or not text[i - 1].isalnum()):
                break
            i += 1
        body = " ".join(text[start:i].split())
        body = re.sub(r"(\bAND\s+)?\b1\s*=\s*1\b(\s+AND\b)?", " ", body, flags=re.I).strip()
        if body and not re.fullmatch(r"\(?\s*\)?", body):
            out.append(body[:300])
    return out


def _filter_columns(filters: list[str]) -> tuple[set[str], bool]:
    """(columns the filters use, whether a column sits inside a function such as COALESCE(col, ''))."""
    cols, wrapped = set(), False
    for f in filters:
        q = re.findall(r"\b[A-Za-z_]\w*\.`?([A-Za-z_]\w*)`?", f)
        cols.update(q or _FILTER_COL.findall(f))
        wrapped |= bool(re.search(r"\b(COALESCE|IFNULL|NVL|UPPER|LOWER|TRIM|SUBSTR\w*|DATE\w*|CAST)\s*\(\s*(?:\w+\.)?`?\w+", f, re.I))
    return cols, wrapped


def parse(sql: str | None, database: str | None = None) -> dict[str, Any] | None:
    """What kind of SAS EP statement this is, or None.

    {"kind": "job", "command", "params"} for sasepFunction calls (passwords left out); {"kind": "format",
    "functions": [{"name", "args"}], "inputs": ["db.table"], "partition": N | None, "filters": [...]} for SAS VA / CAS
    queries joining with per-job EP format functions; {"bookkeeping": True} for EP's own EP.sasep… tables."""
    text = history_store.strip_comments(sql or "")
    m = _CALL.search(sql or "")
    if m:
        body = m.group(1)
        command = (re.match(r"\s*(\w+)", body) or [None, None])[1]
        params = {k: v for k, v in _PARAM.findall(body) if not _SECRET.search(k)}
        return {"kind": "job", "command": command, "params": params}
    fns = [{"name": f.group(1), "args": [a.strip(" `") for a in f.group(2).split(",") if a.strip(" `")]}
           for f in _FORMAT_FN.finditer(text)]
    if fns:
        inputs = [t for t in history_store.tables_in(text, database)
                  if not t["write"] and (t["database"] or "").upper() != "EP"]
        filters = _where_clauses(_PARTITION.sub(" 1 = 1 ", text))
        p = _PARTITION.search(text)
        return {"kind": "format", "functions": fns,
                "inputs": list(dict.fromkeys(f"{t['database']}.{t['table']}" for t in inputs if t["database"])),
                "partition": int(p.group(1)) if p else None, "filters": filters}
    return {"bookkeeping": True} if _BOOKKEEPING.search(text) else None


def masked(sql: str) -> str:
    """The statement with password-like parameters replaced (for display)."""
    return re.sub(r"(\w*(?:password|secret|token)\w*\s*=\s*\\?\")((?:[^\"\\]|\\.)*?)(\\?\")", r"\1***\3", sql or "", flags=re.I)


def _q(sql: str, params: tuple[Any, ...] = (), database: str | None = None) -> list[dict[str, Any]]:
    return db.execute(f"{MARKER} {sql}", params, database=database)[1]


def _safe(fn: Any) -> Any:
    def run() -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - each point reports what it couldn't read
            return {"error": str(exc)[:200]}
    run.__name__ = fn.__name__
    return run


def _verdict(level: str, text: str, step: str | None = None) -> dict[str, Any]:
    return {"level": level, "text": text, **({"step": step} if step else {})}


def _fmt_s(ms: float) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _fmt_b(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _cores(r: dict[str, Any]) -> float:
    quota, period = int(r.get("CFS_QUOTA_NS") or 0), int(r.get("CFS_PERIOD_NS") or 0)
    if quota > 0 and period > 0:
        return round(quota / period, 1)
    return float(r.get("NUM_CPUS") or 0)


def _table_calls(in_db: str, in_table: str) -> list[Any]:
    """Read-only queries about the input table, run in one parallel batch."""
    def partitions() -> Any:
        return _q("SELECT ORDINAL, HOST, ROWS, MEMORY_USE, STORAGE_TYPE FROM information_schema.TABLE_STATISTICS"
                  " WHERE DATABASE_NAME = %s AND TABLE_NAME = %s AND PARTITION_TYPE = 'Master' ORDER BY ORDINAL",
                  (in_db, in_table))

    def sizes() -> Any:
        return _q("SELECT PARTITION AS p, NODE_ID AS node, SUM(COMPRESSED_SIZE) AS bytes FROM information_schema.COLUMNAR_SEGMENTS"
                  " WHERE DATABASE_NAME = %s AND TABLE_NAME = %s GROUP BY 1, 2", (in_db, in_table))

    def layout() -> Any:
        return _q("SELECT ORDINAL, NODE_ID, HOST FROM information_schema.DISTRIBUTED_PARTITIONS"
                  " WHERE DATABASE_NAME = %s AND ROLE = 'Master'", (in_db,))

    def ddl() -> Any:
        r = _q(f"SHOW CREATE TABLE {quote_identifier(in_table)}", database=in_db)[0]
        return r.get("Create Table") or next(iter(r.values()))

    def columns() -> Any:
        return _q("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                  (in_db, in_table))

    def cardinality() -> Any:
        return _q("SELECT COLUMN_NAME AS c, COALESCE(CARDINALITY, AUTOSTATS_CARDINALITY) AS n FROM"
                  " information_schema.OPTIMIZER_STATISTICS WHERE DATABASE_NAME = %s AND TABLE_NAME = %s"
                  " AND (JSON_KEY IS NULL OR JSON_KEY = '')", (in_db, in_table))

    return [partitions, sizes, layout, ddl, columns, cardinality]


def cpus() -> Any:
    return _q("SELECT NODE_ID, IP_ADDR, NUM_CPUS, CFS_QUOTA_NS, CFS_PERIOD_NS FROM information_schema.MV_SYSINFO_CPU"
              " WHERE TYPE = 'LEAF'")


def peers() -> Any:
    """Every SAS EP call (both kinds) in the cluster's query history and the local copy of it, for the "fixed wait",
    "shared wait" and rows-vs-time comparisons: [{"start", "end" (epoch s), "ms", "rows", "ok", "kind", "db", "user"}]."""
    live = live_calls()
    try:
        stored = stored_calls()
    except Exception:  # noqa: BLE001 - the live trace alone still compares
        stored = []
    return merge_calls(live, stored)


def live_calls() -> list[dict[str, Any]]:
    """EP calls in the cluster's query history (MV_TRACE_EVENTS), with their exact start times."""
    rows = _q("SELECT TIME, DETAILS::$start_time AS st, DETAILS::%%duration_ms AS ms, DETAILS::%%row_count AS n,"
              " DETAILS::%%success AS ok, DETAILS::$context_database AS db, DETAILS::$user_name AS u,"
              " LEFT(DETAILS::$query_text, 4000) AS q FROM information_schema.MV_TRACE_EVENTS WHERE EVENT_TYPE = 'Query_completion'"
              " AND (DETAILS::$query_text LIKE %s OR DETAILS::$query_text REGEXP %s)"
              " AND DETAILS::$query_text NOT LIKE %s",
              ("%sasepFunction%", "EP[.]h[0-9A-Fa-f]{32}_EP", f"%{history_store.MARKER_PREFIX}%"))
    live = []
    for r in rows:
        text = history_store._text(r["q"])
        # The WHERE matched the whole text; only the first 4000 characters are read, so the EP function of a long VA
        # statement may lie beyond them: anything without sasepFunction is a format call.
        if _METADATA.match(text):
            continue
        kind = kind_of(text)
        if kind not in ("job", "format"):
            kind = "job" if "sasepfunction" in text.lower() else "format"
        end, ms = epoch(r["TIME"]), float(r["ms"] or 0)
        live.append({"start": epoch(r["st"]) or (end - ms / 1000), "end": end, "ms": ms,
                     "rows": None if r["n"] is None else int(r["n"]), "ok": r["ok"] not in (0, False),
                     "kind": kind, "db": r["db"] or None, "user": r["u"],
                     "stmt": history_store.statement_key(text, r["db"] or None)})
    return live


# SHOW CREATE … / DESCRIBE of an EP function: metadata, not an EP call.
_METADATA = re.compile(r"\s*(?:SHOW|DESC|DESCRIBE)\b", re.I)


def kind_of(sql: str | None) -> str | None:
    """"job" (sasepFunction), "format" (VA / CAS EP.h…_EP function), "bookkeeping" (EP.sasep… tables) or None."""
    if not sql:
        return None
    if re.search(r"sasepFunction`?\s*\(", sql, re.I):
        return "job"
    if re.search(r"`?EP`?\s*\.\s*`?h[0-9A-F]{32}_EP`?\s*\(", sql, re.I):
        return "format"
    if _BOOKKEEPING.search(sql):
        return "bookkeeping"
    return None


def epoch(t: Any) -> float:
    """Seconds since the epoch for a trace time ("2026-10-14T12:13:29.476228", "2026-10-14 12:13:24.230128",
    or a datetime); 0.0 if unreadable. Naive times are read as UTC so differences stay exact."""
    if t is None:
        return 0.0
    if isinstance(t, _dt.datetime):
        d = t
    else:
        try:
            d = _dt.datetime.fromisoformat(str(t).strip().replace(" ", "T")[:26])
        except ValueError:
            return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def stored_calls(since: str | None = None) -> list[dict[str, Any]]:
    """EP calls kept in the local history store (traced runs; the store has no start time, so start = end − ms)."""
    conn = history_store.connect()
    try:
        cond, params = ("AND finished >= ?", [since]) if since else ("", [])
        rows = conn.execute(f"SELECT finished, ms, rows, ok, database, user, sample, stmt, shape FROM runs"
                            f" WHERE source = ? AND origin = 'SAS EP' {cond}", (history_store.source_id(), *params)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if _METADATA.match(r["sample"] or ""):
            continue
        kind = kind_of(r["sample"])
        if kind not in ("job", "format"):
            # The sample is cut at 300 characters: an EP statement whose function comes later is still a format call.
            kind = "format" if "sasepfunction" not in (r["sample"] or "").lower() else "job"
        end = epoch(r["finished"])
        out.append({"start": end - float(r["ms"]) / 1000, "end": end, "ms": float(r["ms"]), "rows": r["rows"],
                    "ok": bool(r["ok"]), "kind": kind, "db": r["database"], "user": r["user"], "stmt": r["stmt"]})
    return out


def merge_calls(live: list[dict[str, Any]], stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live trace rows (with exact start times) plus older stored ones, without counting a run twice."""
    seen = {(round(c["end"], 2), round(c["ms"])) for c in live}
    out = list(live)
    for c in stored:
        if (round(c["end"], 2), round(c["ms"])) not in seen:
            out.append(c)
    out.sort(key=lambda c: c["start"])
    return out


# ------------------------------------------------------------------ fixed wait, shared wait, rows vs time
# Pure functions over a list of calls, so they can be unit tested without a cluster.

PLATEAU_TOLERANCE = 0.04    # a call is on the plateau when it is within ±4 % of the plateau's centre
PLATEAU_MIN_SHARE = 0.3     # … and the plateau holds at least 30 % of the calls
PLATEAU_MIN_CALLS = 3
PLATEAU_FLOOR_MS = 1000     # only waits of a second or more count as a "fixed wait"


def plateau(values: list[float], min_share: float = PLATEAU_MIN_SHARE, min_calls: int = PLATEAU_MIN_CALLS,
            tolerance: float = PLATEAU_TOLERANCE) -> dict[str, Any] | None:
    """The most common runtime band of a set of calls when many take (almost) the same time, else None.

    {"center_ms", "low_ms", "high_ms", "count", "share", "total", "warm_ms" (median of the calls faster than the
    band, or None), "fixed_ms" (estimated fixed wait: centre − warm median, or the centre when there's no faster call)}."""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    best: tuple[int, float] = (0, 0.0)
    for v in vals:
        if v < PLATEAU_FLOOR_MS:
            continue
        n = bisect.bisect_right(vals, v * (1 + tolerance)) - bisect.bisect_left(vals, v * (1 - tolerance))
        if n > best[0]:
            best = (n, v)
    if not best[0]:
        return None
    first = [x for x in vals if abs(x - best[1]) <= tolerance * best[1]]
    center = statistics.median(first)
    members = [x for x in vals if abs(x - center) <= tolerance * center]
    share = len(members) / len(vals)
    if len(members) < min_calls or share < min_share:
        return None
    low, high = min(members), max(members)
    faster = [x for x in vals if x < low]
    warm = statistics.median(faster) if faster else None
    return {"center_ms": round(center), "low_ms": round(low), "high_ms": round(high), "count": len(members),
            "share": round(share, 3), "total": len(vals), "warm_ms": round(warm) if warm is not None else None,
            "fixed_ms": round(center - warm) if warm is not None else round(center)}


def calls_plateau(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """plateau() over calls, with the warm baseline taken only from calls that didn't overlap a plateau call: a call
    that joined a shared wait late is short because it waited less, not because the EP was warm."""
    ok = [c for c in calls if c.get("ok", True)]
    pl = plateau([c["ms"] for c in ok])
    if not pl:
        return None
    slow = [c for c in ok if on_plateau(c["ms"], pl) and c.get("start") and c.get("end")]
    alone = [c["ms"] for c in ok if c["ms"] < pl["low_ms"]
             and not any(s["start"] < c.get("end", 0) and s["end"] > c.get("start", 0) for s in slow)]
    warm = statistics.median(alone) if alone else None
    return {**pl, "warm_ms": round(warm) if warm is not None else None, "warm_calls": len(alone),
            "fixed_ms": round(pl["center_ms"] - warm) if warm is not None else pl["center_ms"]}


def on_plateau(ms: float, p: dict[str, Any] | None, tolerance: float = PLATEAU_TOLERANCE) -> bool:
    return bool(p and ms and abs(ms - p["center_ms"]) <= tolerance * p["center_ms"])


def bursts(calls: list[dict[str, Any]], min_calls: int = 2) -> list[dict[str, Any]]:
    """Calls that overlapped in time, grouped: {"calls", "start", "end", "start_spread_s", "end_spread_s", "shared",
    "members"}. "shared": they started at least a second apart but finished within half a second of each other, so
    they waited for one shared step rather than each for its own."""
    groups: list[list[dict[str, Any]]] = []
    for c in sorted((c for c in calls if c.get("start") and c.get("end")), key=lambda c: c["start"]):
        if groups and c["start"] < max(x["end"] for x in groups[-1]):
            groups[-1].append(c)
        else:
            groups.append([c])
    out = []
    for g in groups:
        if len(g) < min_calls:
            continue
        starts, ends = [x["start"] for x in g], [x["end"] for x in g]
        ss, es = max(starts) - min(starts), max(ends) - min(ends)
        out.append({"calls": len(g), "start": min(starts), "end": max(ends), "start_spread_s": round(ss, 2),
                    "end_spread_s": round(es, 2), "shared": ss >= 1.0 and es <= 0.5, "members": g})
    return out


def rows_fit(calls: list[dict[str, Any]], min_calls: int = 5) -> dict[str, Any] | None:
    """Least-squares fit of runtime on rows returned: {"intercept_ms", "per_1000_rows_ms", "r2", "n", "max_rows"},
    or None when there are too few calls or the row counts don't vary."""
    pts = [(float(c["rows"]), float(c["ms"])) for c in calls if c.get("rows") is not None and c.get("ok", True) and c.get("ms")]
    if len(pts) < min_calls or len({x for x, _ in pts}) < 3:
        return None
    n = len(pts)
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if not sxx:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    intercept = my - slope * mx
    syy = sum((y - my) ** 2 for _, y in pts)
    r2 = (slope * slope * sxx / syy) if syy else 0.0
    return {"intercept_ms": round(intercept), "per_1000_rows_ms": round(slope * 1000, 2), "r2": round(r2, 2), "n": n,
            "max_rows": int(max(x for x, _ in pts))}


def histogram(values: list[float], width_ms: int = 250, max_buckets: int = 14) -> list[dict[str, Any]]:
    """Non-empty runtime buckets ({"lo_ms", "n"}), at most ``max_buckets`` (the rest folded into the last)."""
    counts: dict[int, int] = {}
    for v in values:
        b = int(v // width_ms) * width_ms
        counts[b] = counts.get(b, 0) + 1
    out = [{"lo_ms": k, "n": n} for k, n in sorted(counts.items())]
    if len(out) > max_buckets:
        out = out[: max_buckets - 1] + [{"lo_ms": out[max_buckets - 1]["lo_ms"], "n": sum(b["n"] for b in out[max_buckets - 1:]),
                                         "more": True}]
    return out


def analyze(event: dict[str, Any], plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """The SAS EP card for one statement (a history event or a plan cache entry), or None if it isn't EP."""
    sql = event.get("query_text") or ""
    info = parse(sql, event.get("context_database") or None)
    if info is None:
        return None
    if info.get("bookkeeping"):
        return {"bookkeeping": True, "summary": "SAS EP bookkeeping: the EP registers or removes the function it "
                "generates for a job in its own EP.sasep… table. These statements are small and quick; the work is "
                "in the sasepFunction call and the statement it generates."}
    if info["kind"] == "format":
        return _analyze_format(event, plan, info)
    p = info["params"]
    in_db = p.get("inputDatabase") or event.get("context_database")
    in_table = p.get("inputTable")
    out_table = p.get("outputTable")
    ms = float(event.get("duration_ms") or 0)
    card: dict[str, Any] = {"kind": "job", "kind_label": "sasepFunction job", "command": info["command"],
                            "params": {k: v for k, v in p.items() if k != "externalFunction"},
                            "input": f"{in_db}.{in_table}" if in_db and in_table else None,
                            "output": f"{p.get('outputDatabase') or in_db}.{out_table}" if out_table else None,
                            "duration_ms": ms, "points": []}
    if not (in_db and in_table):
        card["note"] = "The call names no input table, so the table-based checks don't apply."

    call_end = epoch(event.get("finished"))
    call_start = epoch(event.get("start_time")) or (call_end - ms / 1000 if call_end else 0.0)

    def generated() -> Any:
        # The statement the EP generated for this job (CREATE TABLE <out> AS SELECT … FROM <in> AS sasepin JOIN …).
        # Traced when it ran ≥ the threshold or failed; the one inside this call's time window tells where the time went.
        rows = _q("SELECT TIME, DETAILS::$start_time AS st, DETAILS::$query_text AS q, DETAILS::%%duration_ms AS ms,"
                  " DETAILS::%%success AS ok, DETAILS::$error_code AS err FROM information_schema.MV_TRACE_EVENTS"
                  " WHERE EVENT_TYPE = 'Query_completion' AND DETAILS::$query_text LIKE %s AND DETAILS::$query_text LIKE %s"
                  " AND DETAILS::$query_text NOT LIKE %s ORDER BY TIME DESC LIMIT 40",
                  ("%AS sasepin%", f"%{in_table}%", f"%{history_store.MARKER_PREFIX}%"))
        found = [r for r in rows if re.search(rf"FROM\s+`?{re.escape(in_db or '')}`?\s*\.\s*`?{re.escape(in_table)}`?\s+AS\s+sasepin",
                                              history_store._text(r["q"]), re.I)]
        inside = [r for r in found if call_start and call_start - 1 <= epoch(r["TIME"]) <= call_end + 1]
        if found:
            r = (inside or found)[0]
            g_end, g_ms = epoch(r["TIME"]), float(r["ms"] or 0)
            return {"sql": history_store._text(r["q"]), "source": "trace", "in_call": bool(inside),
                    "start": epoch(r["st"]) or g_end - g_ms / 1000, "end": g_end, "ms": g_ms,
                    "ok": r["ok"] not in (0, False), "error": r["err"] or None}
        rows = _q("SELECT LEFT(QUERY_TEXT, 8000) AS q FROM information_schema.MV_PLANCACHE WHERE QUERY_TEXT LIKE %s"
                  " AND QUERY_TEXT LIKE %s LIMIT 5", ("%AS sasepin%", f"%{in_table}%"))
        for r in rows:
            q = history_store._text(r["q"])
            if re.search(rf"`?{re.escape(in_table)}`?\s+AS\s+sasepin", q, re.I):
                return {"sql": q, "source": "plan cache"}
        return None

    def trend() -> Any:
        rows = _q("SELECT TIME, DETAILS::%%duration_ms AS ms, DETAILS::%%success AS ok FROM information_schema.MV_TRACE_EVENTS"
                  " WHERE EVENT_TYPE = 'Query_completion' AND DETAILS::$query_text LIKE %s AND DETAILS::$query_text LIKE %s"
                  " AND DETAILS::$query_text NOT LIKE %s ORDER BY TIME",
                  ("%sasepFunction%", f"%inputTable=%{in_table}%", f"%{history_store.MARKER_PREFIX}%"))
        return [{"t": str(r["TIME"]), "ms": float(r["ms"] or 0), "ok": r["ok"] not in (0, False)} for r in rows]

    calls = [*_table_calls(in_db, in_table), generated, trend] if in_db and in_table else []
    results = dict(zip([c.__name__ for c in [*calls, cpus, peers]], db.parallel(*[_safe(c) for c in [*calls, cpus, peers]])))
    all_calls = results.get("peers") if isinstance(results.get("peers"), list) else []
    pl = calls_plateau(all_calls)
    gen = results.get("generated") if isinstance(results.get("generated"), dict) else None
    this = {"start": call_start, "end": call_end, "ms": ms, "rows": event.get("row_count"), "kind": "job"}
    card["points"].append(breakdown(ms, plan, pl, gen if gen and gen.get("in_call") else None, call_start))
    if gen and gen.get("in_call") and not gen.get("ok"):
        card["points"].append(_generated_failed(gen, p))
    card["points"].append(_peers_point(all_calls, ms, pl))
    card["points"].append(_burst_point(all_calls, this))
    if in_db and in_table:
        card["points"].append(_skew(results, ms))
        card["points"].append(_cores_point(results))
        card["points"].append(_data_in(results, in_db, in_table))
    card["points"].append(_waits(plan, ms))
    if in_db and in_table:
        card["points"].append(_trend(results.get("trend")))
    card["tips"] = tips(card, pl)
    return card


def _analyze_format(event: dict[str, Any], plan: dict[str, Any] | None, info: dict[str, Any]) -> dict[str, Any]:
    """SAS VA / CAS query with per-job EP format functions."""
    ms = float(event.get("duration_ms") or 0)
    db_ = event.get("context_database") or None
    first = info["inputs"][0] if info["inputs"] else None
    in_db, _, in_table = (first or "").rpartition(".")
    card: dict[str, Any] = {
        "kind": "format", "kind_label": "VA / CAS format function", "command": None,
        "functions": info["functions"], "input": first, "inputs": info["inputs"], "output": None,
        "partition": info["partition"], "duration_ms": ms, "points": [],
    }
    key = history_store.statement_key(event.get("query_text"), db_)
    finished = str(event.get("finished") or "")[:19].replace("T", " ")

    def runs() -> Any:
        # Earlier runs of the same statement: the function name changes every run, statement_key ignores it.
        rows = _q("SELECT TIME, DETAILS::%%duration_ms AS ms, DETAILS::%%success AS ok, DETAILS::$query_text AS q"
                  " FROM information_schema.MV_TRACE_EVENTS WHERE EVENT_TYPE = 'Query_completion'"
                  " AND DETAILS::$query_text REGEXP %s AND DETAILS::$query_text LIKE %s AND DETAILS::$query_text NOT LIKE %s"
                  " AND DETAILS::$context_database <=> %s ORDER BY TIME LIMIT 3000",
                  ("EP[.]h[0-9A-Fa-f]{32}_EP", f"%{in_table or ''}%", f"%{history_store.MARKER_PREFIX}%", db_))
        out = []
        for r in rows:
            q = history_store._text(r["q"])
            if history_store.statement_key(q, db_) == key:
                p = _PARTITION.search(q)
                out.append({"t": str(r["TIME"])[:26], "ms": float(r["ms"] or 0), "ok": r["ok"] not in (0, False),
                            "partition": int(p.group(1)) if p else None})
        return out

    calls = [*(_table_calls(in_db, in_table) if in_table else []), runs]
    results = dict(zip([c.__name__ for c in [*calls, cpus, peers]], db.parallel(*[_safe(c) for c in [*calls, cpus, peers]])))
    if not first:
        card["note"] = "No input table could be read from the statement, so the table-based checks don't apply."
    traced = results.get("runs") if isinstance(results.get("runs"), list) else []
    all_calls = results.get("peers") if isinstance(results.get("peers"), list) else []
    pl = calls_plateau(all_calls)
    end = epoch(event.get("finished"))
    this = {"start": epoch(event.get("start_time")) or (end - ms / 1000 if end else 0.0), "end": end, "ms": ms,
            "rows": event.get("row_count"), "kind": "format"}
    card["points"].append(breakdown(ms, plan, pl, None, this["start"]))
    card["points"].append(_peers_point(all_calls, ms, pl))
    card["points"].append(_burst_point(all_calls, this))
    if info["partition"] is not None:
        card["points"].append(_partition_spread(traced, finished, results))
    if first:
        card["points"].append(_skew(results, ms))
        card["points"].append(_cores_point(results))
        card["points"].append(_data_format(results, info))
    card["points"].append(_waits(plan, ms))
    loads = _loads(traced) if info["partition"] is not None else traced
    card["points"].append(_trend(loads))
    card["tips"] = tips(card, pl)
    return card


def _loads(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-partition statements of one CAS load (same statement, finished within 30 s) as one run: the slowest."""
    out: list[dict[str, Any]] = []
    for r in runs:
        if out and abs(_secs(r["t"]) - _secs(out[-1]["t"])) <= 30:
            if r["ms"] > out[-1]["ms"]:
                out[-1] = {**r, "t": out[-1]["t"]}
            continue
        out.append(dict(r))
    return out


def _secs(t: str) -> float:
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(t[:19]).timestamp()
    except ValueError:
        return 0.0


def _partition_spread(runs: list[dict[str, Any]], finished: str, r: dict[str, Any]) -> dict[str, Any]:
    """CAS parallel load: one statement per partition (WHERE partition_id() = N). Their runtimes side by side."""
    point: dict[str, Any] = {"key": "spread", "title": "Runtime per partition (this load)"}
    at = _secs(finished)
    load = [x for x in runs if x["partition"] is not None and at and abs(_secs(x["t"]) - at) <= 30]
    total = len(r["partitions"]) if isinstance(r.get("partitions"), list) else None
    if not load:
        point["verdict"] = _verdict("info", "The other partitions' statements of this load aren't in the query history.")
        return point
    by: dict[int, float] = {}
    for x in load:
        by[x["partition"]] = max(by.get(x["partition"], 0), x["ms"])
    point["partitions"] = [{"partition": k, "ms": v} for k, v in sorted(by.items())]
    times = list(by.values())
    lo, hi, avg = min(times), max(times), sum(times) / len(times)
    missing = (total - len(by)) if total else None
    point["value"] = (f"{len(by)}{f' of {total}' if total else ''} partitions traced: {_fmt_s(lo)} to {_fmt_s(hi)}")
    note = (f" {missing} partition statement{'s' if missing != 1 else ''} aren't in the history: they ran under the trace "
            "threshold (fast) or the trace has moved on." if missing else "")
    if len(by) >= 2 and avg and hi / avg > 1.5:
        point["verdict"] = _verdict("crit" if hi / avg > 2 else "warn",
                                    f"The slowest partition took {hi / avg:.1f}× the average: the load waits for it." + note,
                                    "That's skew in the input (see below): reshard it on a column with many distinct values.")
    else:
        point["verdict"] = _verdict("ok" if len(by) >= 2 else "info",
                                    ("The partitions took about the same time: no skew in this load." if len(by) >= 2 else
                                     "Only one partition's statement is in the history.") + note)
    return point


def _data_format(r: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    point: dict[str, Any] = {"key": "data", "title": "Data going into the EP"}
    parts = r.get("partitions") if isinstance(r.get("partitions"), list) else []
    rows = sum(int(x["ROWS"] or 0) for x in parts)
    size = sum(int(s["bytes"] or 0) for s in r["sizes"]) if isinstance(r.get("sizes"), list) else None
    ncols = r["columns"][0]["n"] if isinstance(r.get("columns"), list) and r["columns"] else None
    ddl = r.get("ddl") if isinstance(r.get("ddl"), str) else ""
    sort = re.search(r"SORT KEY\s*[`\w]*\s*\(([^)]*)\)", ddl or "", re.I)
    sort_cols = [c.strip(" `") for c in sort.group(1).split(",") if c.strip(" `")] if sort else []
    passed = sorted({a.split(".")[-1] for f in info["functions"] for a in f["args"]})
    point["sort_key"] = sort_cols
    point["passed"] = passed
    point["value"] = (f"{rows:,} rows" + (f", {_fmt_b(size)} compressed" if size else "") + (f", {ncols} columns" if ncols else "")
                      + f" in {info['inputs'][0]}; {len(passed)} column{'s' if len(passed) != 1 else ''} passed to the EP"
                      + (f" ({', '.join(passed[:6])}{'…' if len(passed) > 6 else ''})" if passed else ""))
    star = ("The derived table reads SELECT *, but only the columns passed to the EP function and those the outer query "
            "names are used, and a columnstore table only reads those, so the * costs little here. ")
    if info["partition"] is not None:
        star += f"This statement reads one partition (partition_id() = {info['partition']}) of a parallel load. "
    if info["filters"]:
        cols, wrapped = _filter_columns(info["filters"])
        lead = sort_cols[0].lower() if sort_cols else None
        skips = bool(lead and any(c.lower() == lead for c in cols))
        point["filter"] = " AND ".join(info["filters"])[:400]
        point["verdict"] = _verdict(
            "ok" if skips or rows < 1_000_000 else "warn",
            star + "Rows are filtered before the EP" + (", on the sort key's first column, so whole segments are skipped."
                                                         if skips else f", but not on the sort key's first column "
                                                         f"({sort_cols[0] if sort_cols else 'the table has no sort key'}), "
                                                         "so every segment is read."),
            ("" if skips or rows < 1_000_000 else f"A SORT KEY starting with {sorted(cols)[0] if cols else 'the filtered column'} lets the "
             "filter skip segments. ")
            + ("The filter wraps the column in a function (e.g. COALESCE(col, '')), which may keep segments from being "
               "skipped even with a sort key: compare the plain column where possible." if wrapped else "") or None)
    else:
        big = rows >= 1_000_000
        point["verdict"] = _verdict(
            "warn" if big else "ok",
            star + f"No filter: every row of the input ({rows:,}) goes through the EP function.",
            "If the report only shows part of the data, filter it in SAS (a data source filter in VA, or a WHERE in "
            "the CAS action), so SingleStore filters before the EP; a sort key on that column then skips segments."
            if big else None)
    return point


def _peers_point(calls: list[dict[str, Any]], ms: float, pl: dict[str, Any] | None) -> dict[str, Any]:
    """This run's time against every EP call in the history: many calls taking the same time whatever the data
    is a fixed wait (seen on this cluster: most traced EP calls take 5.0 to 5.4 s)."""
    point: dict[str, Any] = {"key": "peers", "title": "Compared with other EP calls"}
    ok = [c for c in calls if c.get("ok")]
    if len(ok) < 5 or not ms:
        point["verdict"] = _verdict("info", "Too few EP calls in the query history to compare.")
        return point
    values = [c["ms"] for c in ok]
    point["histogram"] = histogram(values)
    point["this_ms"] = ms
    fit = rows_fit(ok)
    point["fit"] = fit
    kinds = {k: sum(1 for c in ok if c["kind"] == k) for k in ("job", "format")}
    point["value"] = (f"{len(ok)} traced EP calls ({kinds['job']} jobs, {kinds['format']} VA / CAS), median "
                      f"{_fmt_s(statistics.median(values))}")
    fit_text = ""
    if fit and fit["max_rows"] >= 100:
        grows = fit["per_1000_rows_ms"] * fit["max_rows"] / 1000
        if fit["r2"] < 0.3 or abs(grows) < 0.1 * max(fit["intercept_ms"], 1):
            fit_text = (f" Their time doesn't follow the rows they return (1 to {fit['max_rows']:,} rows; r² {fit['r2']:.2f} "
                        "for a straight line through rows and time): the cost is per call, not per row.")
        else:
            fit_text = (f" Across them, time grows with the rows returned: about {_fmt_s(fit['intercept_ms'])} per call plus "
                        f"{fit['per_1000_rows_ms']:g} ms per 1,000 rows (r² {fit['r2']:.2f}).")
    if pl:
        point["plateau"] = pl
        point["value"] += (f"; {pl['count']} ({pl['share']:.0%}) took {_fmt_s(pl['low_ms'])}–{_fmt_s(pl['high_ms'])}")
        warm = (f" EP calls that ran on their own off the plateau took a median {_fmt_s(pl['warm_ms'])}, so the plateau "
                f"is about {_fmt_s(pl['fixed_ms'])} of waiting on top of the work." if pl.get("warm_ms") is not None else "")
        if on_plateau(ms, pl):
            point["verdict"] = _verdict(
                "warn", f"This call is on the plateau: {pl['share']:.0%} of the traced EP calls take "
                f"{_fmt_s(pl['low_ms'])}–{_fmt_s(pl['high_ms'])} whatever table they read and however many rows they return."
                + warm + fit_text + " That's a fixed wait, not the data. Only calls of 1 s or more are traced, so faster EP "
                "calls aren't in this count (see the EP overview in the Usage tab).",
                "Tuning the SingleStore table won't remove it: raise it with the SAS EP owners (what the EP does in the "
                "first seconds of a call; see the tips below).")
        else:
            point["verdict"] = _verdict("ok", f"This call ({_fmt_s(ms)}) isn't on the {_fmt_s(pl['center_ms'])} plateau that "
                                        f"{pl['share']:.0%} of the traced EP calls sit on, so its time is mostly its own work"
                                        f"{' or a warm EP' if ms < pl['low_ms'] else ''}." + fit_text)
    else:
        point["verdict"] = _verdict("ok", "No runtime is shared by many EP calls, so this run's time is mostly its own work." + fit_text)
    return point


def breakdown(ms: float, plan: dict[str, Any] | None, pl: dict[str, Any] | None, gen: dict[str, Any] | None,
              call_start: float = 0.0) -> dict[str, Any]:
    """Where this call's time went, as far as SingleStore can see: measured parts (the EP's generated statement inside
    a sasepFunction call; queueing and CPU from the plan cache) and estimated ones (the fixed wait from the plateau)."""
    point: dict[str, Any] = {"key": "breakdown", "title": "Where the time went"}
    if not ms:
        point["verdict"] = _verdict("info", "No duration recorded for this call.")
        return point
    runs = max(1, int((plan or {}).get("runs") or 1))
    queued = ((float((plan or {}).get("QUEUED_TIME") or 0) or float((plan or {}).get("WORKLOAD_MANAGEMENT_QUEUED_TIME") or 0)
               + float((plan or {}).get("RESOURCE_POOL_QUEUED_TIME") or 0)) / runs) if plan else None
    cpu = float(plan["CPU_TIME"]) / runs if plan and plan.get("CPU_TIME") is not None else None
    segs: list[dict[str, Any]] = []
    facts: list[str] = []
    if gen and call_start and gen.get("start"):
        before = max(0.0, (gen["start"] - call_start) * 1000)
        g_ms = float(gen.get("ms") or 0)
        after = max(0.0, ms - before - g_ms)
        segs = [{"label": "before the EP's SQL", "ms": round(before), "how": "measured", "kind": "wait"},
                {"label": "EP's generated statement", "ms": round(g_ms), "how": "measured", "kind": "work"},
                {"label": "after (results back to SAS)", "ms": round(after), "how": "measured", "kind": "other"}]
        facts.append(f"The EP sent its generated statement {_fmt_s(before)} after the call started; that statement took "
                     f"{_fmt_s(g_ms)}{'' if gen.get('ok', True) else ' and failed'}.")
    else:
        fixed = None
        if on_plateau(ms, pl) and pl and pl.get("warm_ms") is not None:
            fixed = max(0.0, min(ms, ms - pl["warm_ms"]))
        q = queued or 0.0
        if q:
            segs.append({"label": "queued (workload management)", "ms": round(q), "how": "plan cache", "kind": "queue"})
        if fixed:
            segs.append({"label": "fixed EP wait (estimate)", "ms": round(fixed), "how": "estimate", "kind": "wait"})
        segs.append({"label": "work + transfer" if fixed or q else "whole call", "ms": round(max(0.0, ms - q - (fixed or 0))),
                     "how": "rest", "kind": "work"})
    if queued is not None:
        facts.append(f"Queued {_fmt_s(queued)}" + (" (no workload management queueing)." if not queued else "."))
    if cpu is not None:
        facts.append(f"CPU in SingleStore {_fmt_s(cpu)} per run ({cpu / ms:.1%} of the call)." if ms else "")
    point["segments"] = segs
    point["total_ms"] = round(ms)
    point["value"] = " · ".join(f"{s['label']} {_fmt_s(s['ms'])}" for s in segs)
    waiting = sum(s["ms"] for s in segs if s["kind"] in ("wait", "queue"))
    idle = cpu is not None and ms >= 1000 and cpu < 0.05 * ms
    text = " ".join(f for f in facts if f)
    if waiting >= 0.5 * ms or (idle and on_plateau(ms, pl)):
        share = waiting / ms if waiting else 1 - (cpu or 0) / ms
        point["verdict"] = _verdict(
            "warn", f"About {share:.0%} of this call is waiting, not work. " + text
            + (" The plan cache's execution time includes the EP's wait, but SingleStore spends almost no CPU on it."
               if idle else ""),
            "See the tips below: the wait is outside the data (start-up / connection / shared step on the EP side).")
    else:
        point["verdict"] = _verdict("ok" if text else "info", text or "Only the total is known for this call.")
    return point


def _generated_failed(gen: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    out = f"{params.get('outputDatabase') or params.get('inputDatabase') or ''}.{params.get('outputTable') or ''}".strip(".")
    exists = (gen.get("error") or "").upper() == "ER_TABLE_EXISTS_ERROR"
    return {"key": "generated_failed", "title": "The EP's own statement failed",
            "value": f"{gen.get('error') or 'error'} after {_fmt_s(float(gen.get('ms') or 0))}",
            "verdict": _verdict(
                "crit", "The statement the EP generated for this job failed"
                + (f" because the output table {out} already exists" if exists else "")
                + ", yet the sasepFunction call itself completed normally in SingleStore. The job didn't write its output: "
                "check the status / message the call returned to SAS and the SAS log.",
                ("Drop or rename the output table before the job runs (or have the job replace it) – " if exists else "")
                + "a scheduled job that fails like this every run still costs the full EP wait each time.")}


def _burst_point(calls: list[dict[str, Any]], this: dict[str, Any]) -> dict[str, Any]:
    """EP calls running at the same time as this one: did they share one wait (finishing together)? Many small ones?"""
    point: dict[str, Any] = {"key": "burst", "title": "EP calls running at the same time"}
    if not this.get("start") or not this.get("end"):
        point["verdict"] = _verdict("info", "This call's start time isn't known, so overlapping calls can't be found.")
        return point
    mine = [c for c in calls if c["start"] < this["end"] and c["end"] > this["start"]]
    if not any(abs(c["end"] - this["end"]) < 0.01 and abs(c["ms"] - this["ms"]) < 2 for c in mine):
        mine.append(this)
    group = next((b for b in bursts(mine) if any(m is this or (abs(m["end"] - this["end"]) < 0.01) for m in b["members"])), None)
    if not group:
        point["verdict"] = _verdict("ok", "No other traced EP call ran at the same time.")
        return point
    point["value"] = (f"{group['calls']} EP calls overlapped: started {group['start_spread_s']:g} s apart, finished "
                      f"{group['end_spread_s']:g} s apart")
    rows = [m.get("rows") for m in group["members"] if m.get("rows") is not None]
    small = len(rows) >= 3 and statistics.median(rows) <= 10
    point["members"] = [{"start_offset_s": round(m["start"] - group["start"], 2), "end_offset_s": round(m["end"] - group["start"], 2),
                         "ms": m["ms"], "rows": m.get("rows"), "this": m is this or abs(m["end"] - this["end"]) < 0.01}
                        for m in sorted(group["members"], key=lambda m: m["start"])]
    text = ""
    if group["shared"]:
        text = (f"They started up to {group['start_spread_s']:g} s apart but finished within {group['end_spread_s'] * 1000:.0f} ms "
                "of each other: the later ones didn't wait their own full time, they waited for the same step as the "
                "first. Running EP queries in parallel therefore doesn't multiply the wait; running them one after "
                "another does.")
    if small:
        text += (f" {group['calls']} calls returned only {int(statistics.median(rows))} row(s) each (median): each is a separate "
                 "EP query with its own per-job function (and its own EP.sasepv5 registration).")
    point["verdict"] = _verdict(
        "warn" if small else "info" if not group["shared"] else "ok",
        text or "Other EP calls ran at the same time; they didn't finish together, so each did its own work.",
        "Fewer, larger EP queries pay the wait fewer times: e.g. one VA object / one CAS action asking for several "
        "measures instead of one per measure, or compute the formatted / calculated columns once in the data (a "
        "SingleStore view or table) so no EP function is needed (to verify with SAS: which VA items need EP)."
        if small else None)
    return point


# ------------------------------------------------------------------ tuning tips, with their sources
# "kind": measured (seen in this cluster's data), documented (the source says so), hypothesis (to verify).

SOURCES: dict[str, dict[str, str]] = {
    "s2_extfn": {"short": "external functions", "title": "SingleStore: CREATE [OR REPLACE] EXTERNAL FUNCTION (collocated services, batching)",
                 "url": "https://docs.singlestore.com/db/v9.0/reference/sql-reference/procedural-sql-reference/create-or-replace-external-function/"},
    "s2_vars": {"short": "engine variables", "title": "SingleStore: list of engine variables (external_functions_*, plan_expiration_minutes)",
                "url": "https://docs.singlestore.com/db/v9.0/reference/configuration-reference/engine-variables/list-of-engine-variables/"},
    "s2_history": {"short": "Query History", "title": "SingleStore: Query History (event trace, Duration_threshold_ms; DDL traced from 9.1)",
                   "url": "https://docs.singlestore.com/db/v9.1/query-data/query-tuning/query-history/"},
    "s2_trace": {"short": "trace commands", "title": "SingleStore: trace event commands (CREATE / DROP EVENT TRACE)",
                 "url": "https://docs.singlestore.com/cloud/reference/sql-reference/trace-event-commands/"},
    "s2_pools": {"short": "resource pools", "title": "SingleStore: RESOURCE_POOLS (MAX_CONCURRENCY, MAX_QUEUE_DEPTH)",
                 "url": "https://docs.singlestore.com/cloud/reference/information-schema-reference/resource-and-user-management/resource-pools/"},
    "resolv": {"short": "resolv.conf(5)", "title": "resolv.conf(5): resolver timeout defaults to 5 s; single-request(-reopen)",
               "url": "https://man7.org/linux/man-pages/man5/resolv.conf.5.html"},
    "k8s_dns": {"short": "5 s DNS delay", "title": "The 5-second DNS delay in Kubernetes (conntrack race on parallel A/AAAA lookups)",
                "url": "https://cloud.tencent.com/developer/article/1583706"},
    "sas_speedystore": {"short": "SpeedyStore admin guide", "title": "SAS SpeedyStore: Administration and Configuration Guide (EP on each leaf pod)",
                        "url": "https://documentation.sas.com/api/collections/sasadmincdc/v_042/docsets/calsinglestore/content/calsinglestore.pdf?locale=en"},
    "sas_indb": {"short": "In-Database guide", "title": "SAS Viya In-Database Technologies: Deployment and Administration Guide (EP for SingleStore)",
                 "url": "https://documentation.sas.com/api/collections/pgmsascdc/v_037/docsets/indbepdg/content/indbepdg.pdf?locale=en"},
    "sas_caslib": {"short": "SingleStore caslib", "title": "SAS Communities: Using SAS with SingleStore – aggregate pushdown (epDatabase, udfDatabase, aggregatePushdown)",
                   "url": "https://communities.sas.com/t5/SAS-Communities-Library/Using-SAS-with-SingleStore-Enhancing-Performance-with-Aggregate/ta-p/961443"},
    "sas_multinode": {"short": "NUMREADNODES", "title": "SAS blog: Multi-node data transfer to CAS (NUMREADNODES)",
                      "url": "https://blogs.sas.com/content/sgf/2018/03/29/multi-node-data-transfer-to-cas/"},
}


def _tip(kind: str, text: str, *sources: str) -> dict[str, Any]:
    return {"kind": kind, "text": text, "sources": [{"id": s, **SOURCES[s]} for s in sources]}


def tips(card: dict[str, Any], pl: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Concrete next steps for this EP call, each marked measured / documented / hypothesis, with sources."""
    pts = {p["key"]: p for p in card.get("points", [])}
    ms = float(card.get("duration_ms") or 0)
    out: list[dict[str, Any]] = []
    plateau_hit = on_plateau(ms, pl)
    bd = pts.get("breakdown", {})
    if plateau_hit:
        out.append(_tip("measured",
                        f"{pl['share']:.0%} of the traced EP calls take {_fmt_s(pl['low_ms'])}–{_fmt_s(pl['high_ms'])} whatever "
                        f"they read. On the cluster this was investigated on (Oct 2026) the same sasepFunction job "
                        "also ran in ~0.24 s on other days (a +5.0 s step), and in its slow runs the EP sent its first SQL "
                        "5.2 s after the call started: the wait comes before the EP touches the data (README, SAS EP)."))
        out.append(_tip("hypothesis",
                        "A constant +5.0 s is the Linux resolver's default timeout (resolv.conf timeout:5). If the EP opens a "
                        "connection or looks up a host name at the start of a call, one lost DNS packet (in Kubernetes: the "
                        "conntrack race on parallel A/AAAA lookups) costs exactly 5 s. Ask the SAS EP / platform owners to time "
                        "`getent hosts <the aggregator host the EP uses>` in a leaf pod and to check its /etc/resolv.conf "
                        "(ndots, single-request-reopen).", "resolv", "k8s_dns"))
        out.append(_tip("hypothesis",
                        "Warm calls are fast: the EP probably keeps something (a process, a connection, the job definition) for "
                        "a while and pays the start-up again when it's gone. Ask the SAS EP owners what the EP does in the first "
                        "~5 s of a cold call, whether it reconnects to SingleStore per call, and whether there's an idle "
                        "timeout or keep-alive setting. Their EP log on the leaf pods at this call's start time shows it.",
                        "sas_speedystore", "sas_indb"))
    burst = pts.get("burst", {})
    if burst.get("verdict", {}).get("level") == "warn":
        out.append(_tip("measured", "Many small EP queries ran together: each needs its own per-job EP function. Fewer, "
                        "larger queries pay the fixed cost fewer times.", "s2_extfn"))
    if card.get("kind") == "format":
        out.append(_tip("hypothesis",
                        "SAS VA / CAS uses EP functions for what plain SQL can't express (SAS formats, calculated items, "
                        "custom categories: the va__d_ / va__c_ / __ndistinct columns). Where a report only needs a few of "
                        "them, storing those values as columns (a SingleStore view or table built once) lets VA / CAS read "
                        "them without an EP call. Verify with SAS which items trigger EP.", "sas_speedystore"))
        out.append(_tip("documented",
                        "aggregatePushdown=true on the SingleStore caslib lets CAS push aggregations into SingleStore; "
                        "epDatabase / udfDatabase name where the EP and its functions live. NUMREADNODES only changes how "
                        "many CAS workers read in parallel (the transfer), not the fixed EP wait.", "sas_caslib", "sas_multinode"))
    else:
        out.append(_tip("hypothesis",
                        "Each in-database DATA step is one sasepFunction call with its own generated function, so the fixed "
                        "wait is paid per step: combine small steps (several outputs in one DATA step, BY-group processing) "
                        "and filter the input with WHERE= and only the needed columns with KEEP= so SingleStore does that "
                        "before the EP (check the generated statement above after the change).", "sas_indb"))
    queued = next((s for s in bd.get("segments", []) if s["kind"] == "queue" and s["ms"] > 0.1 * ms), None)
    if queued:
        out.append(_tip("documented", "The call was queued by workload management: give SAS EP work its own resource pool "
                        "(MAX_CONCURRENCY / MAX_QUEUE_DEPTH) or schedule it apart from other heavy work. Ask the "
                        "SingleStore admin – this is a cluster setting.", "s2_pools"))
    else:
        out.append(_tip("measured", "Not resource-pool or workload-management queueing: no queued time in the plan cache "
                        "(and none on this cluster since start-up, MV_WORKLOAD_MANAGEMENT_STATUS).", "s2_pools"))
    out.append(_tip("documented",
                    "Only calls over the trace threshold are in the query history, so fast EP calls are missing from every "
                    "share above. To see them, an admin can recreate the trace with a lower threshold for a while (DROP EVENT "
                    "TRACE Query_completion; CREATE EVENT TRACE Query_completion WITH (Query_text = on, "
                    "Duration_threshold_ms = 100, Trace_all_failed_executions = on); – not 0, and back to 1000 afterwards). "
                    "The Workspace keeps EP calls of any duration it finds.", "s2_history", "s2_trace"))
    return out


# ------------------------------------------------------------------ EP overview (Usage tab)

def overview(since: str | None = None, live: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """EP calls by source from the local store (plus ``live`` calls from the trace the store doesn't have yet): count,
    total time, plateau share, shared-wait bursts, and how many EP calls the plan cache saw in all (the trace misses
    the fast ones). ``since``: cluster time "YYYY-MM-DD HH:MM:SS"."""
    cut = epoch(since) if since else 0.0
    calls = merge_calls([c for c in live or [] if c["end"] >= cut], stored_calls(since))
    src = history_store.source_id()
    conn = history_store.connect()
    try:
        plans = history_store._plan_executions(conn, src, since)
    finally:
        conn.close()
    ok = [c for c in calls if c["ok"]]
    pl = calls_plateau(calls)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c in calls:
        key = (c["kind"], c["db"] or "", c["user"] or "")
        g = groups.setdefault(key, {"kind": c["kind"], "source": _SOURCE_LABEL[c["kind"]], "database": c["db"], "user": c["user"],
                                    "calls": 0, "ms": 0.0, "failed": 0, "plateau": 0, "plateau_ms": 0.0, "times": [], "last": 0.0})
        g["calls"] += 1
        g["ms"] += c["ms"]
        g["failed"] += 0 if c["ok"] else 1
        g["times"].append(c["ms"])
        g["last"] = max(g["last"], c["end"])
        if c["ok"] and on_plateau(c["ms"], pl):
            g["plateau"] += 1
            g["plateau_ms"] += c["ms"]
    rows = []
    for g in groups.values():
        times = g.pop("times")
        rows.append({**g, "ms": round(g["ms"]), "plateau_ms": round(g["plateau_ms"]), "median_ms": round(statistics.median(times)),
                     "last": _iso(g["last"])})
    rows.sort(key=lambda r: -r["ms"])
    bs = bursts(calls)
    # Every EP execution the plan cache saw, however short: sasepFunction plans, and the VA / CAS format functions'
    # registrations (INSERT INTO EP.sasepv5, one per function; written once, seen on more than one aggregator).
    reg: dict[str, int] = {}
    jobs: dict[str, int] = {}
    for p in plans:
        text = p.get("text") or ""
        dbname = p.get("database") or ""
        if re.match(r"\s*INSERT\s+INTO\s+`?EP`?\s*\.\s*`?sasepv\d*", text, re.I):
            reg[dbname] = max(reg.get(dbname, 0), int(p["execs"] or 0))
        elif kind_of(text) == "job":
            jobs[dbname] = jobs.get(dbname, 0) + int(p["execs"] or 0)
    traced_fn = len({(c["db"], c.get("stmt"), round(c["start"])) for c in calls if c["kind"] == "format"})
    traced_jobs = sum(1 for c in calls if c["kind"] == "job")
    total_ms = sum(c["ms"] for c in calls)
    plateau_ms = sum(r["plateau_ms"] for r in rows)
    fixed_ms = sum(r["plateau"] for r in rows) * (pl["fixed_ms"] if pl else 0)
    return {
        "since": since, "calls": len(calls), "ms": round(total_ms), "failed": sum(r["failed"] for r in rows),
        "plateau": pl, "plateau_calls": sum(r["plateau"] for r in rows), "plateau_ms": round(plateau_ms),
        "plateau_time_share": round(plateau_ms / total_ms, 3) if total_ms else 0.0,
        "fixed_wait_ms": round(fixed_ms), "rows": rows,
        "bursts": len(bs), "shared_bursts": sum(1 for b in bs if b["shared"]), "calls_in_bursts": sum(b["calls"] for b in bs),
        "all_format_functions": sum(reg.values()) or None, "traced_format_functions": traced_fn,
        "all_jobs": sum(jobs.values()) or None, "traced_jobs": traced_jobs,
        "fit": rows_fit(ok),
    }


_SOURCE_LABEL = {"job": "SAS job (sasepFunction)", "format": "SAS VA / CAS (format function)"}


def _iso(t: float) -> str | None:
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t else None


def overview_lines(o: dict[str, Any]) -> list[str]:
    """The EP overview as text (tool summary)."""
    if not o or not o.get("calls"):
        return []
    pl = o.get("plateau")
    out = [f"SAS EP: {o['calls']} traced calls, {o['ms'] / 1000:.1f} s in all"
           + (f"; {o['plateau_calls']} on the {pl['low_ms'] / 1000:.2f}–{pl['high_ms'] / 1000:.2f} s plateau "
              f"({o['plateau_time_share']:.0%} of the EP time)" if pl else "") + "."]
    for r in o["rows"][:6]:
        out.append(f"- {r['source']} {r['database'] or '-'} ({r['user'] or '-'}): {r['calls']} calls, {r['ms'] / 1000:.1f} s, "
                   f"median {r['median_ms'] / 1000:.2f} s, {r['plateau']} on the plateau")
    if o.get("all_format_functions"):
        out.append(f"- the plan cache saw {o['all_format_functions']} VA / CAS EP functions registered; {o['traced_format_functions']} "
                   "were traced (the rest ran under the trace threshold)")
    return out


def _skew(r: dict[str, Any], ms: float) -> dict[str, Any]:
    parts = r.get("partitions")
    point: dict[str, Any] = {"key": "skew", "title": "Skew of the input table"}
    if isinstance(parts, dict) or not parts:
        point["verdict"] = _verdict("info", "Rows per partition aren't available"
                                    + (f" ({parts['error']})." if isinstance(parts, dict) else " (no partitions found)."))
        return point
    masters = {(int(x["ORDINAL"]), int(x["NODE_ID"])) for x in r.get("layout") or [] if isinstance(r.get("layout"), list)}
    size = {}
    if isinstance(r.get("sizes"), list):
        for s in r["sizes"]:
            if not masters or (int(s["p"]), int(s["node"])) in masters:
                size[int(s["p"])] = size.get(int(s["p"]), 0) + int(s["bytes"] or 0)
    rows = [{"partition": int(x["ORDINAL"]), "host": x["HOST"], "rows": int(x["ROWS"] or 0),
             "bytes": size.get(int(x["ORDINAL"])) if size else (int(x["MEMORY_USE"] or 0) or None)} for x in parts]
    counts = [x["rows"] for x in rows]
    avg = sum(counts) / len(counts) if counts else 0
    top = max(counts) if counts else 0
    factor = round(top / avg, 2) if avg else None
    point["partitions"] = rows
    point["value"] = f"skew {factor}× (largest partition {top:,} rows, average {avg:,.0f})" if factor else "empty table"
    point["skew"] = factor
    if not factor:
        point["verdict"] = _verdict("info", "The input table is empty.")
        return point
    # Partitions run in parallel: balanced, the job would take avg/max of the time — an upper bound, since part of
    # the runtime is the EP's fixed start-up and the SAS step itself.
    lost = ms * (1 - avg / top) if ms and top else 0
    point["lost_ms"] = round(lost)
    if factor <= 1.2:
        point["verdict"] = _verdict("ok", f"Evenly spread over {len(rows)} partitions: no partition holds the others up.")
    else:
        level = "warn" if factor <= 2 else "crit"
        cand = _shard_candidate(r)
        point["verdict"] = _verdict(
            level, f"The largest partition has {factor}× the average rows. Partitions are processed in parallel, so the "
            f"job waits for the largest (unless SingleStore splits it further): up to {_fmt_s(lost)} of the {_fmt_s(ms)} is time lost to skew.",
            f"Reshard the input table on a column with many distinct values{f', e.g. {cand}' if cand else ''} "
            "(build a copy with SHARD KEY (…) and INSERT … SELECT), or SHARD KEY () for an even random spread "
            "if it isn't joined on a key.")
    return point


def _shard_candidate(r: dict[str, Any]) -> str | None:
    card = r.get("cardinality")
    if not isinstance(card, list) or not card:
        return None
    best = max(card, key=lambda x: int(x["n"] or 0))
    return f"{best['c']} ({int(best['n'] or 0):,} distinct values)" if best.get("n") else None


def _cores_point(r: dict[str, Any]) -> dict[str, Any]:
    point: dict[str, Any] = {"key": "cores", "title": "Partitions vs leaf cores"}
    cpus, layout = r.get("cpus"), r.get("layout")
    if not isinstance(cpus, list) or not cpus or not isinstance(layout, list) or not layout:
        point["verdict"] = _verdict("info", "The partition layout or the leaves' cores couldn't be read.")
        return point
    per_leaf: dict[int, int] = {}
    for x in layout:
        per_leaf[int(x["NODE_ID"])] = per_leaf.get(int(x["NODE_ID"]), 0) + 1
    leaves = []
    for c in cpus:
        node = int(c["NODE_ID"])
        leaves.append({"node": node, "host": c["IP_ADDR"], "cores": _cores(c), "host_cpus": int(c["NUM_CPUS"] or 0),
                       "partitions": per_leaf.get(node, 0)})
    point["leaves"] = leaves
    parts, cores = sum(per_leaf.values()), sum(x["cores"] for x in leaves)
    point["value"] = f"{parts} partitions on {len(leaves)} leaves with {cores:g} cores in all"
    ratio = parts / cores if cores else 0
    limited = any(x["cores"] < x["host_cpus"] for x in leaves)
    note = " (cores: the container's CPU limit, below the host's CPUs)" if limited else ""
    if ratio and ratio < 0.75:
        point["verdict"] = _verdict("warn", f"If the job runs one task per partition (the usual plan for a statement joining with an "
                                    f"external function; EXPLAIN can't show it for EP jobs), at most {parts} of the {cores:g} "
                                    f"leaf cores{note} work on it and the rest stay idle.",
                                    "Partitions are fixed per database: for heavy EP jobs, keep the input in a database "
                                    "with about one partition per leaf core (CREATE DATABASE … PARTITIONS n).")
    elif ratio > 2:
        point["verdict"] = _verdict("warn", f"{parts} partitions share {cores:g} cores{note}: partitions wait for a core, "
                                    "and the EP's work per task stays small.",
                                    "Fewer, larger partitions (about one per core) would queue less; partitions are set "
                                    "per database (CREATE DATABASE … PARTITIONS n).")
    else:
        point["verdict"] = _verdict("ok", f"About {ratio:.1f} partitions per core{note}: the cores are used without much queuing.")
    return point


def _data_in(r: dict[str, Any], in_db: str, in_table: str) -> dict[str, Any]:
    point: dict[str, Any] = {"key": "data", "title": "Data going into the EP"}
    parts = r.get("partitions") if isinstance(r.get("partitions"), list) else []
    rows = sum(int(x["ROWS"] or 0) for x in parts)
    size = sum(int(s["bytes"] or 0) for s in r["sizes"]) if isinstance(r.get("sizes"), list) else None
    ncols = r["columns"][0]["n"] if isinstance(r.get("columns"), list) and r["columns"] else None
    ddl = r.get("ddl") if isinstance(r.get("ddl"), str) else ""
    sort = re.search(r"SORT KEY\s*[`\w]*\s*\(([^)]*)\)", ddl or "", re.I)
    sort_cols = [c.strip(" `") for c in sort.group(1).split(",") if c.strip(" `")] if sort else []
    point["value"] = (f"{rows:,} rows" + (f", {_fmt_b(size)} compressed" if size else "") + (f", {ncols} columns" if ncols else "")
                      + " in the input table")
    point["sort_key"] = sort_cols
    gen = r.get("generated")
    if isinstance(gen, dict) and gen.get("sql"):
        g = gen["sql"]
        point["generated"] = " ".join(g.split())[:1200]
        point["generated_source"] = gen["source"]
        where = re.search(r"\bWHERE\b(.*)$", g, re.I | re.S)
        used = [c for c in re.findall(r"sasepin\.`?(\w+)`?", g.split("FROM", 1)[0])]
        point["columns_used"] = len(set(used))
        if where:
            cols = set(re.findall(r"sasepin\.`?(\w+)`?", where.group(1))) or set(re.findall(r"`(\w+)`", where.group(1)))
            lead = sort_cols[0].lower() if sort_cols else None
            skips = bool(lead and any(c.lower() == lead for c in cols))
            point["filter"] = " ".join(where.group(1).split())[:300]
            point["verdict"] = _verdict(
                "ok" if skips or rows < 1_000_000 else "warn",
                "SingleStore filters the rows before they reach the EP"
                + (", and the filter is on the sort key's first column, so whole segments are skipped." if skips else
                   f", but the filter ({', '.join(sorted(cols)) or 'see below'}) isn't on the sort key's first column"
                   f"{' (' + sort_cols[0] + ')' if sort_cols else ' (the table has no sort key)'}, so every segment is read."),
                None if skips or rows < 1_000_000 else f"A SORT KEY starting with {sorted(cols)[0] if cols else 'the filtered column'} lets the "
                "filter skip segments (rebuild the input table with that sort key).")
        else:
            big = rows >= 1_000_000
            point["verdict"] = _verdict(
                "warn" if big else "ok",
                f"No filter: all {rows:,} rows go through the EP"
                + (f", and {point['columns_used']} of {ncols} columns" if ncols and point.get("columns_used") else "") + ".",
                "If the SAS step only needs part of the data, filter it in SAS before the EP call (a WHERE= data set "
                "option on the input), so SingleStore filters first; a sort key on that column then skips whole "
                "segments." if big else None)
        if ncols and ncols >= 8 and point.get("columns_used", 0) >= ncols:
            # Every column of the input goes through the EP: the SAS step's input has no KEEP= / DROP=.
            v = point["verdict"]
            v["text"] += (f" All {ncols} columns of the input go into the EP (like a SELECT *): if the step uses only some, "
                          "KEEP= / DROP= on the input data set should shrink what is read and sent (check the generated "
                          "statement after the change).")
            if rows >= 1_000_000 and v["level"] == "ok":
                v["level"] = "warn"
    else:
        point["verdict"] = _verdict(
            "info", "The statement the EP generated for this job (CREATE TABLE … AS SELECT … FROM the input AS sasepin "
            "JOIN the job's EP function) isn't in the query history or the plan cache any more, so whether a filter is "
            "applied before the EP can't be seen. EXPLAIN doesn't help: the sasepFunction call is an external function "
            "and shows only as an opaque GeneratedTable, and the job's own function is dropped after the run.")
    return point


def _waits(plan: dict[str, Any] | None, ms: float) -> dict[str, Any]:
    point: dict[str, Any] = {"key": "waits", "title": "Disk / blob cache, memory, spilling, queueing"}
    if not plan:
        point["verdict"] = _verdict("info", "The call's plan is no longer in the plan cache, so these statistics aren't available.")
        return point
    runs = max(1, int(plan.get("runs") or 1))
    avg = float(plan.get("AVERAGE_EXEC_TIME") or 0)
    blob = float(plan.get("BLOB_CACHE_WAIT_TIME_MS") or 0) / runs
    miss = float(plan.get("AVERAGE_BLOB_CACHE_MISS_BYTES") or 0)
    mem = float(plan.get("AVERAGE_MAX_MEMORY_USE") or plan.get("AVERAGE_MEMORY_USE") or 0)
    spill = float(plan.get("AVERAGE_DISK_SPILLING_USE") or 0)
    queued = (float(plan.get("QUEUED_TIME") or 0) or float(plan.get("WORKLOAD_MANAGEMENT_QUEUED_TIME") or 0)
              + float(plan.get("RESOURCE_POOL_QUEUED_TIME") or 0)) / runs
    point["metrics"] = {"runs": runs, "avg_ms": avg, "blob_wait_ms": round(blob, 1), "blob_miss_bytes": miss,
                        "max_memory": mem, "spill": spill, "queued_ms": round(queued, 1)}
    point["value"] = (f"blob cache wait {_fmt_s(blob)}/run, misses {_fmt_b(miss)}, memory {_fmt_b(mem)}, "
                      f"spill {_fmt_b(spill)}, queued {_fmt_s(queued)}/run")
    issues = []
    if spill > 0:
        issues.append(("crit", "it spilled to disk", "give the resource pool more memory or reduce the data the step reads"))
    base = ms or avg
    if base and blob > 0.1 * base:
        issues.append(("crit" if blob > 0.3 * base else "warn", f"{_fmt_s(blob)} per run waiting for data from the blob store",
                       "the input isn't in the local cache: keep hot EP inputs cached, or read less of the table"))
    if base and queued > 0.1 * base:
        issues.append(("warn", f"{_fmt_s(queued)} per run queued by workload management",
                       "schedule EP jobs apart from other heavy work, or give their resource pool more concurrency"))
    if mem >= 1024 ** 3:
        issues.append(("warn", f"{_fmt_b(mem)} of memory", None))
    note = ""
    if ms and avg and avg < 0.5 * ms:
        note = (f" The plan cache counts {_fmt_s(avg)} per call where the trace measured {_fmt_s(ms)}: the time spent "
                "inside the EP (an external function) isn't in these statistics.")
    if issues:
        worst = "crit" if any(i[0] == "crit" for i in issues) else "warn"
        steps = [i[2] for i in issues if i[2]]
        point["verdict"] = _verdict(worst, "The call " + "; ".join(i[1] for i in issues) + "." + note,
                                    ("To do: " + "; ".join(steps) + ".") if steps else None)
    else:
        point["verdict"] = _verdict("ok", "No disk waits, spilling or queueing recorded for this call." + note)
    return point


def _trend(runs: Any) -> dict[str, Any]:
    point: dict[str, Any] = {"key": "trend", "title": "Runtime over time"}
    if not isinstance(runs, list) or not runs:
        point["verdict"] = _verdict("info", "No earlier runs of this EP job in the query history.")
        return point
    ok = [r for r in runs if r["ok"]]
    point["runs"] = [{"t": r["t"][:19], "ms": r["ms"], "ok": r["ok"]} for r in runs[-60:]]
    if len(ok) < 4:
        point["value"] = f"{len(runs)} run{'s' if len(runs) != 1 else ''} in the history"
        point["verdict"] = _verdict("info", "Too few runs to see a trend.")
        return point
    half = len(ok) // 2
    before, after = statistics.median(r["ms"] for r in ok[:half]), statistics.median(r["ms"] for r in ok[half:])
    ratio = after / before if before else 1
    point["value"] = f"median {_fmt_s(before)} → {_fmt_s(after)} ({len(ok)} successful runs, {ok[0]['t'][:10]} to {ok[-1]['t'][:10]})"
    point["change"] = round(ratio, 2)
    if ratio >= 1.5:
        point["verdict"] = _verdict("crit", f"The job got {ratio:.1f}× slower.",
                                    "Compare the input table's size and skew now with when it was fast (it may have grown or "
                                    "been reloaded unevenly).")
    elif ratio >= 1.2:
        point["verdict"] = _verdict("warn", f"The job got {ratio:.1f}× slower.", None)
    else:
        point["verdict"] = _verdict("ok", "Stable" if ratio > 0.83 else f"Faster than before ({ratio:.2f}×).")
    return point
