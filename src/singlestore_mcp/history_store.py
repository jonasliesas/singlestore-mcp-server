"""Durable local copy of the query history, for trends over weeks.

The cluster's query history (``information_schema.MV_TRACE_EVENTS``) is a ring buffer that forgets.
Each finished query it traced is summarised into SQLite at ``~/.singlestore-mcp/query_history.db``
(stdlib sqlite3): when it finished, how long it ran, user, database, success, error code, category,
rows, and its *query shape* (the SQL with literals replaced) as a hash plus a short sample.

Rows are keyed by the cluster they came from (``source``: host:port of the active connection) and the
event key ``NODE_ID:NODE_START_EPOCH_S:EVENT_ID``, so a sync is idempotent and switching connections
never mixes clusters. ``sync()`` runs when the Query History list loads and on every Alerts check.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import statistics
import threading
import time
import urllib.parse
from typing import Any

from .db import db
from .paths import data_dir

# Every statement this server sends for its own bookkeeping starts with a "/* s2-… */" comment;
# the history views leave those out.
MARKER_PREFIX = "/* s2-"
_MARKER = "/* s2-history-store */"
SHAPE_CHARS = 400          # a shape is identified by the first 400 characters of the SQL
_SAMPLE_CHARS = 300
_MIN_SYNC_GAP_S = 20
_OVERLAP_S = 120           # re-read a little before the newest stored event (clock jitter between nodes)

_lock = threading.Lock()
_last_sync: dict[str, float] = {}


# ------------------------------------------------------------------ shapes and origins

def normalize(sql: str) -> str:
    """Query shape: literals replaced, whitespace collapsed, lower case (runs with other values group)."""
    s = re.sub(r"'(?:[^'\\]|\\.|'')*'", "?", sql or "")
    s = re.sub(r"\b\d+(?:\.\d+)?\b", "?", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return re.sub(r"\(\s*\?(?:\s*,\s*\?)*\s*\)", "(?)", s)


def shape_of(sql: str | None) -> str:
    """Short hash of the query shape of the first SHAPE_CHARS characters (the same in the list and the store)."""
    return hashlib.sha1(normalize((sql or "")[:SHAPE_CHARS]).encode("utf-8")).hexdigest()[:12]


_SAS_CAS = re.compile(r"binary_serialization|PARALLELISM_LEVEL\s*=\s*\"?SEGMENT", re.I)
# A table (after FROM / JOIN / INTO / TABLE / UPDATE, optionally database-qualified) named _dm…, _flw… or SASTMP….
_SAS_INDB = re.compile(r"\b(?:FROM|JOIN|INTO|TABLE|UPDATE)\s+(?:`?[\w$]+`?\s*\.\s*)?`?(?:_dm|_flw|SASTMP)[\w$]*`?", re.I)


def origin(sql: str | None) -> str | None:
    """Probable origin of a statement from simple heuristics on its text."""
    if not sql:
        return None
    if _SAS_CAS.search(sql):
        return "SAS CAS"
    if _SAS_INDB.search(sql):
        return "SAS in-database"
    return None


# ------------------------------------------------------------------ the store

def source_id() -> str:
    """The cluster rows come from: host:port of the active connection (never the password)."""
    from . import connections

    p = connections.active()
    if p is None:
        return "env"
    if p.url:
        raw = p.url if "://" in p.url else f"x://{p.url}"
        try:
            u = urllib.parse.urlsplit(raw)
            return f"{u.hostname or '?'}:{u.port or 3306}"
        except ValueError:
            return p.name
    return f"{p.host}:{p.port}"


def _path() -> str:
    return str(data_dir() / "query_history.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        " source TEXT NOT NULL, event_key TEXT NOT NULL, finished TEXT NOT NULL, ms INTEGER NOT NULL,"
        " user TEXT, database TEXT, ok INTEGER NOT NULL, error_code TEXT, category TEXT, rows INTEGER,"
        " shape TEXT NOT NULL, sample TEXT, origin TEXT, PRIMARY KEY (source, event_key))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS runs_time ON runs (source, finished)")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_shape ON runs (source, shape, finished)")
    return conn


def sync(force: bool = False, min_ms: int = 1000) -> dict[str, Any]:
    """Copy new events from the cluster's query history. Cheap: only events after the newest stored one."""
    src = source_id()
    now = time.time()
    with _lock:
        if not force and now - _last_sync.get(src, 0) < _MIN_SYNC_GAP_S:
            return {"skipped": True, "source": src}
        _last_sync[src] = now
    conn = connect()
    try:
        newest = conn.execute("SELECT MAX(finished) FROM runs WHERE source = ?", (src,)).fetchone()[0]
        where = ["EVENT_TYPE = 'Query_completion'", "DETAILS::%%duration_ms >= %s",
                 "(DETAILS::$query_text IS NULL OR DETAILS::$query_text NOT LIKE %s)"]
        params: list[Any] = [int(min_ms), f"%{MARKER_PREFIX}%"]
        if newest:
            where.append("TIME >= %s - INTERVAL %s SECOND")
            params += [newest, _OVERLAP_S]
        rows = db.execute(
            f"{_MARKER} SELECT NODE_ID, NODE_START_EPOCH_S, EVENT_ID, TIME, DETAILS::%%duration_ms AS ms,"
            " DETAILS::$user_name AS user, DETAILS::$context_database AS db, DETAILS::%%success AS ok,"
            " DETAILS::$error_code AS error_code, DETAILS::$query_category AS category,"
            f" DETAILS::%%row_count AS row_count, LEFT(DETAILS::$query_text, {SHAPE_CHARS}) AS head"
            f" FROM information_schema.MV_TRACE_EVENTS WHERE {' AND '.join(where)}",
            tuple(params),
        )[1]
        batch = []
        for r in rows:
            head = r["head"] or ""
            batch.append((
                src, f"{r['NODE_ID']}:{r['NODE_START_EPOCH_S']}:{r['EVENT_ID']}",
                r["TIME"].isoformat(sep=" ") if hasattr(r["TIME"], "isoformat") else str(r["TIME"]),
                int(r["ms"] or 0), r["user"], r["db"] or None, 0 if r["ok"] in (0, False) else 1,
                r["error_code"] or None, r["category"], None if r["row_count"] is None else int(r["row_count"]),
                shape_of(head), " ".join(head.split())[:_SAMPLE_CHARS], origin(head),
            ))
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()
        return {"source": src, "read": len(batch), "added": conn.total_changes - before}
    finally:
        conn.close()


def safe_sync(**kwargs: Any) -> dict[str, Any]:
    try:
        return sync(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the store is a bonus; never fail the caller over it
        return {"error": str(exc)[:300]}


# ------------------------------------------------------------------ trends

def _p95(values: list[int]) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def _buckets(conn: sqlite3.Connection, src: str, since: str, fmt: str) -> list[dict[str, Any]]:
    per: dict[str, list[tuple[int, int]]] = {}
    for r in conn.execute("SELECT finished, ms, ok FROM runs WHERE source = ? AND finished >= ? ORDER BY finished",
                          (src, since)):
        per.setdefault(r["finished"][:fmt], []).append((r["ms"], r["ok"]))
    return [{"t": k, "queries": len(v), "total_ms": sum(m for m, _ in v), "failed": sum(1 for _, ok in v if not ok),
             "p95_ms": _p95([m for m, _ in v])} for k, v in sorted(per.items())]


def trends(days: int = 30, now: str | None = None) -> dict[str, Any]:
    """Daily buckets for ``days`` and hourly buckets for the last 48 h, plus shapes that got slower.

    ``now`` is the cluster's current time ("YYYY-MM-DD HH:MM:SS"), since stored times are cluster times.
    """
    src = source_id()
    days = max(1, min(366, int(days)))
    if now is None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
    t_now = time.mktime(time.strptime(now[:19], "%Y-%m-%d %H:%M:%S"))
    at = lambda secs: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_now - secs))  # noqa: E731
    conn = connect()
    try:
        info = conn.execute("SELECT COUNT(*) AS n, MIN(finished) AS oldest, MAX(finished) AS newest FROM runs"
                            " WHERE source = ?", (src,)).fetchone()
        daily = _buckets(conn, src, at(days * 86400)[:10], 10)
        hourly = _buckets(conn, src, at(48 * 3600)[:13], 13)
        slower = _slower(conn, src, at(7 * 86400), at(14 * 86400))
    finally:
        conn.close()
    return {"source": src, "days": days, "now": now, "stored": info["n"], "oldest": info["oldest"],
            "newest": info["newest"], "daily": daily, "hourly": hourly, **slower}


def _slower(conn: sqlite3.Connection, src: str, week: str, two_weeks: str) -> dict[str, Any]:
    """Per shape: median duration in the last 7 days vs the 7 days before."""
    recent: dict[str, list[int]] = {}
    before: dict[str, list[int]] = {}
    samples: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT shape, finished, ms, sample, database, user, origin FROM runs"
                          " WHERE source = ? AND finished >= ? AND ok = 1", (src, two_weeks)):
        (recent if r["finished"] >= week else before).setdefault(r["shape"], []).append(r["ms"])
        if r["finished"] >= week or r["shape"] not in samples:
            samples[r["shape"]] = {"sample": r["sample"], "database": r["database"], "user": r["user"], "origin": r["origin"]}
    seen_before = {r[0] for r in conn.execute(
        "SELECT DISTINCT shape FROM runs WHERE source = ? AND finished < ?", (src, week))}
    slower, new = [], []
    for shape, ms in recent.items():
        med = statistics.median(ms)
        if shape in before:
            old = statistics.median(before[shape])
            # Markedly slower: at least 1.5x and a second more (noise on tiny queries doesn't count).
            if old > 0 and med >= 1.5 * old and med - old >= 1000:
                slower.append({"shape": shape, "median_ms": int(med), "before_ms": int(old), "ratio": round(med / old, 2),
                               "runs": len(ms), "runs_before": len(before[shape]), **samples[shape]})
        elif shape not in seen_before:
            total = sum(ms)
            if total >= 60_000 or med >= 30_000:  # new and heavy
                new.append({"shape": shape, "median_ms": int(med), "total_ms": total, "runs": len(ms), **samples[shape]})
    slower.sort(key=lambda s: -(s["median_ms"] - s["before_ms"]) * s["runs"])
    new.sort(key=lambda s: -s["total_ms"])
    return {"slower": slower[:25], "new_heavy": new[:25]}
