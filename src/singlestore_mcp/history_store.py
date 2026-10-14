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
import json
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

_EP_FUNCTION = re.compile(r"\bh[0-9A-F]{32}_(\w+)", re.I)


def normalize(sql: str) -> str:
    """Query shape: literals replaced, whitespace collapsed, lower case (runs with other values group).

    SAS EP's per-job function names (EP.h<32 hex>_EP, <db>.h<32 hex>_<db>) change on every run, so they're
    replaced too: runs of the same SAS VA / CAS / EP statement get the same shape."""
    s = _EP_FUNCTION.sub(r"h?_\1", sql or "")
    s = re.sub(r"'(?:[^'\\]|\\.|'')*'", "?", s)
    s = re.sub(r"\b\d+(?:\.\d+)?\b", "?", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return re.sub(r"\(\s*\?(?:\s*,\s*\?)*\s*\)", "(?)", s)


def shape_of(sql: str | None) -> str:
    """Short hash of the query shape of the first SHAPE_CHARS characters (the same in the list and the store)."""
    return hashlib.sha1(normalize((sql or "")[:SHAPE_CHARS]).encode("utf-8")).hexdigest()[:12]


# Comments and quoted strings / identifiers, in one pass so a "--" inside a string isn't taken for a comment.
_LEXEMES = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"|`(?:[^`]|``)*`|/\*.*?\*/|--[^\n]*|#[^\n]*", re.S)


def strip_comments(sql: str | None) -> str:
    """The statement without comments (strings and quoted identifiers kept as they are)."""
    return _LEXEMES.sub(lambda m: " " if m.group(0)[0] in "/-#" else m.group(0), sql or "")


# The plan cache shows parameterized text: "@" for numbers and "^" for strings (seen on 9.0), "?" for prepared
# statement placeholders. A placeholder is one of those characters standing alone (not part of @var or @@sys).
PLAN_PARAM = re.compile(r"(?<![\w@^?])[@^](?![\w@^])")


def statement_key(sql: str | None, database: str | None, parameterized: bool = False) -> str:
    """The same statement whatever its literal values: comments stripped, literals replaced, plus the database.
    ``parameterized`` for plan cache text (placeholders already in place), so both sides of a match agree."""
    text = strip_comments(sql)
    if parameterized:
        text = PLAN_PARAM.sub("?", text)
    return hashlib.sha1(f"{(database or '').lower()}\n{normalize(text[:SHAPE_CHARS * 4])}".encode("utf-8")).hexdigest()[:14]


# ------------------------------------------------------------------ tables a statement reads or writes
# A small tokenizer, not a parser: enough to find the table names after FROM / JOIN / INTO / UPDATE / TABLE
# reliably (comma lists, aliases, backticks, db.table, CTE names, derived tables and table functions skipped).

_TOKEN = re.compile(r"`((?:[^`]|``)*)`|([A-Za-z_$][\w$]*)|(\d+(?:\.\d+)?)|(\S)")
_STRINGS = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"")
# Words that end a table reference (or can't be an alias).
_STOP = {
    "where", "on", "using", "join", "inner", "left", "right", "full", "outer", "cross", "natural", "straight_join",
    "group", "order", "having", "limit", "union", "intersect", "except", "minus", "window", "set", "values", "value",
    "select", "with", "as", "partition", "use", "force", "ignore", "lateral", "for", "into", "lock", "option",
    "returning", "procedure", "fields", "columns", "lines", "character", "format", "from", "and", "or", "not", "when",
    "then", "else", "end", "case", "is", "in", "exists", "between", "like", "table", "default", "duplicate", "key",
    "if", "rlike", "regexp", "sort", "shard", "unique", "primary", "index", "skip", "replace", "max_errors",
}
# FROM inside these functions isn't a table: EXTRACT(YEAR FROM d), TRIM(x FROM y), SUBSTRING(s FROM 2)…
_FROM_FUNCS = {"extract", "trim", "substring", "substr", "position", "overlay", "mid"}
_WRITE_LEAD = {"insert", "replace", "update", "delete", "load", "create", "truncate", "alter", "drop"}
_NOT_TABLES = {"outfile", "dumpfile", "s3", "gcs", "azure", "kafka", "fs", "hdfs", "link", "dual"}


def _tokens(sql: str) -> list[tuple[str, str]]:
    """(kind, text): kind "id" (bare identifier, original case), "qid" (backticked), "num" or "p" (punctuation)."""
    text = _STRINGS.sub(" '' ", strip_comments(sql))
    out: list[tuple[str, str]] = []
    for m in _TOKEN.finditer(text):
        if m.group(1) is not None:
            out.append(("qid", m.group(1).replace("``", "`")))
        elif m.group(2):
            out.append(("id", m.group(2)))
        elif m.group(3):
            out.append(("num", m.group(3)))
        else:
            out.append(("p", m.group(4)))
    return out


def tables_in(sql: str | None, default_db: str | None = None) -> list[dict[str, Any]]:
    """Tables a statement names: [{"database", "table", "write"}], in order, without duplicates.

    Unqualified names resolve to ``default_db`` (the statement's context database); CTE names, derived tables
    (``FROM (SELECT …)``), table functions (``FROM db.fn(…)``) and INTO OUTFILE / S3 / variables are skipped.
    """
    toks = _tokens(sql or "")
    if not toks:
        return []
    low = [t.lower() if k == "id" else ("`" + t if k == "qid" else t) for k, t in toks]
    first = next((k for k, w in enumerate(low) if w != "("), 0)
    lead = low[first]
    is_write = lead in _WRITE_LEAD
    # CTE names: WITH [RECURSIVE] name [(cols)] AS ( … ), name AS ( … )
    ctes: set[str] = set()
    for i, (k, t) in enumerate(toks):
        if k in ("id", "qid") and i + 1 < len(toks):
            j = i + 1
            if low[j] == "(":  # column list
                depth, j = 1, j + 1
                while j < len(toks) and depth:
                    depth += {"(": 1, ")": -1}.get(low[j], 0)
                    j += 1
            if j + 1 < len(toks) and low[j] == "as" and low[j + 1] == "(" and i > 0 and low[i - 1] in ("with", "recursive", ","):
                ctes.add(t.lower())

    seen: set[tuple[str | None, str]] = set()
    out: list[dict[str, Any]] = []
    # Function-call parentheses: a "(" right after an identifier that isn't a keyword.
    parens: list[str] = []

    def name_at(j: int, functions: bool = True) -> tuple[str | None, str, int] | None:
        """Parse [db.]table at token j: (db, table, next index), or None if it isn't a table name."""
        if j >= len(toks) or toks[j][0] not in ("id", "qid"):
            return None
        first = toks[j][1]
        if toks[j][0] == "id" and low[j] in _STOP:
            return None
        if j + 2 < len(toks) and low[j + 1] == "." and toks[j + 2][0] in ("id", "qid"):
            db_, table, nxt = first, toks[j + 2][1], j + 3
        else:
            db_, table, nxt = None, first, j + 1
        if functions and nxt < len(toks) and low[nxt] == "(":
            return None  # a table function, e.g. FROM db.sasepFunction('…')
        return db_, table, nxt

    def skip_alias(j: int) -> int:
        if j < len(toks) and low[j] == "as":
            j += 1
        if j < len(toks) and toks[j][0] in ("id", "qid") and low[j] not in _STOP:
            j += 1
        return j

    def add(db_: str | None, table: str, write: bool) -> None:
        if db_ is None and table.lower() in ctes:
            return
        if table.lower() in _NOT_TABLES and db_ is None:
            return
        key = (db_ or default_db, table)
        if key in seen:
            if write:
                for o in out:
                    if (o["database"], o["table"]) == key:
                        o["write"] = True
            return
        seen.add(key)
        out.append({"database": db_ or default_db, "table": table, "write": write})

    i = 0
    n = len(toks)
    while i < n:
        w = low[i]
        if w == "(":
            prev = low[i - 1] if i else ""
            parens.append(prev if i and toks[i - 1][0] == "id" else "")
            i += 1
            continue
        if w == ")":
            if parens:
                parens.pop()
            i += 1
            continue
        if toks[i][0] != "id":
            i += 1
            continue
        if w == "from":
            if (parens and parens[-1] in _FROM_FUNCS) or (i and low[i - 1] == "distinct"):
                i += 1
                continue
            # FROM a [AS x], b [y] …  (DELETE FROM t: a write target)
            write = is_write and lead == "delete" and not any(low[k] == "from" for k in range(i))
            j = i + 1
            while True:
                parsed = name_at(j)
                if not parsed:
                    break
                db_, table, j = parsed
                add(db_, table, write)
                j = skip_alias(j)
                if j < n and low[j] == ",":
                    j += 1
                    continue
                break
            i = max(j, i + 1)
            continue
        if w in ("join", "straight_join"):
            parsed = name_at(i + 1)
            if parsed:
                add(parsed[0], parsed[1], False)
                i = parsed[2]
                continue
        if w == "into":
            j = i + 1
            if j < n and low[j] == "table":
                j += 1
            if j < n and (low[j] in _NOT_TABLES or low[j] == "@" or toks[j][0] == "p"):
                i = j + 1
                continue
            parsed = name_at(j, functions=False)  # INSERT INTO t (a, b): a column list
            if parsed:
                add(parsed[0], parsed[1], is_write)
                i = parsed[2]
                continue
        # UPDATE as the statement (not ON DUPLICATE KEY UPDATE): UPDATE a [x], b SET …
        if w == "update" and (i == first or low[i - 1] == ";"):
            j = i + 1
            if j < n and low[j] in ("low_priority", "ignore"):
                j += 1
            while True:
                parsed = name_at(j)
                if not parsed:
                    break
                add(parsed[0], parsed[1], True)
                j = skip_alias(parsed[2])
                if j < n and low[j] == ",":
                    j += 1
                    continue
                break
            i = max(j, i + 1)
            continue
        if w == "table" and i and low[i - 1] in ("create", "truncate", "alter", "drop", "analyze", "optimize",
                                                   "reference", "rowstore", "columnstore", "temporary", "global"):
            j = i + 1
            while j < n and low[j] in ("if", "not", "exists"):
                j += 1
            parsed = name_at(j, functions=False)
            if parsed:
                add(parsed[0], parsed[1], lead in ("create", "truncate", "alter", "drop"))
                i = parsed[2]
                continue
        i += 1
    return out


def table_names(sql: str | None, default_db: str | None = None) -> list[str]:
    """``tables_in`` as "db.table" strings ("table" when there's no database)."""
    return [f"{t['database']}.{t['table']}" if t["database"] else t["table"] for t in tables_in(sql, default_db)]


# SAS CAS through SpeedyStore: binary serialization, segment parallelism, or reading one partition at a time
# ("… WHERE partition_id() = 3", the parallel load's statements as the plan cache shows them).
_SAS_CAS = re.compile(r"binary_serialization(?!_internal)|PARALLELISM_LEVEL\s*=\s*\"?SEGMENT|"
                      r"\bWHERE\s+partition_id\(\)\s*=\s*[\d@?]+\s*$", re.I)
# A table (after FROM / JOIN / INTO / TABLE / UPDATE, optionally database-qualified) named _dm…, _flw… or SASTMP….
_SAS_INDB = re.compile(r"\b(?:FROM|JOIN|INTO|TABLE|UPDATE)\s+(?:`?[\w$]+`?\s*\.\s*)?`?(?:_dm|_flw|SASTMP)[\w$]*`?", re.I)
# SAS Embedded Process: the sasepFunction external (table) function, EP's own bookkeeping tables (EP.sasep…),
# and the per-job EP functions SAS Visual Analytics / CAS join to apply SAS formats
# ("… FROM (SELECT * FROM db.t) as t JOIN EP.h<32 hex>_EP(col, …) as ep").
_SAS_EP = re.compile(r"`?sasepFunction`?\s*\(|\b(?:FROM|JOIN|INTO|UPDATE)\s+`?EP`?\s*\.\s*`?sasep|"
                     r"\b(?:FROM|JOIN)\s+`?EP`?\s*\.\s*`?h[0-9A-F]{32}_EP`?\s*\(", re.I)
SAS_WORK_TABLE = re.compile(r"^(?:_dm|_flw|SASTMP)", re.I)
# Bump when origin() changes, so stored rows are labelled again (see _migrate). 1: VA / CAS EP format functions.
_ORIGIN_RULES = 1


_WORKLOAD = re.compile(r"\s*/\* workload:(.+?) stmt:[\w\-]+ \*/")


def origin(sql: str | None) -> str | None:
    """Probable origin of a statement from simple heuristics on its text."""
    if not sql:
        return None
    m = _WORKLOAD.match(sql)
    if m:
        return f"Workload: {m.group(1)}"
    if _SAS_EP.search(sql):
        return "SAS EP"
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


_RUN_COLUMNS = ("source", "event_key", "finished", "ms", "user", "database", "ok", "error_code", "category", "rows",
                "shape", "sample", "origin", "tables", "plan_id", "stmt")
_TABLES_CHARS = 4000       # tables are read from the first 4000 characters of each statement
_schema_ready: set[str] = set()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing store up to date in place (older versions had fewer columns / tables)."""
    have = _columns(conn, "runs")
    added = False
    for col, kind in (("tables", "TEXT"), ("plan_id", "INTEGER"), ("stmt", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {kind}")
            added = True
    if added or conn.execute("SELECT 1 FROM runs WHERE tables IS NULL LIMIT 1").fetchone():
        # Older rows only kept a short sample: fill in what can be read from it (best effort, once).
        rows = conn.execute("SELECT rowid, sample, database FROM runs WHERE tables IS NULL").fetchall()
        conn.executemany("UPDATE runs SET tables = ?, stmt = COALESCE(stmt, ?) WHERE rowid = ?",
                         [(json.dumps(table_names(r["sample"], r["database"])), statement_key(r["sample"], r["database"]),
                           r["rowid"]) for r in rows])
    conn.execute(
        "CREATE TABLE IF NOT EXISTS plans ("
        " source TEXT NOT NULL, plan_key TEXT NOT NULL, node_id INTEGER, plan_id INTEGER, database TEXT,"
        " first_load TEXT, last_executed TEXT, runs INTEGER, failed INTEGER, rows INTEGER, exec_ms REAL, avg_ms REAL,"
        " cpu_ms REAL, avg_mem REAL, avg_max_mem REAL, avg_spill REAL, avg_blob_miss REAL, blob_wait_ms REAL,"
        " queued_ms REAL, warnings TEXT, text TEXT, text_len INTEGER, stmt TEXT, tables TEXT, origin TEXT, seen TEXT,"
        " PRIMARY KEY (source, plan_key))")
    conn.execute("CREATE INDEX IF NOT EXISTS plans_last ON plans (source, last_executed)")
    conn.execute("CREATE INDEX IF NOT EXISTS plans_stmt ON plans (source, stmt)")
    # Cumulative counters over time, so "executions in the last N days" can be told from the plan cache.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS plan_samples (source TEXT NOT NULL, plan_key TEXT NOT NULL, t TEXT NOT NULL,"
        " runs INTEGER, exec_ms REAL, PRIMARY KEY (source, plan_key, t))")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_stmt ON runs (source, stmt)")
    # Stored origins follow the rules they were saved with: when the rules change, label the old rows again (once).
    if conn.execute("PRAGMA user_version").fetchone()[0] < _ORIGIN_RULES:
        conn.executemany("UPDATE runs SET origin = ? WHERE rowid = ?",
                         [(origin(r["sample"]), r["rowid"]) for r in conn.execute("SELECT rowid, sample FROM runs").fetchall()])
        conn.executemany("UPDATE plans SET origin = ? WHERE rowid = ?",
                         [(origin(r["text"]), r["rowid"]) for r in conn.execute("SELECT rowid, text FROM plans").fetchall()])
        conn.execute(f"PRAGMA user_version = {_ORIGIN_RULES}")
    conn.commit()


def connect() -> sqlite3.Connection:
    path = _path()
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        " source TEXT NOT NULL, event_key TEXT NOT NULL, finished TEXT NOT NULL, ms INTEGER NOT NULL,"
        " user TEXT, database TEXT, ok INTEGER NOT NULL, error_code TEXT, category TEXT, rows INTEGER,"
        " shape TEXT NOT NULL, sample TEXT, origin TEXT, tables TEXT, plan_id INTEGER, stmt TEXT,"
        " PRIMARY KEY (source, event_key))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS runs_time ON runs (source, finished)")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_shape ON runs (source, shape, finished)")
    with _lock:
        ready = path in _schema_ready
    if not ready:
        _migrate(conn)
        with _lock:
            _schema_ready.add(path)
    return conn


def sync(force: bool = False, min_ms: int = 1000) -> dict[str, Any]:
    """Copy new events from the cluster's query history. Cheap: only events after the newest stored one.

    Keeps every run of at least ``min_ms`` and every failed run (the trace keeps those whatever their duration
    when ``Trace_all_failed_executions`` is on)."""
    src = source_id()
    now = time.time()
    with _lock:
        if not force and now - _last_sync.get(src, 0) < _MIN_SYNC_GAP_S:
            return {"skipped": True, "source": src}
        _last_sync[src] = now
    conn = connect()
    try:
        newest = conn.execute("SELECT MAX(finished) FROM runs WHERE source = ?", (src,)).fetchone()[0]
        where = ["EVENT_TYPE = 'Query_completion'", "(DETAILS::%%duration_ms >= %s OR DETAILS::%%success = 0)",
                 "(DETAILS::$query_text IS NULL OR DETAILS::$query_text NOT LIKE %s)"]
        params: list[Any] = [int(min_ms), f"%{MARKER_PREFIX}%"]
        if newest:
            where.append("TIME >= %s - INTERVAL %s SECOND")
            params += [newest, _OVERLAP_S]
        rows = db.execute(
            f"{_MARKER} SELECT NODE_ID, NODE_START_EPOCH_S, EVENT_ID, TIME, DETAILS::%%duration_ms AS ms,"
            " DETAILS::$user_name AS user, DETAILS::$context_database AS db, DETAILS::%%success AS ok,"
            " DETAILS::$error_code AS error_code, DETAILS::$query_category AS category, DETAILS::%%plan_id AS plan_id,"
            f" DETAILS::%%row_count AS row_count, LEFT(DETAILS::$query_text, {_TABLES_CHARS}) AS head"
            f" FROM information_schema.MV_TRACE_EVENTS WHERE {' AND '.join(where)}",
            tuple(params),
        )[1]
        batch = []
        for r in rows:
            head = r["head"] or ""
            dbname = r["db"] or None
            batch.append((
                src, f"{r['NODE_ID']}:{r['NODE_START_EPOCH_S']}:{r['EVENT_ID']}",
                r["TIME"].isoformat(sep=" ") if hasattr(r["TIME"], "isoformat") else str(r["TIME"]),
                int(r["ms"] or 0), r["user"], dbname, 0 if r["ok"] in (0, False) else 1,
                r["error_code"] or None, r["category"], None if r["row_count"] is None else int(r["row_count"]),
                shape_of(head), " ".join(head.split())[:_SAMPLE_CHARS], origin(head),
                json.dumps(table_names(head, dbname)), None if r["plan_id"] in (None, -1) else int(r["plan_id"]),
                statement_key(head, dbname),
            ))
        before = conn.total_changes
        conn.executemany(f"INSERT OR IGNORE INTO runs ({', '.join(_RUN_COLUMNS)})"
                         f" VALUES ({', '.join('?' * len(_RUN_COLUMNS))})", batch)
        conn.commit()
        return {"source": src, "read": len(batch), "added": conn.total_changes - before}
    finally:
        conn.close()


# ------------------------------------------------------------------ the plan cache
# MV_PLANCACHE has one row per compiled plan and node, with totals since the plan was compiled: every execution,
# however short (the trace only keeps those over its threshold). Aggregator plans are the users' statements; leaf
# plans are the internal fan-out. Each refresh copies the aggregator plans into the store, so their totals survive
# plan eviction and restarts.

_PLAN_SNAPSHOT_GAP_S = 60       # copy the plan cache at most once a minute
_PLAN_SAMPLE_GAP_S = 600        # and keep its counters over time every 10 minutes
_PLAN_TEXT_CHARS = 4000
_last_plan_snapshot: dict[str, float] = {}
_last_plan_sample: dict[str, float] = {}
# Statements this server (or the driver) sends that the plan cache can't tell apart by a comment: it strips them.
_NOISE_PLAN = re.compile(
    r"^\s*(SELECT\s+(SQL_NO_LOGGING\s+)?([@^?]|\d+)\s*$|SELECT\s+(NOW\(\)|CONNECTION_ID\(\)|DATABASE\(\)|"
    r"TIMESTAMPDIFF\(MINUTE,\s*UTC_TIMESTAMP\(\),\s*NOW\(\)\))(\s+AS\s+`?\w+`?)?\s*$|"
    r"SELECT\s+COUNT\(\*\)\s+AS\s+n\s+FROM\s+\(.*\)\s+AS\s+_q\s*$|(SHOW|USE|SET|EXPLAIN|PROFILE|KILL)\b)",
    re.I | re.S)


def _text(v: Any) -> str:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    return "" if v is None else str(v)


def _ts(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat(sep=" ") if hasattr(v, "isoformat") else str(v)


def read_plancache() -> list[dict[str, Any]]:
    """The aggregators' plans for user statements, from MV_PLANCACHE (one round trip).

    This server's own statements all read information_schema, and the plan cache strips the comments that mark
    them elsewhere, so plans reading information_schema are left out (as are driver pings like SELECT @@…)."""
    rows = db.execute(
        f"{_MARKER} SELECT p.NODE_ID, p.PLAN_ID, p.DATABASE_NAME, LEFT(p.QUERY_TEXT, {_PLAN_TEXT_CHARS}) AS q,"
        " LENGTH(p.QUERY_TEXT) AS qlen, p.COMMITS, p.ROLLBACKS, p.ROWCOUNT, p.EXECUTION_TIME, p.AVERAGE_EXEC_TIME,"
        " p.CPU_TIME, p.AVERAGE_MEMORY_USE, p.AVERAGE_MAX_MEMORY_USE, p.AVERAGE_DISK_SPILLING_USE,"
        " p.AVERAGE_BLOB_CACHE_MISS_BYTES, p.BLOB_CACHE_WAIT_TIME_MS, p.QUEUED_TIME, p.FIRST_LOAD_TIME,"
        " p.LAST_EXECUTED, LEFT(p.PLAN_WARNINGS, 600) AS warnings"
        " FROM information_schema.MV_PLANCACHE p"
        " WHERE p.IS_PROFILE = 0 AND p.NODE_ID IN (SELECT ID FROM information_schema.MV_NODES WHERE TYPE IN ('MA', 'CA'))"
        " AND (p.DATABASE_NAME IS NULL OR p.DATABASE_NAME <> 'information_schema')"
        " AND p.QUERY_TEXT NOT LIKE %s AND p.QUERY_TEXT NOT LIKE %s AND p.QUERY_TEXT NOT LIKE %s",
        ("%information_schema%", "%@@%", "%binary_serialization_internal%"),
    )[1]
    out = []
    for r in rows:
        text = _text(r["q"])
        if not text.strip() or _NOISE_PLAN.match(text):
            continue
        if re.match(r"\s*SELECT\b", text, re.I) and not re.search(r"\bFROM\b", text, re.I):
            continue  # no table: pings, clock reads, SELECT ^ AS x
        dbname = r["DATABASE_NAME"] or None
        runs = int(r["COMMITS"] or 0) + int(r["ROLLBACKS"] or 0)
        first = _ts(r["FIRST_LOAD_TIME"])
        out.append({
            "plan_key": f"{r['NODE_ID']}:{r['PLAN_ID']}:{first}",
            "node_id": int(r["NODE_ID"]), "plan_id": int(r["PLAN_ID"]), "database": dbname,
            "first_load": first, "last_executed": _ts(r["LAST_EXECUTED"]),
            "runs": runs, "failed": int(r["ROLLBACKS"] or 0), "rows": int(r["ROWCOUNT"] or 0),
            "exec_ms": float(r["EXECUTION_TIME"] or 0), "avg_ms": float(r["AVERAGE_EXEC_TIME"] or 0),
            "cpu_ms": float(r["CPU_TIME"] or 0), "avg_mem": float(r["AVERAGE_MEMORY_USE"] or 0),
            "avg_max_mem": float(r["AVERAGE_MAX_MEMORY_USE"] or 0), "avg_spill": float(r["AVERAGE_DISK_SPILLING_USE"] or 0),
            "avg_blob_miss": float(r["AVERAGE_BLOB_CACHE_MISS_BYTES"] or 0),
            "blob_wait_ms": float(r["BLOB_CACHE_WAIT_TIME_MS"] or 0), "queued_ms": float(r["QUEUED_TIME"] or 0),
            "warnings": _text(r["warnings"]).strip() or None,
            "text": text, "text_len": int(r["qlen"] or 0),
            "stmt": statement_key(text, dbname, parameterized=True),
            "tables": table_names(text, dbname), "origin": origin(text),
        })
    return out


_PLAN_COLUMNS = ("node_id", "plan_id", "database", "first_load", "last_executed", "runs", "failed", "rows", "exec_ms",
                 "avg_ms", "cpu_ms", "avg_mem", "avg_max_mem", "avg_spill", "avg_blob_miss", "blob_wait_ms",
                 "queued_ms", "warnings", "text", "text_len", "stmt", "tables", "origin")


def save_plans(plans: list[dict[str, Any]], force: bool = False) -> dict[str, Any]:
    """Upsert the plan cache rows into the store (throttled), and every 10 minutes sample their counters."""
    src = source_id()
    now = time.time()
    with _lock:
        if not force and now - _last_plan_snapshot.get(src, 0) < _PLAN_SNAPSHOT_GAP_S:
            return {"skipped": True}
        _last_plan_snapshot[src] = now
        sample = force or now - _last_plan_sample.get(src, 0) >= _PLAN_SAMPLE_GAP_S
        if sample:
            _last_plan_sample[src] = now
    # Times in the store are cluster times: the newest LAST_EXECUTED stands in for the cluster's clock.
    seen = max((p["last_executed"] or "" for p in plans), default="") or time.strftime("%Y-%m-%d %H:%M:%S")
    conn = connect()
    try:
        cols = ("source", "plan_key", *_PLAN_COLUMNS, "seen")
        conn.executemany(
            f"INSERT INTO plans ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
            " ON CONFLICT (source, plan_key) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in (*_PLAN_COLUMNS, "seen") if c not in ("node_id", "plan_id", "first_load")),
            [(src, p["plan_key"], *[json.dumps(p[c]) if c == "tables" else p[c] for c in _PLAN_COLUMNS], seen)
             for p in plans])
        added = 0
        if sample:
            last = {r["plan_key"]: r["runs"] for r in conn.execute(
                "SELECT s.plan_key, s.runs FROM plan_samples s JOIN (SELECT plan_key, MAX(t) AS t FROM plan_samples"
                " WHERE source = ? GROUP BY plan_key) m ON m.plan_key = s.plan_key AND m.t = s.t WHERE s.source = ?",
                (src, src))}
            rows = [(src, p["plan_key"], seen, p["runs"], p["exec_ms"]) for p in plans if last.get(p["plan_key"]) != p["runs"]]
            conn.executemany("INSERT OR IGNORE INTO plan_samples VALUES (?,?,?,?,?)", rows)
            added = len(rows)
        conn.commit()
        return {"saved": len(plans), "samples": added}
    finally:
        conn.close()


def snapshot_plans(force: bool = False) -> list[dict[str, Any]]:
    """Read the plan cache and copy it into the store; returns the live plans."""
    plans = read_plancache()
    try:
        save_plans(plans, force=force)
    except Exception:  # noqa: BLE001 - the store is a bonus; the live plans are still returned
        pass
    return plans


def stored_plans(since: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
    """Plans kept in the store for the active cluster (also those the cluster has evicted since), newest first."""
    src = source_id()
    conn = connect()
    try:
        cond, params = ("AND last_executed >= ?", [since]) if since else ("", [])
        rows = conn.execute(f"SELECT * FROM plans WHERE source = ? {cond} ORDER BY last_executed DESC LIMIT ?",
                            (src, *params, max(1, int(limit)))).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("source", None)
        d["tables"] = json.loads(d["tables"] or "[]")
        out.append(d)
    return out


def stored_plan(plan_key: str) -> dict[str, Any] | None:
    src = source_id()
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM plans WHERE source = ? AND plan_key = ?", (src, plan_key)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    d = dict(r)
    d.pop("source", None)
    d["tables"] = json.loads(d["tables"] or "[]")
    return d


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


def bucket_runs(bucket: str, failed_only: bool = False, limit: int = 500) -> dict[str, Any]:
    """The stored runs of one Trends bar: a day ("YYYY-MM-DD") or an hour ("YYYY-MM-DD HH"), slowest first."""
    import re

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2})?", bucket or ""):
        raise ValueError("bucket must be YYYY-MM-DD or YYYY-MM-DD HH")
    src = source_id()
    conn = connect()
    try:
        cond = " AND ok = 0" if failed_only else ""
        total = conn.execute(f"SELECT COUNT(*) FROM runs WHERE source = ? AND finished LIKE ?{cond}",
                             (src, bucket + "%")).fetchone()[0]
        rows = conn.execute(
            "SELECT event_key, finished, ms, user, database, ok, error_code, category, rows, sample, origin, shape"
            f" FROM runs WHERE source = ? AND finished LIKE ?{cond} ORDER BY ms DESC LIMIT ?",
            (src, bucket + "%", max(1, min(2000, int(limit))))).fetchall()
    finally:
        conn.close()
    return {"bucket": bucket, "failed_only": failed_only, "total": total,
            "runs": [{"key": r["event_key"], "finished": r["finished"], "ms": r["ms"], "user": r["user"],
                      "database": r["database"], "ok": bool(r["ok"]), "error_code": r["error_code"],
                      "category": r["category"], "rows": r["rows"], "sql": r["sample"], "origin": r["origin"],
                      "shape": r["shape"]} for r in rows]}


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


# ------------------------------------------------------------------ usage: most used tables, most active users

_SYSTEM_DBS = {"information_schema", "memsql", "cluster", "sys", "mysql", "performance_schema"}


def _plan_executions(conn: sqlite3.Connection, src: str, since: str | None) -> list[dict[str, Any]]:
    """Per stored plan: executions and execution time within the period, from the sampled counters.

    A plan's counters are totals since it was compiled. Executions since ``since`` = now - the last sample taken
    before ``since``; a plan compiled after ``since`` counts in full. Without such a sample (the store didn't watch
    the plan then) the total is used and marked approximate."""
    cond, params = ("AND last_executed >= ?", [since]) if since else ("", [])
    plans = conn.execute(f"SELECT plan_key, database, first_load, last_executed, runs, failed, exec_ms, avg_ms, stmt,"
                         f" tables, origin, text FROM plans WHERE source = ? {cond}", (src, *params)).fetchall()
    base: dict[str, tuple[int, float]] = {}
    if since:
        for r in conn.execute(
                "SELECT s.plan_key, s.runs, s.exec_ms FROM plan_samples s JOIN (SELECT plan_key, MAX(t) AS t"
                " FROM plan_samples WHERE source = ? AND t <= ? GROUP BY plan_key) m ON m.plan_key = s.plan_key AND"
                " m.t = s.t WHERE s.source = ?", (src, since, src)):
            base[r["plan_key"]] = (r["runs"] or 0, r["exec_ms"] or 0.0)
    out = []
    for p in plans:
        runs, ms, approx = p["runs"] or 0, p["exec_ms"] or 0.0, False
        if since and p["plan_key"] in base and base[p["plan_key"]][0] <= runs:
            runs, ms = runs - base[p["plan_key"]][0], ms - base[p["plan_key"]][1]
        elif since and (p["first_load"] or "") < since:
            approx = True
        if runs <= 0:
            continue
        out.append({**dict(p), "execs": runs, "exec_ms_period": max(0.0, ms), "approx": approx,
                    "tables": json.loads(p["tables"] or "[]")})
    return out


def usage(since: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Most used tables and most active users from the store (``since``: cluster time "YYYY-MM-DD HH:MM:SS").

    Traced runs (individual statements over the trace threshold, plus failures) carry user, duration and tables;
    the plan cache adds every execution of every statement, however short, but has no user."""
    src = source_id()
    conn = connect()
    try:
        cond, params = ("AND finished >= ?", [since]) if since else ("", [])
        runs = conn.execute(f"SELECT finished, ms, user, database, ok, tables, stmt, origin FROM runs"
                            f" WHERE source = ? {cond}", (src, *params)).fetchall()
        plans = _plan_executions(conn, src, since)
        info = conn.execute("SELECT MIN(finished) AS oldest, COUNT(*) AS n FROM runs WHERE source = ?", (src,)).fetchone()
        pinfo = conn.execute("SELECT MIN(seen) AS first_seen, COUNT(*) AS n FROM plans WHERE source = ?", (src,)).fetchone()
    finally:
        conn.close()

    def blank(dbname: str | None, table: str) -> dict[str, Any]:
        return {"database": dbname, "table": table, "runs": 0, "ms": 0, "failed": 0, "last": None, "users": set(),
                "stmts": set(), "execs": 0, "exec_ms": 0.0, "approx": False, "plan_last": None, "origins": set()}

    tables: dict[str, dict[str, Any]] = {}
    users: dict[str, dict[str, Any]] = {}

    def table_entry(name: str) -> dict[str, Any] | None:
        dbname, _, table = name.rpartition(".")
        if (dbname or "").lower() in _SYSTEM_DBS:
            return None
        return tables.setdefault(name, blank(dbname or None, table))

    for r in runs:
        for name in json.loads(r["tables"] or "[]"):
            t = table_entry(name)
            if t is None:
                continue
            t["runs"] += 1
            t["ms"] += r["ms"]
            t["failed"] += 0 if r["ok"] else 1
            t["last"] = max(t["last"] or "", r["finished"])
            if r["user"]:
                t["users"].add(r["user"])
            if r["stmt"]:
                t["stmts"].add(r["stmt"])
            if r["origin"]:
                t["origins"].add(r["origin"])
        u = users.setdefault(r["user"] or "(unknown)", {"user": r["user"] or "(unknown)", "queries": 0, "ms": 0, "failed": 0,
                                                       "databases": set(), "last": None, "stmts": set(), "max_ms": 0,
                                                       "origins": {}})
        u["queries"] += 1
        u["ms"] += r["ms"]
        u["max_ms"] = max(u["max_ms"], r["ms"])
        u["failed"] += 0 if r["ok"] else 1
        if r["database"]:
            u["databases"].add(r["database"])
        u["last"] = max(u["last"] or "", r["finished"])
        if r["stmt"]:
            u["stmts"].add(r["stmt"])
        if r["origin"]:
            u["origins"][r["origin"]] = u["origins"].get(r["origin"], 0) + 1

    for p in plans:
        for name in p["tables"]:
            t = table_entry(name)
            if t is None:
                continue
            t["execs"] += p["execs"]
            t["exec_ms"] += p["exec_ms_period"]
            t["approx"] |= p["approx"]
            t["plan_last"] = max(t["plan_last"] or "", p["last_executed"] or "")
            if p["stmt"]:
                t["stmts"].add(p["stmt"])
            if p["origin"]:
                t["origins"].add(p["origin"])

    table_list = []
    for t in tables.values():
        last = max(t["last"] or "", t["plan_last"] or "") or None
        table_list.append({
            "database": t["database"], "table": t["table"], "runs": t["runs"], "ms": t["ms"], "failed": t["failed"],
            "avg_ms": round(t["ms"] / t["runs"]) if t["runs"] else None, "last": last,
            "users": sorted(t["users"]), "statements": len(t["stmts"]), "execs": t["execs"],
            "exec_ms": round(t["exec_ms"]), "avg_exec_ms": round(t["exec_ms"] / t["execs"], 1) if t["execs"] else None,
            "approx": t["approx"], "origins": sorted(t["origins"]),
        })
    # Most used: total time (plan cache when known, else traced), then executions.
    table_list.sort(key=lambda t: (-max(t["exec_ms"], t["ms"]), -(t["execs"] or t["runs"])))
    user_list = [{
        "user": u["user"], "queries": u["queries"], "ms": u["ms"], "avg_ms": round(u["ms"] / u["queries"]) if u["queries"] else 0,
        "max_ms": u["max_ms"], "failed": u["failed"], "databases": sorted(u["databases"]), "last": u["last"],
        "statements": len(u["stmts"]), "origins": dict(sorted(u["origins"].items(), key=lambda kv: -kv[1])),
    } for u in users.values()]
    user_list.sort(key=lambda u: (-u["ms"], -u["queries"]))
    return {"source": src, "since": since, "tables": table_list[:limit], "table_count": len(table_list),
            "users": user_list[:limit], "user_count": len(user_list),
            "traced_runs": len(runs), "oldest": info["oldest"], "stored": info["n"],
            "plans": len(plans), "plans_stored": pinfo["n"], "plans_since": pinfo["first_seen"],
            "plan_execs": sum(p["execs"] for p in plans), "approx": any(p["approx"] for p in plans)}
