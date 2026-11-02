"""Cluster Monitor: the Memory tab - where each node's memory goes, which tables hold it and which statements use it.

Sources (SingleStore 9.0 information_schema, checked on 9.0; read-only, one round trip: the reads run in parallel):

- MV_NODES: max_memory / max_table_memory and memory / table memory used per node; MV_SYSINFO_MEM: the host's RAM
  and the SingleStore process's resident size.
- MV_GLOBAL_STATUS: ``Total_server_memory`` (what max_memory limits: at it, queries fail with error 1712),
  ``Alloc_table_memory`` (rowstore tables: rows, indexes, off-row data; at max_table_memory writes fail with 1720),
  ``Buffer_manager_cached_memory`` (freed but kept for reuse: counts in the total, up to 25% of max_memory, ~1% near
  the limit), ``Alloc_query_execution`` (sorts, hash tables, result tables: ~0 with no query running),
  ``Alloc_deleted_version`` (deleted rows the garbage collector hasn't removed: large = blocked by a long snapshot,
  backup or query), ``Malloc_active_memory`` (C runtime, ~1-2 GB normal) and the other ``Alloc_*`` counters, grouped.
- TABLE_STATISTICS: MEMORY_USE per table partition (master and replica): rowstore tables live in memory in full,
  columnstore tables only keep their in-memory segment.
- MV_QUERY_ACTIVITIES_EXTENDED_CUMULATIVE: MEMORY_BS (memory x seconds) per statement and node since the plan was
  loaded. Two reads apart give the memory a statement used in between (``_rates``), without the 1 s wait of the
  delta views.
- MV_RESOURCE_POOL_STATUS + RESOURCE_POOLS: memory per resource pool and its limit.
- The trend: memory per node over 1 h / 24 h / 7 d and its growth rate, from the stored Cluster Monitor history
  (``metrics_store``: the local SQLite file, or the Studio repository's collector on the server) - no cluster query.

``analyse`` turns the raw rows into the tab's data and advice (pure; offline tests).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from mcp.types import CallToolResult

from .. import history_store, metrics_store
from ..db import db
from ._core import APP_ONLY, apps, tool_result
from .cluster_monitor import READ_ONLY, URI, _short_host

_MARKER = "/* s2-cluster-memory */"
_MB = 1024 * 1024

# Where Total_server_memory goes, in groups (the status counters each group adds up).
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("table", "Rowstore tables", ("Alloc_table_memory",)),
    ("query", "Running queries", ("Alloc_query_execution", "Alloc_aggregator_query_execution", "Alloc_columnar_query_execution",
                                  "Alloc_query_execution_temp_table", "Alloc_spill", "Alloc_query_background_tasks")),
    ("cached", "Cached, free for reuse", ("Buffer_manager_cached_memory",)),
    ("connections", "Connections and threads", ("Alloc_client_connection", "Alloc_connection_context", "Alloc_protocol_packet",
                                                "Alloc_thread_stacks", "Alloc_thread_signal_handler_altstack")),
    ("code", "Compiled plans", ("Alloc_plan_cache", "Alloc_compiled_unit_sections", "Alloc_unit_images",
                                "Alloc_object_code_images", "Alloc_unit_ifn_thunks", "Alloc_code_generator")),
    ("txn", "Transactions and replication", ("Alloc_transaction", "Alloc_replication", "Alloc_replication_large",
                                             "Alloc_distributed_transaction")),
    ("columnstore", "Columnstore caches and ingest", ("Alloc_column_store", "Alloc_columnstore_block_cache",
                                                      "Alloc_columnstore_blob_cache", "Alloc_query_ingest", "Alloc_pipelines")),
    ("malloc", "C runtime (malloc)", ("Malloc_active_memory",)),
)
# Parts of Alloc_table_memory (for "of which" in the table group).
TABLE_PARTS: tuple[tuple[str, str], ...] = (
    ("Alloc_table_primary", "rows"), ("Alloc_variable", "variable-length data"), ("Alloc_skiplist_tower", "skiplist indexes"),
    ("Alloc_hash_buckets", "hash indexes"), ("Alloc_deleted_version", "deleted rows not yet cleaned up"),
    ("Alloc_internal_key_node", "internal key nodes"), ("Alloc_vector_index", "vector indexes"),
)
_EXTRA = ("Total_server_memory", "Buffer_manager_memory", "Linux_resident_memory", "Uncommitted_table_memory")
STATUS_VARS = tuple(dict.fromkeys([v for _, _, vs in GROUPS for v in vs] + [v for v, _ in TABLE_PARTS] + list(_EXTRA)))

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def mb_value(text: Any) -> float | None:
    """Bytes from a status value like "632.102 MB" or "11.672 (+0.219) MB" (the first number, in MB)."""
    m = _NUM.search(str(text or ""))
    return float(m.group(0)) * _MB if m else None


def _q(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return db.execute(f"{_MARKER} {sql}", params or None)[1]


def _read() -> dict[str, Any]:
    names = ", ".join(["%s"] * len(STATUS_VARS))
    nodes, status, tables, acts, pools, limits = db.parallel(
        lambda: _q("SELECT n.ID, n.IP_ADDR, n.TYPE, n.STATE, n.MAX_MEMORY_MB, n.MAX_TABLE_MEMORY_MB, n.MEMORY_USED_MB,"
                   " n.TABLE_MEMORY_USED_MB, m.HOST_TOTAL_B, m.HOST_USED_B, m.CGROUP_TOTAL_B, m.MEMSQL_B"
                   " FROM information_schema.MV_NODES n LEFT JOIN information_schema.MV_SYSINFO_MEM m ON m.NODE_ID = n.ID ORDER BY n.ID"),
        lambda: _q(f"SELECT NODE_ID, VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.MV_GLOBAL_STATUS WHERE VARIABLE_NAME IN ({names})",
                   STATUS_VARS),
        lambda: _q("SELECT DATABASE_NAME, TABLE_NAME, STORAGE_TYPE, PARTITION_TYPE, SUM(MEMORY_USE) AS mem, SUM(ROWS) AS n_rows,"
                   " COUNT(*) AS parts FROM information_schema.TABLE_STATISTICS GROUP BY 1, 2, 3, 4"),
        lambda: _q("SELECT COALESCE(AGGREGATOR_ACTIVITY_NAME, ACTIVITY_NAME) AS act, MAX(COALESCE(AGGREGATOR_QUERY_TEXT, QUERY_TEXT)) AS q,"
                   " MAX(DATABASE_NAME) AS db, SUM(MEMORY_BS) AS mem_bs, MAX(ELAPSED_TIME_MS) AS el_ms, SUM(DISK_SPILLING_B) AS spill_b,"
                   " MAX(LAST_FINISHED_TIMESTAMP) AS last FROM information_schema.MV_QUERY_ACTIVITIES_EXTENDED_CUMULATIVE"
                   " GROUP BY 1 ORDER BY mem_bs DESC LIMIT 300"),
        lambda: _q("SELECT POOL_NAME, SUM(MEMORY_USAGE_BYTES) AS mem, SUM(RUNNING_QUERIES) AS running, SUM(QUEUEING_QUERIES) AS queued"
                   " FROM information_schema.MV_RESOURCE_POOL_STATUS GROUP BY 1"),
        lambda: _q("SELECT POOL_NAME, MEMORY_PERCENTAGE, QUERY_MEMORY_PERCENTAGE FROM information_schema.RESOURCE_POOLS"),
    )
    return {"nodes": nodes, "status": status, "tables": tables, "activities": acts, "pools": pools, "pool_limits": limits}


# ------------------------------------------------------------------ memory over time per statement (two reads apart)

_lock = threading.Lock()
_prev: dict[str, Any] = {"t": None, "bs": {}}


def _rates(acts: list[dict[str, Any]], now: float) -> float | None:
    """Adds ``recent_b`` (average bytes in use since the previous read) to each statement; returns that interval."""
    with _lock:
        prev_t, prev = _prev["t"], _prev["bs"]
        _prev["t"], _prev["bs"] = now, {a["act"]: float(a["mem_bs"] or 0) for a in acts}
    gap = None if prev_t is None or now - prev_t > 900 else now - prev_t
    for a in acts:
        a["recent_b"] = None
        if gap and gap > 0.5 and a["act"] in prev:
            a["recent_b"] = max(0.0, (float(a["mem_bs"] or 0) - prev[a["act"]]) / gap)
    return gap


# ------------------------------------------------------------------ the tab's data

def analyse(raw: dict[str, Any], gap_s: float | None = None) -> dict[str, Any]:
    st: dict[int, dict[str, float | None]] = {}
    for r in raw["status"]:
        st.setdefault(int(r["NODE_ID"]), {})[r["VARIABLE_NAME"]] = mb_value(r["VARIABLE_VALUE"])
    nodes, advice = [], []
    for n in raw["nodes"]:
        s = st.get(int(n["ID"]), {})
        total = s.get("Total_server_memory") or (float(n["MEMORY_USED_MB"] or 0) * _MB)
        max_b = float(n["MAX_MEMORY_MB"] or 0) * _MB or None
        tmax_b = float(n["MAX_TABLE_MEMORY_MB"] or 0) * _MB or None
        groups, counted = [], 0.0
        for key, label, names in GROUPS:
            b = sum(s.get(v) or 0 for v in names)
            groups.append({"key": key, "label": label, "bytes": b})
            counted += b
        groups.append({"key": "other", "label": "Other", "bytes": max(0.0, total - counted)})
        table_b = s.get("Alloc_table_memory") or 0
        parts = [{"label": label, "bytes": s.get(v) or 0} for v, label in TABLE_PARTS if s.get(v)]
        host = _short_host(n["IP_ADDR"])
        node = {"id": n["ID"], "host": host, "type": n["TYPE"], "state": n["STATE"], "total_b": total, "max_b": max_b,
                "pct": round(total / max_b * 100, 1) if max_b else None, "table_b": table_b, "table_max_b": tmax_b,
                "table_pct": round(table_b / tmax_b * 100, 1) if tmax_b else None, "groups": groups, "table_parts": parts,
                "host_total_b": n["HOST_TOTAL_B"], "host_used_b": n["HOST_USED_B"], "cgroup_total_b": n["CGROUP_TOTAL_B"],
                "resident_b": s.get("Linux_resident_memory") or n["MEMSQL_B"], "malloc_b": s.get("Malloc_active_memory"),
                "cached_b": s.get("Buffer_manager_cached_memory"), "query_b": next(g["bytes"] for g in groups if g["key"] == "query"),
                "deleted_b": s.get("Alloc_deleted_version")}
        nodes.append(node)
        advice += _node_advice(node)
    tables = _tables(raw["tables"])
    advice += _table_advice(tables, nodes)
    statements = _statements(raw["activities"])
    for x in [x for x in statements if (x["avg_b"] or 0) >= 1024 ** 3][:3]:
        advice.append({"level": "info", "text": f"{x['label']} ({x['database'] or 'no database'}) uses about {_gb(x['avg_b'])} "
                       "while it runs (all nodes together). Its plan (EXPLAIN / PROFILE) shows which hash join, GROUP BY or "
                       "sort holds it.", "activity": x["activity"]})
    for x in [x for x in statements if x["spill_b"] > 0][:3]:
        advice.append({"level": "info", "text": f"{x['label']} spilled {_gb(x['spill_b'])} to disk: it needed more memory "
                       "than it was given (slower, but it didn't fail).", "activity": x["activity"]})
    pools = _pools(raw["pools"], raw["pool_limits"])
    return {"nodes": nodes, "tables": tables[:100], "table_count": len(tables), "databases": _databases(tables),
            "statements": statements[:50], "recent_s": round(gap_s, 1) if gap_s else None, "pools": pools,
            "advice": advice}


def _gb(b: float | None) -> str:
    return "-" if b is None else f"{b / 1024 ** 3:.1f} GB"


def _node_advice(n: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    who = f"{n['host']} ({n['type']})"
    if n["pct"] is not None and n["pct"] >= 95:
        out.append({"level": "err", "text": f"{who}: {n['pct']}% of max_memory in use - queries fail with error 1712 at 100%."})
    elif n["pct"] is not None and n["pct"] >= 85:
        out.append({"level": "warn", "text": f"{who}: {n['pct']}% of max_memory in use."})
    if n["table_pct"] is not None and n["table_pct"] >= 80:
        out.append({"level": "err" if n["table_pct"] >= 95 else "warn",
                    "text": f"{who}: rowstore tables use {n['table_pct']}% of max_table_memory - at 100% INSERT, UPDATE and "
                            "LOAD DATA fail (error 1720). Move large rowstore tables to columnstore, or free memory (OPTIMIZE TABLE ... FULL)."})
    if n["deleted_b"] and n["table_b"] and n["deleted_b"] > 0.1 * n["table_b"] and n["deleted_b"] > 256 * _MB:
        out.append({"level": "warn", "text": f"{who}: {_gb(n['deleted_b'])} of deleted rows not yet cleaned up - the garbage "
                    "collector is behind or blocked by a long-running query, snapshot or backup."})
    if n["malloc_b"] and n["malloc_b"] > 4 * 1024 ** 3:
        out.append({"level": "warn", "text": f"{who}: the C runtime holds {_gb(n['malloc_b'])} (normally 1-2 GB)."})
    host = n["cgroup_total_b"] or n["host_total_b"]
    if host and n["max_b"] and n["max_b"] > 0.92 * host:
        out.append({"level": "warn", "text": f"{who}: max_memory ({_gb(n['max_b'])}) is more than 92% of the host / container "
                    f"memory ({_gb(host)}) - little room for the OS and other processes."})
    if host and n["resident_b"] and n["resident_b"] > 0.9 * host:
        out.append({"level": "err", "text": f"{who}: SingleStore's resident memory is {_gb(n['resident_b'])} of {_gb(host)} - "
                    "risk of the OS killing it (out of memory)."})
    return out


def _tables(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        t = by.setdefault((r["DATABASE_NAME"], r["TABLE_NAME"]), {
            "database": r["DATABASE_NAME"], "table": r["TABLE_NAME"], "storage": r["STORAGE_TYPE"],
            "bytes": 0, "replica_bytes": 0, "rows": 0, "partitions": 0})
        mem = float(r["mem"] or 0)
        if (r["PARTITION_TYPE"] or "").lower() == "master":
            t["bytes"] += mem
            t["rows"] += int(r["n_rows"] or 0)
            t["partitions"] += int(r["parts"] or 0)
        else:
            t["replica_bytes"] += mem
        if r["STORAGE_TYPE"] and "ROWSTORE" in str(r["STORAGE_TYPE"]).upper():
            t["storage"] = r["STORAGE_TYPE"]
    out = [t for t in by.values() if t["bytes"] or t["replica_bytes"]]
    for t in out:
        t["rowstore"] = "ROWSTORE" in str(t["storage"] or "").upper()
        t["bytes_per_row"] = round(t["bytes"] / t["rows"]) if t["rows"] else None
    out.sort(key=lambda t: -(t["bytes"] + t["replica_bytes"]))
    return out


def _databases(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for t in tables:
        d = by.setdefault(t["database"], {"database": t["database"], "bytes": 0, "replica_bytes": 0, "tables": 0})
        d["bytes"] += t["bytes"]
        d["replica_bytes"] += t["replica_bytes"]
        d["tables"] += 1
    return sorted(by.values(), key=lambda d: -(d["bytes"] + d["replica_bytes"]))[:30]


def _table_advice(tables: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table_total = sum(n["table_b"] for n in nodes if n["type"] == "LEAF") or sum(n["table_b"] for n in nodes)
    big = [t for t in tables if t["rowstore"] and t["bytes"] + t["replica_bytes"] > max(512 * _MB, 0.2 * table_total)]
    return [{"level": "info", "text": f"{t['database']}.{t['table']} is a rowstore table holding {_gb(t['bytes'] + t['replica_bytes'])} "
             "of memory (with replicas). If it isn't used for fast single-row lookups or updates, a columnstore copy keeps "
             "it on disk instead.", "table": [t["database"], t["table"]]} for t in big[:5]]


def _statements(acts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for a in acts:
        mem_bs = float(a["mem_bs"] or 0)
        if mem_bs <= 0:
            continue
        sql = history_store.mask_secrets(" ".join(str(a["q"] or "").split()))[:2000]
        el_s = float(a["el_ms"] or 0) / 1000
        out.append({"activity": a["act"], "label": str(a["act"]).rsplit("_", 1)[0], "sql": sql, "database": a["db"],
                    "mem_bs": mem_bs, "avg_b": mem_bs / el_s if el_s > 0 else None, "elapsed_s": round(el_s, 1),
                    "spill_b": float(a["spill_b"] or 0), "recent_b": a.get("recent_b"),
                    "last": str(a["last"]) if a.get("last") is not None else None,
                    "own": history_store.is_studio_query(sql)})
    out.sort(key=lambda s: -s["mem_bs"])
    return out


def _pools(rows: list[dict[str, Any]], limits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lim = {r["POOL_NAME"]: r for r in limits}
    out = [{"pool": r["POOL_NAME"], "bytes": float(r["mem"] or 0), "running": int(r["running"] or 0), "queued": int(r["queued"] or 0),
            "memory_pct": (lim.get(r["POOL_NAME"]) or {}).get("MEMORY_PERCENTAGE"),
            "query_memory_pct": (lim.get(r["POOL_NAME"]) or {}).get("QUERY_MEMORY_PERCENTAGE")} for r in rows]
    return sorted(out, key=lambda p: -p["bytes"])


def trend(range_key: str = "24h") -> dict[str, Any]:
    """Memory per node over the range (% of max_memory, from the stored history) and how fast it grew over the last
    24 h (``metrics_store.trends``: bytes per day and, for a steady climb, days until max_memory)."""
    try:
        hist = metrics_store.history(range_key)
    except Exception as exc:  # noqa: BLE001 - the tab still shows the live figures
        return {"error": str(exc)[:300], "range": range_key}
    points = [{"t": p["t"], "mem": p.get("mem") or {}} for p in hist.get("points", [])]
    try:
        growth = [{"node_id": n["node_id"], "host": n.get("host"), **{k: n["memory"].get(k) for k in
                                                                    ("per_day_b", "days_left", "r2", "used_b", "cap_b", "span_s")}}
                  for n in metrics_store.current_trends() if n.get("memory")]
    except Exception:  # noqa: BLE001 - growth is a bonus
        growth = []
    st = hist.get("status") or {}
    return {"range": hist.get("range", range_key), "range_s": hist.get("range_s"), "bucket_s": hist.get("bucket_s"),
            "points": points, "nodes": hist.get("nodes", []), "growth": growth,
            "status": {k: st.get(k) for k in ("enabled", "interval_s", "last_sample", "retention_days")}}


def collect_memory(range_key: str = "24h") -> dict[str, Any]:
    started = time.time()
    raw = _read()
    gap = _rates(raw["activities"], time.time())
    data = analyse(raw, gap)
    data["trend"] = trend(range_key)
    for g in data["trend"].get("growth", []):
        if g.get("days_left") is not None and g["days_left"] <= 14:
            data["advice"].append({"level": "warn" if g["days_left"] > 3 else "err",
                                   "text": f"{_short_host(str(g.get('host') or g['node_id']))}: memory climbed steadily over the last day "
                                           f"(+{_gb(g['per_day_b'])} a day) and reaches max_memory in about {g['days_left']} days at that rate."})
    data["seconds"] = round(time.time() - started, 2)
    return data


def _summary(d: dict[str, Any]) -> str:
    lines = [f"- {n['host']} ({n['type']}): {_gb(n['total_b'])} of {_gb(n['max_b'])} max_memory ({n['pct']}%), rowstore tables "
             f"{_gb(n['table_b'])}, cached {_gb(n['cached_b'])}, queries {_gb(n['query_b'])}" for n in d["nodes"]]
    lines += [f"- table {t['database']}.{t['table']}: {_gb(t['bytes'])} ({t['storage']})" for t in d["tables"][:5]]
    lines += [f"- [{a['level']}] {a['text']}" for a in d["advice"]]
    return "Memory per node, largest tables and advice:\n" + "\n".join(lines)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_memory(range: str = "24h") -> CallToolResult:  # noqa: A002 - tool argument name
    """Memory per node by use (rowstore tables, queries, cached, connections, plans ...), its trend over ``range``
    ("1h", "24h", "7d"), the tables and statements that use most memory, resource pools and advice, for the Cluster
    Monitor's Memory tab."""
    data = collect_memory(range)
    return tool_result(_summary(data), data)
