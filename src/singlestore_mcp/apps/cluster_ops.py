"""Cluster Monitor: Health, History and Backups tabs.

Each tab has its own app tool, only called while that tab is open (nothing extra on open).

Health (documented SingleStore 9.0 information_schema views, checked on 9.0.40, read-only):
- MV_NODES joined with LEAVES / AGGREGATORS: node state, runtime state, availability group, leaf pairs,
  aggregator role (Leader / Follower), round-trip latency.
- MV_DISTRIBUTED_DATABASES_STATUS: per database summary (healthy / degraded / offline) and partition counts by
  state (online, replicating, recovering, pending, transition, unrecoverable, offline, sync_mismatch).
- DISTRIBUTED_PARTITIONS: masters / replicas per database, offline and UNRECOVERABLE partition instances.
- MV_REPLICATION_MANAGEMENT_ORPHANS_STATE: orphan partitions (no longer part of the cluster).
- MV_AGGREGATED_REPLICATION_STATUS: replication to a secondary cluster (DR): lag and catch-up time.
- MV_EVENTS: the last (up to 1,028) cluster events: node failures, rebalances, memory / disk limits, ...
- MV_GLOBAL_STATUS on the master aggregator: License_type, License_expiration, Seconds_until_expiration,
  License_capacity, Used_cluster_capacity, Maximum_cluster_capacity (never the License / License_key values);
  GLOBAL_VARIABLES: redundancy_level, leaf_failure_detection, auto_attach.

Backups: MV_BACKUP_HISTORY (finished backups: status, size in MB, start / end), MV_BACKUP_STATUS (running ones),
DISTRIBUTED_DATABASES (to show databases with no backup at all).

History: the local SQLite store of ``metrics_store`` (no cluster query at all).

``classify_health`` and ``backup_overview`` are pure functions (offline tests).
"""

from __future__ import annotations

import datetime
import json
import time
from typing import Any

from mcp.types import CallToolResult

from .. import metrics_store
from ..db import db
from ._core import APP_ONLY, apps, jsonable_rows, tool_result
from .cluster_monitor import READ_ONLY, URI, _short_host

_MARKER = "/* s2-cluster-health */"
_DOCS = "https://docs.singlestore.com/db/v9.0/"
DOCS = {
    "events": _DOCS + "reference/information-schema-reference/cluster-component/mv-events/",
    "databases": _DOCS + "reference/information-schema-reference/cluster-component/mv-distributed-databases-status/",
    "partitions": _DOCS + "reference/information-schema-reference/cluster-component/distributed-partitions/",
    "leaves": _DOCS + "reference/information-schema-reference/cluster-component/leaves/",
    "aggregators": _DOCS + "reference/information-schema-reference/cluster-component/aggregators/",
    "orphans": _DOCS + "reference/troubleshooting-reference/understanding-orphaned-partitions/",
    "ha": _DOCS + "user-and-cluster-administration/high-availability-and-disaster-recovery/",
    "manage_ha": _DOCS + "user-and-cluster-administration/high-availability-and-disaster-recovery/managing-high-availability/",
    "replication": _DOCS + "user-and-cluster-administration/high-availability-and-disaster-recovery/replicating-data-across-clusters/monitoring-replication/",
    "license": _DOCS + "user-and-cluster-administration/maintain-your-cluster/setting-or-replacing-a-license/",
    "capacity": _DOCS + "user-and-cluster-administration/maintain-your-cluster/setting-or-replacing-a-license/determining-capacity-and-usage-for-your-license/",
    "backup": _DOCS + "manage-data/back-up-and-restore-data/",
    "backup_history": _DOCS + "reference/information-schema-reference/query-performance-workload-management-and-statistics/mv-backup-history/",
    "backup_status": _DOCS + "reference/information-schema-reference/query-performance-workload-management-and-statistics/mv-backup-status/",
}

# Event types that point at a problem, with a plain explanation (from the MV_EVENTS reference) and a docs link.
EVENT_HELP: dict[str, tuple[str, str]] = {
    "NODE_OFFLINE": ("A node went offline. Its partitions are unavailable unless replicas on another availability group take over.", "manage_ha"),
    "NODE_UNREACHABLE": ("The master aggregator couldn't reach a node (missed heartbeats).", "manage_ha"),
    "NODE_DETACHED": ("A node was detached from the cluster (it failed or was taken out); it reattaches when it is back.", "manage_ha"),
    "NODE_PING_VIOLATION": ("A node missed a ping / heartbeat check.", "manage_ha"),
    "HEARTBEAT_QUERY_FAILURE": ("A heartbeat query to a node failed.", "manage_ha"),
    "PARTITION_UNRECOVERABLE": ("A partition was lost and cannot be recovered (restore it from a backup).", "partitions"),
    "MAX_MEMORY": ("A node reached max_memory: new queries that need memory fail until memory is freed.", "events"),
    "MAX_TABLE_MEMORY": ("A node reached maximum_table_memory: writes to rowstore tables fail until memory is freed.", "events"),
    "MAX_MEMORY_ON_REPLAY": ("A node ran out of memory while replaying its logs (recovery).", "events"),
    "NODE_MEMORY_VIOLATION": ("A node hit a memory error or crossed a memory threshold.", "events"),
    "NODE_DISK_VIOLATION": ("A node hit a disk error or crossed a disk threshold.", "events"),
    "LOW_DISK_SPACE": ("A node ran low on disk space.", "events"),
    "INGEST_ERRORS_OUT_OF_DISK": ("Data loading fails because the disk is (nearly) full.", "events"),
    "MAX_BLOB_CACHE_SIZE_ON_REPLAY": ("The blob cache ran out of space during replay.", "events"),
    "BLOBCACHE_LOW_DISK": ("The blob cache is at or near its disk limit.", "events"),
    "WORKLOAD_THROTTLE": ("Writes were throttled because a synchronous replica fell behind.", "events"),
    "REPAIR_JOB_STALLED": ("A background repair job is slower than expected.", "events"),
    "BOTTOMLESS_UPLOAD_DELAYED": ("Uploads to remote (bottomless) storage are slower than expected.", "events"),
    "BOTTOMLESS_INGEST_THROTTLED": ("Writes were slowed because uploads to remote storage can't keep up.", "events"),
    "BOTTOMLESS_RATE_LIMITED": ("Calls to remote storage were rate limited.", "events"),
    "BOTTOMLESS_UPLOAD_VERIFICATION_FAILURE": ("An upload to remote storage failed verification.", "events"),
    "PIPELINE_STOPPED": ("A pipeline stopped (the details hold its last error code). It doesn't load until started again.", "events"),
    "REBALANCE_STARTED": ("A partition rebalance started: partitions move between leaves.", "manage_ha"),
    "REBALANCE_FINISHED": ("A partition rebalance finished.", "manage_ha"),
    "DATABASE_REPLICATION_STOP": ("Replication of a database stopped.", "replication"),
}
_SEV_RANK = {"critical": 0, "warning": 1, "info": 2}
_UNLIMITED = 2 ** 63  # capacity values this large mean "no limit" (the engine shows 2^64 - 1 units)
_LICENSE_VARS = ("License_type", "License_version", "License_expiration", "Seconds_until_expiration",
                 "License_capacity", "Used_cluster_capacity", "Maximum_cluster_capacity")


def _query(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    return jsonable_rows(db.execute(f"{_MARKER} {sql}", params)[1])


def _guard(fn: Any) -> Any:
    """A read whose view fails (e.g. not present on this version) leaves its part empty, not the whole panel."""
    def run() -> tuple[list[dict[str, Any]], str | None]:
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001 - reported in the panel
            return [], str(exc)[:300]
    return run


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).split()[0]))
    except (TypeError, ValueError):
        return None


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ health

_HEALTH_SQL = {
    "nodes": (
        "SELECT n.ID, n.IP_ADDR, n.PORT, n.TYPE, n.STATE, n.AVAILABILITY_GROUP, n.VERSION, n.UPTIME,"
        " COALESCE(l.RUNTIME_STATE, a.RUNTIME_STATE) AS RUNTIME_STATE, l.PAIR_HOST, l.PAIR_PORT, a.ROLE,"
        " COALESCE(l.AVERAGE_ROUNDTRIP_LATENCY, a.AVERAGE_ROUNDTRIP_LATENCY) AS LATENCY"
        " FROM information_schema.MV_NODES n"
        " LEFT JOIN information_schema.LEAVES l ON l.NODE_ID = n.ID"
        " LEFT JOIN information_schema.AGGREGATORS a ON a.NODE_ID = n.ID ORDER BY n.ID"
    ),
    "databases": "SELECT * FROM information_schema.MV_DISTRIBUTED_DATABASES_STATUS",
    "partitions": (
        "SELECT DATABASE_NAME, ROLE, STATE, IS_OFFLINE, IS_READ_REPLICA, COUNT(*) AS N"
        " FROM information_schema.DISTRIBUTED_PARTITIONS GROUP BY 1, 2, 3, 4, 5"
    ),
    "orphans": "SELECT DATABASE_NAME, COUNT(*) AS N FROM information_schema.MV_REPLICATION_MANAGEMENT_ORPHANS_STATE GROUP BY 1",
    "replication": (
        "SELECT IS_DR, IS_SYNC, DATABASE_NAME, PRIMARY_URI, SECONDARY_URI, PRIMARY_STATE, SECONDARY_STATE, LSN_LAG,"
        " VOLUME_LAG_MB, REPLICATION_THROUGHPUT_MBPS, ESTIMATED_CATCHUP_TIME_S"
        " FROM information_schema.MV_AGGREGATED_REPLICATION_STATUS WHERE IS_DR = 1 OR SECONDARY_URI IS NOT NULL"
    ),
    "events": (
        "SELECT ORIGIN_NODE_ID, EVENT_TIME, SEVERITY, EVENT_TYPE, LEFT(DETAILS, 1000) AS DETAILS,"
        " TIMESTAMPDIFF(SECOND, EVENT_TIME, NOW()) AS AGO_S"
        " FROM information_schema.MV_EVENTS ORDER BY EVENT_TIME DESC LIMIT 300"
    ),
    "settings": (
        "SELECT s.VARIABLE_NAME, s.VARIABLE_VALUE FROM information_schema.MV_GLOBAL_STATUS s"
        " JOIN information_schema.MV_NODES n ON n.ID = s.NODE_ID"
        f" WHERE n.TYPE = 'MA' AND s.VARIABLE_NAME IN ({', '.join(repr(v) for v in _LICENSE_VARS)})"
        " UNION ALL SELECT LOWER(VARIABLE_NAME), VARIABLE_VALUE FROM information_schema.GLOBAL_VARIABLES"
        " WHERE VARIABLE_NAME IN ('redundancy_level', 'leaf_failure_detection', 'auto_attach')"
    ),
}


def _read_health() -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    names = list(_HEALTH_SQL)
    results = db.parallel(*[_guard(lambda n=n: _query(_HEALTH_SQL[n])) for n in names])
    raw = {n: rows for n, (rows, _) in zip(names, results)}
    errors = {n: err for n, (_, err) in zip(names, results) if err}
    return raw, errors


def _issue(severity: str, title: str, explanation: str, docs: str, **extra: Any) -> dict[str, Any]:
    return {"severity": severity, "title": title, "explanation": explanation, "docs": DOCS.get(docs, docs), **extra}


def _event_details(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except (TypeError, ValueError):
        return {"text": str(raw)}


def classify_health(raw: dict[str, list[dict[str, Any]]], now: float | None = None, event_window_s: int = 86400) -> dict[str, Any]:
    """Turn the raw view rows into nodes, databases, events, license and a list of issues (worst first).

    Severity: ``critical`` blocks work now (node offline, partitions offline / unrecoverable, database offline,
    licence capacity exceeded); ``warning`` needs a look (degraded database, failure events in the last 24 h,
    replication lag, license expired or expiring); ``info`` is worth knowing (no high availability, rebalance)."""
    now = time.time() if now is None else now
    issues: list[dict[str, Any]] = []
    settings = {str(r["VARIABLE_NAME"]): r["VARIABLE_VALUE"] for r in raw.get("settings", [])}

    # ---- nodes
    nodes = []
    for r in raw.get("nodes", []):
        state, runtime = str(r.get("STATE") or ""), str(r.get("RUNTIME_STATE") or "")
        node = {
            "id": r["ID"], "host": _short_host(str(r.get("IP_ADDR") or "")), "port": r.get("PORT"), "type": r.get("TYPE"),
            "state": state, "runtime_state": runtime or None, "availability_group": r.get("AVAILABILITY_GROUP"),
            "pair": f"{_short_host(str(r['PAIR_HOST']))}:{r.get('PAIR_PORT')}" if r.get("PAIR_HOST") else None,
            "role": r.get("ROLE"), # AVERAGE_ROUNDTRIP_LATENCY is in milliseconds (LEAVES / AGGREGATORS reference).
            "latency_ms": round(float(r["LATENCY"]), 3) if r.get("LATENCY") is not None else None,
            "version": r.get("VERSION"), "uptime_s": r.get("UPTIME"),
        }
        nodes.append(node)
        what = "Leaf" if node["type"] == "LEAF" else "Master aggregator" if node["type"] == "MA" else "Aggregator"
        if state.lower() != "online":
            issues.append(_issue(
                "critical", f"{what} {node['host']} is {state or 'not online'}",
                "This node isn't serving queries. On a leaf, its partitions are offline unless their replicas in the "
                "other availability group took over; queries that need them fail.",
                "manage_ha" if node["type"] == "LEAF" else "aggregators", node_id=node["id"]))
        elif runtime and runtime.lower() != "online":
            # RUNTIME_STATE is documented as "Internal": worth showing, not worth a warning.
            issues.append(_issue("info", f"{what} {node['host']} runtime state is {runtime}",
                                 "The node is online in the cluster metadata but reports a different (internal) runtime state.",
                                 "leaves" if node["type"] == "LEAF" else "aggregators", node_id=node["id"]))

    redundancy = _int(settings.get("redundancy_level"))
    leaves = [n for n in nodes if n["type"] == "LEAF"]
    groups: dict[Any, int] = {}
    for n in leaves:
        groups[n["availability_group"]] = groups.get(n["availability_group"], 0) + 1
    if redundancy == 1 and leaves:
        issues.append(_issue(
            "info", "No high availability (redundancy_level 1)",
            "Each partition has only a master copy. If a leaf fails, its partitions are offline until the leaf is "
            "back; nothing fails over. High availability needs redundancy_level 2 and leaves in two availability groups.",
            "ha"))
    elif redundancy == 2 and leaves:
        if len(groups) < 2:
            issues.append(_issue("warning", "redundancy_level 2 but only one availability group",
                                 "Replicas need leaves in two availability groups; with one group there is nowhere to place them.", "ha"))
        elif len(set(groups.values())) > 1:
            issues.append(_issue("warning", "Availability groups have different numbers of leaves",
                                 "Leaves are paired across the two availability groups; an unequal number leaves some partitions without a replica.",
                                 "manage_ha", groups={str(k): v for k, v in groups.items()}))
        unpaired = [n["host"] for n in leaves if not n["pair"]]
        if unpaired:
            issues.append(_issue("warning", f"{len(unpaired)} leaf/leaves without a pair",
                                 "With redundancy_level 2 each leaf should be paired with a leaf in the other availability group: "
                                 + ", ".join(unpaired), "manage_ha"))

    # ---- partitions and databases
    parts: dict[str, dict[str, int]] = {}
    for r in raw.get("partitions", []):
        p = parts.setdefault(r["DATABASE_NAME"], {"masters": 0, "replicas": 0, "offline": 0, "unrecoverable": 0, "read_replicas": 0})
        n = int(r.get("N") or 0)
        role = str(r.get("ROLE") or "")
        if role == "Master":
            p["masters"] += n
        elif role == "Replica":
            p["replicas"] += n
        if _truthy(r.get("IS_READ_REPLICA")):
            p["read_replicas"] += n
        if _truthy(r.get("IS_OFFLINE")):
            p["offline"] += n
        if str(r.get("STATE") or "").upper() == "UNRECOVERABLE":
            p["unrecoverable"] += n
    orphans = {r["DATABASE_NAME"]: int(r.get("N") or 0) for r in raw.get("orphans", [])}

    def low(r: dict[str, Any]) -> dict[str, Any]:
        return {str(k).lower(): v for k, v in r.items()}

    databases = []
    for r in map(low, raw.get("databases", [])):
        name = r.get("database_name")
        p = parts.get(name, {})
        d = {"database": name, "summary": str(r.get("summary") or ""), "partitions": _int(r.get("num_partitions")),
             **{k: _int(r.get(k)) or 0 for k in ("online", "replicating", "recovering", "pending", "transition",
                                                  "unrecoverable", "offline", "sync_mismatch")},
             "masters": p.get("masters", 0), "replicas": p.get("replicas", 0), "orphans": orphans.get(name, 0),
             "offline_instances": p.get("offline", 0), "unrecoverable_instances": p.get("unrecoverable", 0)}
        databases.append(d)
        s = d["summary"].lower()
        if d["unrecoverable"] or d["unrecoverable_instances"]:
            issues.append(_issue("critical", f"{name}: unrecoverable partitions",
                                 "Some partitions of this database were lost and can't be recovered from the cluster itself; "
                                 "the data in them is only in a backup.", "partitions", database=name))
        elif d["offline"] or s == "offline":
            issues.append(_issue("critical", f"{name}: {d['offline'] or 'some'} partition(s) offline",
                                 "Queries that touch these partitions fail. Usually the leaf that holds them is down; "
                                 "they come back when it does (or when a replica is promoted).", "databases", database=name))
        elif s and s != "healthy":
            issues.append(_issue("warning", f"{name}: {d['summary']}",
                                 "The database works, but not every partition is in its normal state (e.g. a replica is "
                                 "recovering or catching up), so it has less protection against a failure right now.",
                                 "databases", database=name))
        if d["sync_mismatch"]:
            issues.append(_issue("warning", f"{name}: replication mode mismatch",
                                 "Some partition instances replicate in a different mode (sync / async) from the database.",
                                 "databases", database=name))
        if redundancy == 2 and d["masters"] and d["replicas"] < d["masters"]:
            issues.append(_issue("warning", f"{name}: {d['masters'] - d['replicas']} partition(s) without a replica",
                                 "With redundancy_level 2 every master partition should have a replica in the other availability group.",
                                 "manage_ha", database=name))
    known = {d["database"] for d in databases}
    total_orphans = sum(orphans.values())
    if total_orphans:
        issues.append(_issue("warning", f"{total_orphans} orphan partition(s)",
                             "Partition copies on leaves that the cluster no longer counts as part of any database "
                             f"({', '.join(sorted(orphans))}). They use disk; a DBA checks them before clearing them.",
                             "orphans"))
    databases.sort(key=lambda d: (d["summary"].lower() == "healthy", d["database"] or ""))

    # ---- replication to other clusters
    replication = []
    for r in raw.get("replication", []):
        catch = r.get("ESTIMATED_CATCHUP_TIME_S")
        rep = {"database": r.get("DATABASE_NAME"), "primary": r.get("PRIMARY_URI"), "secondary": r.get("SECONDARY_URI"),
               "primary_state": r.get("PRIMARY_STATE"), "secondary_state": r.get("SECONDARY_STATE"),
               "lsn_lag": _int(r.get("LSN_LAG")), "volume_lag_mb": r.get("VOLUME_LAG_MB"),
               "throughput_mbps": r.get("REPLICATION_THROUGHPUT_MBPS"), "catchup_s": catch}
        replication.append(rep)
        if catch is not None and float(catch) > 60:
            issues.append(_issue("warning", f"Replication of {rep['database']} is {float(catch):.0f} s behind",
                                 "The secondary cluster lags the primary; a failover now would lose the changes not yet copied.",
                                 "replication", database=rep["database"]))

    # ---- events
    events = []
    for r in raw.get("events", []):
        ago = _int(r.get("AGO_S"))
        etype = str(r.get("EVENT_TYPE") or "")
        events.append({"node_id": r.get("ORIGIN_NODE_ID"), "time": r.get("EVENT_TIME"), "ago_s": ago,
                       "severity": str(r.get("SEVERITY") or "").upper(), "type": etype,
                       "details": _event_details(r.get("DETAILS")), "help": EVENT_HELP.get(etype, (None,))[0]})
    recent = [e for e in events if e["ago_s"] is not None and e["ago_s"] <= event_window_s]
    by_type: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in recent:
        if e["severity"] in ("ERROR", "WARNING") or e["type"] in EVENT_HELP and e["type"] not in ("REBALANCE_STARTED", "REBALANCE_FINISHED"):
            by_type.setdefault((e["type"], e["severity"]), []).append(e)
    hours = round(event_window_s / 3600)
    for (etype, sev), es in sorted(by_type.items()):
        help_text, docs = EVENT_HELP.get(etype, ("See the event details for what happened.", "events"))
        issues.append(_issue("critical" if sev == "ERROR" else "warning",
                             f"{len(es)} {etype} event{'s' if len(es) != 1 else ''} in the last {hours} h",
                             help_text, docs, event_type=etype, last_ago_s=min(e["ago_s"] for e in es)))
    started = next((e for e in events if e["type"] == "REBALANCE_STARTED"), None)
    finished = next((e for e in events if e["type"] == "REBALANCE_FINISHED"), None)
    if started and (finished is None or (finished["ago_s"] or 0) > (started["ago_s"] or 0)):
        issues.append(_issue("info", "A partition rebalance is running",
                             "Partitions are moving between leaves; queries keep working but may be slower until it finishes.",
                             "manage_ha", last_ago_s=started["ago_s"]))

    # ---- license
    exp = _int(settings.get("License_expiration"))
    until = _int(settings.get("Seconds_until_expiration"))
    cap_used, cap_max = _int(settings.get("Used_cluster_capacity")), _int(settings.get("Maximum_cluster_capacity"))
    lic_cap = _int(settings.get("License_capacity"))
    license_ = {
        "type": settings.get("License_type"), "version": settings.get("License_version"),
        "expires_at": datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).isoformat() if exp else None,
        "expired": bool(exp and exp < now),
        "seconds_until_expiration": until,
        "capacity_units": None if lic_cap is None or lic_cap >= _UNLIMITED else lic_cap,
        "used_units": cap_used,
        "max_units": None if cap_max is None or cap_max >= _UNLIMITED else cap_max,
        "unlimited": lic_cap is not None and lic_cap >= _UNLIMITED,
    }
    if exp and exp < now:
        issues.append(_issue("warning", f"License expired on {license_['expires_at'][:10]}",
                             "License_expiration is in the past. Ask the cluster's SingleStore contact for a new license "
                             "(an admin sets it; this app never changes it).", "license"))
    elif exp and exp - now < 30 * 86400:
        issues.append(_issue("warning", f"License expires on {license_['expires_at'][:10]}",
                             "Less than 30 days left. An admin sets the new license.", "license"))
    if license_["max_units"] is not None and cap_used is not None and cap_used > license_["max_units"]:
        issues.append(_issue("critical", "License capacity exceeded",
                             f"The leaves use {cap_used} license units; the license allows {license_['max_units']}.", "capacity"))

    issues.sort(key=lambda i: _SEV_RANK.get(i["severity"], 3))
    worst = issues[0]["severity"] if issues else None
    versions = sorted({str(n["version"]) for n in nodes if n["version"]})
    return {
        "status": "critical" if worst == "critical" else "warning" if worst == "warning" else "healthy",
        "issues": issues,
        "nodes": nodes,
        "availability_groups": {str(k): v for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))},
        "databases": databases,
        "unknown_partition_databases": sorted(set(parts) - known),
        "replication": replication,
        "events": events,
        "event_window_s": event_window_s,
        "license": license_,
        "settings": {"redundancy_level": redundancy, "leaf_failure_detection": settings.get("leaf_failure_detection"),
                     "auto_attach": settings.get("auto_attach")},
        "versions": versions,
        "docs": DOCS,
    }


def collect_health() -> dict[str, Any]:
    raw, errors = _read_health()
    data = classify_health(raw)
    data["errors"] = errors
    data["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return data


def _health_summary(d: dict[str, Any]) -> str:
    counts = {s: sum(1 for i in d["issues"] if i["severity"] == s) for s in ("critical", "warning", "info")}
    lines = [f"Cluster health: {d['status']} ({counts['critical']} critical, {counts['warning']} warning, {counts['info']} info); "
             f"{len(d['nodes'])} nodes, {len(d['databases'])} databases, SingleStore {', '.join(d['versions'])}."]
    for i in d["issues"][:12]:
        lines.append(f"- [{i['severity']}] {i['title']}: {i['explanation']} ({i['docs']})")
    if d.get("errors"):
        lines.append("Views that couldn't be read: " + "; ".join(f"{k}: {v}" for k, v in d["errors"].items()))
    return "\n".join(lines)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_health() -> CallToolResult:
    """Cluster health for the Cluster Monitor's Health tab: node and availability-group state, partition health per
    database (masters / replicas, offline, unrecoverable, orphans), replication lag, recent cluster events
    (MV_EVENTS), license and version, with the problems listed worst first and a plain explanation each."""
    data = collect_health()
    return tool_result(_health_summary(data), data)


# ------------------------------------------------------------------ backups

def _ts(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        d = datetime.datetime.fromisoformat(str(v).replace(" ", "T"))
    except ValueError:
        return None
    return d.replace(tzinfo=d.tzinfo or datetime.timezone.utc).timestamp()


def backup_overview(history: list[dict[str, Any]], running: list[dict[str, Any]], databases: list[str],
                    now: float, warn_days: float = 7) -> dict[str, Any]:
    """Per database: last successful backup and its age, last failure, totals; issues for missing, old and
    failed backups. ``now`` must be on the same clock as the timestamps (the cluster's, see collect_backups)."""
    rows = []
    for r in history:
        start, end = _ts(r.get("START_TIMESTAMP")), _ts(r.get("END_TIMESTAMP"))
        rows.append({
            "id": r.get("BACKUP_ID"), "database": r.get("DATABASE_NAME"), "status": str(r.get("STATUS") or ""),
            "type": r.get("TYPE") or None, "implementation": r.get("IMPLEMENTATION"),
            "start": r.get("START_TIMESTAMP"), "end": r.get("END_TIMESTAMP"),
            "duration_s": round(end - start, 1) if start is not None and end is not None and end >= start else None,
            "size_mb": r.get("SIZE"), "partitions": r.get("NUM_PARTITIONS"), "path": r.get("BACKUP_PATH"),
            "incremental_id": r.get("INCR_BACKUP_ID"), "error_code": r.get("ERROR_CODE"),
            "error": r.get("ERROR_MESSAGE") or None, "server_version": r.get("SERVER_VERSION"),
            "_end": end if end is not None else start,
        })
    rows.sort(key=lambda r: r["_end"] or 0, reverse=True)
    per_db: dict[str, dict[str, Any]] = {}
    for name in databases:
        per_db[name] = {"database": name, "backups": 0, "failures": 0, "last_success": None, "last_failure": None}
    for r in rows:
        d = per_db.setdefault(r["database"], {"database": r["database"], "backups": 0, "failures": 0,
                                              "last_success": None, "last_failure": None, "dropped": True})
        d["backups"] += 1
        ok = r["status"].lower() == "success"
        if not ok:
            d["failures"] += 1
        key = "last_success" if ok else "last_failure"
        if d[key] is None:
            d[key] = {k: v for k, v in r.items() if not k.startswith("_")}
    issues = []
    warn_s = warn_days * 86400
    out = []
    for d in sorted(per_db.values(), key=lambda x: x["database"] or ""):
        ls = d["last_success"]
        age = None
        if ls is not None:
            t = _ts(ls["end"]) or _ts(ls["start"])
            age = max(0.0, now - t) if t is not None else None
        d["age_s"] = round(age) if age is not None else None
        failed_after = d["last_failure"] is not None and (ls is None or (_ts(d["last_failure"]["end"] or d["last_failure"]["start"]) or 0) > (_ts(ls["end"]) or 0))
        if d.get("dropped"):
            d["state"] = "dropped"
        elif ls is None:
            d["state"] = "never"
        elif failed_after:
            d["state"] = "failed"
        elif age is not None and age > warn_s:
            d["state"] = "old"
        else:
            d["state"] = "ok"
        out.append(d)
        if d["state"] == "failed":
            issues.append({"severity": "warning", "database": d["database"], "title": f"{d['database']}: the last backup failed",
                           "explanation": (d["last_failure"].get("error") or "See the error in the backup history.")[:300]})
        elif d["state"] == "old":
            issues.append({"severity": "warning", "database": d["database"],
                           "title": f"{d['database']}: last good backup is {age / 86400:.0f} days old",
                           "explanation": f"Older than the {warn_days:g}-day limit set here."})
    never = [d["database"] for d in out if d["state"] == "never"]
    if never and databases:
        issues.append({"severity": "warning" if rows else "info", "database": None,
                       "title": f"{len(never)} of {len(databases)} database(s) have no backup in the history",
                       "explanation": "MV_BACKUP_HISTORY only records backups made with BACKUP DATABASE on this cluster "
                                      "(and is emptied by CLEAR BACKUP_HISTORY); backups taken another way (e.g. disk "
                                      "snapshots) don't show here."})
    return {
        "databases": out,
        "history": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:200]],
        "running": [{"node_id": r.get("ORIGIN_NODE_ID"), "database": r.get("DATABASE_NAME"), "target": r.get("TARGET"),
                     "rowstore_pct": r.get("ROWSTORE_PROGRESS"), "columnstore_pct": r.get("COLUMNSTORE_PROGRESS"),
                     "status": r.get("STATUS"), "step_remaining_s": r.get("STEP_EST_SEC_REMAIN"),
                     "implementation": r.get("IMPLEMENTATION")} for r in running],
        "issues": issues,
        "warn_days": warn_days,
        "total": len(rows),
        "failed_total": sum(1 for r in rows if r["status"].lower() != "success"),
        "docs": {k: DOCS[k] for k in ("backup", "backup_history", "backup_status")},
    }


def collect_backups(warn_days: float = 7) -> dict[str, Any]:
    (hist, e1), (running, e2), (dbs, e3) = db.parallel(
        _guard(lambda: _query(
            "SELECT BACKUP_ID, DATABASE_NAME, START_TIMESTAMP, END_TIMESTAMP, NUM_PARTITIONS, BACKUP_PATH, STATUS, SIZE,"
            " INCR_BACKUP_ID, TYPE, SERVER_VERSION, IMPLEMENTATION, ERROR_CODE, LEFT(ERROR_MESSAGE, 500) AS ERROR_MESSAGE,"
            " NOW() AS NOW_T FROM information_schema.MV_BACKUP_HISTORY ORDER BY BACKUP_ID DESC LIMIT 1000")),
        _guard(lambda: _query("SELECT * FROM information_schema.MV_BACKUP_STATUS")),
        _guard(lambda: _query("SELECT DATABASE_NAME, NOW() AS NOW_T FROM information_schema.DISTRIBUTED_DATABASES")),
    )
    # Backup timestamps are DATETIMEs in the cluster's time zone: compare them with the cluster's NOW(), read the
    # same way (both as if UTC), so the age is right whatever the time zones of the cluster and this machine.
    now_rows = hist or dbs
    now = _ts(now_rows[0].get("NOW_T")) if now_rows else None
    data = backup_overview(hist, running, sorted(r["DATABASE_NAME"] for r in dbs), now or time.time(), warn_days)
    data["errors"] = {k: v for k, v in (("history", e1), ("running", e2), ("databases", e3)) if v}
    data["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return data


def _backup_summary(d: dict[str, Any]) -> str:
    lines = [f"{d['total']} backup(s) in MV_BACKUP_HISTORY ({d['failed_total']} failed), {len(d['running'])} running."]
    for i in d["issues"][:10]:
        lines.append(f"- {i['title']}: {i['explanation']}")
    for x in d["databases"][:30]:
        ls = x["last_success"]
        lines.append(f"- {x['database']}: {x['state']}" + (f", last good backup {ls['end']}" if ls else ""))
    return "\n".join(lines)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_backups(warn_days: float = 7) -> CallToolResult:
    """Backup overview for the Cluster Monitor's Backups tab: per database the last successful backup and its age,
    failed backups, sizes and durations (MV_BACKUP_HISTORY) and backups running now (MV_BACKUP_STATUS). Read-only.

    Args:
        warn_days: Flag databases whose last successful backup is older than this many days.
    """
    data = collect_backups(max(0.1, float(warn_days)))
    return tool_result(_backup_summary(data), data)


# ------------------------------------------------------------------ history

@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cluster_monitor_history(range: str = "1h", sample_now: bool = False) -> CallToolResult:  # noqa: A002
    """Stored per-node metrics (CPU, memory, disk, queued and running queries, connections) for the History tab.

    Args:
        range: "1h", "24h" or "7d".
        sample_now: Take one sample now first (otherwise only the local store is read; no cluster query).
    """
    if sample_now:
        metrics_store.sample_once(force=True)
    data = metrics_store.history(range)
    st = data["status"]
    pts = data["points"]
    summary = (f"{len(pts)} point(s) over {data['range']} (every {data['bucket_s']} s) for {len(data['nodes'])} node(s); "
               f"sampler {'on' if st['running'] else 'off'}, every {st['interval_s']} s, kept {st['retention_days']} days.")
    return tool_result(summary, data)
