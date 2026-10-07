"""Workload advisor: table-design and workload recommendations from the whole query history.

Every distinct query in the history (``information_schema.MV_TRACE_EVENTS``) is EXPLAINed, which compiles
it without running it. The plans say exactly which table columns each query filters, joins and groups on,
and where data is broadcast or reshuffled between leaves. Weighted by how often and how long the queries
ran, that gives per table:

- a SORT KEY on the columns most time is spent filtering (segment elimination);
- a SHARD KEY on the join / group-by columns when data is reshuffled or broadcast;
- REFERENCE table candidates (small tables broadcast to every leaf);
- indexes for filtered rowstore tables, and missing column statistics (ANALYZE);

plus workload findings (huge results, SELECT *, recurring failures).
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .db import db, quote_identifier

MARKER = "/* s2-query-history */"
_MAX_QUERIES = 300
_WORKERS = 6

_LEADING_COMMENTS = re.compile(r"^(\s+|/\*.*?\*/|--[^\n]*\n|#[^\n]*\n)*", re.S)
_EXPLAINABLE = re.compile(r"^(SELECT|WITH|INSERT|REPLACE|UPDATE|DELETE)\b", re.I)
_CTAS = re.compile(r"^CREATE\s+(?:(?:ROWSTORE|REFERENCE|TEMPORARY|GLOBAL)\s+)*TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                   r"(\S+)\s*(?:\(.*?\)\s*)?AS\s+(SELECT\b.*)$", re.I | re.S)
_SCAN = re.compile(r"(ColumnStoreScan|TableScan|IndexRangeScan|IndexSeek|IndexScan)\s+([\w$`.]+)(?:\s+AS\s+([\w$`]+))?")
_EST_ROWS = re.compile(r"est_table_rows:([\d,]+)")
_COLREF = r"`?([A-Za-z_][\w$]*)`?\.`?([A-Za-z_][\w$]*)`?"
_PRED = re.compile(_COLREF + r"\s*(<=>|>=|<=|!=|<>|=|<|>|\bNOT\s+IN\b|\bIN\b|\bBETWEEN\b|\bLIKE\b|\bIS\b)", re.I)
_EQ_PAIR = re.compile(_COLREF + r"\s*=\s*" + _COLREF)
_ANALYZE = re.compile(r"ANALYZE TABLE\s+(\S+)\s+COLUMNS\s+(.+?)\s+ENABLE", re.I)

_SYSTEM_DBS = {"information_schema", "memsql", "cluster", "sys", "mysql", "performance_schema"}
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def strip(sql: str) -> str:
    return _LEADING_COMMENTS.sub("", sql or "", count=1).strip()


def explain_target(sql: str) -> tuple[str | None, str | None]:
    """(statement to EXPLAIN, why not) — only statements EXPLAIN can compile without running anything."""
    body = strip(sql)
    if len(body) > 200_000:
        return None, "The statement is too long to analyze."
    if _EXPLAINABLE.match(body):
        return body, None
    m = _CTAS.match(body)
    if m:
        return m.group(2), None
    return None, "EXPLAIN only applies to SELECT, INSERT, REPLACE, UPDATE, DELETE and CREATE TABLE … AS SELECT."


def _normalize(sql: str) -> str:
    """Query shape: literals replaced, whitespace collapsed (runs of the same query with other values group)."""
    s = re.sub(r"'(?:[^'\\]|\\.|'')*'", "?", sql)
    s = re.sub(r"\b\d+(?:\.\d+)?\b", "?", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return re.sub(r"\(\s*\?(?:\s*,\s*\?)*\s*\)", "(?)", s)


def _num(text: str) -> int:
    return int(text.replace(",", ""))


def parse_plan(lines: list[str], default_db: str | None) -> dict[str, Any]:
    """Tables scanned (with aliases), filter / join / group-by columns per table, data movement, ANALYZE hints."""
    tables: dict[str, dict[str, Any]] = {}
    alias: dict[str, str] = {}
    for line in lines:
        m = _SCAN.search(line)
        if not m:
            continue
        name = m.group(2).replace("`", "")
        if "." not in name and default_db:
            name = f"{default_db}.{name}"
        rows = _EST_ROWS.search(line)
        t = tables.setdefault(name, {"op": m.group(1), "rows": None, "unsorted": False})
        t["unsorted"] |= "SORT KEY __UNORDERED" in line
        if rows:
            t["rows"] = max(t["rows"] or 0, _num(rows.group(1)))
        short = name.rsplit(".", 1)[-1]
        alias[short.lower()] = name
        if m.group(3):
            alias[m.group(3).replace("`", "").lower()] = name

    def resolve(prefix: str) -> str | None:
        return alias.get(prefix.lower())

    out: dict[str, Any] = {"tables": tables, "filters": [], "joins": [], "groups": [],
                           "broadcast": False, "reshuffle": False, "analyze": []}
    for line in lines:
        s = line.strip()
        if "Broadcast" in s:
            out["broadcast"] = True
        if "Repartition" in s or "Reshuffle" in s:
            out["reshuffle"] = True
        a = _ANALYZE.search(s)
        if a:
            out["analyze"].append((a.group(1), [c.strip(" `") for c in a.group(2).split(",")]))
        if s.startswith(("ColumnStoreFilter", "Filter")) or "IndexSeek" in s or "IndexRangeScan" in s:
            for p in _PRED.finditer(s):
                table = resolve(p.group(1))
                if table:
                    op = p.group(3).upper()
                    if op == "IS":
                        continue  # IS [NOT] NULL can't skip segments
                    kind = "range" if op in ("<", ">", "<=", ">=", "BETWEEN") else "like" if op == "LIKE" \
                        else "equality" if op in ("=", "IN", "<=>", "IS") else "other"
                    out["filters"].append((table, p.group(2), kind))
        if "Join" in s:
            for p in _EQ_PAIR.finditer(s):
                for prefix, col in ((p.group(1), p.group(2)), (p.group(3), p.group(4))):
                    table = resolve(prefix)
                    if table:
                        out["joins"].append((table, col))
        g = re.search(r"groups:\[(.*?)\]", s)
        if g and "GroupBy" in s:
            for p in re.finditer(_COLREF, g.group(1)):
                table = resolve(p.group(1))
                if table:
                    out["groups"].append((table, p.group(2)))
    return out


def _history() -> list[dict[str, Any]]:
    # No query parameters here, so % stays single (the driver only formats when given parameters).
    return db.execute(
        f"{MARKER} SELECT DETAILS::$query_text AS q, DETAILS::$context_database AS db, DETAILS::%duration_ms AS ms,"
        " DETAILS::%success AS ok, DETAILS::%row_count AS row_count, DETAILS::$error_code AS error_code"
        " FROM information_schema.MV_TRACE_EVENTS WHERE EVENT_TYPE = 'Query_completion'"
        # Every statement this server sends for itself starts with a "/* s2-… */" marker.
        " AND (DETAILS::$query_text IS NULL OR DETAILS::$query_text NOT LIKE '%/* s2-%')"
    )[1]


def _table_info(names: list[str]) -> dict[str, dict[str, Any]]:
    """SHOW CREATE TABLE (keys, storage) and row counts for each table."""
    def show(name: str) -> tuple[str, dict[str, Any]]:
        dbname, _, table = name.rpartition(".")
        try:
            ddl = db.execute(f"{MARKER} SHOW CREATE TABLE {quote_identifier(table)}", database=dbname or None)[1][0]
            text = ddl.get("Create Table") or next(iter(ddl.values()))
        except Exception as exc:  # noqa: BLE001 - dropped temp tables etc.
            return name, {"missing": True, "error": str(exc)[:160]}
        shard = re.search(r"SHARD KEY\s*[`\w]*\s*\(([^)]*)\)", text, re.I)
        sort = re.search(r"SORT KEY\s*[`\w]*\s*\(([^)]*)\)", text, re.I)
        clean = lambda m: [c.strip(" `") for c in m.group(1).split(",") if c.strip(" `")] if m else None  # noqa: E731
        return name, {
            "reference": bool(re.search(r"CREATE\s+REFERENCE\s+TABLE", text, re.I)),
            "rowstore": bool(re.search(r"CREATE\s+ROWSTORE", text, re.I)),
            "shard_key": clean(shard), "sort_key": clean(sort), "ddl": text,
            "indexes": re.findall(r"^\s*(?:UNIQUE\s+)?KEY\s+`?[\w$]*`?\s*\(([^)]*)\)", text, re.I | re.M),
        }

    with ThreadPoolExecutor(_WORKERS) as pool:
        info = dict(pool.map(show, names))
    if names:
        # Row counts (master partitions only), one round trip for all tables.
        dbs = sorted({n.rpartition(".")[0] for n in names if "." in n})
        if dbs:
            marks = ", ".join(["%s"] * len(dbs))
            for r in db.execute(
                f"{MARKER} SELECT DATABASE_NAME, TABLE_NAME, SUM(ROWS) AS n FROM information_schema.TABLE_STATISTICS"
                f" WHERE PARTITION_TYPE = 'Master' AND DATABASE_NAME IN ({marks}) GROUP BY 1, 2", tuple(dbs))[1]:
                key = f"{r['DATABASE_NAME']}.{r['TABLE_NAME']}"
                if key in info:
                    info[key]["rows"] = int(r["n"] or 0)
            # Column cardinality from the optimizer's statistics (no table scans): a shard key needs many values.
            for r in db.execute(
                f"{MARKER} SELECT DATABASE_NAME, TABLE_NAME, COLUMN_NAME,"
                " COALESCE(CARDINALITY, AUTOSTATS_CARDINALITY) AS card FROM information_schema.OPTIMIZER_STATISTICS"
                f" WHERE DATABASE_NAME IN ({marks}) AND (JSON_KEY IS NULL OR JSON_KEY = '')", tuple(dbs))[1]:
                key = f"{r['DATABASE_NAME']}.{r['TABLE_NAME']}"
                if key in info and r["card"] is not None:
                    info[key].setdefault("cardinality", {})[r["COLUMN_NAME"].lower()] = int(r["card"])
    return info


_DDL = re.compile(r"(CREATE\s+(?:\w+\s+)*?)TABLE\s+`([^`]+)`\s*\(\n(.*)\n\)\s*(.*)$", re.S)


def rebuild_sql(ddl: str | None, dbname: str, table: str, suffix: str, *, shard: list[str] | None = None,
                sort: list[str] | None = None, reference: bool = False, current_shard: list[str] | None = None,
                current_sort: list[str] | None = None) -> str | None:
    """Statements that build a copy of a table with other keys from its SHOW CREATE TABLE, fill it, and (commented
    out) swap the names. Keys can't be changed in place, and this uses only plain CREATE TABLE + INSERT … SELECT."""
    m = _DDL.match(ddl or "")
    if not m:
        return None
    lines = []
    for line in m.group(3).split("\n"):
        s = line.strip().lstrip(",").strip().rstrip(",").strip()
        if s and not re.match(r"(SORT|SHARD)\s+KEY\b", s, re.I):
            lines.append(f"  {s}")
    q = quote_identifier
    sort_cols = sort if sort is not None else current_sort
    shard_cols = None if reference else shard if shard is not None else current_shard
    if sort_cols:
        lines.append(f"  SORT KEY ({', '.join(q(c) for c in sort_cols)})")
    if shard_cols is not None:
        lines.append(f"  SHARD KEY ({', '.join(q(c) for c in shard_cols)})")
    prefix = "CREATE REFERENCE " if reference else m.group(1)
    new, old = f"{table}{suffix}", f"{table}_old"
    return (f"-- Keys can't be changed in place: build a copy with the new keys, check it with your queries,\n"
            f"-- then swap the names. Needs free memory / disk for a second copy of the table.\n"
            f"{prefix}TABLE {q(dbname)}.{q(new)} (\n" + ",\n".join(lines) + f"\n) {m.group(4)};\n"
            f"INSERT INTO {q(dbname)}.{q(new)} SELECT * FROM {q(dbname)}.{q(table)};\n"
            f"-- ALTER TABLE {q(dbname)}.{q(table)} RENAME TO {q(old)};\n"
            f"-- ALTER TABLE {q(dbname)}.{q(new)} RENAME TO {q(table)};")


def _fmt_s(ms: float) -> str:
    s = ms / 1000
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.1f} h"


def _fmt_rows(n: int | None) -> str:
    n = n or 0
    return f"{n / 1e9:.1f} billion" if n >= 1e9 else f"{n / 1e6:.1f} million" if n >= 1e6 else f"{n:,}"


def advise(max_queries: int = _MAX_QUERIES, refresh: bool = False) -> dict[str, Any]:
    started = time.time()
    events = _history()
    newest_key = f"{len(events)}:{sum(int(e['ms'] or 0) for e in events)}"
    with _cache_lock:
        if not refresh and _cache.get("key") == newest_key:
            return {**_cache["result"], "cached": True}

    # --- group runs of the same query shape
    groups: dict[str, dict[str, Any]] = {}
    workload = {"big_results": [], "select_star": [], "failures": defaultdict(lambda: {"count": 0, "ms": 0, "sample": ""})}
    for e in events:
        sql = strip(e["q"] or "")
        if not sql:
            continue
        ms = int(e["ms"] or 0)
        if e["ok"] in (0, False):
            f = workload["failures"][e["error_code"] or "error"]
            f["count"] += 1
            f["ms"] += ms
            f["sample"] = f["sample"] or " ".join(sql.split())[:200]
            continue
        rows = int(e["row_count"] or 0)
        if rows >= 1_000_000 and re.match(r"^(SELECT|WITH)\b", sql, re.I):
            workload["big_results"].append({"rows": rows, "ms": ms, "sql": " ".join(sql.split())[:200], "db": e["db"]})
        if re.search(r"\bSELECT\s+\*\s+FROM\b", sql, re.I) and (rows >= 100_000 or ms >= 10_000):
            workload["select_star"].append({"rows": rows, "ms": ms, "sql": " ".join(sql.split())[:200], "db": e["db"]})
        target, _ = explain_target(sql)
        if not target:
            continue
        g = groups.setdefault(_normalize(target), {"sql": target, "db": e["db"] or None, "runs": 0, "ms": 0})
        g["runs"] += 1
        g["ms"] += ms

    ranked = sorted(groups.values(), key=lambda g: -g["ms"])
    chosen, skipped = ranked[:max_queries], max(0, len(ranked) - max_queries)

    def explain(g: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        try:
            rows = db.execute(f"{MARKER} EXPLAIN {g['sql']}", database=g["db"])[1]
            return g, parse_plan([next(iter(r.values())) or "" for r in rows], g["db"]), None
        except Exception as exc:  # noqa: BLE001 - temp tables gone, privileges…
            return g, None, str(exc)[:160]

    with ThreadPoolExecutor(_WORKERS) as pool:
        plans = list(pool.map(explain, chosen))

    # --- per table: where time is spent filtering, joining, grouping
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "queries": 0, "runs": 0, "ms": 0, "est_rows": None, "unsorted": False, "op": None,
        "filters": defaultdict(lambda: {"queries": 0, "runs": 0, "ms": 0, "kinds": set()}),
        "joins": defaultdict(lambda: {"queries": 0, "runs": 0, "ms": 0}),
        "groups": defaultdict(lambda: {"queries": 0, "runs": 0, "ms": 0}),
        "moves": {"queries": 0, "ms": 0, "broadcast_queries": 0}, "analyze": set(), "examples": [],
    })
    failed = 0
    for g, plan, err in plans:
        if not plan:
            failed += 1
            continue
        for name, t in plan["tables"].items():
            p = per[name]
            p["queries"] += 1
            p["runs"] += g["runs"]
            p["ms"] += g["ms"]
            p["unsorted"] |= t["unsorted"]
            p["op"] = p["op"] or t["op"]
            if t["rows"]:
                p["est_rows"] = max(p["est_rows"] or 0, t["rows"])
            if len(p["examples"]) < 3:
                p["examples"].append({"sql": " ".join(g["sql"].split())[:220], "ms": g["ms"], "runs": g["runs"]})
            if plan["broadcast"] or plan["reshuffle"]:
                p["moves"]["queries"] += 1
                p["moves"]["ms"] += g["ms"]
                p["moves"]["broadcast_queries"] += int(plan["broadcast"])
        for key, bucket in (("filters", "filters"), ("joins", "joins"), ("groups", "groups")):
            seen: set[tuple[str, str]] = set()
            for item in plan[key]:
                table, col = item[0], item[1]
                if (table, col.lower()) in seen or table not in per:
                    if bucket == "filters" and table in per:
                        per[table]["filters"][col]["kinds"].add(item[2])
                    continue
                seen.add((table, col.lower()))
                c = per[table][bucket][col]
                c["queries"] += 1
                c["runs"] += g["runs"]
                c["ms"] += g["ms"]
                if bucket == "filters":
                    c["kinds"].add(item[2])
        for table, cols in plan["analyze"]:
            name = table.replace("`", "")
            for t in (name, *(n for n in per if n.endswith("." + name.rsplit(".", 1)[-1]))):
                if t in per:
                    per[t]["analyze"].update(cols)
                    break

    for name in [n for n in per if n.split(".", 1)[0].lower() in _SYSTEM_DBS]:
        del per[name]  # system views: their design can't be changed
    info = _table_info(list(per))
    tables = [_recommend(name, p, info.get(name, {})) for name, p in per.items()]
    tables = [t for t in tables if not t.get("gone")]
    order = {"high": 0, "medium": 1, "low": 2, "ok": 3}
    tables.sort(key=lambda t: (order[t["priority"]], -t["ms"]))

    failures = sorted(({"error_code": k, **v} for k, v in workload["failures"].items()), key=lambda f: -f["count"])
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seconds": round(time.time() - started, 1),
        "events": len(events),
        "query_shapes": len(groups),
        "analyzed": len(chosen) - failed,
        "explain_failed": failed,
        "skipped": skipped,
        "tables": tables,
        "workload": {
            "big_results": sorted(workload["big_results"], key=lambda x: -x["ms"])[:10],
            "select_star": sorted(workload["select_star"], key=lambda x: -x["ms"])[:10],
            "failures": failures[:10],
        },
    }
    with _cache_lock:
        _cache.update(key=newest_key, result=result)
    return result


def _ranked(cols: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = [{"column": c, **{k: (sorted(v) if isinstance(v, set) else v) for k, v in s.items()}} for c, s in cols.items()]
    return sorted(out, key=lambda c: (-c["ms"], -c["queries"]))


def _recommend(name: str, p: dict[str, Any], t: dict[str, Any]) -> dict[str, Any]:
    dbname, _, table = name.rpartition(".")
    if t.get("missing"):
        return {"gone": True}
    rows = t.get("rows") if t.get("rows") is not None else p["est_rows"]
    filters, joins, groups = _ranked(p["filters"]), _ranked(p["joins"]), _ranked(p["groups"])
    recs: list[dict[str, Any]] = []
    fq = f"{quote_identifier(dbname)}.{quote_identifier(table)}" if dbname else quote_identifier(table)
    big = (rows or 0) >= 1_000_000
    sort_key, shard_key = t.get("sort_key"), t.get("shard_key")

    def add(kind: str, priority: str, title: str, detail: str, sql: str | None = None, new_table: str | None = None) -> None:
        recs.append({"kind": kind, "priority": priority, "title": title, "detail": detail, **({"sql": sql} if sql else {}),
                     # A rebuilt copy: the Advisor's "Compare with new table" checks it against the original.
                     **({"new_table": new_table} if sql and new_table else {})})

    # --- sort key (columnstore): the columns most query time filters on
    if not t.get("rowstore") and filters:
        top = filters[0]
        cols = [top["column"]]
        if len(filters) > 1 and filters[1]["ms"] >= 0.5 * top["ms"]:
            # Equality filters first, then a range column (e.g. region = ? AND date > ?).
            second = filters[1]
            if "range" in top["kinds"] and "equality" in second["kinds"] and "range" not in second["kinds"]:
                cols = [second["column"], top["column"]]
            else:
                cols.append(second["column"])
        current = [c.lower() for c in (sort_key or [])]
        share = top["ms"] / p["ms"] if p["ms"] else 0
        if current and current[0] == cols[0].lower():
            add("sort", "ok", f"Sort key ({', '.join(sort_key)}) fits the workload",
                f"The column filtered most ({top['column']}, {top['queries']} queries) already leads the sort key.")
        else:
            prio = "high" if big and share >= 0.3 and (rows or 0) >= 10_000_000 else "medium" if big and share >= 0.1 else "low"
            kinds = "/".join(top["kinds"]) or "filter"
            add("sort", prio, f"SORT KEY ({', '.join(cols)})",
                f"{top['queries']} quer{'y' if top['queries'] == 1 else 'ies'} ({_fmt_s(top['ms'])} in total, "
                f"{share:.0%} of the time spent on this table) filter on {top['column']} ({kinds}). "
                + (f"The table has no sort key, so every segment is read. " if not current else
                   f"The current sort key ({', '.join(sort_key)}) doesn't start with it. ")
                + "With this sort key SingleStore skips the segments outside the filter."
                + ("" if big else " The table is small, so the gain is small."),
                rebuild_sql(t.get("ddl"), dbname, table, "_sorted", sort=cols,
                            current_shard=shard_key, current_sort=sort_key), f"{table}_sorted")
    elif not t.get("rowstore") and big and p["ms"]:
        add("sort", "low", "No filters on this table",
            "The analyzed queries read this table without filtering it, so a sort key wouldn't help them. "
            "If queries usually read only part of it (a period, a region), add that filter.")

    # --- shard key: data moved for joins / group-bys
    keycols = joins or groups
    card = t.get("cardinality") or {}
    # Too few distinct values spread rows unevenly over the partitions (skew); unknown cardinality is checked by hand.
    min_card = 10_000 if big else 100
    if keycols and not t.get("reference"):
        current = [c.lower() for c in (shard_key or [])]
        moved = p["moves"]["queries"]
        usable = [c for c in keycols if card.get(c["column"].lower(), min_card) >= min_card]
        top = usable[0] if usable else keycols[0]
        if current and keycols[0]["column"].lower() in current:
            add("shard", "ok", f"Shard key ({', '.join(shard_key)}) fits the joins",
                f"Joins / group-bys use {keycols[0]['column']}, which is in the shard key, so they run locally on each leaf.")
        elif moved and big and not usable:
            few = ", ".join(f"{c['column']} ({card[c['column'].lower()]:,} values)" for c in keycols[:3] if c["column"].lower() in card)
            add("shard", "low", "Keep the current shard key",
                f"Queries join / group on {few}, but these have too few distinct values to shard {_fmt_rows(rows)} rows "
                "on: most partitions would get almost nothing and a few would get everything (skew). The data movement "
                "for these group-bys is small anyway, since there are few groups.")
        elif moved and big:
            what = "joined" if joins else "grouped"
            add("shard", "high" if p["moves"]["ms"] >= 0.3 * p["ms"] else "medium", f"SHARD KEY ({top['column']})",
                f"{top['queries']} quer{'y' if top['queries'] == 1 else 'ies'} {what} on {top['column']} "
                f"({_fmt_s(top['ms'])}), and {moved} of the queries on this table move data between leaves "
                f"(reshuffle or broadcast). The table is {'keyless' if shard_key == [] else f'sharded on ({', '.join(shard_key)})' if shard_key else 'without a shard key'}. "
                "Sharding it on the join column — and the tables it joins with on the same column — lets the "
                "join run locally on each leaf."
                + (f" It has {card[top['column'].lower()]:,} distinct values, enough to spread the rows evenly."
                   if top["column"].lower() in card else
                   f" Check first that it has many distinct values (no statistics yet): "
                   f"SELECT APPROX_COUNT_DISTINCT({quote_identifier(top['column'])}) FROM {fq};"),
                rebuild_sql(t.get("ddl"), dbname, table, "_resharded", shard=[top["column"]],
                            current_shard=shard_key, current_sort=sort_key), f"{table}_resharded")

    # --- reference table: small and broadcast
    if p["moves"]["broadcast_queries"] and not t.get("reference") and rows is not None and rows <= 1_000_000:
        add("reference", "medium", "Make it a REFERENCE table",
            f"It's small ({_fmt_rows(rows)} rows) and gets broadcast to every leaf in {p['moves']['broadcast_queries']} "
            f"quer{'y' if p['moves']['broadcast_queries'] == 1 else 'ies'}. A reference table has a full copy on every "
            "leaf, so joins with it never move data.",
            rebuild_sql(t.get("ddl"), dbname, table, "_ref", reference=True, current_sort=sort_key), f"{table}_ref")

    # --- rowstore: index on the filtered columns
    if t.get("rowstore") and filters:
        indexed = {c.split(",")[0].strip(" `").lower() for c in t.get("indexes") or []}
        top = filters[0]
        if top["column"].lower() not in indexed:
            add("index", "medium" if big else "low", f"Index on {top['column']}",
                f"{top['queries']} quer{'y' if top['queries'] == 1 else 'ies'} filter this rowstore table on "
                f"{top['column']} without an index, so every row is read.",
                f"CREATE INDEX {quote_identifier('ix_' + top['column'])} ON {fq} ({quote_identifier(top['column'])});")

    # --- columnstore: hash index for selective equality lookups (col = ? / IN (…) on a column with many values)
    if not t.get("rowstore") and big:
        indexed = {c.split(",")[0].strip(" `").lower() for c in t.get("indexes") or []}
        lead = (sort_key or [""])[0].lower()
        for f in filters:
            col = f["column"]
            values = card.get(col.lower())
            if "equality" not in f["kinds"] or col.lower() in indexed or col.lower() == lead:
                continue
            if values is not None and values < 100_000:
                continue  # few values: each lookup still matches many rows, the sort key / segment skipping does better
            add("index", "medium" if values else "low", f"Hash index on {col}",
                f"{f['queries']} quer{'y' if f['queries'] == 1 else 'ies'} ({_fmt_s(f['ms'])}) look up rows by "
                f"{col} = …"
                + (f", and it has {values:,} distinct values, so each lookup matches few rows." if values else
                   ". Worth it if the column has many distinct values (check: "
                   f"SELECT APPROX_COUNT_DISTINCT({quote_identifier(col)}) FROM {fq}).")
                + " A columnstore hash index finds those rows without scanning the column segments. It can be added "
                  "in place, but costs some disk and slows loading slightly.",
                f"CREATE INDEX {quote_identifier('ix_' + col)} ON {fq} ({quote_identifier(col)}) USING HASH;")
            break

    # --- statistics
    if p["analyze"]:
        cols = ", ".join(quote_identifier(c) for c in sorted(p["analyze"]))
        add("stats", "medium", "Collect column statistics",
            f"The optimizer has no histograms for {', '.join(sorted(p['analyze']))}, which queries filter on, so its "
            "row estimates (and join order) can be off.", f"ANALYZE TABLE {fq} COLUMNS {cols} ENABLE;")

    order = {"high": 0, "medium": 1, "low": 2, "ok": 3}
    recs.sort(key=lambda r: order[r["priority"]])
    priority = recs[0]["priority"] if recs else "ok"
    return {
        "table": name, "database": dbname, "name": table, "rows": rows,
        "storage": "reference" if t.get("reference") else "rowstore" if t.get("rowstore") else "columnstore",
        "shard_key": shard_key, "sort_key": sort_key,
        "queries": p["queries"], "runs": p["runs"], "ms": p["ms"],
        "filters": filters[:6], "joins": joins[:6], "groups": groups[:6],
        "moves": p["moves"], "examples": p["examples"],
        "recommendations": recs, "priority": priority,
    }


def summary(result: dict[str, Any]) -> str:
    lines = [f"Advisor: {result['analyzed']} query shapes analyzed from {result['events']} history events, "
             f"{len(result['tables'])} tables."]
    for t in result["tables"][:12]:
        for r in t["recommendations"]:
            if r["priority"] != "ok":
                lines.append(f"- [{r['priority']}] {t['table']}: {r['title']} — {r['detail'][:200]}")
    for f in result["workload"]["failures"][:3]:
        lines.append(f"- failures: {f['count']}× {f['error_code']}: {f['sample'][:100]}")
    return "\n".join(lines)
