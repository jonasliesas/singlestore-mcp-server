"""Query History app: every finished query the cluster traced, with runtime filters and tuning advice.

Reads SingleStore's query history (``CREATE EVENT TRACE Query_completion``), which keeps
each finished query with its duration, user, database, rows and result in the ring buffer
``information_schema.MV_TRACE_EVENTS``. Unlike the plan cache, it has one row per run.

For a selected run, ``query_history_tuning`` combines the run itself, its plan's statistics
from the plan cache (memory, disk spilling, queueing, warnings), ``EXPLAIN`` (which compiles
but doesn't run the statement) and the tables' structure, and turns them into concrete
recommendations. No Claude needed; the app can also hand the analysis to Claude.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import assistant, history_store, query_advisor, sas_ep
from ..db import db, quote_identifier
from ._core import APP_ONLY, apps, jsonable, jsonable_rows, register_app, tool_result, with_browser_link

URI = "ui://singlestore/query-history.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
# Tags this app's own statements, so its EXPLAINs never show up in the history it shows.
_MARKER = "/* s2-query-history */"
MIN_RUNTIME_MS = 1000
_MAX_EVENTS = 5000
_LIST_SQL_CHARS = 400

register_app(
    URI,
    "query_history.html",
    name="Query History",
    description="Every finished query on the cluster, filtered by runtime, with tuning recommendations",
)

_ENABLE_HINT = ("Query history is off or empty. An admin turns it on with: CREATE EVENT TRACE Query_completion "
                "WITH (Query_text = on, Duration_threshold_ms = 1000);")


def _query(sql: str, params: tuple[Any, ...] = (), database: str | None = None) -> list[dict[str, Any]]:
    # With params, a literal % in the SQL must be written %% (the driver formats it); without, it stays %.
    return db.execute(f"{_MARKER} {sql}", params, database=database)[1]


# ------------------------------------------------------------------ the list


def collect(min_ms: int = MIN_RUNTIME_MS, hours: float | None = None, limit: int = _MAX_EVENTS) -> dict[str, Any]:
    min_ms = max(MIN_RUNTIME_MS, int(min_ms or MIN_RUNTIME_MS))
    limit = max(1, min(_MAX_EVENTS, int(limit)))
    where = ["EVENT_TYPE = 'Query_completion'", "DETAILS::%%duration_ms >= %s",
             "(DETAILS::$query_text IS NULL OR DETAILS::$query_text NOT LIKE %s)"]
    # Every statement this server sends for itself starts with a "/* s2-… */" marker: leave them all out.
    params: list[Any] = [min_ms, f"%{history_store.MARKER_PREFIX}%"]
    if hours:
        where.append("TIME >= NOW() - INTERVAL %s SECOND")
        params.append(int(float(hours) * 3600))
    def live_plans() -> list[dict[str, Any]] | None:
        try:
            return history_store.snapshot_plans()
        except Exception:  # noqa: BLE001 - no access to the plan cache: the list works without it
            return None

    rows, info, offset, _synced, trace, plans = db.parallel(
        lambda: _query(
            "SELECT NODE_ID, NODE_START_EPOCH_S, EVENT_ID, TIME,"
            " DETAILS::%%duration_ms AS ms, DETAILS::$start_time AS started, DETAILS::$user_name AS user,"
            " DETAILS::$context_database AS db, DETAILS::$query_category AS category, DETAILS::%%success AS ok,"
            " DETAILS::%%row_count AS row_count, DETAILS::$error_code AS error_code, DETAILS::%%plan_id AS plan_id,"
            " DETAILS::$resource_pool_name AS pool,"
            f" LEFT(DETAILS::$query_text, {_LIST_SQL_CHARS}) AS sql_head, LENGTH(DETAILS::$query_text) AS sql_len"
            f" FROM information_schema.MV_TRACE_EVENTS WHERE {' AND '.join(where)}"
            " ORDER BY TIME DESC LIMIT %s",
            (*params, limit),
        ),
        lambda: _query(
            "SELECT COUNT(*) AS events, MIN(TIME) AS oldest, MAX(TIME) AS newest"
            " FROM information_schema.MV_TRACE_EVENTS WHERE EVENT_TYPE = 'Query_completion'"
        ),
        # The cluster's clock offset from UTC, so the app can show times in the viewer's time zone.
        lambda: _query("SELECT TIMESTAMPDIFF(MINUTE, UTC_TIMESTAMP(), NOW()) AS offset_min"),
        # Keep the local copy for the Trends tab up to date (only events newer than the last sync).
        history_store.safe_sync,
        trace_status,
        # Every statement's totals since its plan was compiled (also the fast ones the trace leaves out),
        # copied into the local store too.
        live_plans,
    )
    events = [{
        "key": f"{r['NODE_ID']}:{r['NODE_START_EPOCH_S']}:{r['EVENT_ID']}",
        "node_id": int(r["NODE_ID"]),
        "finished": jsonable(r["TIME"]),
        "started": r["started"],
        "ms": int(r["ms"] or 0),
        "user": r["user"],
        "database": r["db"] or None,
        "category": r["category"],
        "ok": r["ok"] is None or bool(r["ok"]),
        "rows": None if r["row_count"] is None else int(r["row_count"]),
        "error_code": r["error_code"],
        "plan_id": None if r["plan_id"] in (None, -1) else int(r["plan_id"]),
        "pool": r["pool"],
        "sql": sas_ep.masked(" ".join((r["sql_head"] or "").split())),
        "sql_truncated": (r["sql_len"] or 0) > _LIST_SQL_CHARS,
        "shape": history_store.shape_of(r["sql_head"]),
        "origin": history_store.origin(r["sql_head"]),
        # Runs of the same statement (other literal values) in the same database share this key.
        "stmt": history_store.statement_key(r["sql_head"], r["db"] or None),
        "tables": history_store.table_names(r["sql_head"], r["db"] or None),
    } for r in rows]
    total = info[0] if info else {}
    plan_stats, plan_list = _merge_plans(events, plans, hours)
    return {
        "min_ms": min_ms,
        "hours": hours,
        "limit": limit,
        "limited": len(events) >= limit,
        "events": events,
        "history": {"events": int(total.get("events") or 0), "oldest": jsonable(total.get("oldest")),
                    "newest": jsonable(total.get("newest"))},
        "utc_offset_min": int(offset[0]["offset_min"]) if offset else 0,
        "enabled_hint": None if total.get("events") else _ENABLE_HINT,
        "trace": trace,
        "plan_stats": plan_stats,
        "plans": plan_list,
        "plancache_error": None if plans is not None else "The plan cache couldn't be read (MV_PLANCACHE).",
    }


# ------------------------------------------------------------------ event trace configuration


def trace_status() -> dict[str, Any]:
    """The Query_completion event trace's configuration and state per node (MV_TRACE_EVENTS_STATUS)."""
    try:
        rows = _query("SELECT NODE_ID, NODE_TYPE, ACTIVE, CREATION_TIME, CONFIGURATION, CURRENT_MEMORY_USAGE_B,"
                      " TOTAL_EVENTS_TRACED, UNCONSUMED_EVENTS FROM information_schema.MV_TRACE_EVENTS_STATUS"
                      " WHERE EVENT_TYPE = 'Query_completion' ORDER BY NODE_ID")
    except Exception as exc:  # noqa: BLE001 - e.g. no permission: the page says so
        return {"error": str(exc)[:200], "nodes": []}
    import json

    nodes = []
    for r in rows:
        cfg = r["CONFIGURATION"]
        if isinstance(cfg, (str, bytes)):
            try:
                cfg = json.loads(cfg or "{}")
            except ValueError:
                cfg = {"raw": str(cfg)}
        nodes.append({
            "node_id": int(r["NODE_ID"]), "node_type": r["NODE_TYPE"], "active": str(r["ACTIVE"]).upper() == "YES",
            "created": jsonable(r["CREATION_TIME"]), "config": jsonable(cfg or {}),
            "memory_b": int(r["CURRENT_MEMORY_USAGE_B"] or 0), "traced": int(r["TOTAL_EVENTS_TRACED"] or 0),
            "unconsumed": int(r["UNCONSUMED_EVENTS"] or 0),
        })
    configs = [n["config"] for n in nodes if n["active"]]
    first = configs[0] if configs else {}
    return {
        "nodes": nodes,
        "active": bool(configs),
        "duration_threshold_ms": first.get("Duration_threshold_ms"),
        "query_text": first.get("Query_text") in (1, "1", True, "ON", "on"),
        "trace_failed": first.get("Trace_all_failed_executions") in (1, "1", True, "ON", "on"),
        "enhanced_stats": str(first.get("Enhanced_stats_collection", "OFF")).upper() == "ON",
        "same_everywhere": all(c == first for c in configs),
    }


# ------------------------------------------------------------------ the plan cache, next to the trace

_PLAN_FIELDS = ("runs", "failed", "rows", "exec_ms", "avg_ms", "cpu_ms", "avg_mem", "avg_max_mem", "avg_spill",
                "avg_blob_miss", "blob_wait_ms", "queued_ms", "first_load", "last_executed", "warnings")
_MAX_PLAN_ROWS = 600


def _plan_summary(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p.get(k) for k in _PLAN_FIELDS} | {"plan_key": p["plan_key"], "node_id": p["node_id"]}


def _merge_plans(events: list[dict[str, Any]], live: list[dict[str, Any]] | None,
                 hours: float | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(plan statistics for the traced runs by "node:plan_id", plan-cache-only statements).

    A traced run names its plan (node + plan id), so its statement's totals come from that plan. Statements the
    trace never kept (all their runs under the threshold) are listed from the plan cache — the live one plus plans
    the local store kept after the cluster evicted them — merged across aggregators by statement."""
    live = live or []
    by_plan = {f"{p['node_id']}:{p['plan_id']}": p for p in live}
    plan_stats = {}
    traced_stmts = set()
    for e in events:
        traced_stmts.add(e["stmt"])
        ref = f"{e['node_id']}:{e['plan_id']}" if e.get("plan_id") is not None else None
        if ref and ref in by_plan and ref not in plan_stats:
            plan_stats[ref] = _plan_summary(by_plan[ref])
    traced_plans = {plan_stats[r]["plan_key"] for r in plan_stats}

    # Stored plans (kept after eviction) for the period, then the live ones over them.
    since = None
    if hours:
        newest = max((p["last_executed"] or "" for p in live), default="")
        if newest:
            import datetime as _dt

            since = (_dt.datetime.fromisoformat(newest[:19]) - _dt.timedelta(hours=float(hours))).isoformat(sep=" ")
    try:
        stored = {p["plan_key"]: p for p in history_store.stored_plans(since, limit=3000)}
    except Exception:  # noqa: BLE001 - store unreadable: live plans only
        stored = {}
    live_keys = {p["plan_key"] for p in live}
    for p in live:
        stored[p["plan_key"]] = p
    groups: dict[str, dict[str, Any]] = {}
    for p in stored.values():
        if p["plan_key"] in traced_plans or p["stmt"] in traced_stmts:
            continue
        if since and (p.get("last_executed") or "") < since:
            continue
        g = groups.get(p["stmt"])
        if g is None:
            g = groups[p["stmt"]] = {
                "key": f"plan:{p['stmt']}", "stmt": p["stmt"], "database": p.get("database"),
                "sql": " ".join((p.get("text") or "").split())[:_LIST_SQL_CHARS],
                "sql_truncated": (p.get("text_len") or 0) > _LIST_SQL_CHARS, "origin": p.get("origin"),
                "tables": p.get("tables") or [], "plan_keys": [], "nodes": [], "live": False,
                "runs": 0, "failed": 0, "rows": 0, "exec_ms": 0.0, "cpu_ms": 0.0, "blob_wait_ms": 0.0, "queued_ms": 0.0,
                "avg_max_mem": 0.0, "avg_spill": 0.0, "avg_blob_miss": 0.0, "first_load": None, "last_executed": None,
                "warnings": None,
            }
        g["plan_keys"].append(p["plan_key"])
        if p["node_id"] not in g["nodes"]:
            g["nodes"].append(p["node_id"])
        g["live"] |= p["plan_key"] in live_keys
        for k in ("runs", "failed", "rows", "exec_ms", "cpu_ms", "blob_wait_ms", "queued_ms"):
            g[k] += p.get(k) or 0
        for k in ("avg_max_mem", "avg_spill", "avg_blob_miss"):
            g[k] = max(g[k], p.get(k) or 0)
        g["first_load"] = min(filter(None, [g["first_load"], p.get("first_load")]), default=None)
        g["last_executed"] = max(filter(None, [g["last_executed"], p.get("last_executed")]), default=None)
        g["warnings"] = g["warnings"] or p.get("warnings")
    plan_list = []
    for g in groups.values():
        if not g["runs"]:
            continue
        g["avg_ms"] = round(g["exec_ms"] / g["runs"], 1)
        plan_list.append(g)
    plan_list.sort(key=lambda g: g["last_executed"] or "", reverse=True)
    return plan_stats, plan_list[:_MAX_PLAN_ROWS]


def _summary(data: dict[str, Any]) -> str:
    ev = data["events"]
    lines = [f"{len(ev)} quer{'y' if len(ev) == 1 else 'ies'} that ran {data['min_ms'] / 1000:g} s or longer"
             f"{f' in the last {data['hours']:g} h' if data['hours'] else ''} (shown in the Query History app)."]
    if data["enabled_hint"]:
        lines.append(data["enabled_hint"])
    tr = data.get("trace") or {}
    if tr.get("active"):
        lines.append(f"The cluster traces queries of at least {tr.get('duration_threshold_ms')} ms"
                     f"{' and every failed one' if tr.get('trace_failed') else ''}; faster ones are only in the plan cache.")
    if data.get("plans"):
        lines.append(f"{len(data['plans'])} more statements are only in the plan cache (all runs under the threshold).")
    for e in sorted(ev, key=lambda e: -e["ms"])[:10]:
        lines.append(f"- {e['ms'] / 1000:.1f} s, {e['started']}, {e['user']}@{e['database'] or '-'}"
                     f"{'' if e['ok'] else ' FAILED'}: {e['sql'][:150]}")
    return "\n".join(lines)


def _event(key: str) -> dict[str, Any]:
    try:
        node, epoch, event_id = (int(p) for p in key.split(":"))
    except ValueError as exc:
        raise ValueError(f"Not an event key: {key!r}") from exc
    rows = _query("SELECT TIME, DETAILS FROM information_schema.MV_TRACE_EVENTS"
                  " WHERE NODE_ID = %s AND NODE_START_EPOCH_S = %s AND EVENT_ID = %s", (node, epoch, event_id))
    if not rows:
        raise LookupError("That query is no longer in the query history (the ring buffer has moved on).")
    details = rows[0]["DETAILS"]
    if isinstance(details, str):
        import json

        details = json.loads(details or "{}")
    return {"key": key, "node_id": node, "finished": jsonable(rows[0]["TIME"]), **(details or {})}


# ------------------------------------------------------------------ tuning analysis

_LEADING_COMMENTS = re.compile(r"^(\s+|/\*.*?\*/|--[^\n]*\n|#[^\n]*\n)*", re.S)
_EXPLAINABLE = re.compile(r"^(SELECT|WITH|INSERT|REPLACE|UPDATE|DELETE)\b", re.I)
_CTAS = re.compile(r"^CREATE\s+(?:(?:ROWSTORE|REFERENCE|TEMPORARY|GLOBAL)\s+)*TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                   r"(\S+)\s*(?:\(.*?\)\s*)?AS\s+(SELECT\b.*)$", re.I | re.S)
_SCAN = re.compile(r"(ColumnStoreScan|TableScan|IndexRangeScan|IndexSeek|IndexScan)\s+([\w$`.]+)")
_EST = re.compile(r"(est_table_rows|est_filtered|est_rows):([\d,]+)")


def _strip(sql: str) -> str:
    return _LEADING_COMMENTS.sub("", sql or "", count=1).strip()


def _explain_target(sql: str) -> tuple[str | None, str | None]:
    """(statement to EXPLAIN, why not) — only statements EXPLAIN can compile without running anything."""
    body = _strip(sql)
    if len(body) > 200_000:
        return None, "The statement is too long to analyze."
    if _EXPLAINABLE.match(body):
        return body, None
    m = _CTAS.match(body)
    if m:
        return m.group(2), None
    return None, "EXPLAIN only applies to SELECT, INSERT, REPLACE, UPDATE, DELETE and CREATE TABLE … AS SELECT."


def _num(text: str) -> int:
    return int(text.replace(",", ""))


def _parse_explain(lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"analyze": [], "scans": [], "broadcast": False, "repartition": False,
                           "nested_loop": False, "gather_rows": None, "warnings": [], "insert": False}
    for line in lines:
        s = line.strip()
        if s.startswith("InsertInto"):
            out["insert"] = True
        if s.upper().startswith("ANALYZE TABLE"):
            out["analyze"].append(s)
        elif s.startswith("WARNING"):
            out["warnings"].append(s)
        if "Broadcast" in s:
            out["broadcast"] = True
        if "Repartition" in s or "Reshuffle" in s:
            out["repartition"] = True
        if "NestedLoopJoin" in s:
            out["nested_loop"] = True
        if s.startswith("Gather") and "est_rows:" in s:
            out["gather_rows"] = _num(dict(_EST.findall(s)).get("est_rows", "0"))
        m = _SCAN.search(s)
        if m:
            est = {k: _num(v) for k, v in _EST.findall(s)}
            out["scans"].append({
                "op": m.group(1), "table": m.group(2).replace("`", ""),
                "unsorted": "SORT KEY __UNORDERED" in s,
                "table_type": (re.search(r"table_type:(\S+)", s) or [None, None])[1],
                "rows": est.get("est_table_rows"), "filtered": est.get("est_filtered"),
            })
    # A scan "has a filter" when the line right above it filters (ColumnStoreFilter / Filter).
    for i, line in enumerate(lines):
        m = _SCAN.search(line)
        if m:
            above = lines[i - 1].strip() if i else ""
            for sc in out["scans"]:
                if sc["table"] == m.group(2).replace("`", "") and "filtered" in sc:
                    sc["has_filter"] = above.startswith(("ColumnStoreFilter", "Filter")) or "IndexSeek" in line \
                        or "IndexRangeScan" in line
    return out


def _plan_stats(event: dict[str, Any]) -> dict[str, Any] | None:
    plan_id = event.get("plan_id")
    if plan_id in (None, -1):
        return None
    rows = _query(
        "SELECT COMMITS + ROLLBACKS AS runs, EXECUTION_TIME, AVERAGE_EXEC_TIME, AVERAGE_MEMORY_USE,"
        " AVERAGE_MAX_MEMORY_USE, AVERAGE_DISK_SPILLING_USE, QUEUED_TIME, WORKLOAD_MANAGEMENT_QUEUED_TIME,"
        " RESOURCE_POOL_QUEUED_TIME, LEAFNETWORK_TIME, CPU_TIME, ROWLOCK_TIME, LOGFLUSH_TIME,"
        " BLOB_CACHE_WAIT_TIME_MS, AVERAGE_BLOB_CACHE_MISS_BYTES, PLAN_WARNINGS, OPTIMIZER_NOTES, LAST_EXECUTED"
        " FROM information_schema.MV_PLANCACHE WHERE PLAN_ID = %s AND NODE_ID = %s LIMIT 1",
        (int(plan_id), int(event["node_id"])),
    )
    return jsonable_rows(rows)[0] if rows else None


def _notes(plan: dict[str, Any] | None) -> dict[str, Any]:
    """The plan cache's OPTIMIZER_NOTES: JSON diagnostics (table row counts, outdated statistics, broadcasts…)."""
    import json

    try:
        notes = json.loads((plan or {}).get("OPTIMIZER_NOTES") or "{}")
        return notes if isinstance(notes, dict) else {}
    except ValueError:
        return {}


def _tables(scans: list[dict[str, Any]], default_db: str | None) -> list[dict[str, Any]]:
    """SHOW CREATE TABLE for each scanned table (shard key, sort key, storage), in parallel."""
    seen: dict[str, tuple[str | None, str]] = {}
    for sc in scans:
        name = sc["table"]
        dbname, _, table = name.rpartition(".")
        seen.setdefault(name, (dbname or default_db, table))

    def show(dbname: str | None, table: str) -> dict[str, Any]:
        try:
            ddl = _query(f"SHOW CREATE TABLE {quote_identifier(table)}", database=dbname)[0]
            text = ddl.get("Create Table") or next(iter(ddl.values()))
        except Exception as exc:  # noqa: BLE001 - shown as "couldn't read"
            return {"database": dbname, "table": table, "error": str(exc)[:200]}
        shard = re.search(r"SHARD KEY\s*[`\w]*\s*\(([^)]*)\)", text, re.I)
        sort = re.search(r"SORT KEY\s*[`\w]*\s*\(([^)]*)\)", text, re.I)
        return {
            "database": dbname, "table": table,
            "reference": bool(re.search(r"CREATE\s+REFERENCE\s+TABLE", text, re.I)),
            "rowstore": bool(re.search(r"CREATE\s+ROWSTORE", text, re.I)),
            "shard_key": shard.group(1).replace("`", "").strip() if shard else None,
            "sort_key": sort.group(1).replace("`", "").strip() if sort else None,
            "ddl": text,
        }

    targets = list(seen.values())[:6]
    return list(db.parallel(*[(lambda d=d, t=t: show(d, t)) for d, t in targets])) if targets else []


def _fmt_ms(ms: float) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _fmt_rows(n: int) -> str:
    return f"{n / 1e9:.1f} billion" if n >= 1e9 else f"{n / 1e6:.1f} million" if n >= 1e6 else f"{n:,}"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def recommendations(event: dict[str, Any], explain: dict[str, Any] | None, plan: dict[str, Any] | None,
                    tables: list[dict[str, Any]], star: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Findings, most important first: {severity: high|medium|info, title, detail, sql?}.

    ``star``: {"flag": bool, "why_not": str | None, "tables": […]} from query_advisor.select_star / star_finding;
    without it SELECT * is judged from the text alone."""
    out: list[dict[str, Any]] = []

    def add(severity: str, title: str, detail: str, sql: str | None = None) -> None:
        out.append({"severity": severity, "title": title, "detail": detail, **({"sql": sql} if sql else {})})

    sql = _strip(event.get("query_text") or "")
    upper = sql.upper()
    ms = float(event.get("duration_ms") or 0)
    rows = int(event.get("row_count") or 0)
    ok = event.get("success") in (None, 1, True)

    # --- what the statement is
    if not ok:
        msg = event.get("error_message") or event.get("error_code") or "unknown error"
        if "Kafka" in msg or "broker" in msg.lower():
            add("high", "Failed: Kafka brokers unreachable",
                f"It didn't run slowly — it waited for Kafka and gave up: {msg[:300]} "
                "Check that the brokers are up and reachable from the master aggregator (network, DNS, TLS / SASL settings).")
        else:
            add("high", "The statement failed", f"{event.get('error_code') or ''} {msg[:400]}".strip())
    if re.match(r"^(ATTACH|DETACH|REBALANCE|BACKUP|RESTORE|CREATE\s+DATABASE|DROP\s+DATABASE|SNAPSHOT|OPTIMIZE)\b", upper):
        add("info", "Administrative operation",
            "This is cluster administration, not a query: its runtime depends on data volume and cluster state, "
            "and query tuning doesn't apply.")
        return out
    if re.search(r"\bSLEEP\s*\(", upper):
        add("info", "Deliberate wait", "The statement calls SLEEP(), so its runtime is intended.")
        return out

    # --- the run itself
    if rows >= 1_000_000 and upper.startswith(("SELECT", "WITH")):
        rate = rows / (ms / 1000) if ms else 0
        add("high", f"Returns {_fmt_rows(rows)} rows to the client",
            f"Most of the {_fmt_ms(ms)} is probably spent sending rows to the client ({_fmt_rows(int(rate))} rows/s). "
            "Filter or aggregate in the database instead, so only the result travels. For SAS: push the processing "
            "down (in-database procedures, implicit / explicit SQL pass-through, or SpeedyStore CAS pushdown) rather "
            "than reading the whole table.")
    if star is None:
        found = query_advisor.select_star(sql, event.get("context_database") or None)
        star = {"flag": bool(found and not found["exempt"]), "why_not": found and found["exempt"],
                "tables": found["tables"] if found else []}
    if star["flag"]:
        names = ", ".join(star["tables"][:3])
        add("medium", "SELECT * reads every column",
            f"Columnstore tables only read the columns a query names, and {names} "
            f"{'is' if len(star['tables']) == 1 else 'are'} wide. List just the columns you need; this cuts I/O and "
            "network time a lot.")
    elif star.get("why_not") and history_store.origin(sql) != "SAS EP":
        # (SAS EP calls get their own card instead: see sas_ep.analyze.)
        add("info", "SELECT * is fine here", f"It reads every column, but this is {star['why_not']}, so listing "
            "the columns wouldn't save anything.")
    is_read = upper.startswith(("SELECT", "WITH", "INSERT", "REPLACE")) or bool(_CTAS.match(sql))
    # Only statements that read a table (not SELECT 1, table functions such as sasepFunction(…)…).
    reads_table = any(not t["write"] for t in history_store.tables_in(sql, event.get("context_database")))
    if is_read and reads_table and " WHERE " not in f" {upper} " and " LIMIT " not in f" {upper} " and " JOIN " not in upper:
        add("medium", "No filter: the whole table is read",
            "There's no WHERE clause, so every row is scanned. If only part of the data is needed (a period, a "
            "region…), filter on a column in the table's SORT KEY so whole segments are skipped.")
    m = _CTAS.match(sql)
    if m and "SHARD KEY" not in upper:
        add("medium", "CREATE TABLE … AS SELECT without a shard key",
            f"The new table {m.group(1)} gets no shard key, so rows are spread randomly and later joins or "
            "group-bys on it must move data between leaves. Declare the keys in the statement, e.g. "
            "CREATE TABLE t (SHARD KEY (id), SORT KEY (date)) AS SELECT …. If it's a temporary work table "
            "(SAS _dm… tables are), consider whether it's needed at all.")
    if rows >= 10_000_000 and (upper.startswith(("INSERT", "REPLACE")) or m):
        add("info", f"Writes {_fmt_rows(rows)} rows",
            f"Writing that many rows takes time regardless of tuning ({_fmt_rows(int(rows / (ms / 1000)))} rows/s "
            "here). Check that the copy is necessary; reading the source directly or a view may avoid it.")

    # --- plan cache statistics
    if plan:
        spill = float(plan.get("AVERAGE_DISK_SPILLING_USE") or 0)
        if spill > 0:
            add("high", f"Spilled {_fmt_bytes(spill)} to disk",
                "The query needed more memory than it was allowed and wrote intermediate results to disk, which "
                "is slow. Reduce the data it has to sort / hash (filter earlier, fewer columns, pre-aggregate), or "
                "give the resource pool more memory.")
        total = float(plan.get("EXECUTION_TIME") or 0)
        queued = float(plan.get("WORKLOAD_MANAGEMENT_QUEUED_TIME") or 0) + float(plan.get("RESOURCE_POOL_QUEUED_TIME") or 0)
        if total and queued / total > 0.1:
            add("high", f"Spent {queued / total:.0%} of its time waiting in a queue",
                "Workload management or the resource pool held the query back because the cluster was busy. "
                "Spread heavy jobs out, or adjust the resource pool's concurrency / memory limits.")
        mem = float(plan.get("AVERAGE_MAX_MEMORY_USE") or plan.get("AVERAGE_MEMORY_USE") or 0)
        if mem >= 1024 ** 3:
            add("medium", f"Uses {_fmt_bytes(mem)} of memory", "A large hash table or sort is built. Filter or "
                "aggregate earlier, or join on the shard key so each leaf only handles its own rows.")
        warnings = (plan.get("PLAN_WARNINGS") or "").strip()
        if warnings:
            add("medium", "Plan warnings", warnings[:600])
        notes = _notes(plan)
        outdated = [t for t, c in notes.get("table_row_counts", {}).items() if isinstance(c, dict) and c.get("autostats_outdated")]
        if outdated:
            add("high", "Outdated table statistics",
                f"The optimizer's statistics for {', '.join(outdated)} are out of date, so its estimates may be wrong. "
                "Refresh them:", "\n".join(f"ANALYZE TABLE {t};" for t in outdated))

    # --- EXPLAIN
    if explain:
        if explain["analyze"]:
            add("high", "Missing column statistics",
                "The optimizer has no histograms for columns this query filters on, so its row estimates and join "
                "order may be poor. Collect them (once; they then stay up to date):",
                "\n".join(explain["analyze"]))
        if explain["broadcast"]:
            add("medium", "A table is broadcast to every leaf",
                "One side of a join is copied to all leaves. Fine for small tables (make them REFERENCE tables); "
                "for big ones, join on the shard keys of both tables.")
        if explain["repartition"]:
            add("medium", "Data is reshuffled between leaves",
                "A join or group-by isn't on the shard key, so rows are redistributed first. Shard both tables on "
                "the join column (or group on the shard key) to keep the work local.")
        if explain["nested_loop"]:
            add("high", "Nested-loop join",
                "A join has no usable equality condition or index, so every row is compared with every row. Add an "
                "equality join condition, or an index on the join column.")
        # INSERT … SELECT writes on the leaves (local:yes); its Gather only collects row counts.
        if explain["gather_rows"] and explain["gather_rows"] >= 1_000_000 and not explain["insert"]:
            add("medium", f"About {_fmt_rows(explain['gather_rows'])} rows are gathered on the aggregator",
                "The leaves send a large result to the aggregator. Aggregate or filter before it is gathered.")
        by_table = {f"{t['database']}.{t['table']}": t for t in tables if not t.get("error")}
        counts = _notes(plan).get("table_row_counts", {}) if plan else {}
        moves_data = explain["broadcast"] or explain["repartition"] or bool(re.search(r"\b(JOIN|GROUP\s+BY)\b", upper))
        for sc in explain["scans"]:
            t = by_table.get(sc["table"]) or next((v for k, v in by_table.items() if k.endswith("." + sc["table"])), None)
            if sc.get("rows") is None:  # EXPLAIN without estimates: use the optimizer's row count
                known = counts.get(sc["table"]) or next((c for k, c in counts.items() if k.endswith("." + sc["table"])), None)
                sc["rows"] = known.get("rowcount") if isinstance(known, dict) else None
            big = (sc.get("rows") or 0) >= 1_000_000
            if sc["unsorted"] and big and sc.get("has_filter"):
                add("medium", f"{sc['table']} has no sort key",
                    f"The table ({_fmt_rows(sc['rows'])} rows) is filtered, but without a SORT KEY every segment "
                    "must be read. A sort key on the column you usually filter on (often a date) lets SingleStore "
                    "skip segments.",
                    f"-- e.g. recreate with: SORT KEY (<filter column>)\nSHOW CREATE TABLE {sc['table']};")
            if sc["op"] == "TableScan" and big:
                add("medium", f"Full scan of rowstore table {sc['table']}",
                    "Every row of an in-memory table is read. Add an index on the filtered / joined columns.")
            if t and t.get("shard_key") == "" and not t.get("reference") and big and moves_data:
                add("info", f"{sc['table']} is keyless",
                    "SHARD KEY () spreads rows evenly but randomly, so joins and group-bys on it always move data. "
                    "Give it a shard key on its main join column if it's joined often.")

    if not any(f["severity"] in ("high", "medium") for f in out) and ok and history_store.origin(sql) != "SAS EP":
        add("info", "No obvious problems found",
            f"The plan looks reasonable; {_fmt_ms(ms)} is likely what this much data costs. To see exactly where the "
            "time goes, PROFILE the statement (note: PROFILE runs it again) and look at the slowest operators.",
            f"PROFILE {sql[:2000]};\nSHOW PROFILE;" if len(sql) < 2000 else None)
    order = {"high": 0, "medium": 1, "info": 2}
    out.sort(key=lambda f: order[f["severity"]])
    return out


def analyze(key: str) -> dict[str, Any]:
    return analyze_event(_event(key))


def analyze_event(event: dict[str, Any]) -> dict[str, Any]:
    """Tuning analysis of one query: ``event`` has at least query_text and context_database (a history
    event, or a statement from a workload run with its measured duration_ms)."""
    target, why_not = _explain_target(event.get("query_text") or "")
    database = event.get("context_database") or None

    def run_explain() -> dict[str, Any]:
        if not target:
            return {"lines": [], "error": why_not}
        try:
            rows = _query(f"EXPLAIN {target}", database=database)
            return {"lines": [next(iter(r.values())) or "" for r in rows], "error": None}
        except Exception as exc:  # noqa: BLE001 - e.g. a temp table that no longer exists
            return {"lines": [], "error": f"EXPLAIN failed: {str(exc)[:300]}"}

    def run_plan() -> dict[str, Any] | None:
        try:
            return _plan_stats(event)
        except Exception:  # noqa: BLE001 - plan cache not readable: analyze without it
            return None

    def run_explain_json() -> dict[str, Any] | None:
        if not target:
            return None
        try:
            return _explain_json(f"EXPLAIN JSON {target}", database)
        except Exception:  # noqa: BLE001 - the text EXPLAIN reports the error
            return None

    found = query_advisor.select_star(event.get("query_text") or "", database)

    def run_counts() -> dict[str, int]:
        if not found or found["exempt"]:
            return {}
        try:
            return query_advisor.column_counts(found["tables"])
        except Exception:  # noqa: BLE001 - unknown widths: flagged as before
            return {}

    explained, plan, tree, counts = db.parallel(run_explain, run_plan, run_explain_json, run_counts)
    flag, why_not = query_advisor.star_finding(found, counts)
    star = {"flag": flag, "why_not": why_not, "tables": found["tables"] if found else []}
    # EXPLAIN JSON is complete (the text form can be cut short on big plans), so it's preferred for the findings.
    parsed = _parse_explain_json(tree) if tree else _parse_explain(explained["lines"]) if explained["lines"] else None
    tables = _tables(parsed["scans"], database) if parsed else []
    recs = recommendations(event, parsed, plan, tables, star)
    return {
        "event": jsonable(event),
        "explain": explained,
        "tree": tree,
        "plan": plan,
        "plan_note": None if plan or event.get("plan_id") in (None, -1)
        else "The plan is no longer in the plan cache, so memory / spilling / queueing statistics aren't available.",
        "tables": [{k: v for k, v in t.items() if k != "ddl"} | {"ddl": t.get("ddl")} for t in tables],
        "recommendations": recs,
        "stats_tables": _stats_tables(parsed, tables, database),
        "profile_allowed": _profile_target(event.get("query_text") or "")[0] is not None,
        "sas_ep": _ep_card(event, plan),
    }


def _ep_card(event: dict[str, Any], plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """The SAS EP card (input skew, partitions vs cores, data in, waits, trend) for SAS EP statements."""
    if history_store.origin(_strip(event.get("query_text") or "")) != "SAS EP":
        return None
    if not plan:
        # This run's plan was recompiled or evicted: use the statement's plan as the local copy kept it.
        try:
            key = history_store.statement_key(event.get("query_text"), event.get("context_database"))
            kept = [p for p in history_store.stored_plans(limit=3000) if p["stmt"] == key]
        except Exception:  # noqa: BLE001 - the card says the statistics aren't available
            kept = []
        if kept:
            p = max(kept, key=lambda x: x.get("last_executed") or "")
            plan = {"runs": p["runs"], "AVERAGE_EXEC_TIME": p["avg_ms"], "BLOB_CACHE_WAIT_TIME_MS": p["blob_wait_ms"],
                    "AVERAGE_BLOB_CACHE_MISS_BYTES": p["avg_blob_miss"], "AVERAGE_MAX_MEMORY_USE": p["avg_max_mem"],
                    "AVERAGE_MEMORY_USE": p["avg_mem"], "AVERAGE_DISK_SPILLING_USE": p["avg_spill"],
                    "QUEUED_TIME": p["queued_ms"]}
    try:
        return sas_ep.analyze(event, plan)
    except Exception as exc:  # noqa: BLE001 - the rest of the analysis still stands
        return {"error": f"The SAS EP analysis failed: {str(exc)[:200]}", "points": []}


# ------------------------------------------------------------------ EXPLAIN JSON / PROFILE JSON as a tree
# Verified on 9.0: EXPLAIN JSON <stmt> and SHOW PLAN JSON <plan_id> return {"explain": [operator…],
# "missing_histograms": {db: {`table`: [`col`]}}, "plan_warnings": {…}}; SHOW PROFILE JSON (after PROFILE) returns
# {"profile": [operator…], "query_info": {"total_runtime_ms"…}, "execution_warnings"…}, operators carrying
# {"actual_row_count": {"value"…}, "actual_total_time": {…}, "memory_usage": {…}, "segments_scanned": {…}…}.
# Each operator has "executor" and its children in "inputs" (and "subselects").

_KEEP = ("table", "db", "alias", "index", "table_type", "storage", "est_rows", "est_table_rows", "est_filtered",
         "est_select_cost", "local_est_rows", "type", "partitions", "parallelism_level", "limit", "est_rows_source",
         "est_filtered_source", "columnstore_in_memory_scan_type", "destination_database")
_KEEP_LISTS = ("condition", "groups", "order", "shard_key")
_METRICS = ("actual_row_count", "actual_total_time", "memory_usage", "spill_outputted_rows", "segments_scanned",
            "segments_skipped", "segments_fully_contained", "network_traffic", "network_time",
            "columnstore_filter_total_rows_in", "number_of_blocks_eliminated_for_block_elim")
_MOVES = ("Broadcast", "Repartition", "ShuffleGroupBy")
_NL_BIG = 100_000            # rows on a nested-loop join's input that make it worth flagging
_SCANS = ("ColumnStoreScan", "TableScan", "IndexRangeScan", "IndexSeek", "IndexScan", "ColumnStoreScanByKey")


def _int(v: Any) -> int | None:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _metric(node: dict[str, Any], name: str) -> int | None:
    v = node.get(name)
    return _int(v.get("value")) if isinstance(v, dict) else None


def _slim(node: dict[str, Any], parent: str | None, profiled: bool) -> dict[str, Any]:
    """One operator for the page: the fields worth showing, its metrics, its children and what stands out."""
    ex = str(node.get("executor") or "?")
    out: dict[str, Any] = {"op": ex}
    for k in _KEEP:
        if node.get(k) not in (None, "", []):
            out[k] = node[k]
    for k in _KEEP_LISTS:
        if isinstance(node.get(k), list) and node[k]:
            out[k] = [" ".join(str(x).split())[:200] for x in node[k][:6]]
    for k in _METRICS:
        m = _metric(node, k)
        if m is not None:
            out.setdefault("m", {})[k] = m
    if node.get("query"):
        out["query"] = " ".join(str(node["query"]).split())[:400]
    flags: list[list[str]] = []
    rows = _int(node.get("est_table_rows")) or 0
    est = _int(node.get("est_rows")) or 0
    m = out.get("m", {})
    if ex.startswith("Broadcast"):
        flags.append(["warn", "Broadcast: these rows are copied to every leaf"])
    elif ex in ("Repartition", "ShuffleGroupBy"):
        key = ", ".join(node.get("shard_key") or [])
        flags.append(["warn", f"Reshuffle: rows move between leaves{f' (re-sharded on {key})' if key else ''}, because "
                              "the join / group-by isn't on the shard key"])
    elif ex == "NestedLoopJoin":
        sides = [_first_rows(c) for c in node.get("inputs") or [] if isinstance(c, dict)]
        if any(n is None or n >= _NL_BIG for n in sides) or not sides:
            flags.append(["crit", "Nested-loop join: every row is compared with every row"])
    elif ex == "HashTableBuild":
        build = _first_rows(node)
        if build and build >= 1_000_000:
            flags.append(["warn", f"Hash join builds a hash table from about {build:,} rows"])
    elif ex in _SCANS and node.get("storage") != "list":
        filtered = parent in ("ColumnStoreFilter", "Filter")
        if "__UNORDERED" in str(node.get("index") or "") and filtered and rows >= 1_000_000:
            flags.append(["warn", "Full scan: the table has no sort key, so no segments can be skipped for this filter"])
        elif ex == "TableScan" and "rowstore" in str(node.get("table_type") or "") and rows >= 1_000_000:
            flags.append(["warn", "Full scan of a rowstore table: an index on the filtered column would avoid it"])
        if profiled and filtered and (m.get("segments_scanned") or 0) > 0 and not m.get("segments_skipped") and rows >= 1_000_000:
            flags.append(["warn", f"No segment elimination: all {m['segments_scanned']:,} segments were read"])
    if ex == "Gather" and est >= 1_000_000:
        flags.append(["warn", f"About {est:,} rows are gathered on the aggregator"])
    if (m.get("spill_outputted_rows") or 0) > 0:
        flags.append(["crit", f"Spilled {m['spill_outputted_rows']:,} rows to disk"])
    if flags:
        out["flags"] = flags
    kids = [c for c in (node.get("inputs") or []) if isinstance(c, dict)]
    subs = [c for c in (node.get("subselects") or []) if isinstance(c, dict)]
    if kids or subs:
        out["inputs"] = [_slim(c, ex, profiled) for c in kids] + [{**_slim(c, ex, profiled), "subselect": True} for c in subs]
    return out


def _first_rows(node: dict[str, Any]) -> int | None:
    """The nearest row estimate at or below an operator (a hash table's build side)."""
    for key in ("est_rows", "est_filtered", "est_table_rows"):
        if _int(node.get(key)):
            return _int(node.get(key))
    for c in node.get("inputs") or []:
        n = _first_rows(c)
        if n:
            return n
    return None


def _tree(doc: dict[str, Any], key: str) -> dict[str, Any]:
    profiled = key == "profile"
    missing = []
    for dbname, tbls in (doc.get("missing_histograms") or {}).items():
        for t, cols in (tbls or {}).items():
            missing.append({"database": dbname, "table": t.strip("`"), "columns": [c.strip("`") for c in cols]})
    info = doc.get("query_info") or {}
    return {
        "kind": key,
        "operators": [_slim(n, None, profiled) for n in doc.get(key) or [] if isinstance(n, dict)],
        "missing_histograms": missing,
        "warnings": [str(w)[:400] for w in (doc.get("plan_warnings") or {}).values()][:10]
        + [str(w)[:400] for w in (doc.get("execution_warnings") or {}).values()][:10],
        "total_ms": _int(info.get("total_runtime_ms")),
    }


def _load_json_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    import json

    text = "\n".join(str(next(iter(r.values())) or "") for r in rows)
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("Unexpected EXPLAIN JSON")
    return doc


_RAW_MAX_CHARS = 300_000


def _with_raw(tree: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """The tree plus the raw JSON for the page's "Raw" toggle, unless it's very large."""
    import json

    raw = json.dumps(doc, indent=1)
    return {**tree, "raw": raw if len(raw) <= _RAW_MAX_CHARS else None}


def _explain_json(statement: str, database: str | None) -> dict[str, Any]:
    doc = _load_json_rows(_query(statement, database=database))
    return _with_raw(_tree(doc, "explain"), doc)


def _parse_explain_json(tree: dict[str, Any]) -> dict[str, Any]:
    """The same findings as _parse_explain, from EXPLAIN JSON (complete where the text form may be cut short)."""
    out: dict[str, Any] = {"analyze": [], "scans": [], "broadcast": False, "repartition": False,
                           "nested_loop": False, "gather_rows": None, "warnings": [], "insert": False}
    for m in tree.get("missing_histograms") or []:
        out["analyze"].append(f"ANALYZE TABLE {m['database']}.{quote_identifier(m['table'])} COLUMNS "
                              f"{', '.join(quote_identifier(c) for c in m['columns'])} ENABLE;")

    def walk(n: dict[str, Any], parent: str | None) -> None:
        op = n["op"]
        if op.startswith("InsertInto"):
            out["insert"] = True
        if op.startswith("Broadcast"):
            out["broadcast"] = True
        if op in ("Repartition", "ShuffleGroupBy") or "Reshuffle" in op:
            out["repartition"] = True
        if op == "NestedLoopJoin":
            # Only worth a finding when an input is large (or unknown): on a few hundred rows it's cheap.
            sides = [_first_rows(c) for c in n.get("inputs") or []]
            if not sides or any(x is None or x >= _NL_BIG for x in sides):
                out["nested_loop"] = True
        if op == "Gather" and out["gather_rows"] is None and n.get("est_rows"):
            out["gather_rows"] = _int(n["est_rows"])
        if op in _SCANS and n.get("table") and n.get("storage") != "list":
            name = f"{n['db']}.{n['table']}" if n.get("db") else str(n["table"])
            out["scans"].append({
                "op": op, "table": name, "unsorted": "__UNORDERED" in str(n.get("index") or ""),
                "table_type": n.get("table_type"), "rows": _int(n.get("est_table_rows")),
                "filtered": _int(n.get("est_filtered")),
                "has_filter": parent in ("ColumnStoreFilter", "Filter") or op in ("IndexSeek", "IndexRangeScan"),
            })
        for c in n.get("inputs") or []:
            walk(c, op)

    for n in tree.get("operators") or []:
        walk(n, None)
    out["warnings"] = list(tree.get("warnings") or [])
    return out


def _stats_tables(parsed: dict[str, Any] | None, tables: list[dict[str, Any]], database: str | None) -> list[dict[str, Any]]:
    """The tables the query reads (for "Analyze table(s)"), per database, and whether statistics look missing."""
    if not parsed:
        return []
    missing = set()
    for line in parsed["analyze"]:
        m = re.search(r"ANALYZE TABLE\s+(\S+)", line, re.I)
        if m:
            missing.add(m.group(1).replace("`", ""))
    out: dict[str, dict[str, Any]] = {}
    for sc in parsed["scans"]:
        dbname, _, table = sc["table"].rpartition(".")
        dbname = dbname or database
        if not dbname or dbname.lower() in ("information_schema",):
            continue
        key = f"{dbname}.{table}"
        out.setdefault(key, {"database": dbname, "table": table,
                             "missing_stats": key in missing or table in missing, "rows": sc.get("rows")})
    return list(out.values())


def analyze_plan(plan_key: str) -> dict[str, Any]:
    """Tuning analysis of a plan-cache-only statement: its cached plan (SHOW PLAN JSON, on the aggregator that
    compiled it — only possible when that's the one this server is connected to) and its totals."""
    p = history_store.stored_plan(plan_key)
    if not p:
        raise LookupError("That statement is no longer in the plan cache or the local copy.")
    event = {
        "query_text": p["text"], "context_database": p["database"], "duration_ms": p["avg_ms"],
        "row_count": round((p["rows"] or 0) / p["runs"]) if p["runs"] else 0, "success": True,
        "plan_id": p["plan_id"], "node_id": p["node_id"], "user_name": None, "aggregate": True,
    }
    local = _query("SELECT NODE_ID FROM information_schema.LMV_NODES")
    here = int(local[0]["NODE_ID"]) if local else None
    tree, lines, note = None, [], None
    if here == p["node_id"]:
        # The cached plan itself; only on the aggregator that compiled it, and only when the cluster kept its
        # explain information (enable_disk_plan_explain, verified: error 2394 otherwise).
        def text() -> list[str]:
            return [next(iter(r.values())) or "" for r in _query(f"SHOW PLAN {int(p['plan_id'])}")]

        try:
            doc, lines = db.parallel(lambda: _load_json_rows(_query(f"SHOW PLAN JSON {int(p['plan_id'])}")), text)
            tree = _with_raw(_tree(doc, "explain"), doc)
        except Exception as exc:  # noqa: BLE001 - evicted, or no explain kept with the plan
            note = f"SHOW PLAN isn't available for it ({str(exc)[:160]})."
    else:
        note = f"The cached plan is on node {p['node_id']}; SHOW PLAN only reads the connected aggregator's plans."
    if tree is None:
        # EXPLAIN the statement with sample values in place of its parameters (it compiles, nothing runs). The
        # plan's shape (scans, joins, data movement) doesn't depend on the values; the row estimates may.
        target, _why = _explain_target(history_store.PLAN_PARAM.sub(lambda m: "1" if m.group(0) == "@" else "''", p["text"]))
        if target and (p.get("text_len") or 0) <= len(p["text"]):
            try:
                tree, lines = db.parallel(
                    lambda: _explain_json(f"EXPLAIN JSON {target}", p["database"]),
                    lambda: [next(iter(r.values())) or "" for r in _query(f"EXPLAIN {target}", database=p["database"])])
                note = (note + " " if note else "") + ("Plan below: EXPLAIN of the statement with sample values (1 / '') "
                                                       "in place of its parameters, so row estimates may differ.")
            except Exception as exc:  # noqa: BLE001 - e.g. a type mismatch with the sample values
                note = (note + " " if note else "") + f"EXPLAIN with sample values failed: {str(exc)[:160]}"
    parsed = _parse_explain_json(tree) if tree else None
    tables = _tables(parsed["scans"], p["database"]) if parsed else []
    stats = {"runs": p["runs"], "EXECUTION_TIME": p["exec_ms"], "AVERAGE_EXEC_TIME": p["avg_ms"],
             "AVERAGE_MEMORY_USE": p["avg_mem"], "AVERAGE_MAX_MEMORY_USE": p["avg_max_mem"],
             "AVERAGE_DISK_SPILLING_USE": p["avg_spill"], "CPU_TIME": p["cpu_ms"], "QUEUED_TIME": p["queued_ms"],
             "BLOB_CACHE_WAIT_TIME_MS": p["blob_wait_ms"], "AVERAGE_BLOB_CACHE_MISS_BYTES": p["avg_blob_miss"],
             "PLAN_WARNINGS": p["warnings"], "LAST_EXECUTED": p["last_executed"], "FIRST_LOAD_TIME": p["first_load"]}
    found = query_advisor.select_star(p["text"], p["database"])
    counts = query_advisor.column_counts(found["tables"]) if found and not found["exempt"] else {}
    flag, why_not = query_advisor.star_finding(found, counts)
    recs = recommendations(event, parsed, stats, tables, {"flag": flag, "why_not": why_not,
                                                          "tables": found["tables"] if found else []})
    return {
        "event": jsonable(event), "explain": {"lines": lines, "error": None if lines else note}, "tree": tree,
        "plan": jsonable(stats), "plan_note": note,
        "tables": [{k: v for k, v in t.items() if k != "ddl"} | {"ddl": t.get("ddl")} for t in tables],
        "recommendations": recs, "stats_tables": _stats_tables(parsed, tables, p["database"]),
        "profile_allowed": False,
        "sas_ep": _ep_card(event, stats),
    }


# ------------------------------------------------------------------ PROFILE (runs the statement; read-only only)

def _profile_target(sql: str) -> tuple[str | None, str | None]:
    """(statement to PROFILE, why not): only read-only SELECT / WITH, never SELECT … INTO or locking reads."""
    from .. import compare

    body = _strip(sql)
    if not compare._READ_ONLY.match(body):
        return None, "Only SELECT / WITH statements can be profiled here (PROFILE runs the statement)."
    if history_store.origin(body) == "SAS EP":
        return None, "A SAS Embedded Process call runs a SAS job; it isn't profiled."
    if compare._WRITES.search(history_store.strip_comments(body)):
        return None, "This statement writes (INTO / FOR UPDATE / DML), so it isn't profiled."
    return body, None


def profile_event(key: str, timeout_s: float = 300) -> dict[str, Any]:
    """PROFILE one read-only query from the history (it really runs) and read SHOW PROFILE JSON.

    Runs on its own connection (PROFILE and SHOW PROFILE must share a session), tagged so it stays out of the
    history; after ``timeout_s`` it's cancelled with KILL QUERY (after checking that connection still runs it)."""
    import json
    import uuid

    import singlestoredb as s2

    from .. import compare, connections

    event = _event(key)
    target, why_not = _profile_target(event.get("query_text") or "")
    if not target:
        raise ValueError(why_not)
    job = uuid.uuid4().hex[:10]
    # The marker carries "job=…" so compare._kill finds (and only cancels) this very statement.
    marker = f"{_MARKER[:-3]} job={job} */"
    conn = s2.connect(**connections.connect_kwargs(connections.active()), autocommit=True, parse_json=False)
    timer = None
    try:
        cur = conn.cursor()
        database = event.get("context_database") or None
        if database:
            cur.execute(f"USE {quote_identifier(database)}")
        cur.execute("SELECT CONNECTION_ID() AS id")
        conn_id = int(_first_value(cur.fetchone()))
        killed = {"done": False}

        def on_timeout() -> None:
            try:
                killed["done"] = compare._kill(conn_id, job)
            except Exception:  # noqa: BLE001 - reported as a timeout either way
                killed["done"] = True

        timer = threading.Timer(max(5.0, float(timeout_s)), on_timeout)
        started = time.perf_counter()
        timer.start()
        try:
            cur.execute(f"{marker} PROFILE {target}")
            while cur.fetchmany(10000):
                pass  # read and drop the rows: the profile is what matters
        except Exception as exc:
            if killed["done"]:
                raise ValueError(f"PROFILE was cancelled after {timeout_s:g} s.") from exc
            raise
        finally:
            timer.cancel()
        wall_ms = round((time.perf_counter() - started) * 1000)
        cur.execute("SHOW PROFILE JSON")
        row = cur.fetchone()
        doc = json.loads(_first_value(row) or "{}")
    finally:
        if timer:
            timer.cancel()
        conn.close()
    return {**_with_raw(_tree(doc, "profile"), doc), "wall_ms": wall_ms}


def _first_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def _analysis_summary(a: dict[str, Any]) -> str:
    e = a["event"]
    lines = [f"Tuning analysis of a {float(e.get('duration_ms') or 0) / 1000:.1f} s query by {e.get('user_name')} "
             f"in {e.get('context_database') or '-'}:", sas_ep.masked(" ".join((e.get("query_text") or "").split()))[:500]]
    for r in a["recommendations"]:
        lines.append(f"- [{r['severity']}] {r['title']}: {r['detail']}")
    lines += _ep_lines(a.get("sas_ep"))
    return "\n".join(lines)


def _ep_lines(ep: dict[str, Any] | None) -> list[str]:
    """The SAS EP card as text (tool summary, Claude's prompt)."""
    if not ep:
        return []
    if ep.get("bookkeeping") or ep.get("error"):
        return [f"SAS EP: {ep.get('summary') or ep.get('error')}"]
    if ep.get("kind") == "format":
        out = [f"SAS EP format function (SAS VA / CAS): {', '.join(f['name'] + '(' + ', '.join(f['args']) + ')' for f in ep.get('functions', []))}"
               f" on {', '.join(ep.get('inputs') or []) or '?'}"
               + (f", partition {ep['partition']} of a parallel load" if ep.get("partition") is not None else "") + ":"]
    else:
        out = [f"SAS EP call ({ep.get('command')}), input {ep.get('input')}, output {ep.get('output')}:"]
    for p in ep.get("points", []):
        v = p.get("verdict") or {}
        out.append(f"- {p['title']}: {p.get('value') or ''} [{v.get('level')}] {v.get('text', '')}"
                   + (f" Next step: {v['step']}" if v.get("step") else ""))
    return out


# ------------------------------------------------------------------ tools


@apps.tool(resource_uri=URI, title="Query History", annotations=READ_ONLY)
def query_history(min_seconds: float = 1.0, hours: float | None = None, tab: str = "queries") -> CallToolResult:
    """Open the Query History: every finished query on the cluster with its runtime, user, database and
    result, filtered by minimum runtime (1 s and up). Select a query in the app for tuning recommendations.

    Uses SingleStore's query history (event tracing, information_schema.MV_TRACE_EVENTS). Use this when the
    user asks about slow / long-running queries or jobs over time (the Cluster Monitor shows only what's
    running now).

    Args:
        min_seconds: Only queries that ran at least this long (minimum 1).
        hours: Only the last N hours (default: everything in the history).
        tab: "queries" (default), "trends" (daily / hourly load, failures and p95 from a local copy of the history
            kept across the cluster's ring buffer, plus queries that got slower), "usage" (most used tables and
            most active users) or "advisor" (table-design advice from the whole history).

    The result includes ``browser_url``: post it as a clickable link right under the app.
    """
    tab = tab if tab in ("advisor", "trends", "usage") else "queries"
    full = collect(int(max(1.0, min_seconds) * 1000), hours)
    data = {**_first_slice(full), "tab": tab}
    args = {"min_seconds": min_seconds, **({"hours": hours} if hours else {}), **({"tab": tab} if tab != "queries" else {})}
    return tool_result(with_browser_link(_summary(full), data, "query_history", args), data)


# What a model-visible tool sends along must stay small (Claude limits the size of a tool result), so it
# carries only the latest queries; the page then loads the full list itself with query_history_data.
_FIRST_SLICE = 40


def _first_slice(data: dict[str, Any], keep: int = _FIRST_SLICE) -> dict[str, Any]:
    events = data["events"]
    # The plan cache statements and statistics come with the full list (query_history_data), not here.
    return {**data, "events": [{k: v for k, v in e.items() if k != "tables"} for e in events[:keep]],
            "partial": len(events) > keep or bool(data.get("plans")),
            "plans": [], "plan_stats": {}}


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_data(min_ms: int = MIN_RUNTIME_MS, hours: float | None = None, limit: int = _MAX_EVENTS) -> CallToolResult:
    """Refresh the Query History list."""
    data = collect(min_ms, hours, limit)
    return tool_result(_summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_event(key: str) -> CallToolResult:
    """One query from the history with its full SQL and details."""
    event = jsonable(_event(key))
    return tool_result(" ".join((event.get("query_text") or "").split())[:300], {"event": event})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_tuning(key: str) -> CallToolResult:
    """Tuning recommendations for one query from the history (EXPLAIN, plan cache statistics, table design)."""
    a = analyze(key)
    return tool_result(_analysis_summary(a), a)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_plan_tuning(plan_key: str) -> CallToolResult:
    """Tuning recommendations for a statement known only from the plan cache (its totals and cached plan)."""
    a = analyze_plan(plan_key)
    return tool_result(_analysis_summary(a), a)


# PROFILE really runs the statement (read-only SELECT / WITH only).
RUNS_QUERY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=RUNS_QUERY)
def query_history_profile(key: str, timeout_s: float = 300) -> CallToolResult:
    """PROFILE one read-only query (SELECT / WITH) from the history: runs it once more and returns the operator
    tree with actual rows, time, memory, spilling and segments skipped (SHOW PROFILE JSON)."""
    data = profile_event(key, timeout_s)
    return tool_result(f"Profiled: {data.get('total_ms') or data['wall_ms']} ms.", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_usage(days: float = 7) -> CallToolResult:
    """Most used tables and most active users over the last ``days`` (0: everything stored), from the local
    copy of the query history (traced runs) and of the plan cache (every execution, no user)."""
    synced, now, _plans = db.parallel(
        lambda: history_store.safe_sync(force=True),
        lambda: _query("SELECT NOW() AS t")[0]["t"],
        lambda: history_store.snapshot_plans(force=True),
    )
    since = None
    if days and float(days) > 0:
        import datetime as _dt

        since = (now - _dt.timedelta(days=float(days))).isoformat(sep=" ")
    data = history_store.usage(since)
    data.update(days=days, now=jsonable(now), sync=synced)
    lines = [f"Usage over {'everything stored' if not since else f'the last {float(days):g} days'}: "
             f"{data['table_count']} tables, {data['user_count']} users."]
    lines += [f"- {t['database']}.{t['table']}: {t['execs'] or t['runs']} runs" for t in data["tables"][:5]]
    return tool_result("\n".join(lines), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_advisor(refresh: bool = False) -> CallToolResult:
    """Table-design advice from the whole query history: sort keys, shard keys, reference tables, indexes,
    statistics, plus workload findings. EXPLAINs every distinct query (without running it)."""
    result = query_advisor.advise(refresh=refresh)
    return tool_result(query_advisor.summary(result), result)


@apps.tool(resource_uri=URI, title="Query advisor", annotations=READ_ONLY)
def query_advisor_report(refresh: bool = False) -> CallToolResult:
    """Recommend table design changes from the whole query history: SORT KEY and SHARD KEY per table, REFERENCE
    table candidates, indexes and missing statistics, plus workload issues (huge results, SELECT *, failures).

    Every distinct query in the history is EXPLAINed (compiled, not run) to see which columns it filters,
    joins and groups on, weighted by how long those queries ran. Opens the Query History app on its Advisor tab.

    The result includes ``browser_url``: post it as a clickable link right under the app.
    """
    result = query_advisor.advise(refresh=refresh)
    data = {**_first_slice(collect(), 0), "advisor": result, "tab": "advisor"}
    return tool_result(with_browser_link(query_advisor.summary(result), data, "query_history", {"tab": "advisor"}), data)


# ------------------------------------------------------------------ Claude, in the app
# Like the SQL Editor's chat: headless Claude Code with read-only database tools answers in the app, so it
# works in the desktop window and the browser too (no Claude chat needed).

_answers: dict[str, dict[str, Any]] = {}
_answers_lock = threading.Lock()


# One Claude process serves the whole screen (warmed when a query is opened), answering one query at a time.
_WORKER_ID = "qh-history"
_asking: dict[str, str | None] = {"key": None}


def _deliver(editor_id: str, text: str, kind: str) -> None:
    with _answers_lock:
        key = _asking["key"] or editor_id
        _asking["key"] = None
        _answers[key] = {"text": text, "kind": kind, "t": round(time.time(), 1)}
        while len(_answers) > 50:
            _answers.pop(next(iter(_answers)))


def _tuning_prompt(a: dict[str, Any], question: str | None) -> str:
    e = a["event"]
    recs = "\n".join(f"- [{r['severity']}] {r['title']}: {r['detail']}" + (f"\n  {r['sql']}" if r.get("sql") else "")
                     for r in a["recommendations"])
    tables = "\n\n".join(t["ddl"] for t in a["tables"] if t.get("ddl"))
    plan = a.get("plan") or {}
    stats = ", ".join(f"{k}={plan[k]}" for k in ("runs", "AVERAGE_EXEC_TIME", "AVERAGE_MAX_MEMORY_USE",
                                                  "AVERAGE_DISK_SPILLING_USE", "WORKLOAD_MANAGEMENT_QUEUED_TIME",
                                                  "CPU_TIME") if k in plan)
    return "\n\n".join(p for p in [
        f"Question: {question or 'How can this query be made faster? Give concrete, prioritized recommendations: rewritten SQL where it helps, and table design changes (shard key, sort key, reference tables, statistics) with the statements to run. Say what you expect each change to gain.'}",
        f"A query from the SingleStore query history (context database: {e.get('context_database') or '(none)'}). "
        f"It ran {float(e.get('duration_ms') or 0) / 1000:.1f} s, user {e.get('user_name')}, "
        f"{'succeeded' if e.get('success') in (None, 1, True) else 'failed: ' + str(e.get('error_message') or e.get('error_code'))}, "
        f"{e.get('row_count')} rows, started {e.get('start_time')}.",
        f"```sql\n{sas_ep.masked(e.get('query_text') or '')[:12000]}\n```",
        f"The Query History app's own analysis:\n{recs}" if recs else "",
        "\n".join(_ep_lines(a.get("sas_ep"))),
        "EXPLAIN:\n```\n" + "\n".join(a["explain"]["lines"])[:6000] + "\n```" if a["explain"]["lines"] else "",
        f"Plan cache statistics (times in ms, memory in bytes): {stats}" if stats else "",
        f"Tables:\n```sql\n{tables[:6000]}\n```" if tables else "",
        "You can check things with the read-only database tools (e.g. row counts, column cardinality, "
        "information_schema). Don't run the slow query itself.",
    ] if p)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_assistant() -> CallToolResult:
    """Whether Claude can answer in the app (Claude Code installed)."""
    info = assistant.status()
    return tool_result("available" if info["available"] else info["reason"], info)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_ask(key: str, question: str | None = None, profile: str | None = None) -> CallToolResult:
    """Ask Claude, in the app, how to tune one query from the history; poll query_history_answer for the reply."""
    a = analyze(key)
    with _answers_lock:
        if _asking["key"] and assistant.job_state(_WORKER_ID):
            raise ValueError("Claude is still answering about another query; wait for it or press Stop.")
        _answers.pop(key, None)
        _asking["key"] = key
    try:
        assistant.ask(_WORKER_ID, _tuning_prompt(a, question), _deliver, profile)
    except Exception:
        _asking["key"] = None
        raise
    return tool_result("Claude is looking at the query.", {"started": True})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_answer(key: str) -> CallToolResult:
    """Claude's in-app answer for one query (or its progress while it works)."""
    with _answers_lock:
        answer = _answers.get(key)
        job = assistant.job_state(_WORKER_ID) if _asking["key"] == key else None
    return tool_result(answer["text"][:300] if answer else "working", {"answer": answer, "job": job})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_ask_cancel(key: str) -> CallToolResult:
    """Stop Claude's in-app answer for one query."""
    stopped = _asking["key"] == key and assistant.cancel(_WORKER_ID)
    return tool_result("Stopped." if stopped else "Nothing to stop.", {"stopped": bool(stopped)})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_assistant_warm() -> CallToolResult:
    """Start the screen's Claude process ahead of the first question (no model call)."""
    started = assistant.warm(_WORKER_ID)
    return tool_result("Assistant ready." if started else "Assistant unavailable.", {"warm": started})
