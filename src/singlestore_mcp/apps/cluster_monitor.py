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
    point = {"t": round(now, 1), "cpu": {str(n["id"]): n["cpu"]["singlestore_pct"] for n in nodes if n["cpu"]}}
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
        " WRITE_CUMULATIVE_B, TIMESTAMP_NS FROM information_schema.MV_SYSINFO_DISK"
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


def collect(include_internal: bool = False) -> dict[str, Any]:
    user_filter = "" if include_internal else " AND USER <> 'distributed'"
    # Independent reads, run concurrently so their round trips overlap.
    nodes, mem_rows, (rates, measured), queries, idle_rows = db.parallel(
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
