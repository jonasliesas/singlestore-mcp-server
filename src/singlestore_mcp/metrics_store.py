"""Persisted metrics history: a light background sampler that keeps per-node CPU, memory, disk, queued and
running queries and connections in SQLite, for the Cluster Monitor's History tab (1 h / 24 h / 7 d).

- One sample a minute (``SINGLESTORE_MCP_METRICS_INTERVAL``, 15-300 s), only while a server process runs.
  ``SINGLESTORE_MCP_METRICS=0`` turns the sampler off (as ``SINGLESTORE_MCP_ALERTS=0`` does the alerts).
- A sample is four information_schema reads in parallel (the Cluster Monitor's CPU/disk counters, nodes +
  memory, and process-list counts): one cluster round trip. It starts ``SINGLESTORE_MCP_METRICS_DELAY`` s
  (default 20) after the server, so it never slows start-up, and runs in its own thread, so tool calls
  never wait for it.
- Stored in ``~/.singlestore-mcp/metrics.db`` (``SINGLESTORE_MCP_HOME``), keyed by cluster (host:port of the
  active connection, as the query history). Two server processes (Claude's and the desktop Workspace) share
  the file: a process skips its sample when the other one already sampled the same cluster in this interval.
- Retention ``SINGLESTORE_MCP_METRICS_RETENTION_DAYS`` (default 7, 1-90). Raw samples are kept for 24 h;
  older ones are folded into 10-minute rows (averages weighted by sample count, peaks for CPU and queue).

Read-only on the cluster.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

from .db import db
from .paths import data_dir

MARKER = "/* s2-metrics */"
COARSE_S = 600            # older samples are folded into 10-minute rows
RAW_KEEP_S = 86400        # raw samples are kept for 24 h
_COMPACT_EVERY_S = 3600
_OFF = ("0", "false", "no", "off")

_lock = threading.Lock()
_schema_ready: set[str] = set()
_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict[str, Any] = {"running": False, "last_sample": None, "last_error": None, "samples": 0, "skipped": 0,
                           "last_seconds": None, "source": None}
_last_compact: dict[str, float] = {}

# Value columns, how they are folded into a coarse row, and how they are read back per bucket.
_COLS = ("cpu", "cpu_max", "host_cpu", "mem_b", "mem_max_b", "disk_used_b", "disk_total_b", "queued", "queued_max",
         "running", "conns")


# ------------------------------------------------------------------ settings

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(os.environ.get(name, default)))))
    except (TypeError, ValueError):
        return default


def settings() -> dict[str, Any]:
    return {
        "enabled": os.environ.get("SINGLESTORE_MCP_METRICS", "1").strip().lower() not in _OFF,
        "interval_s": _env_int("SINGLESTORE_MCP_METRICS_INTERVAL", 60, 15, 300),
        "retention_days": _env_int("SINGLESTORE_MCP_METRICS_RETENTION_DAYS", 7, 1, 90),
        "delay_s": _env_int("SINGLESTORE_MCP_METRICS_DELAY", 20, 0, 3600),
    }


# ------------------------------------------------------------------ SQLite

def _path() -> str:
    return str(data_dir() / "metrics.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or _path()
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    with _lock:
        ready = path in _schema_ready
    if not ready:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS metrics ("
            " source TEXT NOT NULL, node_id INTEGER NOT NULL, t INTEGER NOT NULL, res INTEGER NOT NULL,"
            " host TEXT, type TEXT, n INTEGER NOT NULL DEFAULT 1,"
            + ", ".join(f"{c} REAL" for c in _COLS)
            + ", PRIMARY KEY (source, node_id, res, t))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS metrics_time ON metrics (source, t)")
        conn.commit()
        with _lock:
            _schema_ready.add(path)
    return conn


def write(conn: sqlite3.Connection, source: str, t: int, res: int, rows: list[dict[str, Any]]) -> int:
    """Store one sample (one row per node) at bucket time ``t``. Idempotent: the same bucket is replaced."""
    cols = ("source", "node_id", "t", "res", "host", "type", "n", *_COLS)
    conn.executemany(
        f"INSERT OR REPLACE INTO metrics ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [(source, r["node_id"], t, res, r.get("host"), r.get("type"), 1, *(r.get(c) for c in _COLS)) for r in rows],
    )
    conn.commit()
    return len(rows)


def compact(conn: sqlite3.Connection, now: float, retention_days: int, raw_keep_s: int = RAW_KEEP_S) -> dict[str, int]:
    """Fold raw samples older than ``raw_keep_s`` into COARSE_S rows and drop everything past the retention.

    The cut-off is aligned to COARSE_S, so a coarse row is only ever built from a complete bucket."""
    cutoff = int((now - raw_keep_s) // COARSE_S * COARSE_S)
    w = "SUM({c} * n) / NULLIF(SUM(CASE WHEN {c} IS NULL THEN 0 ELSE n END), 0)"
    folded = conn.execute(
        "INSERT OR REPLACE INTO metrics (source, node_id, t, res, host, type, n, " + ", ".join(_COLS) + ")"
        f" SELECT source, node_id, (t / {COARSE_S}) * {COARSE_S}, {COARSE_S}, MAX(host), MAX(type), SUM(n), "
        + ", ".join(
            "MAX(cpu_max)" if c == "cpu_max" else "MAX(queued_max)" if c == "queued_max"
            else "MAX(mem_max_b)" if c == "mem_max_b" else "MAX(disk_total_b)" if c == "disk_total_b"
            else w.format(c=c)
            for c in _COLS)
        + f" FROM metrics WHERE res <> {COARSE_S} AND t < ? GROUP BY source, node_id, (t / {COARSE_S})",
        (cutoff,),
    ).rowcount
    dropped_raw = conn.execute(f"DELETE FROM metrics WHERE res <> {COARSE_S} AND t < ?", (cutoff,)).rowcount
    expired = conn.execute("DELETE FROM metrics WHERE t < ?", (int(now - retention_days * 86400),)).rowcount
    conn.commit()
    return {"folded": max(0, folded), "raw_removed": max(0, dropped_raw), "expired": max(0, expired)}


RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}


def bucket_for(range_s: int) -> int:
    """Chart resolution: about 60-340 points whatever the range."""
    return 60 if range_s <= 3600 else 300 if range_s <= 86400 else 1800


def read(conn: sqlite3.Connection, source: str, range_s: int, now: float, bucket_s: int | None = None) -> dict[str, Any]:
    """Points for the chart: per bucket, per node, CPU %, memory % of max_memory, disk % used, queued (peak),
    running and connections (averages). Averages are weighted by the number of samples behind each row."""
    b = int(bucket_s or bucket_for(range_s))
    w = "SUM({c} * n) / NULLIF(SUM(CASE WHEN {c} IS NULL THEN 0 ELSE n END), 0)"
    rows = conn.execute(
        f"SELECT (t / {b}) * {b} AS bt, node_id, MAX(host) AS host, MAX(type) AS type, SUM(n) AS n,"
        f" {w.format(c='cpu')} AS cpu, MAX(cpu_max) AS cpu_max, {w.format(c='mem_b')} AS mem_b, MAX(mem_max_b) AS mem_max_b,"
        f" {w.format(c='disk_used_b')} AS disk_used_b, MAX(disk_total_b) AS disk_total_b, MAX(queued_max) AS queued,"
        f" {w.format(c='running')} AS running, {w.format(c='conns')} AS conns"
        " FROM metrics WHERE source = ? AND t >= ? GROUP BY bt, node_id ORDER BY bt, node_id",
        (source, int(now - range_s)),
    ).fetchall()
    points: dict[int, dict[str, Any]] = {}
    nodes: dict[int, dict[str, Any]] = {}
    for r in rows:
        p = points.setdefault(r["bt"], {"t": r["bt"], "cpu": {}, "cpu_max": {}, "mem": {}, "disk": {}, "queued": {},
                                         "running": {}, "conns": {}})
        k = str(r["node_id"])
        nodes[r["node_id"]] = {"id": r["node_id"], "host": r["host"], "type": r["type"]}
        if r["cpu"] is not None:
            p["cpu"][k] = round(r["cpu"], 1)
        if r["cpu_max"] is not None:
            p["cpu_max"][k] = round(r["cpu_max"], 1)
        if r["mem_b"] is not None and r["mem_max_b"]:
            p["mem"][k] = round(r["mem_b"] / r["mem_max_b"] * 100, 1)
        if r["disk_used_b"] is not None and r["disk_total_b"]:
            p["disk"][k] = round(r["disk_used_b"] / r["disk_total_b"] * 100, 1)
        for c in ("queued", "running", "conns"):
            if r[c] is not None:
                p[c][k] = round(r[c], 1)
    span = conn.execute("SELECT MIN(t), MAX(t), COUNT(*) FROM metrics WHERE source = ?", (source,)).fetchone()
    return {"range_s": range_s, "bucket_s": b, "points": list(points.values()),
            "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "stored": {"first_t": span[0], "last_t": span[1], "rows": span[2]}}


# ------------------------------------------------------------------ sampling

def _q(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    return db.execute(f"{MARKER} {sql}", params)[1]


def _collect() -> list[dict[str, Any]]:
    """One sample of every node: four reads in parallel (one round trip)."""
    from .apps import cluster_monitor as cm  # the monitor's counter samples double as this sampler's baseline

    (rates, _gap), nodes, procs = db.parallel(
        cm._rates,
        lambda: _q("SELECT n.ID, n.IP_ADDR, n.TYPE, n.MAX_MEMORY_MB, m.MEMSQL_B FROM information_schema.MV_NODES n"
                   " LEFT JOIN information_schema.MV_SYSINFO_MEM m ON m.NODE_ID = n.ID"),
        lambda: _q("SELECT NODE_ID, COUNT(*) AS conns,"
                   " SUM(CASE WHEN COMMAND <> 'Sleep' AND (INFO IS NULL OR INFO NOT LIKE %s) THEN 1 ELSE 0 END) AS running,"
                   " SUM(CASE WHEN REASON_FOR_QUEUEING IS NOT NULL AND REASON_FOR_QUEUEING <> '' THEN 1 ELSE 0 END) AS queued"
                   " FROM information_schema.MV_PROCESSLIST WHERE USER <> 'distributed' GROUP BY NODE_ID", ("%/* s2-%",)),
    )
    return build_rows(nodes, rates, procs)


def build_rows(nodes: list[dict[str, Any]], rates: dict[int, dict[str, Any]], procs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows to store from MV_NODES (+ memory), the Cluster Monitor's rates and the process-list counts."""
    pl = {r["NODE_ID"]: r for r in procs}
    out = []
    for n in nodes:
        r = rates.get(n["ID"], {})
        cpu = r.get("cpu") or {}
        disk = next(iter(r.get("mounts") or []), None)
        p = pl.get(n["ID"], {})
        queued = int(p.get("queued") or 0)
        out.append({
            "node_id": n["ID"],
            "host": str(n["IP_ADDR"]).split(".svc-")[0],
            "type": n["TYPE"],
            "cpu": cpu.get("singlestore_pct"),
            "cpu_max": cpu.get("singlestore_pct"),
            "host_cpu": cpu.get("host_pct"),
            "mem_b": n.get("MEMSQL_B"),
            "mem_max_b": (n.get("MAX_MEMORY_MB") or 0) * 1024 * 1024 or None,
            "disk_used_b": disk["used_b"] if disk else None,
            "disk_total_b": disk["total_b"] if disk else None,
            "queued": queued,
            "queued_max": queued,
            "running": int(p.get("running") or 0),
            "conns": int(p.get("conns") or 0),
        })
    return out


def _source() -> str:
    from . import history_store

    return history_store.source_id()


def sample_once(force: bool = False) -> dict[str, Any]:
    """Take one sample and store it (unless another process sampled this cluster less than an interval ago)."""
    cfg = settings()
    interval = cfg["interval_s"]
    started = time.time()
    source = _source()
    conn = connect()
    try:
        t = int(started // interval * interval)
        if not force:
            # Another server process (Claude's or the desktop Workspace's) already sampled this cluster this interval.
            if conn.execute("SELECT 1 FROM metrics WHERE source = ? AND res = ? AND t = ? LIMIT 1", (source, interval, t)).fetchone():
                _status["skipped"] += 1
                return {"skipped": True, "source": source}
        rows = _collect()
        n = write(conn, source, t, interval, rows)
        if force or started - _last_compact.get(source, 0) > _COMPACT_EVERY_S:
            compact(conn, started, cfg["retention_days"])
            _last_compact[source] = started
    finally:
        conn.close()
    _status.update(last_sample=round(time.time(), 1), last_error=None, samples=_status["samples"] + 1,
                   last_seconds=round(time.time() - started, 2), source=source)
    return {"skipped": False, "source": source, "nodes": n, "seconds": _status["last_seconds"]}


def history(range_key: str = "1h") -> dict[str, Any]:
    """Stored history of the active cluster for a range ("1h", "24h", "7d"). Local SQLite only: no cluster query."""
    range_s = RANGES.get(range_key, 3600)
    source = _source()
    conn = connect()
    try:
        data = read(conn, source, range_s, time.time())
    finally:
        conn.close()
    return {**data, "range": range_key if range_key in RANGES else "1h", "source": source, "status": status()}


def status() -> dict[str, Any]:
    cfg = settings()
    return {**_status, **cfg, "running": bool(_thread and _thread.is_alive()), "file": _path()}


# ------------------------------------------------------------------ the background thread

def _loop() -> None:
    cfg = settings()
    if _stop.wait(cfg["delay_s"]):  # let the server start first
        return
    while not _stop.is_set():
        try:
            sample_once()
        except Exception as exc:  # noqa: BLE001 - the sampler must never take the server down
            _status["last_error"] = str(exc)[:300]
        _stop.wait(settings()["interval_s"])
    _status["running"] = False


def start() -> bool:
    """Start the sampler thread (once per process). SINGLESTORE_MCP_METRICS=0 turns it off."""
    global _thread
    if not settings()["enabled"]:
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="s2-metrics", daemon=True)
        _status["running"] = True
        _thread.start()
    return True


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout)
