"""Cluster Monitor app: per-node CPU, memory and disk, plus active queries.

CPU time and disk I/O in MV_SYSINFO_* are cumulative counters, so rates come
from the difference between two samples. The previous sample is kept between
refreshes; on the first call (or after a long gap) two samples are taken a
second apart.
"""

from __future__ import annotations

import datetime
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from ..db import db
from ._core import APP_ONLY, apps, jsonable_rows, register_app, tool_result, with_browser_link

URI = "ui://singlestore/cluster-monitor.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
# Tags the monitor's own queries so the active-query list can leave them out.
_MARKER = "/* s2-cluster-monitor */"
_MAX_SAMPLE_GAP_S = 300
_MIN_SAMPLE_GAP_S = 0.5

register_app(
    URI,
    "cluster_monitor.html",
    name="Cluster Monitor",
    description="CPU, memory and disk usage per SingleStore node, and the queries running right now",
)

_NODE_TYPES = {"MA": "Master aggregator", "CA": "Child aggregator", "LEAF": "Leaf"}
_sample_lock = threading.Lock()
_prev_sample: dict[int, dict[str, Any]] = {}

# CPU readings kept in this server process so the chart has history when the
# app (re)opens. Only collected while a monitor is refreshing.
_HISTORY_SECONDS = 900
_history: deque[dict[str, Any]] = deque()
_history_lock = threading.Lock()


def _record_history(nodes: list[dict[str, Any]]) -> None:
    now = time.time()
    point: dict[str, Any] = {"t": round(now, 1), "cpu": {str(n["id"]): n["cpu"]["singlestore_pct"] for n in nodes if n["cpu"]}}
    # Bytes/s the node's SingleStore process read and wrote (all its mounts; MV_SYSINFO_DISK), for the IO tab.
    rd, wr = {}, {}
    for n in nodes:
        reads = [m["read_bps"] for m in n["disks"] if m["read_bps"] is not None]
        writes = [m["write_bps"] for m in n["disks"] if m["write_bps"] is not None]
        if reads:
            rd[str(n["id"])] = max(reads)
        if writes:
            wr[str(n["id"])] = max(writes)
    point["rd"], point["wr"] = rd, wr
    with _history_lock:
        _history.append(point)
        while _history and now - _history[0]["t"] > _HISTORY_SECONDS:
            _history.popleft()


def history() -> list[dict[str, Any]]:
    with _history_lock:
        return list(_history)


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return jsonable_rows(db.execute(f"{_MARKER} {sql}", params)[1])


def _counters() -> dict[int, dict[str, Any]]:
    cpu, disk = db.parallel(lambda: _query(
        "SELECT NODE_ID, NUM_CPUS, CFS_QUOTA_NS, CFS_PERIOD_NS, MEMSQL_TOTAL_CUMULATIVE_NS,"
        " TOTAL_USED_CUMULATIVE_NS, IDLE_CUMULATIVE_NS, TIMESTAMP_NS FROM information_schema.MV_SYSINFO_CPU"
    ), lambda: _query(
        "SELECT NODE_ID, MOUNT_POINT, MEMSQL_DIRS, MOUNT_TOTAL_B, MOUNT_USED_B, READ_CUMULATIVE_B,"
        " WRITE_CUMULATIVE_B, READ_OPERATIONS_CUMULATIVE_PER_DEVICE, WRITE_OPERATIONS_CUMULATIVE_PER_DEVICE, TIMESTAMP_NS"
        " FROM information_schema.MV_SYSINFO_DISK"
    ))
    out: dict[int, dict[str, Any]] = {}
    for r in cpu:
        quota, period = r["CFS_QUOTA_NS"] or 0, r["CFS_PERIOD_NS"] or 0
        out[r["NODE_ID"]] = {
            "ts": int(r["TIMESTAMP_NS"]),
            "cores": round(quota / period, 2) if quota > 0 and period > 0 else r["NUM_CPUS"],
            "host_cpus": r["NUM_CPUS"],
            "memsql_ns": int(r["MEMSQL_TOTAL_CUMULATIVE_NS"]),
            "used_ns": int(r["TOTAL_USED_CUMULATIVE_NS"]),
            "idle_ns": int(r["IDLE_CUMULATIVE_NS"]),
            "mounts": {},
        }
    for r in disk:
        node = out.setdefault(r["NODE_ID"], {"mounts": {}})
        node["mounts"][r["MOUNT_POINT"]] = {
            "ts": int(r["TIMESTAMP_NS"]),
            "dirs": [d for d in (r["MEMSQL_DIRS"] or "").split(",") if d],
            "total_b": int(r["MOUNT_TOTAL_B"] or 0),
            "used_b": int(r["MOUNT_USED_B"] or 0),
            "read_b": int(r["READ_CUMULATIVE_B"] or 0),
            "write_b": int(r["WRITE_CUMULATIVE_B"] or 0),
            "read_ops": int(r["READ_OPERATIONS_CUMULATIVE_PER_DEVICE"] or 0),
            "write_ops": int(r["WRITE_OPERATIONS_CUMULATIVE_PER_DEVICE"] or 0),
        }
    return out


def _rates() -> tuple[dict[int, dict[str, Any]], float]:
    """Current counters plus rates since the previous sample. Returns (nodes, seconds measured)."""
    global _prev_sample
    with _sample_lock:
        now = _counters()
        prev = _prev_sample
        gap = _sample_gap(prev, now)
        if gap is None or not _MIN_SAMPLE_GAP_S <= gap <= _MAX_SAMPLE_GAP_S:
            prev = now
            time.sleep(1.0)
            now = _counters()
            gap = _sample_gap(prev, now)
        _prev_sample = now

    result: dict[int, dict[str, Any]] = {}
    for node_id, cur in now.items():
        old = prev.get(node_id, {})
        dt_ns = cur.get("ts", 0) - old.get("ts", 0)
        cpu = None
        if "ts" in cur and dt_ns > 0:
            d_memsql = cur["memsql_ns"] - old["memsql_ns"]
            d_used, d_idle = cur["used_ns"] - old["used_ns"], cur["idle_ns"] - old["idle_ns"]
            cpu = {
                "cores": cur["cores"],
                "host_cpus": cur["host_cpus"],
                "singlestore_pct": round(max(0.0, d_memsql / (dt_ns * cur["cores"]) * 100), 1),
                "host_pct": round(d_used / (d_used + d_idle) * 100, 1) if d_used + d_idle > 0 else None,
            }
        mounts = []
        for point, m in cur["mounts"].items():
            om = old.get("mounts", {}).get(point)
            secs = (m["ts"] - om["ts"]) / 1e9 if om and m["ts"] > om["ts"] else None
            mounts.append({
                "mount_point": point,
                "dirs": m["dirs"],
                "total_b": m["total_b"],
                "used_b": m["used_b"],
                "read_bps": round(max(0, m["read_b"] - om["read_b"]) / secs) if secs else None,
                "write_bps": round(max(0, m["write_b"] - om["write_b"]) / secs) if secs else None,
                # Per device; 0 where the device doesn't report operations (e.g. NFS).
                "read_iops": round(max(0, m["read_ops"] - om.get("read_ops", 0)) / secs, 1) if secs else None,
                "write_iops": round(max(0, m["write_ops"] - om.get("write_ops", 0)) / secs, 1) if secs else None,
            })
        mounts.sort(key=lambda m: ("DATA" not in m["dirs"], m["mount_point"]))
        result[node_id] = {"cpu": cpu, "mounts": mounts}
    return result, gap or 0.0


def _sample_gap(prev: dict[int, dict[str, Any]], now: dict[int, dict[str, Any]]) -> float | None:
    gaps = [(now[n]["ts"] - prev[n]["ts"]) / 1e9 for n in now if n in prev and "ts" in now[n] and "ts" in prev[n]]
    return min(gaps) if gaps else None


def _short_host(host: str) -> str:
    # Kubernetes service names: keep the pod part ("node-...-leaf-ag1-0").
    return host.split(".svc-")[0] if ".svc-" in host else host


# Statements that ran recently, from the aggregators' plan caches: catches the many queries that finish
# between two refreshes and so never show up in the process list. The plan cache keeps parameterized text
# without comments, so this server's own reads (all of information_schema) are left out by what they read.
_RECENT_SECONDS = 60
_RECENT_SQL = (
    "SELECT p.NODE_ID, p.DATABASE_NAME, p.QUERY_TEXT, p.COMMITS + p.ROLLBACKS AS RUNS, p.ROLLBACKS,"
    " p.AVERAGE_EXEC_TIME, TIMESTAMPDIFF(SECOND, p.LAST_EXECUTED, NOW()) AS AGO_S"
    " FROM information_schema.MV_PLANCACHE p JOIN information_schema.MV_NODES n ON n.ID = p.NODE_ID"
    " WHERE n.TYPE <> 'LEAF' AND p.LAST_EXECUTED >= NOW() - INTERVAL %s SECOND"
    " AND p.QUERY_TEXT NOT LIKE %s AND p.QUERY_TEXT NOT LIKE %s AND p.QUERY_TEXT NOT LIKE %s"
    " AND p.DATABASE_NAME NOT IN ('', 'information_schema')"
    " ORDER BY p.LAST_EXECUTED DESC, p.AVERAGE_EXEC_TIME DESC LIMIT 100"
)
_RECENT_PARAMS = (_RECENT_SECONDS, "%information_schema.%", "SELECT SQL_NO_LOGGING%", "%binary_serialization%")


def _recent(names: dict[int, str]) -> list[dict[str, Any]]:
    out = []
    for r in _query(_RECENT_SQL, _RECENT_PARAMS):
        out.append({
            "node_id": r["NODE_ID"],
            "node": names.get(r["NODE_ID"], str(r["NODE_ID"])),
            "database": r["DATABASE_NAME"],
            "sql": r["QUERY_TEXT"],
            "runs": int(r["RUNS"] or 0),          # since the plan was loaded, not just in the window
            "rollbacks": int(r["ROLLBACKS"] or 0),
            "avg_ms": r["AVERAGE_EXEC_TIME"],
            "ago_s": max(0, int(r["AGO_S"] or 0)),
        })
    return out


def collect(include_internal: bool = False) -> dict[str, Any]:
    user_filter = "" if include_internal else " AND USER <> 'distributed'"
    # Independent reads, run concurrently so their round trips overlap.
    nodes, mem_rows, (rates, measured), queries, idle_rows, recent_rows = db.parallel(
        lambda: _query(
            "SELECT ID, IP_ADDR, PORT, TYPE, STATE, AVAILABILITY_GROUP, MAX_MEMORY_MB, MEMORY_USED_MB,"
            " UPTIME, VERSION FROM information_schema.MV_NODES ORDER BY ID"
        ),
        lambda: _query(
            "SELECT NODE_ID, CGROUP_TOTAL_B, CGROUP_USED_B, HOST_TOTAL_B, MEMSQL_B FROM information_schema.MV_SYSINFO_MEM"
        ),
        _rates,
        lambda: _query(
            "SELECT NODE_ID, ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO, TRANSACTION_STATE, RESOURCE_POOL,"
            " REASON_FOR_QUEUEING FROM information_schema.MV_PROCESSLIST"
            f" WHERE COMMAND <> 'Sleep'{user_filter}"
            " AND (INFO IS NULL OR INFO NOT LIKE %s)"
            # Leaf-side fan-out of system-view reads, including this monitor's own.
            " AND NOT (USER = 'distributed' AND DB = 'information_schema')"
            " ORDER BY TIME DESC LIMIT 200",
            # This server's own statements (this monitor, the alert checks, …) all start with "/* s2-".
            ("%/* s2-%",),
        ),
        lambda: _query(
            "SELECT COUNT(*) AS n FROM information_schema.MV_PROCESSLIST WHERE COMMAND = 'Sleep' AND USER <> 'distributed'"
        ),
        lambda: _recent({}),
    )
    mem = {r["NODE_ID"]: r for r in mem_rows}
    idle = idle_rows[0]["n"]

    node_list = []
    for n in nodes:
        m = mem.get(n["ID"], {})
        r = rates.get(n["ID"], {})
        node_list.append({
            "id": n["ID"],
            "host": _short_host(n["IP_ADDR"]),
            "port": n["PORT"],
            "type": n["TYPE"],
            "type_label": _NODE_TYPES.get(n["TYPE"], n["TYPE"]),
            "state": n["STATE"],
            "availability_group": n["AVAILABILITY_GROUP"],
            "uptime_s": n["UPTIME"],
            "version": n["VERSION"],
            "cpu": r.get("cpu"),
            "memory": {
                "singlestore_b": m.get("MEMSQL_B"),
                "max_memory_b": (n["MAX_MEMORY_MB"] or 0) * 1024 * 1024,
                "container_limit_b": m.get("CGROUP_TOTAL_B"),
                "container_used_b": m.get("CGROUP_USED_B"),
            },
            "disks": r.get("mounts", []),
        })
    _record_history(node_list)

    node_names = {n["id"]: n["host"] for n in node_list}
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sample_seconds": round(measured, 1),
        "include_internal": include_internal,
        "nodes": node_list,
        "queries": [
            {
                "node_id": q["NODE_ID"],
                "node": node_names.get(q["NODE_ID"], str(q["NODE_ID"])),
                "id": q["ID"],
                "user": q["USER"],
                "client": q["HOST"],
                "database": q["DB"],
                "command": q["COMMAND"],
                "seconds": q["TIME"],
                "state": q["STATE"],
                "sql": q["INFO"],
                "transaction": q["TRANSACTION_STATE"],
                "resource_pool": q["RESOURCE_POOL"],
                "queued_reason": q["REASON_FOR_QUEUEING"] or None,
            }
            for q in queries
        ],
        "idle_connections": int(idle or 0),
        "recent_seconds": _RECENT_SECONDS,
        "recent": [{**r, "node": node_names.get(r["node_id"], r["node"])} for r in recent_rows],
    }


def _summary(data: dict[str, Any]) -> str:
    lines = [f"{len(data['nodes'])} node(s) (shown in the Cluster Monitor app):"]
    for n in data["nodes"]:
        cpu = n["cpu"]
        mem = n["memory"]
        disk = next(iter(n["disks"]), None)
        parts = [f"{n['host']} ({n['type_label']}, {n['state']})"]
        if cpu:
            parts.append(f"CPU {cpu['singlestore_pct']}% of {cpu['cores']} cores")
        if mem["singlestore_b"] and mem["max_memory_b"]:
            parts.append(f"memory {mem['singlestore_b'] / mem['max_memory_b'] * 100:.0f}% of max_memory")
        if disk and disk["total_b"]:
            parts.append(f"disk {disk['used_b'] / disk['total_b'] * 100:.0f}% used")
        lines.append("- " + ", ".join(parts))
    qs = data["queries"]
    lines.append(f"{len(qs)} active quer{'y' if len(qs) == 1 else 'ies'}, {data['idle_connections']} idle connection(s).")
    for q in qs[:10]:
        sql = " ".join((q["sql"] or q["command"] or "").split())
        lines.append(f"- {q['seconds']}s {q['user']}@{q['database'] or '-'} on {q['node']}: {sql[:160]}")
    recent = data.get("recent") or []
    lines.append(f"{len(recent)} statement(s) ran in the last {data.get('recent_seconds', 60)} s (from the plan cache).")
    for r in recent[:5]:
        sql = " ".join((r["sql"] or "").split())
        lines.append(f"- {r['ago_s']}s ago, avg {r['avg_ms']} ms, {r['runs']} run(s) since cached, {r['database']}: {sql[:160]}")
    return "\n".join(lines)


@apps.tool(resource_uri=URI, title="Cluster Monitor", annotations=READ_ONLY)
def cluster_monitor() -> CallToolResult:
    """Open a live monitor of the SingleStore cluster.

    Shows every node (aggregators and leaves) with CPU, memory and disk
    usage, and the queries running right now, with auto-refresh. Use this
    when the user wants to see cluster load, resource usage or what is
    running.

    The result includes ``browser_url``, which opens this view full-window in
    the user's browser: post it as a clickable link right under the app.
    """
    data = collect()
    summary = with_browser_link(_summary(data), data, "cluster_monitor", {})
    return tool_result(summary, data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_data(include_internal: bool = False) -> CallToolResult:
    """Refresh data (including CPU history) for the Cluster Monitor app."""
    data = collect(include_internal)
    data["history"] = history()
    data["history_seconds"] = _HISTORY_SECONDS
    return tool_result(_summary(data), data)


# ---------------------------------------------------------------------------
# Disk, Bottomless and IO tabs. Only queried while their tab is open, through
# their own app tools, so the overview refresh stays as it was.
#
# Sources (checked on 9.0.40 and in the 9.0 docs):
# - MV_DISK_USAGE: per node, disk used per SingleStore directory (user_data,
#   plancache, tracelogs, auditlogs, other_data = system files).
# - MV_DATA_DISK_USAGE: per node, database and partition, user_data split into
#   BLOBS_B (columnstore blobs), TEMP_BLOBS_B, LOGS_B (transaction logs),
#   SNAPSHOTS_B (rowstore snapshots) and OTHER_B.
# - MV_SYSINFO_DISK: the mount (total / used, as the OS reports it).
# - MV_GLOBAL_STATUS: Disk_total_size_mb, Disk_available_size_mb,
#   Disk_uncategorized_usage_mb, Disk_spilling_usage_mb(_limit_mb),
#   Blob_cache_max_size_mb_adjusted_for_low_disk (present on 9.0.40, not documented).
# - MV_GLOBAL_VARIABLES: maximum_blob_cache_size_mb / _percent (per node, not synced).
# - MV_CACHED_BLOBS: what is in the blob cache now (bottomless databases only).
# - COLUMNAR_SEGMENTS: compressed columnstore size per node and table.
# - MV_BOTTOMLESS_STATUS_EXTENDED: per node and partition, uploaded / downloaded /
#   evicted bytes and API requests (running counts: rates come from deltas here)
#   and the upload lag (UPLOAD_QUEUE_WAIT_SECS); MV_BOTTOMLESS_SUMMARY_PER_NODE: the
#   engine's own MB/s figures; MV_BLOB_CACHE_EVENTS: retained fetch / eviction events.
# - MV_PLANCACHE: AVERAGE_BLOB_CACHE_MISS_BYTES and BLOB_CACHE_WAIT_TIME_MS (a total
#   over all execution threads of all runs).
# SingleStore 9.0 exposes no blob cache hit counter, so no hit ratio is computed.
# ---------------------------------------------------------------------------

_DISK_STATUS_VARS = (
    "Disk_total_size_mb", "Disk_available_size_mb", "Disk_uncategorized_usage_mb", "Disk_spilling_usage_mb",
    "Disk_spilling_usage_limit_mb", "Blob_cache_max_size_mb_adjusted_for_low_disk",
)
_MB = 1024 * 1024


def _nodes_brief() -> list[dict[str, Any]]:
    return [{"id": r["ID"], "host": _short_host(r["IP_ADDR"]), "port": r["PORT"], "type": r["TYPE"],
             "type_label": _NODE_TYPES.get(r["TYPE"], r["TYPE"]), "state": r["STATE"]}
            for r in _query("SELECT ID, IP_ADDR, PORT, TYPE, STATE FROM information_schema.MV_NODES ORDER BY ID")]


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).split()[0]))
    except (TypeError, ValueError):
        return None


def _mb(st: dict[str, Any], name: str) -> int | None:
    v = st.get(name)
    return None if v is None else int(v) * _MB


def _status_and_limits() -> dict[int, dict[str, Any]]:
    """Per node: the disk / blob cache status variables plus the blob cache size limit, in one round trip."""
    names = ", ".join(["%s"] * len(_DISK_STATUS_VARS))
    rows = _query(
        f"SELECT NODE_ID, VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.MV_GLOBAL_STATUS WHERE VARIABLE_NAME IN ({names})"
        " UNION ALL SELECT NODE_ID, LOWER(VARIABLE_NAME), VARIABLE_VALUE FROM information_schema.MV_GLOBAL_VARIABLES"
        " WHERE VARIABLE_NAME IN ('maximum_blob_cache_size_mb', 'maximum_blob_cache_size_percent')",
        _DISK_STATUS_VARS,
    )
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        name, value = r["VARIABLE_NAME"], r["VARIABLE_VALUE"]
        if name == "maximum_blob_cache_size_percent":
            try:
                parsed: Any = float(value)
            except (TypeError, ValueError):
                parsed = None
        else:
            parsed = _to_int(value)
        out.setdefault(r["NODE_ID"], {})[name] = parsed
    return out


def _mount_groups(mounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the nodes' mounts that are really one file system (same server, same size and usage).

    On shared storage (e.g. one NFS export for every pod) all nodes report the same total and used
    bytes: those numbers are per mount, not per node.
    """
    groups: list[dict[str, Any]] = []
    for m in mounts:
        name = m["mount_name"] or ""
        server = name.split(":", 1)[0] if ":" in name else None
        for g in groups:
            close = m["total_b"] == g["total_b"] and abs(m["used_b"] - g["used_b"]) <= max(g["total_b"] * 0.001, _MB)
            if close and server == g["server"]:
                g["node_ids"].append(m["node_id"])
                g["mount_names"].append(name)
                break
        else:
            groups.append({"server": server, "total_b": m["total_b"], "used_b": m["used_b"],
                           "mount_point": m["mount_point"], "node_ids": [m["node_id"]], "mount_names": [name]})
    for g in groups:
        g["node_ids"].sort()
        g["shared"] = len(g["node_ids"]) > 1
        g["free_b"] = max(0, g["total_b"] - g["used_b"])
    return groups


# MV_DISK_USAGE measures the directories when queried (about 3 s on a network file system), so its
# result is reused for a minute unless the user asks for a fresh measurement.
_DIRS_TTL_S = 60
_dirs_cache: dict[str, Any] = {}
_dirs_lock = threading.Lock()


def _dir_usage(force: bool) -> tuple[list[dict[str, Any]], float]:
    with _dirs_lock:
        hit = _dirs_cache.get("value")
        if hit is not None and not force and time.time() - hit[1] < _DIRS_TTL_S:
            return hit
    rows = _query("SELECT NODE_ID, MEMSQL_DIR, DISK_USED_B FROM information_schema.MV_DISK_USAGE")
    value = (rows, time.time())
    with _dirs_lock:
        _dirs_cache["value"] = value
    return value


def collect_disk(force: bool = False) -> dict[str, Any]:
    nodes, (dirs, dirs_at), data_rows, mounts, status, cached, tables = db.parallel(
        _nodes_brief,
        lambda: _dir_usage(force),
        lambda: _query(
            "SELECT NODE_ID, DATABASE_NAME, SUM(BLOBS_B) AS blobs, SUM(TEMP_BLOBS_B) AS temp_blobs, SUM(LOGS_B) AS logs,"
            " SUM(SNAPSHOTS_B) AS snapshots, SUM(OTHER_B) AS other, COUNT(*) AS parts"
            " FROM information_schema.MV_DATA_DISK_USAGE GROUP BY NODE_ID, DATABASE_NAME"
        ),
        lambda: _query(
            "SELECT NODE_ID, MOUNT_NAME, MOUNT_POINT, MEMSQL_DIRS, MOUNT_TOTAL_B, MOUNT_USED_B FROM information_schema.MV_SYSINFO_DISK"
        ),
        _status_and_limits,
        lambda: _query(
            "SELECT NODE_ID, COUNT(*) AS blobs, SUM(SIZE) AS bytes, SUM(EVICTABLE) AS evictable"
            " FROM information_schema.MV_CACHED_BLOBS GROUP BY NODE_ID"
        ),
        lambda: _query(
            "SELECT NODE_ID, DATABASE_NAME, TABLE_NAME, SUM(COMPRESSED_SIZE) AS bytes"
            " FROM information_schema.COLUMNAR_SEGMENTS GROUP BY NODE_ID, DATABASE_NAME, TABLE_NAME"
            " ORDER BY bytes DESC LIMIT 400"
        ),
    )
    by_node: dict[int, dict[str, Any]] = {
        n["id"]: {**n, "_dirs": {}, "_data": {}, "databases": [], "tables": [], "mount": None, "_status": status.get(n["id"], {})}
        for n in nodes
    }
    for r in dirs:
        if r["NODE_ID"] in by_node:
            by_node[r["NODE_ID"]]["_dirs"][r["MEMSQL_DIR"]] = int(r["DISK_USED_B"] or 0)
    for r in data_rows:
        node = by_node.get(r["NODE_ID"])
        if node is None:
            continue
        parts = {k: int(r[k] or 0) for k in ("blobs", "temp_blobs", "logs", "snapshots", "other")}
        for k, v in parts.items():
            node["_data"][k] = node["_data"].get(k, 0) + v
        node["databases"].append({"database": r["DATABASE_NAME"], "bytes": sum(parts.values()), **parts,
                                  "partitions": int(r["parts"] or 0)})
    mount_rows = []
    for r in mounts:
        dirs_list = [d for d in (r["MEMSQL_DIRS"] or "").split(",") if d]
        m = {"node_id": r["NODE_ID"], "mount_name": r["MOUNT_NAME"], "mount_point": r["MOUNT_POINT"], "dirs": dirs_list,
             "total_b": int(r["MOUNT_TOTAL_B"] or 0), "used_b": int(r["MOUNT_USED_B"] or 0)}
        node = by_node.get(r["NODE_ID"])
        # The data mount (the one holding DATA) stands for the node.
        if node is not None and (node["mount"] is None or "DATA" in dirs_list):
            node["mount"] = m
        mount_rows.append(m)
    for r in cached:
        node = by_node.get(r["NODE_ID"])
        if node is not None:
            node["_cache"] = {"blobs": int(r["blobs"] or 0), "bytes": int(r["bytes"] or 0), "evictable": int(r["evictable"] or 0)}
    for r in tables:
        node = by_node.get(r["NODE_ID"])
        if node is not None and len(node["tables"]) < 10:
            node["tables"].append({"database": r["DATABASE_NAME"], "table": r["TABLE_NAME"], "bytes": int(r["bytes"] or 0)})

    groups = _mount_groups([m for m in mount_rows if "DATA" in m["dirs"]] or mount_rows)
    out_nodes = []
    for n in by_node.values():
        st, dirs_b, data = n.pop("_status"), n.pop("_dirs"), n.pop("_data")
        cache = n.pop("_cache", {"blobs": 0, "bytes": 0, "evictable": 0})
        n["databases"].sort(key=lambda d: -d["bytes"])
        n["databases"] = n["databases"][:10]
        group = next((i for i, g in enumerate(groups) if n["id"] in g["node_ids"]), None)
        n.update({
            # This node's own SingleStore files (MV_DISK_USAGE), user_data split by MV_DATA_DISK_USAGE.
            "usage": {
                "columnstore_blobs_b": data.get("blobs", 0),
                "temp_blobs_b": data.get("temp_blobs", 0),
                "transaction_logs_b": data.get("logs", 0),
                "rowstore_snapshots_b": data.get("snapshots", 0),
                "other_database_files_b": data.get("other", 0),
                # user_data the per-database view doesn't explain (normally about 0).
                "user_data_unattributed_b": max(0, dirs_b.get("user_data", 0) - sum(data.values())),
                "plan_cache_b": dirs_b.get("plancache", 0),
                "trace_logs_b": dirs_b.get("tracelogs", 0),
                "audit_logs_b": dirs_b.get("auditlogs", 0),
                "system_files_b": dirs_b.get("other_data", 0),
            },
            "singlestore_b": sum(dirs_b.values()),
            "blob_cache": {
                **cache,
                "limit_b": _mb(st, "maximum_blob_cache_size_mb"),
                "limit_adjusted_b": _mb(st, "Blob_cache_max_size_mb_adjusted_for_low_disk"),
                "limit_percent": st.get("maximum_blob_cache_size_percent"),
            },
            "spilling": {"used_b": _mb(st, "Disk_spilling_usage_mb"), "limit_b": _mb(st, "Disk_spilling_usage_limit_mb")},
            # The engine's own view of the disk it runs on (MB granularity).
            "engine_disk": {"total_b": _mb(st, "Disk_total_size_mb"), "available_b": _mb(st, "Disk_available_size_mb"),
                            "uncategorized_b": _mb(st, "Disk_uncategorized_usage_mb")},
            "mount_group": group,
        })
        out_nodes.append(n)
    for i, g in enumerate(groups):
        g["index"] = i
        g["singlestore_b"] = sum(n["singlestore_b"] for n in out_nodes if n["id"] in g["node_ids"])
        # Used space that no SingleStore node on this mount accounts for: other software, file system overhead.
        g["other_b"] = max(0, g["used_b"] - g["singlestore_b"])
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "nodes": out_nodes,
        "mounts": groups,
        "dirs_measured_at": datetime.datetime.fromtimestamp(dirs_at, datetime.timezone.utc).isoformat(),
        "bottomless_cache_in_use": any(n["blob_cache"]["blobs"] for n in out_nodes),
    }


def _gb(b: Any) -> str:
    return f"{(b or 0) / 1e9:.1f} GB"


def _disk_summary(d: dict[str, Any]) -> str:
    lines = []
    for g in d["mounts"]:
        who = ("shared by nodes " if g["shared"] else "node ") + ", ".join(str(i) for i in g["node_ids"])
        lines.append(f"Mount {g['mount_point']} ({who}): {_gb(g['used_b'])} of {_gb(g['total_b'])} used; "
                     f"SingleStore {_gb(g['singlestore_b'])}, other {_gb(g['other_b'])}, free {_gb(g['free_b'])}")
    for n in d["nodes"]:
        u = n["usage"]
        lines.append(f"- {n['host']} ({n['type_label']}): SingleStore {_gb(n['singlestore_b'])} (columnstore "
                     f"{_gb(u['columnstore_blobs_b'])}, transaction logs {_gb(u['transaction_logs_b'])}, snapshots "
                     f"{_gb(u['rowstore_snapshots_b'])}, plan cache {_gb(u['plan_cache_b'])})")
    return "\n".join(lines)


# Running counts in MV_BOTTOMLESS_STATUS_EXTENDED, summed per node and per database for rates.
_BL_COUNTERS = ("UPLOADED_BYTES", "BLOB_CACHE_DOWNLOADED_BYTES", "BLOB_CACHE_EVICTED_BYTES",
                "BLOB_CACHE_DOWNLOADED_FILES", "BLOB_CACHE_EVICTED_FILES", "NUM_GET_REQUESTS", "NUM_PUT_REQUESTS",
                "NUM_FAILED_API_REQUESTS")
_bl_lock = threading.Lock()
_bl_prev: dict[str, Any] = {}
# Remote storage rates kept like the CPU history (only while the Bottomless / IO tab refreshes).
_io_history: deque[dict[str, Any]] = deque()


def _base_db(name: str, ordinal: Any) -> str:
    # Partition databases may be named "<db>_<ordinal>": fold them into their database.
    suffix = f"_{ordinal}"
    return name[: -len(suffix)] if ordinal is not None and name.endswith(suffix) else name


def _bottomless_sample() -> dict[str, Any]:
    cols = ", ".join(_BL_COUNTERS)
    status, summary, events = db.parallel(
        lambda: _query(
            f"SELECT NODE_ID, DATABASE_NAME, ORDINAL, BUCKET, STORAGE_DIRECTORY, {cols}, UPLOAD_QUEUE_WAIT_SECS,"
            " SECS_SINCE_LAST_UPLOAD, LAST_UPLOAD_RESULT, NUM_OUTSTANDING_REQUESTS, IS_BEING_RATE_LIMITED"
            " FROM information_schema.MV_BOTTOMLESS_STATUS_EXTENDED"
        ),
        lambda: _query(
            "SELECT NODE_ID, UPLOAD_QUEUE_WAIT_SECS, UPLOAD_MB_PER_SEC, LAST_UPLOAD_RESULT, BLOB_CACHE_DOWNLOAD_MB_PER_SEC,"
            " BLOB_CACHE_EVICTION_MB_PER_SEC, API_REQUESTS_PER_SEC, IS_BEING_RATE_LIMITED, OBJECT_STORE_AVAILABILITY"
            " FROM information_schema.MV_BOTTOMLESS_SUMMARY_PER_NODE"
        ),
        lambda: _query(
            "SELECT NODE_ID, EVENT_TYPE, COUNT(*) AS n, SUM(FILE_SIZE) AS bytes, AVG(FETCH_TIME_TAKEN_MS) AS avg_fetch_ms,"
            " MAX(FETCH_TIME_TAKEN_MS) AS max_fetch_ms FROM information_schema.MV_BLOB_CACHE_EVENTS GROUP BY NODE_ID, EVENT_TYPE"
        ),
    )
    return {"t": time.monotonic(), "status": status, "summary": summary, "events": events}


def _sum_counters(rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]) -> dict[Any, dict[str, int]]:
    out: dict[Any, dict[str, int]] = {}
    for r in rows:
        acc = out.setdefault(key(r), dict.fromkeys(_BL_COUNTERS, 0))
        for c in _BL_COUNTERS:
            acc[c] += int(r[c] or 0)
    return out


def _counter_rates(cur: dict[str, int] | None, old: dict[str, int] | None, secs: float | None) -> dict[str, Any]:
    if not cur or not old or not secs:
        return {}
    # A count that went down was reset (restart, partition moved): no rate for it this time.
    return {c: round((cur[c] - old[c]) / secs, 1) if cur[c] >= old[c] else None for c in _BL_COUNTERS}


def _upload_state(rows: list[dict[str, Any]]) -> dict[str, Any]:
    waits = [float(r["UPLOAD_QUEUE_WAIT_SECS"]) for r in rows if r["UPLOAD_QUEUE_WAIT_SECS"] is not None]
    since = [float(r["SECS_SINCE_LAST_UPLOAD"]) for r in rows if r["SECS_SINCE_LAST_UPLOAD"] is not None]
    results = sorted({str(r["LAST_UPLOAD_RESULT"]) for r in rows if r["LAST_UPLOAD_RESULT"]})
    return {
        "upload_lag_s": max(waits) if waits else None,
        "since_last_upload_s": min(since) if since else None,
        "outstanding_requests": sum(int(r["NUM_OUTSTANDING_REQUESTS"] or 0) for r in rows),
        "rate_limited": any(str(r["IS_BEING_RATE_LIMITED"]).lower() in ("1", "true", "yes") for r in rows),
        "last_upload_results": results[:5],
        "partitions": len(rows),
    }


def collect_bottomless() -> dict[str, Any]:
    global _bl_prev
    nodes, sample = db.parallel(_nodes_brief, _bottomless_sample)
    with _bl_lock:
        prev = _bl_prev
        secs = sample["t"] - prev["t"] if prev else None
        if secs is not None and not _MIN_SAMPLE_GAP_S <= secs <= _MAX_SAMPLE_GAP_S:
            prev, secs = {}, None
        _bl_prev = sample
    status = sample["status"]

    def by_db(r: dict[str, Any]) -> str:
        return _base_db(r["DATABASE_NAME"], r["ORDINAL"])

    per_node, per_db = _sum_counters(status, lambda r: r["NODE_ID"]), _sum_counters(status, by_db)
    old = prev.get("status", [])
    old_node, old_db = _sum_counters(old, lambda r: r["NODE_ID"]), _sum_counters(old, by_db)
    summary = {r["NODE_ID"]: r for r in sample["summary"]}
    events: dict[int, dict[str, Any]] = {}
    for r in sample["events"]:
        events.setdefault(r["NODE_ID"], {})[str(r["EVENT_TYPE"]).lower()] = {
            "count": int(r["n"] or 0), "bytes": int(r["bytes"] or 0),
            "avg_fetch_ms": r["avg_fetch_ms"], "max_fetch_ms": r["max_fetch_ms"]}

    out_nodes = []
    for n in nodes:
        s = summary.get(n["id"])
        out_nodes.append({
            **n,
            "totals": per_node.get(n["id"]),
            "rates": _counter_rates(per_node.get(n["id"]), old_node.get(n["id"]), secs),
            **_upload_state([r for r in status if r["NODE_ID"] == n["id"]]),
            # The engine's own rates (MV_BOTTOMLESS_SUMMARY_PER_NODE), as reported.
            "engine": None if s is None else {
                "upload_mb_s": s["UPLOAD_MB_PER_SEC"], "download_mb_s": s["BLOB_CACHE_DOWNLOAD_MB_PER_SEC"],
                "eviction_mb_s": s["BLOB_CACHE_EVICTION_MB_PER_SEC"], "api_requests_s": s["API_REQUESTS_PER_SEC"],
                "upload_queue_wait_s": s["UPLOAD_QUEUE_WAIT_SECS"], "last_upload_result": s["LAST_UPLOAD_RESULT"],
                "rate_limited": s["IS_BEING_RATE_LIMITED"], "object_store_availability": s["OBJECT_STORE_AVAILABILITY"]},
            "events": events.get(n["id"], {}),
        })
    databases = []
    for name, totals in sorted(per_db.items()):
        rows = [r for r in status if by_db(r) == name]
        first = next((r for r in rows if r["BUCKET"] or r["STORAGE_DIRECTORY"]), {})
        databases.append({"database": name, "bucket": first.get("BUCKET"), "directory": first.get("STORAGE_DIRECTORY"),
                          "totals": totals, "rates": _counter_rates(totals, old_db.get(name), secs), **_upload_state(rows)})
    data = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sample_seconds": round(secs, 1) if secs else None,
        "has_bottomless": bool(status),
        "nodes": out_nodes,
        "databases": databases,
        # SingleStore 9.0 has no blob cache hit counter: fetches (= misses) are shown instead.
        "hit_ratio_available": False,
    }
    _record_io(data)
    return data


def _record_io(data: dict[str, Any]) -> None:
    if not data["sample_seconds"] or not data["has_bottomless"]:
        return
    point: dict[str, Any] = {"t": round(time.time(), 1), "up": {}, "down": {}, "evict": {}}
    for n in data["nodes"]:
        for key, col in (("up", "UPLOADED_BYTES"), ("down", "BLOB_CACHE_DOWNLOADED_BYTES"), ("evict", "BLOB_CACHE_EVICTED_BYTES")):
            v = n["rates"].get(col)
            if v is not None:
                point[key][str(n["id"])] = round(v)
    with _history_lock:
        _io_history.append(point)
        while _io_history and point["t"] - _io_history[0]["t"] > _HISTORY_SECONDS:
            _io_history.popleft()


def _bottomless_summary(d: dict[str, Any]) -> str:
    if not d["has_bottomless"]:
        return "No bottomless (unlimited storage) databases: nothing is uploaded to or fetched from object storage."
    lines = [f"{len(d['databases'])} bottomless database(s)."]
    for n in d["nodes"]:
        if not n["totals"]:
            continue
        r = n["rates"]
        lines.append(f"- {n['host']}: upload {(r.get('UPLOADED_BYTES') or 0) / 1e6:.2f} MB/s, "
                     f"fetch {(r.get('BLOB_CACHE_DOWNLOADED_BYTES') or 0) / 1e6:.2f} MB/s, upload lag {n['upload_lag_s']} s")
    return "\n".join(lines)


_BLOB_WAIT_SQL = (
    "SELECT p.NODE_ID, n.TYPE AS NODE_TYPE, p.DATABASE_NAME, p.QUERY_TEXT, p.COMMITS + p.ROLLBACKS AS RUNS,"
    " p.BLOB_CACHE_WAIT_TIME_MS, p.AVERAGE_BLOB_CACHE_MISS_BYTES, p.AVERAGE_EXEC_TIME,"
    " TIMESTAMPDIFF(SECOND, p.LAST_EXECUTED, NOW()) AS AGO_S"
    " FROM information_schema.MV_PLANCACHE p JOIN information_schema.MV_NODES n ON n.ID = p.NODE_ID"
    " WHERE (p.BLOB_CACHE_WAIT_TIME_MS > 0 OR p.AVERAGE_BLOB_CACHE_MISS_BYTES > 0)"
    " AND p.QUERY_TEXT NOT LIKE %s AND p.DATABASE_NAME NOT IN ('', 'information_schema')"
    " ORDER BY p.BLOB_CACHE_WAIT_TIME_MS DESC, p.AVERAGE_BLOB_CACHE_MISS_BYTES DESC LIMIT 25"
)


def collect_io() -> dict[str, Any]:
    bottomless, waits = db.parallel(collect_bottomless, lambda: _query(_BLOB_WAIT_SQL, ("%information_schema.%",)))
    with _history_lock:
        io_hist = list(_io_history)
    return {
        "generated_at": bottomless["generated_at"],
        "bottomless": bottomless,
        "io_history": io_hist,
        "history_seconds": _HISTORY_SECONDS,
        "blob_waits": [{
            "node_id": r["NODE_ID"], "node_type": r["NODE_TYPE"], "database": r["DATABASE_NAME"], "sql": r["QUERY_TEXT"],
            "runs": int(r["RUNS"] or 0), "wait_ms": r["BLOB_CACHE_WAIT_TIME_MS"],
            "avg_miss_bytes": r["AVERAGE_BLOB_CACHE_MISS_BYTES"], "avg_ms": r["AVERAGE_EXEC_TIME"],
            "ago_s": max(0, int(r["AGO_S"] or 0)),
        } for r in waits],
    }


def _io_summary(d: dict[str, Any]) -> str:
    lines = [_bottomless_summary(d["bottomless"]), f"{len(d['blob_waits'])} cached plan(s) waited on the blob cache."]
    for w in d["blob_waits"][:5]:
        lines.append(f"- {w['wait_ms']} ms total wait, {w['runs']} runs, {w['database']}: {' '.join((w['sql'] or '').split())[:140]}")
    return "\n".join(lines)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_disk(force: bool = False) -> CallToolResult:
    """Disk use per node by type (columnstore, logs, snapshots, plan cache, ...), per mount, and the largest databases / tables.

    Args:
        force: Measure the SingleStore directories again now (otherwise a measurement up to a minute old is reused).
    """
    data = collect_disk(force)
    return tool_result(_disk_summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_bottomless() -> CallToolResult:
    """Traffic to and from remote (object) storage per node and database, for the Cluster Monitor's Bottomless tab."""
    data = collect_bottomless()
    return tool_result(_bottomless_summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_io() -> CallToolResult:
    """Blob cache IO over the session and the statements that waited most on the blob cache, for the IO tab."""
    data = collect_io()
    return tool_result(_io_summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_remote_stats() -> CallToolResult:
    """Bytes each bottomless database keeps in remote storage (MV_BOTTOMLESS_REMOTE_STATS queries object storage: on demand only)."""
    rows = _query(
        "SELECT DATABASE_NAME, ORDINAL, BLOBS_BYTES, LOG_CHUNKS_BYTES, SNAPSHOTS_BYTES, OTHER_FILE_BYTES, NUM_BLOBS"
        " FROM information_schema.MV_BOTTOMLESS_REMOTE_STATS"
    )
    per_db: dict[str, dict[str, Any]] = {}
    for r in rows:
        acc = per_db.setdefault(_base_db(r["DATABASE_NAME"], r["ORDINAL"]),
                                {"blobs": 0, "logs": 0, "snapshots": 0, "other": 0, "n_blobs": 0, "incomplete": False})
        if r["BLOBS_BYTES"] is None:  # a NULL row: the figures aren't known yet, retry later
            acc["incomplete"] = True
        for k, col in (("blobs", "BLOBS_BYTES"), ("logs", "LOG_CHUNKS_BYTES"), ("snapshots", "SNAPSHOTS_BYTES"),
                       ("other", "OTHER_FILE_BYTES"), ("n_blobs", "NUM_BLOBS")):
            acc[k] += int(r[col] or 0)
    dbs = [{"database": k, **v, "bytes": v["blobs"] + v["logs"] + v["snapshots"] + v["other"]} for k, v in sorted(per_db.items())]
    return tool_result(f"Remote storage of {len(dbs)} bottomless database(s)", {"databases": dbs})
