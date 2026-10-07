"""Before/after check for a rebuilt table: run the workload's heaviest read-only queries on both.

For an Advisor recommendation that builds a copy of a table with other keys (``cars_big_240_sorted``),
``candidates`` picks the heaviest SELECT / WITH query shapes from the query history that read the table,
and ``start`` runs each against the original and against the copy (table name replaced), alternating
old / new, N runs each. Each query is wrapped as ``SELECT COUNT(*) AS n FROM (<query>) AS _q`` so no rows
stream to the client; the row counts double as a check that both tables give the same answer.

Runs happen on their own connection, so a per-query timeout can cancel just that statement:
``KILL QUERY`` on its connection id, sent from another connection after checking (on that aggregator's
own PROCESSLIST) that the id still runs this job's statement.
"""

from __future__ import annotations

import re
import statistics
import threading
import time
import uuid
from typing import Any

import singlestoredb as s2

from . import history_store, query_advisor
from .db import db, quote_identifier

MARKER = "/* s2-compare */"
_MAX_JOBS = 10
_READ_ONLY = re.compile(r"^(SELECT|WITH)\b", re.I)
# Statements that look like reads but write (SELECT … INTO …, locking reads).
_WRITES = re.compile(r"\bINTO\s+(OUTFILE|DUMPFILE|KAFKA|S3|GCS|AZURE|FS|HDFS|@|`?\w+`?\s*(,|$))|\bFOR\s+UPDATE\b|"
                     r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE|CALL|LOAD|GRANT|REVOKE|SET|KILL)\b\s",
                     re.I)

_candidates: dict[str, dict[str, Any]] = {}   # "db.table->new" -> {shape: query}
_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _split(name: str, default_db: str | None) -> tuple[str | None, str]:
    name = name.replace("`", "")
    dbname, _, table = name.rpartition(".")
    return (dbname or default_db), table


def read_only(sql: str) -> bool:
    body = query_advisor.strip(sql)
    if not _READ_ONLY.match(body):
        return False
    # Ignore string literals when looking for write keywords.
    bare = re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", body)
    return not _WRITES.search(bare)


# --------------------------------------------------------------- replacing the table name

_TOKEN = re.compile(r"""('(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.|"")*"|/\*.*?\*/|--[^\n]*|#[^\n]*|`(?:[^`]|``)+`|[A-Za-z_$][\w$]*|\s+|.)""", re.S)


def _ident(tok: str) -> str | None:
    if tok.startswith("`") and tok.endswith("`") and len(tok) >= 2:
        return tok[1:-1].replace("``", "`")
    if re.match(r"[A-Za-z_$][\w$]*$", tok):
        return tok
    return None


def replace_table(sql: str, dbname: str | None, table: str, new_table: str, context_db: str | None) -> tuple[str, int]:
    """Replace references to ``dbname.table`` with ``new_table`` (same database). Returns (sql, replacements).

    Handles ``db.table``, `` `db`.`table` ``, mixed quoting and the bare name (when the query's context
    database is the table's database). String literals and comments are left alone; ``x.table`` with
    another qualifier (a column called like the table) and ``table(`` (a function) are not touched.
    """
    toks = _TOKEN.findall(sql)
    out: list[str] = []
    n = 0
    sig = [k for k, t in enumerate(toks) if not t.isspace()]  # indexes of non-space tokens

    def prev_sig(k: int) -> int | None:
        j = k - 1
        while j >= 0 and toks[j].isspace():
            j -= 1
        return j if j >= 0 else None

    def next_sig(k: int) -> int | None:
        j = k + 1
        while j < len(toks) and toks[j].isspace():
            j += 1
        return j if j < len(toks) else None

    replace_at: dict[int, str] = {}
    q_new = quote_identifier(new_table)
    for k in sig:
        if _ident(toks[k]) != table:
            continue
        p = prev_sig(k)
        nx = next_sig(k)
        if nx is not None and toks[nx] == "(":
            continue  # a function call
        if p is not None and toks[p] == ".":
            q = prev_sig(p)
            qual = _ident(toks[q]) if q is not None else None
            if qual is not None and dbname is not None and qual == dbname:
                # db.table: the qualifier stays (same database)
                replace_at[k] = q_new
            continue
        if nx is not None and toks[nx] == ".":
            # table.column: a qualifier; replaced with the bare new name like the FROM clause
            replace_at[k] = q_new
            continue
        if dbname is None or context_db is None or context_db == dbname:
            replace_at[k] = q_new
    for k, t in enumerate(toks):
        if k in replace_at:
            out.append(replace_at[k])
            n += 1
        else:
            out.append(t)
    return "".join(out), n


def _references(sql: str, dbname: str, table: str, context_db: str | None) -> bool:
    if table.lower() not in sql.lower():
        return False
    return replace_table(sql, dbname, table, table + "__probe", context_db)[1] > 0


# --------------------------------------------------------------- candidates

def _table_exists(dbname: str, table: str) -> bool:
    rows = db.execute(f"{MARKER} SELECT 1 AS x FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                      (dbname, table))[1]
    return bool(rows)


def candidates(table: str, new_table: str, limit: int = 8) -> dict[str, Any]:
    """The heaviest read-only query shapes in the history that read ``table`` (db.table)."""
    dbname, name = _split(table, None)
    if not dbname:
        raise ValueError("Give the table as database.table.")
    new_db, new_name = _split(new_table, dbname)
    if new_db != dbname:
        raise ValueError("The new table must be in the same database as the original.")
    if new_name == name:
        raise ValueError("The new table must have another name than the original.")
    old_ok, new_ok = db.parallel(lambda: _table_exists(dbname, name), lambda: _table_exists(dbname, new_name))
    if not old_ok:
        raise LookupError(f"Table {dbname}.{name} doesn't exist (names are case-sensitive).")
    if not new_ok:
        return {"table": f"{dbname}.{name}", "new_table": f"{dbname}.{new_name}", "exists": False, "queries": [],
                "message": f"{dbname}.{new_name} doesn't exist yet. Build it first with the recommendation's statements "
                           "(CREATE TABLE … and INSERT … SELECT), then compare."}
    rows = db.execute(
        f"{MARKER} SELECT DETAILS::$query_text AS q, DETAILS::$context_database AS db, DETAILS::%%duration_ms AS ms, TIME"
        " FROM information_schema.MV_TRACE_EVENTS WHERE EVENT_TYPE = 'Query_completion' AND DETAILS::%%success = 1"
        " AND DETAILS::$query_text LIKE %s AND DETAILS::$query_text NOT LIKE %s ORDER BY TIME DESC LIMIT 5000",
        (f"%{name}%", f"%{history_store.MARKER_PREFIX}%"),
    )[1]
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        sql = query_advisor.strip(r["q"] or "").rstrip().rstrip(";").rstrip()
        if not sql or len(sql) > 100_000 or not read_only(sql):
            continue
        ctx = r["db"] or None
        if not _references(sql, dbname, name, ctx):
            continue
        shape = history_store.shape_of(sql) + (f"@{ctx}" if ctx else "")
        g = groups.setdefault(shape, {"shape": shape, "sql": sql, "database": ctx, "runs": 0, "total_ms": 0, "ms": []})
        g["runs"] += 1
        g["total_ms"] += int(r["ms"] or 0)
        g["ms"].append(int(r["ms"] or 0))
    ranked = sorted(groups.values(), key=lambda g: -g["total_ms"])[: max(1, min(25, int(limit)))]
    out = []
    for g in ranked:
        rewritten, n = replace_table(g["sql"], dbname, name, new_name, g["database"])
        out.append({"shape": g["shape"], "sql": g["sql"], "new_sql": rewritten, "replacements": n, "database": g["database"],
                    "runs": g["runs"], "total_ms": g["total_ms"], "median_ms": int(statistics.median(g["ms"]))})
    with _lock:
        _candidates[f"{dbname}.{name}->{new_name}"] = {q["shape"]: q for q in out}
        while len(_candidates) > 20:
            _candidates.pop(next(iter(_candidates)))
    return {"table": f"{dbname}.{name}", "new_table": f"{dbname}.{new_name}", "exists": True, "queries": out,
            "message": None if out else f"No successful SELECT / WITH queries on {dbname}.{name} in the query history."}


# --------------------------------------------------------------- running

def wrap(sql: str, job: str) -> str:
    return f"{MARKER[:-3]} job={job} */ SELECT COUNT(*) AS n FROM (\n{sql}\n) AS _q"


def _connect(database: str | None) -> Any:
    from . import connections

    profile = connections.active()
    conn = s2.connect(**connections.connect_kwargs(profile), autocommit=True)
    if database:
        cur = conn.cursor()
        cur.execute(f"USE {quote_identifier(database)}")
        cur.close()
    return conn


def _kill(conn_id: int, job: str) -> bool:
    """Cancel the statement on ``conn_id`` if it is still this job's (checked on that aggregator's PROCESSLIST)."""
    from . import connections

    killer = s2.connect(**connections.connect_kwargs(connections.active()), autocommit=True)
    try:
        cur = killer.cursor()
        cur.execute(f"{MARKER} SELECT ID FROM information_schema.PROCESSLIST WHERE ID = %s AND INFO LIKE %s",
                    (int(conn_id), f"%job={job}%"))
        if not cur.fetchall():
            return False
        cur.execute(f"KILL QUERY {int(conn_id)}")
        return True
    finally:
        killer.close()


def _timed(conn: Any, sql: str, timeout_s: float, job: str, conn_id: int, rtt: float) -> dict[str, Any]:
    killed = {"done": False}

    def on_timeout() -> None:
        try:
            killed["done"] = _kill(conn_id, job)
        except Exception:  # noqa: BLE001 - reported as a timeout either way
            killed["done"] = True

    timer = threading.Timer(timeout_s, on_timeout)
    cur = conn.cursor()
    started = time.perf_counter()
    timer.start()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        elapsed = time.perf_counter() - started
        n = rows[0][0] if rows and not isinstance(rows[0], dict) else (rows[0]["n"] if rows else None)
        return {"ms": max(0, round((elapsed - rtt) * 1000)), "wall_ms": round(elapsed * 1000), "rows": int(n) if n is not None else None}
    except Exception as exc:  # noqa: BLE001
        if killed["done"] or time.perf_counter() - started >= timeout_s:
            return {"error": f"timed out after {timeout_s:g} s (cancelled)", "timeout": True}
        return {"error": str(exc)[:300]}
    finally:
        timer.cancel()
        cur.close()


def _rtt(conn: Any) -> float:
    cur = conn.cursor()
    times = []
    for _ in range(3):
        t = time.perf_counter()
        cur.execute(f"{MARKER} SELECT 1")
        cur.fetchall()
        times.append(time.perf_counter() - t)
    cur.close()
    return statistics.median(times)


def _run_job(job: dict[str, Any]) -> None:
    jid = job["id"]
    conns: dict[str | None, tuple[Any, int, float]] = {}
    try:
        for q in job["queries"]:
            if job["cancel"]:
                break
            q["status"] = "running"
            dbname = q["database"]
            if dbname not in conns:
                conn = _connect(dbname)
                cur = conn.cursor()
                cur.execute("SELECT CONNECTION_ID()")
                conn_id = int(_first(cur))
                cur.close()
                conns[dbname] = (conn, conn_id, _rtt(conn))
            conn, conn_id, rtt = conns[dbname]
            job["conn_id"] = conn_id
            q["rtt_ms"] = round(rtt * 1000)
            old_sql, new_sql = wrap(q["sql"], jid), wrap(q["new_sql"], jid)
            for i in range(job["runs"]):
                # Alternate the order each round so caching / warm-up favours neither table.
                order = (("old", old_sql), ("new", new_sql)) if i % 2 == 0 else (("new", new_sql), ("old", old_sql))
                for side, sql in order:
                    if job["cancel"]:
                        break
                    q["current"] = f"{side} run {i + 1}"
                    r = _timed(conn, sql, job["timeout_s"], jid, conn_id, rtt)
                    q[side].append(r)
                    job["done_runs"] += 1
                    if r.get("timeout"):
                        q["errors"].append(f"{side}: {r['error']}")
                    elif r.get("error"):
                        q["errors"].append(f"{side}: {r['error']}")
                if q["errors"]:
                    break  # a failing (or timed-out) query isn't run again
            q["current"] = None
            _summarize(q)
            q["status"] = "cancelled" if job["cancel"] else "done"
    except Exception as exc:  # noqa: BLE001 - reported in the job
        job["error"] = str(exc)[:400]
    finally:
        for conn, _, _ in conns.values():
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        for q in job["queries"]:
            if q["status"] in ("pending", "running"):
                q["status"] = "cancelled" if job["cancel"] else q["status"]
                _summarize(q)
        job["finished"] = time.time()
        job["conn_id"] = None
        job["state"] = "cancelled" if job["cancel"] else "error" if job.get("error") else "done"


def _first(cur: Any) -> Any:
    row = cur.fetchall()[0]
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _summarize(q: dict[str, Any]) -> None:
    old = [r["ms"] for r in q["old"] if "ms" in r]
    new = [r["ms"] for r in q["new"] if "ms" in r]
    q["old_median_ms"] = int(statistics.median(old)) if old else None
    q["new_median_ms"] = int(statistics.median(new)) if new else None
    q["speedup"] = round(q["old_median_ms"] / q["new_median_ms"], 2) if old and new and q["new_median_ms"] > 0 else None
    old_rows = {r["rows"] for r in q["old"] if "rows" in r}
    new_rows = {r["rows"] for r in q["new"] if "rows" in r}
    q["old_rows"] = next(iter(old_rows)) if len(old_rows) == 1 else sorted(old_rows) or None
    q["new_rows"] = next(iter(new_rows)) if len(new_rows) == 1 else sorted(new_rows) or None
    q["rows_match"] = (old_rows == new_rows) if old_rows and new_rows else None


def start(table: str, new_table: str, shapes: list[str], runs: int = 3, timeout_s: float = 120) -> dict[str, Any]:
    dbname, name = _split(table, None)
    _, new_name = _split(new_table, dbname)
    with _lock:
        known = _candidates.get(f"{dbname}.{name}->{new_name}")
        running = [j for j in _jobs.values() if j["state"] == "running"]
    if running:
        raise ValueError("A comparison is already running; wait for it or stop it.")
    if not known:
        raise LookupError("Load the candidate queries first (they expire when the server restarts).")
    picked = [known[s] for s in shapes if s in known]
    if not picked:
        raise ValueError("Select at least one query.")
    if not _table_exists(dbname, new_name):
        raise LookupError(f"{dbname}.{new_name} doesn't exist.")
    for q in picked:
        if not read_only(q["sql"]) or not read_only(q["new_sql"]):
            raise ValueError("Only SELECT / WITH queries can be compared.")
    runs = max(1, min(10, int(runs)))
    timeout_s = max(5.0, min(1800.0, float(timeout_s)))
    jid = uuid.uuid4().hex[:10]
    job = {
        "id": jid, "state": "running", "table": f"{dbname}.{name}", "new_table": f"{dbname}.{new_name}",
        "runs": runs, "timeout_s": timeout_s, "started": time.time(), "finished": None, "cancel": False, "error": None,
        "total_runs": 2 * runs * len(picked), "done_runs": 0,
        "queries": [{"shape": q["shape"], "sql": q["sql"], "new_sql": q["new_sql"], "database": q["database"],
                     "status": "pending", "old": [], "new": [], "errors": [], "current": None} for q in picked],
    }
    with _lock:
        _jobs[jid] = job
        while len(_jobs) > _MAX_JOBS:
            _jobs.pop(next(iter(_jobs)))
    threading.Thread(target=_run_job, args=(job,), name=f"s2-compare-{jid}", daemon=True).start()
    return status(jid)


def status(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise LookupError("That comparison is no longer known (the server restarted?).")
    return {k: v for k, v in job.items() if k not in ("cancel", "conn_id")} | {"cancelling": job["cancel"] and job["state"] == "running",
                                                             "elapsed_s": round((job["finished"] or time.time()) - job["started"], 1)}


def cancel(job_id: str) -> dict[str, Any]:
    """Stop after the statement that's running now (that one is cancelled too)."""
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise LookupError("That comparison is no longer known.")
    if not job["cancel"] and job["state"] == "running":
        job["cancel"] = True
        conn_id = job.get("conn_id")
        if conn_id:
            # Cancel the statement running now too (only if it's still this job's).
            threading.Thread(target=lambda: _safe_kill(conn_id, job_id), daemon=True).start()
    return status(job_id)


def _safe_kill(conn_id: int, job_id: str) -> None:
    try:
        _kill(conn_id, job_id)
    except Exception:  # noqa: BLE001 - the job ends after this statement anyway
        pass
