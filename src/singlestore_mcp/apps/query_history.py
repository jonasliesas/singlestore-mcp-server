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

from .. import assistant, query_advisor
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
    params: list[Any] = [min_ms, f"%{_MARKER}%"]
    if hours:
        where.append("TIME >= NOW() - INTERVAL %s SECOND")
        params.append(int(float(hours) * 3600))
    rows, info, offset = db.parallel(
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
    )
    events = [{
        "key": f"{r['NODE_ID']}:{r['NODE_START_EPOCH_S']}:{r['EVENT_ID']}",
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
        "sql": " ".join((r["sql_head"] or "").split()),
        "sql_truncated": (r["sql_len"] or 0) > _LIST_SQL_CHARS,
    } for r in rows]
    total = info[0] if info else {}
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
    }


def _summary(data: dict[str, Any]) -> str:
    ev = data["events"]
    lines = [f"{len(ev)} quer{'y' if len(ev) == 1 else 'ies'} that ran {data['min_ms'] / 1000:g} s or longer"
             f"{f' in the last {data['hours']:g} h' if data['hours'] else ''} (shown in the Query History app)."]
    if data["enabled_hint"]:
        lines.append(data["enabled_hint"])
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
        " BLOB_CACHE_WAIT_TIME_MS, PLAN_WARNINGS, OPTIMIZER_NOTES, LAST_EXECUTED"
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
                    tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings, most important first: {severity: high|medium|info, title, detail, sql?}."""
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
    if re.search(r"\bSELECT\s+\*\s+FROM\b", upper):
        add("medium", "SELECT * reads every column",
            "Columnstore tables only read the columns a query names. List just the columns you need; on wide "
            "tables this cuts I/O and network time a lot.")
    is_read = upper.startswith(("SELECT", "WITH", "INSERT", "REPLACE")) or bool(_CTAS.match(sql))
    if is_read and " WHERE " not in f" {upper} " and " LIMIT " not in f" {upper} " and " JOIN " not in upper:
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

    if not any(f["severity"] in ("high", "medium") for f in out) and ok:
        add("info", "No obvious problems found",
            f"The plan looks reasonable; {_fmt_ms(ms)} is likely what this much data costs. To see exactly where the "
            "time goes, PROFILE the statement (note: PROFILE runs it again) and look at the slowest operators.",
            f"PROFILE {sql[:2000]};\nSHOW PROFILE;" if len(sql) < 2000 else None)
    order = {"high": 0, "medium": 1, "info": 2}
    out.sort(key=lambda f: order[f["severity"]])
    return out


def analyze(key: str) -> dict[str, Any]:
    event = _event(key)
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

    explained, plan = db.parallel(run_explain, run_plan)
    parsed = _parse_explain(explained["lines"]) if explained["lines"] else None
    tables = _tables(parsed["scans"], database) if parsed else []
    recs = recommendations(event, parsed, plan, tables)
    return {
        "event": jsonable(event),
        "explain": explained,
        "plan": plan,
        "plan_note": None if plan or event.get("plan_id") in (None, -1)
        else "The plan is no longer in the plan cache, so memory / spilling / queueing statistics aren't available.",
        "tables": [{k: v for k, v in t.items() if k != "ddl"} | {"ddl": t.get("ddl")} for t in tables],
        "recommendations": recs,
    }


def _analysis_summary(a: dict[str, Any]) -> str:
    e = a["event"]
    lines = [f"Tuning analysis of a {float(e.get('duration_ms') or 0) / 1000:.1f} s query by {e.get('user_name')} "
             f"in {e.get('context_database') or '-'}:", " ".join((e.get("query_text") or "").split())[:500]]
    for r in a["recommendations"]:
        lines.append(f"- [{r['severity']}] {r['title']}: {r['detail']}")
    return "\n".join(lines)


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
        tab: "queries" (default) or "advisor" (table-design advice from the whole history).

    The result includes ``browser_url``: post it as a clickable link right under the app.
    """
    data = {**collect(int(max(1.0, min_seconds) * 1000), hours), "tab": "advisor" if tab == "advisor" else "queries"}
    args = {"min_seconds": min_seconds, **({"hours": hours} if hours else {}), **({"tab": "advisor"} if tab == "advisor" else {})}
    return tool_result(with_browser_link(_summary(data), data, "query_history", args), data)


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
    data = {**collect(), "advisor": result, "tab": "advisor"}
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
        f"```sql\n{(e.get('query_text') or '')[:12000]}\n```",
        f"The Query History app's own analysis:\n{recs}" if recs else "",
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
