"""Alerts: a background checker in the server process that watches the cluster against simple rules.

Rules (each on/off, thresholds editable in the Alerts view, stored in ``~/.singlestore-mcp/alerts.json``):

- a query running longer than N s (MV_PROCESSLIST);
- a finished query slower than N s, and failed queries (new MV_TRACE_EVENTS since the last check);
- failed pipeline batches, new pipeline errors and pipelines in the Error state
  (PIPELINES_BATCHES_SUMMARY, PIPELINES_ERRORS, PIPELINES);
- node memory over X% of max_memory, disk over X% (MV_SYSINFO_MEM / MV_SYSINFO_DISK, as the Cluster Monitor);
- a node that isn't online (MV_NODES).

Every check runs a few queries in parallel (tagged ``/* s2-alerts */`` and left out of everything they watch).
An alert has a stable key, so a long-running query or a full disk is *one* alert with first / last seen and a
count, not one per minute. Alerts that recur after being acknowledged become unacknowledged again.

State is kept in memory and in alerts.json (history capped), re-read when another server process (e.g. the
desktop workspace next to the one Claude runs) changed it. The checker never raises: errors become a
"checks failing" alert.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any

from . import history_store
from .db import db
from .paths import data_dir

MARKER = "/* s2-alerts */"
MAX_ALERTS = 300
_NOT_LIKE_OURS = f"%{history_store.MARKER_PREFIX}%"

DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "long_running": {"enabled": True, "seconds": 60, "label": "Query running longer than", "unit": "s"},
    "slow_query": {"enabled": True, "seconds": 300, "label": "Finished query slower than", "unit": "s"},
    "failed_query": {"enabled": True, "label": "Failed queries"},
    "pipeline": {"enabled": True, "label": "Failed pipeline batches, pipeline errors and stopped-with-error pipelines"},
    "memory": {"enabled": True, "percent": 85, "label": "Node memory above (% of max_memory)", "unit": "%"},
    "disk": {"enabled": True, "percent": 90, "label": "Disk usage above", "unit": "%"},
    "node_offline": {"enabled": True, "label": "Node not online"},
}
DEFAULT_SETTINGS = {"interval_s": 60, "notify": False}

_lock = threading.RLock()
_state: dict[str, Any] = {}
_mtime: float = 0.0
_status: dict[str, Any] = {"running": False, "last_check": None, "last_error": None, "last_seconds": None, "checks": 0}
_thread: threading.Thread | None = None
_stop = threading.Event()
_wake = threading.Event()


def _file():
    return data_dir() / "alerts.json"


def _fresh() -> dict[str, Any]:
    return {"rules": copy.deepcopy(DEFAULT_RULES), "settings": dict(DEFAULT_SETTINGS), "alerts": [], "watermarks": {}}


def _load() -> None:
    """(Re)read alerts.json if it changed on disk. Call with _lock held."""
    global _state, _mtime
    path = _file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        if not _state:
            _state = _fresh()
        return
    if _state and mtime == _mtime:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    state = _fresh()
    for name, rule in (data.get("rules") or {}).items():
        if name in state["rules"] and isinstance(rule, dict):
            state["rules"][name].update({k: v for k, v in rule.items() if k in ("enabled", "seconds", "percent")})
    state["settings"].update({k: v for k, v in (data.get("settings") or {}).items() if k in DEFAULT_SETTINGS})
    state["alerts"] = [a for a in data.get("alerts") or [] if isinstance(a, dict) and a.get("key")]
    state["watermarks"] = data.get("watermarks") or {}
    _state, _mtime = state, mtime


def _save() -> None:
    """Write alerts.json atomically. Call with _lock held."""
    global _mtime
    _state["alerts"] = _state["alerts"][-MAX_ALERTS:]
    path = _file()
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    rules = {k: {f: v for f, v in r.items() if f in ("enabled", "seconds", "percent")} for k, r in _state["rules"].items()}
    tmp.write_text(json.dumps({**_state, "rules": rules}, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)
    _mtime = path.stat().st_mtime


def _now() -> float:
    return round(time.time(), 1)


# ------------------------------------------------------------------ public state

def state(include_alerts: bool = True) -> dict[str, Any]:
    with _lock:
        _load()
        alerts = list(reversed(_state["alerts"]))  # newest first
        return {
            "rules": copy.deepcopy(_state["rules"]),
            "settings": dict(_state["settings"]),
            "unacknowledged": sum(1 for a in alerts if not a.get("acked")),
            "active": sum(1 for a in alerts if a.get("active")),
            "total": len(alerts),
            "status": dict(_status, interval_s=_state["settings"]["interval_s"]),
            **({"alerts": copy.deepcopy(alerts)} if include_alerts else {}),
            "newest": alerts[0]["last_seen"] if alerts else None,
            "newest_unacked": [{"id": a["id"], "title": a["title"], "severity": a["severity"]}
                               for a in alerts if not a.get("acked")][:5],
        }


def update_rules(rules: dict[str, Any] | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    with _lock:
        _load()
        for name, change in (rules or {}).items():
            rule = _state["rules"].get(name)
            if rule is None or not isinstance(change, dict):
                raise ValueError(f"Unknown alert rule {name!r}")
            if "enabled" in change:
                rule["enabled"] = bool(change["enabled"])
            if "seconds" in change and "seconds" in rule:
                rule["seconds"] = max(1, int(float(change["seconds"])))
            if "percent" in change and "percent" in rule:
                rule["percent"] = max(1, min(100, int(float(change["percent"]))))
        for key, value in (settings or {}).items():
            if key == "interval_s":
                _state["settings"]["interval_s"] = max(15, min(3600, int(float(value))))
            elif key == "notify":
                _state["settings"]["notify"] = bool(value)
            else:
                raise ValueError(f"Unknown alert setting {key!r}")
        _save()
    _wake.set()  # a new interval / rule applies at once
    return state(include_alerts=False)


def acknowledge(ids: list[str] | None = None) -> int:
    """Acknowledge the given alerts, or all of them (ids None)."""
    n = 0
    with _lock:
        _load()
        for a in _state["alerts"]:
            if not a.get("acked") and (ids is None or a["id"] in ids):
                a["acked"] = _now()
                n += 1
        _save()
    return n


def clear(only_acknowledged: bool = False) -> int:
    with _lock:
        _load()
        before = len(_state["alerts"])
        _state["alerts"] = [a for a in _state["alerts"] if only_acknowledged and not a.get("acked")]
        _save()
        return before - len(_state["alerts"])


# ------------------------------------------------------------------ recording

def _record(found: list[dict[str, Any]], source: str, continuing_kinds: set[str]) -> None:
    """Merge one check's findings into the alert list. Call with _lock held.

    ``continuing`` findings (a query still running, a full disk) update one alert; while it stays
    active it isn't re-raised. ``occurrence`` findings (a failure) add to the count and re-open it.
    Continuing alerts of the checked kinds that weren't found again are marked ended.
    """
    now = _now()
    by_key = {a["key"]: a for a in _state["alerts"] if a.get("source") == source}
    seen: set[str] = set()
    for f in found:
        key = f["key"]
        seen.add(key)
        a = by_key.get(key)
        if a is None:
            a = {"id": f"{int(now * 1000):x}{len(_state['alerts']) % 1000:03d}", "key": key, "source": source,
                 "first_seen": now, "count": 0, "acked": None}
            _state["alerts"].append(a)
            by_key[key] = a
        elif not f.get("continuing") or not a.get("active"):
            # Newest at the end of the list (the view shows newest first).
            _state["alerts"].remove(a)
            _state["alerts"].append(a)
        if f.get("continuing"):
            if not a.get("active"):  # new, or back after it ended
                a["count"] = a.get("count", 0) + 1
                a["acked"] = None
                a.pop("ended", None)
                a.pop("unwatched", None)
            a["active"] = True
        else:
            a["count"] = a.get("count", 0) + int(f.get("count", 1))
            a["acked"] = None
            a["active"] = False
        a.update({k: v for k, v in f.items() if k not in ("key", "count", "continuing")})
        a["last_seen"] = now
    for a in by_key.values():
        if a.get("active") and a["kind"] in continuing_kinds and a["key"] not in seen:
            a["active"] = False
            a["ended"] = now
    for a in _state["alerts"]:
        if a.get("active") and a.get("source") != source:
            # Another cluster (the active connection was switched): no longer watched, so no longer "active".
            a["active"] = False
            a["unwatched"] = True


# ------------------------------------------------------------------ the checks

def _q(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    return db.execute(f"{MARKER} {sql}", params)[1]


def _short(sql: str | None, n: int = 160) -> str:
    return " ".join((sql or "").split())[:n]


def _check_running(rule: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _q(
        "SELECT NODE_ID, ID, USER, DB, TIME, STATE, LEFT(INFO, 2000) AS INFO FROM information_schema.MV_PROCESSLIST"
        " WHERE COMMAND <> 'Sleep' AND USER <> 'distributed' AND TIME >= %s AND INFO IS NOT NULL"
        " AND INFO NOT LIKE %s ORDER BY TIME DESC LIMIT 50",
        (int(rule["seconds"]), _NOT_LIKE_OURS),
    )
    return [{
        "key": f"running:{r['NODE_ID']}:{r['ID']}:{history_store.shape_of(r['INFO'])}",
        "kind": "long_running", "severity": "warning" if r["TIME"] < 10 * rule["seconds"] else "critical",
        "title": f"Query running {r['TIME']} s",
        "detail": f"{r['USER']}@{r['DB'] or '-'} (connection {r['ID']}, node {r['NODE_ID']}, {r['STATE'] or 'running'}): {_short(r['INFO'])}",
        "sql": (r["INFO"] or "")[:2000], "seconds": r["TIME"], "connection_id": r["ID"], "node_id": r["NODE_ID"],
        "continuing": True,
    } for r in rows]


def _check_history(slow: dict[str, Any] | None, failed: dict[str, Any] | None, mark: dict[str, Any]) -> list[dict[str, Any]]:
    """Finished queries since the last check: slower than the threshold, or failed."""
    if mark.get("trace_time") is None:
        # First check on this cluster: start from now (no flood of old alerts).
        mark["trace_time"] = str(_q("SELECT NOW(6) AS t")[0]["t"])
        return []
    conds = []
    params: list[Any] = [mark["trace_time"], _NOT_LIKE_OURS]
    if slow:
        conds.append("DETAILS::%%duration_ms >= %s")
        params.append(int(slow["seconds"]) * 1000)
    if failed:
        conds.append("DETAILS::%%success = 0")
    rows = _q(
        "SELECT TIME, DETAILS::%%duration_ms AS ms, DETAILS::%%success AS ok, DETAILS::$user_name AS user,"
        " DETAILS::$context_database AS db, DETAILS::$error_code AS error_code, DETAILS::$error_message AS error_message,"
        " LEFT(DETAILS::$query_text, 2000) AS q FROM information_schema.MV_TRACE_EVENTS"
        " WHERE EVENT_TYPE = 'Query_completion' AND TIME > %s"
        " AND (DETAILS::$query_text IS NULL OR DETAILS::$query_text NOT LIKE %s)"
        f" AND ({' OR '.join(conds)}) ORDER BY TIME LIMIT 500",
        tuple(params),
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        mark["trace_time"] = max(mark["trace_time"], str(r["TIME"]))
        shape = history_store.shape_of(r["q"])
        ok = r["ok"] not in (0, False)
        ms = int(r["ms"] or 0)
        if not ok and failed:
            key = f"failed:{r['error_code'] or '?'}:{shape}"
            f = out.setdefault(key, {
                "key": key, "kind": "failed_query", "severity": "warning", "count": 0,
                "title": f"Query failed: {r['error_code'] or 'error'}",
                "detail": f"{r['user']}@{r['db'] or '-'}: {(r['error_message'] or '')[:300]}",
                "sql": (r["q"] or "")[:2000], "shape": shape})
            f["count"] += 1
        elif ok and slow and ms >= int(slow["seconds"]) * 1000:
            key = f"slow:{shape}"
            f = out.setdefault(key, {
                "key": key, "kind": "slow_query", "severity": "info", "count": 0, "seconds": 0,
                "detail": f"{r['user']}@{r['db'] or '-'}: {_short(r['q'])}", "sql": (r["q"] or "")[:2000], "shape": shape})
            f["count"] += 1
            f["seconds"] = max(f["seconds"], round(ms / 1000, 1))
            f["title"] = f"Query took {f['seconds']:g} s"
    return list(out.values())


def _check_pipelines(mark: dict[str, Any]) -> list[dict[str, Any]]:
    first = mark.get("batch_id") is None
    batches, errors, states, top = db.parallel(
        lambda: [] if first else _q(
            "SELECT DATABASE_NAME, PIPELINE_NAME, COUNT(*) AS n, MAX(BATCH_ID) AS last_id, MAX(START_TIME) AS t"
            " FROM information_schema.PIPELINES_BATCHES_SUMMARY WHERE BATCH_STATE = 'Failed' AND BATCH_ID > %s"
            " GROUP BY 1, 2", (int(mark["batch_id"]),)),
        lambda: [] if first else _q(
            "SELECT DATABASE_NAME, PIPELINE_NAME, ERROR_CODE, ERROR_KIND, COUNT(*) AS n, MAX(ERROR_UNIX_TIMESTAMP) AS t,"
            " MAX(ERROR_MESSAGE) AS msg FROM information_schema.PIPELINES_ERRORS WHERE ERROR_UNIX_TIMESTAMP > %s"
            " GROUP BY 1, 2, 3, 4", (float(mark["error_ts"]),)),
        lambda: _q("SELECT DATABASE_NAME, PIPELINE_NAME, STATE FROM information_schema.PIPELINES WHERE STATE = 'Error'"),
        lambda: _q("SELECT (SELECT MAX(BATCH_ID) FROM information_schema.PIPELINES_BATCHES_SUMMARY) AS b,"
                   " (SELECT MAX(ERROR_UNIX_TIMESTAMP) FROM information_schema.PIPELINES_ERRORS) AS e"),
    )
    if top:
        mark["batch_id"] = max(int(mark.get("batch_id") or 0), int(top[0]["b"] or 0))
        mark["error_ts"] = max(float(mark.get("error_ts") or 0), float(top[0]["e"] or 0))
    out = []
    for r in batches:
        name = f"{r['DATABASE_NAME']}.{r['PIPELINE_NAME']}"
        out.append({"key": f"pipeline-batch:{name}", "kind": "pipeline", "severity": "warning", "count": int(r["n"]),
                    "title": f"Pipeline batch failed: {name}",
                    "detail": f"{r['n']} failed batch(es), last started {r['t']}.", "pipeline": name})
    for r in errors:
        name = f"{r['DATABASE_NAME']}.{r['PIPELINE_NAME']}"
        out.append({"key": f"pipeline-error:{name}:{r['ERROR_CODE']}", "kind": "pipeline", "severity": "warning",
                    "count": int(r["n"]), "title": f"Pipeline error {r['ERROR_CODE']} ({r['ERROR_KIND']}): {name}",
                    "detail": (r["msg"] or "")[:400], "pipeline": name})
    for r in states:
        name = f"{r['DATABASE_NAME']}.{r['PIPELINE_NAME']}"
        out.append({"key": f"pipeline-state:{name}", "kind": "pipeline_state", "severity": "critical",
                    "title": f"Pipeline stopped with an error: {name}", "pipeline": name,
                    "detail": "The pipeline is in the Error state; it doesn't load until it's started again.",
                    "continuing": True})
    return out


def _check_nodes(rules: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    mem_rule, disk_rule, node_rule = (rules[k] if rules[k]["enabled"] else None for k in ("memory", "disk", "node_offline"))
    nodes, mem, disk = db.parallel(
        lambda: _q("SELECT ID, IP_ADDR, PORT, TYPE, STATE, MAX_MEMORY_MB FROM information_schema.MV_NODES"),
        lambda: _q("SELECT NODE_ID, MEMSQL_B FROM information_schema.MV_SYSINFO_MEM") if mem_rule else [],
        lambda: _q("SELECT NODE_ID, MOUNT_POINT, MEMSQL_DIRS, MOUNT_TOTAL_B, MOUNT_USED_B"
                   " FROM information_schema.MV_SYSINFO_DISK WHERE MEMSQL_DIRS <> ''") if disk_rule else [],
    )
    host = {n["ID"]: n["IP_ADDR"].split(".svc-")[0] for n in nodes}
    out = []
    if node_rule:
        for n in nodes:
            if str(n["STATE"]).lower() != "online":
                out.append({"key": f"node:{n['ID']}", "kind": "node_offline", "severity": "critical",
                            "title": f"Node {host[n['ID']]} is {n['STATE']}", "continuing": True,
                            "detail": f"{n['TYPE']} node {n['IP_ADDR']}:{n['PORT']} reports state {n['STATE']}."})
    if mem_rule:
        max_b = {n["ID"]: (n["MAX_MEMORY_MB"] or 0) * 1024 * 1024 for n in nodes}
        for m in mem:
            total = max_b.get(m["NODE_ID"]) or 0
            if total and m["MEMSQL_B"] is not None:
                pct = m["MEMSQL_B"] / total * 100
                if pct >= mem_rule["percent"]:
                    out.append({"key": f"memory:{m['NODE_ID']}", "kind": "memory", "continuing": True,
                                "severity": "critical" if pct >= 95 else "warning", "percent": round(pct, 1),
                                "title": f"Memory {pct:.0f}% on {host.get(m['NODE_ID'], m['NODE_ID'])}",
                                "detail": f"SingleStore uses {m['MEMSQL_B'] / 1024 ** 3:.1f} GB of {total / 1024 ** 3:.1f} GB max_memory."})
    if disk_rule:
        for d in disk:
            total, used = int(d["MOUNT_TOTAL_B"] or 0), int(d["MOUNT_USED_B"] or 0)
            if total:
                pct = used / total * 100
                if pct >= disk_rule["percent"]:
                    out.append({"key": f"disk:{d['NODE_ID']}:{d['MOUNT_POINT']}", "kind": "disk", "continuing": True,
                                "severity": "critical" if pct >= 97 else "warning", "percent": round(pct, 1),
                                "title": f"Disk {pct:.0f}% full on {host.get(d['NODE_ID'], d['NODE_ID'])}",
                                "detail": f"{d['MOUNT_POINT']} ({d['MEMSQL_DIRS']}): {used / 1024 ** 3:.0f} of {total / 1024 ** 3:.0f} GB used."})
    return out


def check_once() -> dict[str, Any]:
    """Run every enabled rule once and record the findings. Never raises."""
    started = time.time()
    with _lock:
        _load()
        rules = copy.deepcopy(_state["rules"])
    try:
        source = history_store.source_id()
    except Exception:  # noqa: BLE001
        source = "?"
    with _lock:
        mark = dict(_state["watermarks"].get(source) or {})
    start_mark = dict(mark)
    on ={k: (r if r["enabled"] else None) for k, r in rules.items()}
    tasks: list[tuple[str, Any]] = []
    if on["long_running"]:
        tasks.append(("long_running", lambda: _check_running(on["long_running"])))
    if on["slow_query"] or on["failed_query"]:
        tasks.append(("history", lambda: _check_history(on["slow_query"], on["failed_query"], mark)))
    if on["pipeline"]:
        tasks.append(("pipeline", lambda: _check_pipelines(mark)))
    if on["memory"] or on["disk"] or on["node_offline"]:
        tasks.append(("nodes", lambda: _check_nodes(rules)))

    def guarded(name: str, fn: Any) -> Any:
        def run() -> tuple[str, Any, str | None]:
            try:
                return name, fn(), None
            except Exception as exc:  # noqa: BLE001 - reported as a "checks failing" alert
                return name, [], f"{name}: {str(exc)[:300]}"
        return run

    results = db.parallel(*[guarded(n, f) for n, f in tasks]) if tasks else []
    # Keep a durable copy of the history for the Trends tab (cheap: only new events).
    history_store.safe_sync()
    found = [f for _, items, _ in results for f in items]
    errors = [e for _, _, e in results if e]
    if errors and len(errors) == len(tasks):
        found.append({"key": "checks-failing", "kind": "check_error", "severity": "critical",
                      "title": "Alert checks can't reach the cluster", "detail": "; ".join(errors)[:600], "continuing": True})
    checked = {"long_running" if on["long_running"] else "", "pipeline_state" if on["pipeline"] else "",
               "memory" if on["memory"] else "", "disk" if on["disk"] else "", "node_offline" if on["node_offline"] else "",
               "check_error"} - {""}
    # A continuing kind whose check itself failed wasn't really "not found": don't end those alerts.
    failed_tasks = {n for n, _, e in results if e}
    if "long_running" in failed_tasks:
        checked.discard("long_running")
    if "pipeline" in failed_tasks:
        checked.discard("pipeline_state")
    if "nodes" in failed_tasks:
        checked -= {"memory", "disk", "node_offline"}
    with _lock:
        _load()
        stored = _state["watermarks"].get(source) or {}
        if any(stored.get(k) != start_mark.get(k) for k in ("trace_time", "batch_id", "error_ts")):
            # Another server process (e.g. the desktop workspace next to Claude's) checked the same cluster
            # meanwhile and already recorded these occurrences: keep only the continuing findings.
            found = [f for f in found if f.get("continuing")]
            mark = {k: max(v, stored[k]) if stored.get(k) is not None and v is not None else (v if v is not None else stored.get(k))
                    for k, v in {**stored, **mark}.items()}
        _record(found, source, checked)
        _state["watermarks"][source] = {**(_state["watermarks"].get(source) or {}), **mark}
        _save()
    _status.update(last_check=_now(), last_error="; ".join(errors) or None,
                   last_seconds=round(time.time() - started, 2), checks=_status["checks"] + 1, source=source)
    return {"found": len(found), "errors": errors, "seconds": _status["last_seconds"]}


# ------------------------------------------------------------------ the background thread

def _loop() -> None:
    time.sleep(float(os.environ.get("SINGLESTORE_MCP_ALERTS_DELAY", "15")))  # let the server start first
    while not _stop.is_set():
        try:
            check_once()
        except Exception as exc:  # noqa: BLE001 - the checker must never take the server down
            _status["last_error"] = f"checker: {str(exc)[:300]}"
        with _lock:
            try:
                _load()
                interval = int(_state["settings"]["interval_s"])
            except Exception:  # noqa: BLE001
                interval = 60
        _wake.clear()
        _wake.wait(interval)
        if _stop.is_set():
            break
    _status["running"] = False


def start() -> bool:
    """Start the background checker (once per process). SINGLESTORE_MCP_ALERTS=0 turns it off."""
    global _thread
    if os.environ.get("SINGLESTORE_MCP_ALERTS", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="s2-alerts", daemon=True)
        _status["running"] = True
        _thread.start()
    return True


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    _wake.set()
    if _thread:
        _thread.join(timeout)


def check_now() -> None:
    """Wake the checker for an immediate check (or run one inline if it isn't running)."""
    if _thread and _thread.is_alive():
        _wake.set()
    else:
        check_once()
