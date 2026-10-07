"""Clean-up of leftover work tables (SAS in-database / Data Management / flow temp tables).

SAS jobs leave tables such as ``_dmallchars``, ``_dmCorrInTbl`` or
``_flw000d7eb2ea03ef511f1_0_0_1`` behind in SingleStore. ``scan`` finds them
across every non-system database by name pattern (and, optionally, every table
older than N days) in two rounds of parallel queries, not one query per table:

1. ``information_schema.TABLES`` (all base tables) and ``VIEWS`` (definitions,
   to find views that depend on a candidate);
2. for the candidates only: ``TABLE_STATISTICS`` (rows and memory of the
   master partitions, one copy for reference tables), ``COLUMNAR_SEGMENTS``
   (compressed on-disk size) and the query history
   (``MV_TRACE_EVENTS``, ``DETAILS::$query_text``) for "last used".

``drop`` re-checks everything on the server and drops one object per
statement (``DROP TABLE IF EXISTS `db`.`t```), never a multi-table statement.
Dependent views block a table unless they are explicitly included (they are
then dropped first, one ``DROP VIEW IF EXISTS`` each). Every real drop is
logged to ``~/.singlestore-mcp/cleanup-log.jsonl``.

Settings (patterns, age filter, "recently used" window, dry-run) live in
``~/.singlestore-mcp/cleanup.json``.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import getpass
import json
import re
import threading
from typing import Any

from .db import db, quote_identifier
from .paths import data_dir

SYSTEM_DATABASES = frozenset({"information_schema", "memsql", "cluster", "sys", "mysql", "performance_schema"})

DEFAULT_PATTERNS: list[dict[str, str]] = [
    {"pattern": "_dm*", "kind": "SAS DM"},
    {"pattern": "_flw*", "kind": "SAS flow"},
    {"pattern": "SASTMP*", "kind": "temp"},
    {"pattern": "_tmp*", "kind": "temp"},
]
DEFAULT_SETTINGS: dict[str, Any] = {
    "patterns": DEFAULT_PATTERNS,
    "older_than_days": None,   # also offer every table not created/altered for this many days
    "recent_days": 7,          # used or created this recently = risky
    "dry_run": True,           # on until the user turns it off
}
MAX_CANDIDATES = 2000
_LIKE_FILTER_MAX = 300         # above this many candidates, fetch the history and match in Python
_HISTORY_ROWS = 20000
_SQL_TEXT_CHARS = 8000
# Tags this module's own statements, so its LIKE '%name%' search never counts as "use" of a table.
MARKER = "/* s2-cleanup */"

_lock = threading.Lock()


# ------------------------------------------------------------------ settings and log


def _settings_file():
    return data_dir() / "cleanup.json"


def _log_file():
    return data_dir() / "cleanup-log.jsonl"


def _clean_patterns(patterns: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for p in patterns or []:
        if isinstance(p, str):
            p = {"pattern": p}
        if not isinstance(p, dict):
            continue
        pat = str(p.get("pattern") or "").strip()
        if not pat or pat in ("*", "%") or pat in seen:
            continue  # a bare "*" would offer every table
        seen.add(pat)
        out.append({"pattern": pat, "kind": str(p.get("kind") or "").strip() or "work table"})
    return out


def load_settings() -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        data = json.loads(_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    s = {**DEFAULT_SETTINGS, **{k: v for k, v in data.items() if k in DEFAULT_SETTINGS}}
    s["patterns"] = _clean_patterns(s["patterns"]) if "patterns" in data else [dict(p) for p in DEFAULT_PATTERNS]
    s["older_than_days"] = _pos_int(s["older_than_days"])
    s["recent_days"] = _pos_int(s["recent_days"]) or DEFAULT_SETTINGS["recent_days"]
    s["dry_run"] = bool(s["dry_run"])
    return s


def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        s = load_settings()
        for k in DEFAULT_SETTINGS:
            if k in changes:
                s[k] = changes[k]
        s["patterns"] = _clean_patterns(s["patterns"])
        s["older_than_days"] = _pos_int(s["older_than_days"])
        s["recent_days"] = _pos_int(s["recent_days"]) or DEFAULT_SETTINGS["recent_days"]
        s["dry_run"] = bool(s["dry_run"])
        _settings_file().write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


def _pos_int(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def read_log(limit: int = 50) -> list[dict[str, Any]]:
    try:
        lines = _log_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def _append_log(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    with _lock, _log_file().open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")


# ------------------------------------------------------------------ helpers


def _q(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    # With params, a literal % in the SQL must be written %%; without params it stays %.
    return db.execute(f"{MARKER} {sql}", params or None)[1]


def _num(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, _dt.datetime) else str(v)


def _dt_of(v: Any) -> _dt.datetime | None:
    if isinstance(v, _dt.datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return _dt.datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def match_kind(name: str, patterns: list[dict[str, str]]) -> dict[str, str] | None:
    """The first pattern (shell-style, case-insensitive) that matches a table name."""
    low = name.lower()
    for p in patterns:
        if fnmatch.fnmatchcase(low, p["pattern"].lower()):
            return p
    return None


def _name_re(name: str) -> re.Pattern[str]:
    # The name as a whole identifier: _dmCorrInTbl must not match _dmCorrInTbl2.
    return re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", re.IGNORECASE)


def _like(name: str) -> str:
    return "%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _in(values: list[str]) -> str:
    return ", ".join(["%s"] * len(values))


def drop_statement(database: str, name: str, view: bool = False) -> str:
    return f"DROP {'VIEW' if view else 'TABLE'} IF EXISTS {quote_identifier(database)}.{quote_identifier(name)}"


# ------------------------------------------------------------------ scan


def _all_tables() -> list[dict[str, Any]]:
    sys_list = sorted(SYSTEM_DATABASES)
    return _q(
        "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, STORAGE_TYPE, DISTRIBUTED, CREATE_TIME, ALTER_TIME,"
        " UPDATE_TIME, CREATE_USER, TABLE_COMMENT FROM information_schema.TABLES"
        f" WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA NOT IN ({_in(sys_list)})",
        tuple(sys_list),
    )


def _all_views() -> list[dict[str, Any]]:
    sys_list = sorted(SYSTEM_DATABASES)
    return _q(
        "SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS"
        f" WHERE TABLE_SCHEMA NOT IN ({_in(sys_list)})",
        tuple(sys_list),
    )


def _stats(dbs: list[str], names: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    rows = _q(
        "SELECT DATABASE_NAME, TABLE_NAME,"
        " SUM(CASE WHEN PARTITION_TYPE = 'Master' THEN ROWS END) AS master_rows,"
        " MAX(CASE WHEN PARTITION_TYPE = 'Reference' THEN ROWS END) AS ref_rows,"
        " SUM(CASE WHEN PARTITION_TYPE = 'Master' THEN MEMORY_USE END) AS master_mem,"
        " MAX(CASE WHEN PARTITION_TYPE = 'Reference' THEN MEMORY_USE END) AS ref_mem"
        f" FROM information_schema.TABLE_STATISTICS WHERE DATABASE_NAME IN ({_in(dbs)}) AND TABLE_NAME IN ({_in(names)})"
        " GROUP BY DATABASE_NAME, TABLE_NAME",
        (*dbs, *names),
    )
    return {(r["DATABASE_NAME"], r["TABLE_NAME"]): r for r in rows}


def _disk(dbs: list[str], names: list[str]) -> dict[tuple[str, str], int]:
    rows = _q(
        "SELECT DATABASE_NAME, TABLE_NAME, SUM(COMPRESSED_SIZE) AS disk FROM information_schema.COLUMNAR_SEGMENTS"
        f" WHERE DATABASE_NAME IN ({_in(dbs)}) AND TABLE_NAME IN ({_in(names)}) GROUP BY DATABASE_NAME, TABLE_NAME",
        (*dbs, *names),
    )
    return {(r["DATABASE_NAME"], r["TABLE_NAME"]): _num(r["disk"]) or 0 for r in rows}


def _history(names: list[str]) -> dict[str, Any]:
    """Query-history rows (time, user, database, text) that may mention one of ``names``."""
    base = (
        "SELECT TIME, DETAILS::$user_name AS user, DETAILS::$context_database AS db,"
        f" LEFT(DETAILS::$query_text, {_SQL_TEXT_CHARS}) AS q FROM information_schema.MV_TRACE_EVENTS"
        " WHERE EVENT_TYPE = 'Query_completion' AND DETAILS::$query_text IS NOT NULL"
        " AND DETAILS::$query_text NOT LIKE %s"
    )
    params: list[Any] = ["%" + MARKER + "%"]
    if len(names) <= _LIKE_FILTER_MAX:
        uniq = sorted({n.lower() for n in names})
        base += " AND (" + " OR ".join(["DETAILS::$query_text LIKE %s"] * len(uniq)) + ")"
        params += [_like(n) for n in uniq]
    rows, info = db.parallel(
        lambda: _q(base + " ORDER BY TIME DESC LIMIT %s", (*params, _HISTORY_ROWS)),
        lambda: _q("SELECT COUNT(*) AS n, MIN(TIME) AS oldest FROM information_schema.MV_TRACE_EVENTS"
                   " WHERE EVENT_TYPE = 'Query_completion'"),
    )
    total = info[0] if info else {}
    return {"rows": rows, "events": int(total.get("n") or 0), "oldest": _iso(total.get("oldest"))}


def _dependent_views(cands: list[dict[str, Any]], views: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, str]]]:
    out: dict[tuple[str, str], list[dict[str, str]]] = {}
    for c in cands:
        rx = _name_re(c["name"])
        for v in views:
            text = v["VIEW_DEFINITION"] or ""
            if not rx.search(text):
                continue
            # A view in another database only counts if its definition names this table's database.
            if v["TABLE_SCHEMA"] != c["database"] and not _name_re(c["database"]).search(text):
                continue
            out.setdefault((c["database"], c["name"]), []).append(
                {"database": v["TABLE_SCHEMA"], "name": v["TABLE_NAME"]})
    return out


def _now() -> _dt.datetime:
    rows = _q("SELECT NOW() AS now")
    return _dt_of(rows[0]["now"]) if rows else _dt.datetime.now()


def _enrich(cands: list[dict[str, Any]], views: list[dict[str, Any]], now: _dt.datetime | None,
            recent_days: int, with_history: bool = True) -> dict[str, Any]:
    """Rows, size, dependent views and last use for the candidates (one round of parallel queries)."""
    if not cands:
        return {"history": None}
    dbs = sorted({c["database"] for c in cands})
    names = sorted({c["name"] for c in cands})
    calls = [lambda: _stats(dbs, names), lambda: _disk(dbs, names)]
    if with_history:
        calls.append(lambda: _history(names))
    if now is None:
        calls.append(_now)
    results = db.parallel(*calls)
    stats, disk = results[0], results[1]
    hist = results[2] if with_history else None
    if now is None:
        now = results[-1]
    deps = _dependent_views(cands, views)
    recent = _dt.timedelta(days=recent_days)
    compiled = [(c, _name_re(c["name"])) for c in cands] if hist else []
    last_use: dict[tuple[str, str], dict[str, Any]] = {}
    for r in (hist or {}).get("rows", []):
        text = r["q"] or ""
        for c, rx in compiled:
            k = (c["database"], c["name"])
            if k in last_use or not rx.search(text):
                continue
            # A bare name only refers to this database's table when run in it; db.name refers anywhere.
            qualified = re.search(re.escape(c["database"]) + r"[`\"]?\s*\.\s*[`\"]?" + re.escape(c["name"]), text, re.I)
            if not qualified and (r["db"] or "") != c["database"]:
                continue
            last_use[k] = {"time": _iso(r["TIME"]), "user": r["user"], "sql": " ".join(text[:300].split())}
    for c in cands:
        k = (c["database"], c["name"])
        s = stats.get(k) or {}
        rows = _num(s.get("master_rows"))
        if rows is None:
            rows = _num(s.get("ref_rows"))
        mem = _num(s.get("master_mem"))
        if mem is None:
            mem = _num(s.get("ref_mem"))
        d = disk.get(k)
        c["rows"] = rows
        c["memory_bytes"] = mem
        c["disk_bytes"] = d
        c["size_bytes"] = (mem or 0) + (d or 0)
        c["views"] = deps.get(k, [])
        c["last_used"] = last_use.get(k)
        created = _dt_of(c["created"])
        stamps = [t for t in (created, _dt_of(c["altered"])) if t]
        changed = max(stamps) if stamps else None
        c["age_days"] = None if created is None else round((now - created).total_seconds() / 86400, 1)
        risks = []
        lu = _dt_of(c["last_used"]["time"]) if c["last_used"] else None
        if lu and now - lu < recent:
            risks.append(f"used {_ago(now - lu)} ago")
        if changed and now - changed < recent:
            risks.append(f"{'created' if changed == created else 'altered'} {_ago(now - changed)} ago")
        if c["views"]:
            risks.append(f"{len(c['views'])} dependent view{'s' if len(c['views']) > 1 else ''}")
        c["risks"] = risks
        c["risky"] = bool(risks)
    return {"history": hist and {"events": hist["events"], "oldest": hist["oldest"]}, "now": _iso(now)}


def _ago(delta: _dt.timedelta) -> str:
    s = max(0, delta.total_seconds())
    if s < 3600:
        return f"{int(s // 60)} min"
    if s < 172800:
        return f"{int(s // 3600)} h"
    return f"{int(s // 86400)} days"


def _candidate(t: dict[str, Any], kind: str, pattern: str | None) -> dict[str, Any]:
    return {
        "database": t["TABLE_SCHEMA"],
        "name": t["TABLE_NAME"],
        "kind": kind,
        "pattern": pattern,
        "storage": t["STORAGE_TYPE"],
        "reference": t["DISTRIBUTED"] == 0,
        "created": _iso(t["CREATE_TIME"]),
        "altered": _iso(t["ALTER_TIME"]),
        "owner": t["CREATE_USER"] or None,
        "comment": t["TABLE_COMMENT"] or None,
    }


def scan(patterns: list[dict[str, str]] | None = None, older_than_days: int | None = None,
         recent_days: int | None = None) -> dict[str, Any]:
    settings = load_settings()
    patterns = _clean_patterns(patterns) if patterns is not None else settings["patterns"]
    older_than_days = _pos_int(older_than_days)
    recent_days = _pos_int(recent_days) or settings["recent_days"]
    tables, views, now = db.parallel(_all_tables, _all_views, _now)
    cutoff = now - _dt.timedelta(days=older_than_days) if older_than_days else None
    cands: list[dict[str, Any]] = []
    for t in tables:
        if t["TABLE_SCHEMA"] in SYSTEM_DATABASES:
            continue
        p = match_kind(t["TABLE_NAME"], patterns)
        if p:
            cands.append(_candidate(t, p["kind"], p["pattern"]))
        elif cutoff:
            last = max([x for x in (_dt_of(t["CREATE_TIME"]), _dt_of(t["ALTER_TIME"])) if x] or [now])
            if last < cutoff:
                cands.append(_candidate(t, f"older than {older_than_days} days", None))
    truncated = len(cands) > MAX_CANDIDATES
    cands.sort(key=lambda c: (c["database"].lower(), c["name"].lower()))
    cands = cands[:MAX_CANDIDATES]
    meta = _enrich(cands, views, now, recent_days)
    per_db: dict[str, dict[str, int]] = {}
    for c in cands:
        d = per_db.setdefault(c["database"], {"tables": 0, "size_bytes": 0, "rows": 0})
        d["tables"] += 1
        d["size_bytes"] += c["size_bytes"] or 0
        d["rows"] += c["rows"] or 0
    return {
        "candidates": cands,
        "by_database": per_db,
        "total_bytes": sum(c["size_bytes"] or 0 for c in cands),
        "tables_scanned": len(tables),
        "truncated": truncated,
        "patterns": patterns,
        "older_than_days": older_than_days,
        "recent_days": recent_days,
        "history": meta.get("history"),
        "now": _iso(now),
    }


def scan_summary(data: dict[str, Any], limit: int = 25) -> str:
    c = data["candidates"]
    if not c:
        return (f"No leftover work tables found ({data['tables_scanned']} tables scanned; patterns: "
                + ", ".join(p["pattern"] for p in data["patterns"]) + ").")
    lines = [f"{len(c)} candidate work table(s), {_bytes(data['total_bytes'])} in total"
             f" ({sum(x['risky'] for x in c)} flagged as risky):"]
    for db_name, d in sorted(data["by_database"].items()):
        lines.append(f"- {db_name}: {d['tables']} table(s), {_bytes(d['size_bytes'])}")
    for x in c[:limit]:
        lines.append(f"  {x['database']}.{x['name']} ({x['kind']}, {x['rows'] if x['rows'] is not None else '?'} rows,"
                     f" {_bytes(x['size_bytes'])}{'; ' + ', '.join(x['risks']) if x['risks'] else ''})")
    if len(c) > limit:
        lines.append(f"  ... {len(c) - limit} more (shown in the app)")
    return "\n".join(lines)


def _bytes(n: Any) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f} {unit}" if unit == "B" or v >= 10 else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v} B"


# ------------------------------------------------------------------ drop


def _norm(items: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for it in items or []:
        if isinstance(it, dict):
            d, n = it.get("database"), it.get("name") or it.get("table")
        elif isinstance(it, (list, tuple)) and len(it) == 2:
            d, n = it
        else:
            raise ValueError(f"Not a table reference: {it!r} (expected {{database, name}})")
        if not d or not n:
            raise ValueError(f"Not a table reference: {it!r}")
        k = (str(d), str(n))
        if k not in out:
            out.append(k)
    return out


def _cluster_user() -> str | None:
    try:
        rows = _q("SELECT CURRENT_USER() AS u")
        return rows[0]["u"] if rows else None
    except Exception:  # noqa: BLE001 - only for the log
        return None


def drop(tables: Any, views: Any = None, dry_run: bool = True) -> dict[str, Any]:
    """Drop the given tables (and explicitly included dependent views), one statement each.

    Everything is re-checked on the server first: system databases are refused,
    each table must exist and be a base table, and a table with a dependent view
    that isn't in ``views`` is skipped. With ``dry_run`` nothing is executed; the
    result lists the exact statements.
    """
    want = _norm(tables)
    want_views = _norm(views)
    if not want:
        raise ValueError("No tables selected.")
    if len(want) > MAX_CANDIDATES:
        raise ValueError(f"At most {MAX_CANDIDATES} tables per clean-up.")
    for d, _ in want + want_views:
        if d.lower() in SYSTEM_DATABASES:
            raise ValueError(f"Tables in the system database {d!r} are never dropped.")
    dbs = sorted({d for d, _ in want})
    names = sorted({n for _, n in want})
    found_rows, all_views, user, now = db.parallel(
        lambda: _q(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, STORAGE_TYPE, DISTRIBUTED, CREATE_TIME, ALTER_TIME,"
            " UPDATE_TIME, CREATE_USER, TABLE_COMMENT FROM information_schema.TABLES"
            f" WHERE TABLE_SCHEMA IN ({_in(dbs)}) AND TABLE_NAME IN ({_in(names)})",
            (*dbs, *names),
        ),
        _all_views,
        _cluster_user,
        _now,
    )
    found = {(r["TABLE_SCHEMA"], r["TABLE_NAME"]): r for r in found_rows}
    existing_views = {(v["TABLE_SCHEMA"], v["TABLE_NAME"]) for v in all_views}
    settings = load_settings()
    cands, results = [], []
    for k in want:
        t = found.get(k)
        if t is None:
            results.append({"type": "table", "database": k[0], "name": k[1], "status": "skipped",
                            "error": "not found (already dropped?)"})
        elif t["TABLE_TYPE"] != "BASE TABLE":
            results.append({"type": "table", "database": k[0], "name": k[1], "status": "skipped",
                            "error": f"is a {t['TABLE_TYPE'].lower()}, not a table"})
        else:
            p = match_kind(t["TABLE_NAME"], settings["patterns"])
            cands.append(_candidate(t, p["kind"] if p else "table", p["pattern"] if p else None))
    _enrich(cands, all_views, now, settings["recent_days"], with_history=False)
    included = set(want_views)
    for v in want_views:
        if v not in existing_views:
            results.append({"type": "view", "database": v[0], "name": v[1], "status": "skipped",
                            "error": "view not found"})
    plan_views: list[tuple[str, str]] = []
    plan_tables: list[dict[str, Any]] = []
    for c in cands:
        missing = [v for v in c["views"] if (v["database"], v["name"]) not in included]
        if missing:
            results.append({"type": "table", "database": c["database"], "name": c["name"], "status": "blocked",
                            "error": "dependent view(s) not included: "
                                     + ", ".join(f"{v['database']}.{v['name']}" for v in missing)})
            continue
        for v in c["views"]:
            k = (v["database"], v["name"])
            if k not in plan_views:
                plan_views.append(k)
        plan_tables.append(c)
    # Views the user included that no remaining table needs are still dropped only if they exist.
    for v in want_views:
        if v in existing_views and v not in plan_views:
            plan_views.append(v)
    steps: list[dict[str, Any]] = (
        [{"type": "view", "database": d, "name": n, "sql": drop_statement(d, n, view=True)} for d, n in plan_views]
        + [{"type": "table", "database": c["database"], "name": c["name"], "kind": c["kind"], "rows": c["rows"],
            "size_bytes": c["size_bytes"], "sql": drop_statement(c["database"], c["name"])} for c in plan_tables]
    )
    log: list[dict[str, Any]] = []
    stamp = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        from . import connections

        prof = connections.active()
        conn_name = prof.name if prof else None
    except Exception:  # noqa: BLE001 - only for the log
        conn_name = None
    for st in steps:
        res = dict(st)
        if dry_run:
            res["status"] = "dry-run"
        else:
            try:
                db.execute(st["sql"])
                res["status"] = "dropped"
            except Exception as exc:  # noqa: BLE001 - report per object and carry on
                res["status"] = "error"
                res["error"] = str(exc)
            log.append({"time": stamp, "user": user, "os_user": _os_user(), "connection": conn_name,
                        "type": st["type"], "database": st["database"], "name": st["name"],
                        "kind": st.get("kind"), "rows": st.get("rows"), "size_bytes": st.get("size_bytes"),
                        "status": res["status"], "error": res.get("error"), "sql": st["sql"]})
        results.append(res)
    _append_log(log)
    order = {"view": 0, "table": 1}
    results.sort(key=lambda r: (r["status"] in ("dropped", "dry-run"), order[r["type"]]))
    dropped = [r for r in results if r["status"] == "dropped"]
    return {
        "dry_run": bool(dry_run),
        "statements": [s["sql"] for s in steps],
        "results": results,
        "dropped": len(dropped),
        "dropped_bytes": sum(r.get("size_bytes") or 0 for r in dropped if r["type"] == "table"),
        "errors": sum(r["status"] == "error" for r in results),
        "blocked": sum(r["status"] in ("blocked", "skipped") for r in results),
        "log": read_log(20) if not dry_run else None,
    }


def _os_user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return None


def drop_summary(data: dict[str, Any]) -> str:
    if data["dry_run"]:
        head = f"Dry run: {len(data['statements'])} statement(s) would run (nothing was dropped)."
    else:
        head = (f"Dropped {data['dropped']} object(s), {_bytes(data['dropped_bytes'])} freed;"
                f" {data['errors']} error(s), {data['blocked']} skipped/blocked.")
    lines = [head, *data["statements"][:30]]
    for r in data["results"]:
        if r["status"] in ("error", "blocked", "skipped"):
            lines.append(f"- {r['database']}.{r['name']}: {r['status']} ({r.get('error')})")
    return "\n".join(lines)
