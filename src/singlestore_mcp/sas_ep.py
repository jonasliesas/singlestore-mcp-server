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
- On this cluster most EP calls of both kinds take 5.0–5.4 s whatever they read: a fixed wait, which the card
  points out by comparing a run with every other EP call in the history.

Sources: TABLE_STATISTICS (rows per master partition), COLUMNAR_SEGMENTS (compressed bytes per partition),
DISTRIBUTED_PARTITIONS (partitions per leaf), MV_SYSINFO_CPU (leaf cores, with the container's CFS quota),
SHOW CREATE TABLE (shard / sort key), OPTIMIZER_STATISTICS (column cardinality), MV_TRACE_EVENTS and MV_PLANCACHE
(the generated statement, earlier runs). All read-only; nothing here runs the EP (and it's never PROFILEd).
"""

from __future__ import annotations

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
    """Runtimes of every SAS EP call in the query history (both kinds), for the "fixed wait" comparison."""
    rows = _q("SELECT DETAILS::%%duration_ms AS ms FROM information_schema.MV_TRACE_EVENTS WHERE EVENT_TYPE = 'Query_completion'"
              " AND DETAILS::%%success = 1 AND (DETAILS::$query_text LIKE %s OR DETAILS::$query_text REGEXP %s)"
              " AND DETAILS::$query_text NOT LIKE %s",
              ("%sasepFunction%", "EP[.]h[0-9A-Fa-f]{32}_EP", f"%{history_store.MARKER_PREFIX}%"))
    return [float(r["ms"] or 0) for r in rows]


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

    def generated() -> Any:
        # The statement the EP generated for this job (CREATE TABLE <out> AS SELECT … FROM <in> AS sasepin JOIN …).
        rows = _q("SELECT TIME, DETAILS::$query_text AS q, DETAILS::%%duration_ms AS ms FROM information_schema.MV_TRACE_EVENTS"
                  " WHERE EVENT_TYPE = 'Query_completion' AND DETAILS::$query_text LIKE %s AND DETAILS::$query_text LIKE %s"
                  " AND DETAILS::$query_text NOT LIKE %s ORDER BY TIME DESC LIMIT 5",
                  ("%AS sasepin%", f"%{in_table}%", f"%{history_store.MARKER_PREFIX}%"))
        found = [r for r in rows if re.search(rf"FROM\s+`?{re.escape(in_db or '')}`?\s*\.\s*`?{re.escape(in_table)}`?\s+AS\s+sasepin",
                                              history_store._text(r["q"]), re.I)]
        if found:
            return {"sql": history_store._text(found[0]["q"]), "source": "trace"}
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
    if in_db and in_table:
        card["points"].append(_skew(results, ms))
        card["points"].append(_cores_point(results))
        card["points"].append(_data_in(results, in_db, in_table))
    card["points"].append(_waits(plan, ms))
    if in_db and in_table:
        card["points"].append(_trend(results.get("trend")))
    card["points"].append(_peers_point(results.get("peers"), ms))
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
    if info["partition"] is not None:
        card["points"].append(_partition_spread(traced, finished, results))
    if first:
        card["points"].append(_skew(results, ms))
        card["points"].append(_cores_point(results))
        card["points"].append(_data_format(results, info))
    card["points"].append(_waits(plan, ms))
    loads = _loads(traced) if info["partition"] is not None else traced
    card["points"].append(_trend(loads))
    card["points"].append(_peers_point(results.get("peers"), ms))
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


def _peers_point(peers: Any, ms: float) -> dict[str, Any]:
    """This run's time against every EP call in the history: many calls taking the same time whatever the data
    is a fixed wait (seen on this cluster: most EP calls take 5.0 to 5.4 s)."""
    point: dict[str, Any] = {"key": "peers", "title": "Compared with other EP calls"}
    if not isinstance(peers, list) or len(peers) < 5 or not ms:
        point["verdict"] = _verdict("info", "Too few EP calls in the query history to compare.")
        return point
    near = sum(1 for x in peers if abs(x - ms) <= 0.05 * ms)
    share = near / len(peers)
    med = statistics.median(peers)
    point["value"] = (f"{len(peers)} EP calls in the history, median {_fmt_s(med)}; {near} ({share:.0%}) took "
                      f"{_fmt_s(ms)} ± 5%")
    if share >= 0.3:
        point["verdict"] = _verdict(
            "warn", f"{share:.0%} of all EP calls take almost exactly this long, whatever table they read and however "
            "big it is. That points at a fixed wait (the EP starting up, or a time-out somewhere), not at the data.",
            "Check the SAS EP side (its start-up, connection and time-out settings, and its logs) for a wait of about "
            f"{_fmt_s(ms)}; tuning the SingleStore table won't remove it.")
    else:
        point["verdict"] = _verdict("ok", "This run's time isn't shared by many other EP calls, so it's mostly this job's own work.")
    return point


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
