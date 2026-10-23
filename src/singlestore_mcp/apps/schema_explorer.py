"""Schema Explorer app: browse databases, tables, columns, DDL and sample rows.

The model opens it through ``schema_explorer`` (optionally pre-selecting a
database and table); the UI then lazy-loads everything else through the
app-only ``schema_explorer_*`` helper tools.

Size/row facts for SingleStore: ``information_schema.TABLES.TABLE_ROWS`` and
``DATA_LENGTH`` are 0 for columnstore tables, so row counts and in-memory
size come from ``TABLE_STATISTICS`` (master partitions for distributed
tables, one copy for reference tables) and on-disk columnstore size from
``COLUMNAR_SEGMENTS``. Every identifier is backtick-quoted; table names are
case-sensitive (``CARS`` and ``cars`` can coexist).

The Clean-up panel (header button, or the ``cleanup_work_tables`` tool) finds
and drops leftover SAS work tables; its logic is in ``..housekeeping``.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import housekeeping
from ..db import db, quote_identifier
from ._core import APP_ONLY, apps, jsonable, jsonable_rows, register_app, tool_result, with_browser_link

URI = "ui://singlestore/schema-explorer.html"
SYSTEM_DATABASES = ("information_schema", "cluster", "memsql")
PREVIEW_ROWS = 50
_MAX_CELL_CHARS = 2000
_READ_ONLY = ToolAnnotations(readOnlyHint=True)

register_app(
    URI,
    "schema_explorer.html",
    name="Schema Explorer",
    description="Browse SingleStore databases, tables, columns, shard/sort keys, DDL and sample rows",
)


class NotFoundError(LookupError):
    pass


def _query(sql: str, params: tuple[Any, ...] = (), **kw: Any) -> list[dict[str, Any]]:
    return jsonable_rows(db.execute(sql, params, **kw)[1])


def _num(v: Any) -> float | int | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


# --------------------------------------------------------------------------
# Databases
# --------------------------------------------------------------------------


_SECRET_KEY = re.compile(r"key|secret|token|password|credential|sas|signature", re.IGNORECASE)


def _safe_config(raw: Any) -> Any:
    """The database's storage config without anything that looks like a credential."""
    if raw in (None, ""):
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None  # not JSON: don't show raw text that might hold a secret
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    return None


def _storage_info() -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    """Which databases are bottomless (unlimited storage), with their remote location; plus the data directory.

    MV_BOTTOMLESS_DATABASES lists the attached bottomless databases (STORAGE_TYPE = S3 / Azure / GCP,
    STORAGE_CONFIG as given at CREATE DATABASE); MV_BOTTOMLESS_STATUS has each one's BUCKET and
    STORAGE_DIRECTORY. Every other database keeps all its data on the nodes' local disks.
    """
    try:
        dbs, where, datadir = db.parallel(
            lambda: _query("SELECT DATABASE_NAME, STORAGE_TYPE, STORAGE_CONFIG, IS_DETACHED, RETENTION_PERIOD_MINUTES,"
                           " CREATED_TIME FROM information_schema.MV_BOTTOMLESS_DATABASES"),
            lambda: _query("SELECT DATABASE_NAME, MIN(BUCKET) AS bucket, MIN(STORAGE_DIRECTORY) AS directory"
                           " FROM information_schema.MV_BOTTOMLESS_STATUS GROUP BY DATABASE_NAME"),
            lambda: _query("SELECT @@datadir AS d"),
        )
    except Exception as exc:  # noqa: BLE001 - optional detail (e.g. no permission on the MV_ views)
        return {}, None, str(exc)
    paths = {r["DATABASE_NAME"]: r for r in where}
    out = {}
    for r in dbs:
        w = paths.get(r["DATABASE_NAME"], {})
        bucket, directory = w.get("bucket"), w.get("directory")
        out[r["DATABASE_NAME"]] = {
            "kind": "bottomless",
            "provider": r["STORAGE_TYPE"],
            "bucket": bucket,
            "directory": directory,
            "remote_path": "/".join(x.strip("/") for x in (bucket, directory) if x) or None,
            "config": _safe_config(r["STORAGE_CONFIG"]),
            "detached": r["IS_DETACHED"],
            "retention_minutes": r["RETENTION_PERIOD_MINUTES"],
            "created": r["CREATED_TIME"],
        }
    return out, (datadir[0]["d"] if datadir else None), None


def list_databases() -> list[dict[str, Any]]:
    schemata, counts, (storage, datadir, storage_error) = db.parallel(
        lambda: _query("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA"),
        lambda: _query(
            "SELECT TABLE_SCHEMA,"
            " SUM(TABLE_TYPE = 'BASE TABLE') AS n_tables,"
            " SUM(TABLE_TYPE <> 'BASE TABLE') AS n_views"
            " FROM information_schema.TABLES GROUP BY TABLE_SCHEMA"
        ),
        _storage_info,
    )
    by_db = {r["TABLE_SCHEMA"]: r for r in counts}
    out = []
    for r in schemata:
        name = r["SCHEMA_NAME"]
        c = by_db.get(name, {})
        system = name in SYSTEM_DATABASES
        if system:
            store = None
        elif storage_error:
            store = {"kind": "unknown", "error": storage_error}
        else:
            store = storage.get(name) or {"kind": "local", "data_directory": datadir}
        out.append(
            {
                "name": name,
                "system": system,
                "tables": int(_num(c.get("n_tables")) or 0),
                "views": int(_num(c.get("n_views")) or 0),
                "storage": store,
            }
        )
    out.sort(key=lambda d: (d["system"], d["name"].lower(), d["name"]))
    return out


# --------------------------------------------------------------------------
# Tables of one database
# --------------------------------------------------------------------------

_STATS_SQL = (
    "SELECT TABLE_NAME,"
    " SUM(CASE WHEN PARTITION_TYPE = 'Master' THEN ROWS END) AS master_rows,"
    " MAX(CASE WHEN PARTITION_TYPE = 'Reference' THEN ROWS END) AS ref_rows,"
    " SUM(CASE WHEN PARTITION_TYPE = 'Master' THEN MEMORY_USE END) AS master_mem,"
    " MAX(CASE WHEN PARTITION_TYPE = 'Reference' THEN MEMORY_USE END) AS ref_mem"
    " FROM information_schema.TABLE_STATISTICS WHERE DATABASE_NAME = %s{extra}"
    " GROUP BY TABLE_NAME"
)
_DISK_SQL = (
    "SELECT TABLE_NAME, SUM(COMPRESSED_SIZE) AS disk"
    " FROM information_schema.COLUMNAR_SEGMENTS WHERE DATABASE_NAME = %s{extra}"
    " GROUP BY TABLE_NAME"
)
_TABLES_SQL = (
    "SELECT TABLE_NAME, TABLE_TYPE, STORAGE_TYPE, DISTRIBUTED, TABLE_ROWS, CREATE_TIME, UPDATE_TIME,"
    " ALTER_TIME, TABLE_COMMENT, CREATE_USER"
    " FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s{extra}"
)


def _table_entries(database: str, table: str | None = None) -> list[dict[str, Any]]:
    extra, params = ("", (database,)) if table is None else (" AND TABLE_NAME = %s", (database, table))
    tables, stats_rows, disk_rows = db.parallel(
        lambda: _query(_TABLES_SQL.format(extra=extra), params),
        lambda: _query(_STATS_SQL.format(extra=extra), params),
        lambda: _query(_DISK_SQL.format(extra=extra), params),
    )
    if not tables:
        return []
    stats = {r["TABLE_NAME"]: r for r in stats_rows}
    disk = {r["TABLE_NAME"]: r for r in disk_rows}
    out = []
    for t in tables:
        name = t["TABLE_NAME"]
        is_view = t["TABLE_TYPE"] != "BASE TABLE"
        s = stats.get(name) or {}
        rows = _num(s.get("master_rows"))
        if rows is None:
            rows = _num(s.get("ref_rows"))
        mem = _num(s.get("master_mem"))
        if mem is None:
            mem = _num(s.get("ref_mem"))
        disk_bytes = _num((disk.get(name) or {}).get("disk"))
        if rows is None and not is_view:
            rows = _num(t["TABLE_ROWS"])
        size = None if is_view else (mem or 0) + (disk_bytes or 0)
        out.append(
            {
                "name": name,
                "type": t["TABLE_TYPE"],
                "storage": None if is_view else t["STORAGE_TYPE"],
                "reference": (not is_view) and t["DISTRIBUTED"] == 0,
                "rows": None if is_view else rows,
                "memory_bytes": None if is_view else mem,
                "disk_bytes": None if is_view else disk_bytes,
                "size_bytes": size,
                "created": t["CREATE_TIME"],
                "updated": t["UPDATE_TIME"],
                "altered": t["ALTER_TIME"],
                "comment": t["TABLE_COMMENT"] or None,
                "owner": t["CREATE_USER"] or None,
            }
        )
    out.sort(key=lambda r: (r["name"].lower(), r["name"]))
    return out


def _require_database(database: str) -> None:
    rows = _query("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (database,))
    if not rows:
        raise NotFoundError(f"Database {database!r} not found (names are case-sensitive).")


def list_tables(database: str) -> dict[str, Any]:
    tables = _table_entries(database)
    if not tables:
        _require_database(database)
    return {"database": database, "tables": tables}


# --------------------------------------------------------------------------
# One table's detail
# --------------------------------------------------------------------------

_KEY_RE = re.compile(
    r"(?P<kind>SHARD|SORT|PRIMARY|UNIQUE)?\s*KEY\s*(?P<name>`(?:[^`]|``)*`)?\s*\(",
    re.IGNORECASE,
)
_OTHER_KEY_RE = re.compile(
    r"(?P<kind>FULLTEXT|VECTOR)\b[^(`]*?(?:KEY|INDEX)?\s*(?P<name>`(?:[^`]|``)*`)?\s*\(",
    re.IGNORECASE,
)
_KEY_LINE_RE = re.compile(r"(?m)^\s*,?\s*((?:SHARD|SORT|PRIMARY|UNIQUE|FULLTEXT|KEY|VECTOR)\b.*)$", re.IGNORECASE)


def _paren_body(ddl: str, start: int) -> tuple[str, int]:
    """Text inside the parenthesis opened at ``start - 1``, skipping backtick/quoted names."""
    depth, i, quote = 1, start, None
    while i < len(ddl):
        ch = ddl[i]
        if quote:
            if ch == quote:
                if i + 1 < len(ddl) and ddl[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif ch in "`'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return ddl[start:i], i + 1
        i += 1
    return ddl[start:], len(ddl)


def _split_columns(body: str) -> list[str]:
    cols = []
    for m in re.finditer(r"`((?:[^`]|``)*)`(\s+DESC)?", body, re.IGNORECASE):
        cols.append(m.group(1).replace("``", "`") + (" DESC" if m.group(2) else ""))
    return cols


def parse_keys(ddl: str) -> dict[str, Any]:
    """Pull SHARD / SORT / PRIMARY / other keys out of SHOW CREATE TABLE output."""
    head = ddl.split("(", 1)[0].upper()
    keys: dict[str, Any] = {
        "reference": " REFERENCE " in f" {head} ",
        "shard": None,
        "sort": None,
        "primary": None,
        "indexes": [],
    }
    # Only scan the body's key lines (after the column list), i.e. lines that start with a key clause.
    for line_match in _KEY_LINE_RE.finditer(ddl):
        segment_start = line_match.start(1)
        m = _KEY_RE.match(ddl, segment_start) or _OTHER_KEY_RE.match(ddl, segment_start)
        if not m:
            continue
        body, end = _paren_body(ddl, m.end())
        tail = ddl[end : ddl.find("\n", end) if ddl.find("\n", end) != -1 else len(ddl)].strip().rstrip(",")
        kind = (m.group("kind") or "KEY").upper()
        name = m.group("name")
        name = name[1:-1].replace("``", "`") if name else None
        cols = _split_columns(body)
        entry = {"kind": kind, "name": name, "columns": cols, "using": tail or None}
        if kind == "SHARD":
            keys["shard"] = entry
        elif kind == "SORT":
            keys["sort"] = entry
        elif kind == "PRIMARY":
            keys["primary"] = entry
            keys["indexes"].append(entry)
        elif kind == "KEY" and tail and "CLUSTERED COLUMNSTORE" in tail.upper():
            keys["sort"] = {**entry, "kind": "SORT"}
        else:
            keys["indexes"].append(entry)
    if keys["shard"] is None and keys["primary"] is not None and not keys["reference"]:
        keys["shard"] = {**keys["primary"], "kind": "SHARD", "implicit": True}
    return keys


def _show_create(database: str, table: str) -> tuple[str | None, str | None]:
    try:
        cols, rows, _ = db.execute(f"SHOW CREATE TABLE {quote_identifier(database)}.{quote_identifier(table)}")
        return (rows[0][cols[1]] if rows and len(cols) > 1 else None), None
    except Exception as exc:  # noqa: BLE001 - DDL is optional; show the reason in the UI
        return None, str(exc)


def table_detail(database: str, table: str) -> dict[str, Any]:
    entries, column_rows, (ddl, ddl_error) = db.parallel(
        lambda: _table_entries(database, table),
        lambda: _query(
            "SELECT ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_KEY,"
            " COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (database, table),
        ),
        lambda: _show_create(database, table),
    )
    if not entries:
        _require_database(database)
        raise NotFoundError(f"Table {table!r} not found in database {database!r} (names are case-sensitive).")
    info = entries[0]
    columns = [
        {
            "position": r["ORDINAL_POSITION"],
            "name": r["COLUMN_NAME"],
            "type": r["COLUMN_TYPE"],
            "data_type": r["DATA_TYPE"],
            "nullable": r["IS_NULLABLE"] == "YES",
            "key": r["COLUMN_KEY"] or None,
            "default": r["COLUMN_DEFAULT"],
            "extra": r["EXTRA"] or None,
            "comment": r["COLUMN_COMMENT"] or None,
        }
        for r in column_rows
    ]
    is_view = info["type"] != "BASE TABLE"
    keys = parse_keys(ddl) if ddl and not is_view else None
    return {
        "database": database,
        "table": table,
        "info": info,
        "columns": columns,
        "ddl": ddl,
        "ddl_error": ddl_error,
        "keys": keys,
    }


def preview(database: str, table: str, limit: int = PREVIEW_ROWS) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    sql = f"SELECT * FROM {quote_identifier(database)}.{quote_identifier(table)} LIMIT {limit}"
    columns, rows, _ = db.execute(sql, max_rows=limit)
    clean = []
    for row in rows:
        out = {}
        for k, v in row.items():
            v = jsonable(v)
            if isinstance(v, str) and len(v) > _MAX_CELL_CHARS:
                v = v[:_MAX_CELL_CHARS] + "…"
            out[k] = v
        clean.append(out)
    return {"database": database, "table": table, "sql": sql, "columns": columns, "rows": clean, "limit": limit}


# --------------------------------------------------------------------------
# Model-facing summaries
# --------------------------------------------------------------------------


def _fmt_rows(n: Any) -> str:
    return "? rows" if n is None else f"{int(n):,} row{'' if int(n) == 1 else 's'}"


def _key_text(k: dict[str, Any] | None) -> str:
    if not k:
        return "none"
    if not k["columns"]:
        return "() (keyless)" if k["kind"] == "SHARD" else "() (unordered)"
    return "(" + ", ".join(k["columns"]) + ")" + (" implicit from PRIMARY KEY" if k.get("implicit") else "")


def _tables_summary(database: str, tables: list[dict[str, Any]], limit: int = 60) -> str:
    n_views = sum(t["type"] != "BASE TABLE" for t in tables)
    lines = [f"Database {database}: {len(tables) - n_views} table(s), {n_views} view(s)."]
    for t in tables[:limit]:
        if t["type"] == "BASE TABLE":
            kind = (t["storage"] or "").lower() + (" reference" if t["reference"] else "")
            lines.append(f"- {t['name']} ({kind}, {_fmt_rows(t['rows'])})")
        else:
            lines.append(f"- {t['name']} ({t['type'].lower()})")
    if len(tables) > limit:
        lines.append(f"... {len(tables) - limit} more (shown in the app)")
    return "\n".join(lines)


def _detail_summary(d: dict[str, Any]) -> str:
    info = d["info"]
    head = f"{d['database']}.{d['table']}: {info['type']}"
    if info["type"] == "BASE TABLE":
        head += f", {info['storage']}{' REFERENCE' if info['reference'] else ''}, {_fmt_rows(info['rows'])}"
    lines = [head]
    if d["keys"] and d["keys"]["reference"]:
        lines.append("Reference table (replicated to every node, no shard key)")
    elif d["keys"]:
        lines.append(f"Shard key: {_key_text(d['keys']['shard'])}; sort key: {_key_text(d['keys']['sort'])}")
    cols = ", ".join(f"{c['name']} {c['type']}" for c in d["columns"][:80])
    more = f" ... +{len(d['columns']) - 80} more" if len(d["columns"]) > 80 else ""
    lines.append(f"Columns ({len(d['columns'])}): {cols}{more}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@apps.tool(resource_uri=URI, title="Schema Explorer", annotations=_READ_ONLY)
def schema_explorer(database: str | None = None, table: str | None = None) -> CallToolResult:
    """Open an interactive Schema Explorer for the SingleStore cluster.

    Shows every database and its tables/views with storage type
    (columnstore/rowstore/reference), row counts and sizes; for a selected
    table it shows columns, shard key and sort key, the full CREATE TABLE DDL
    and a 50-row data preview. The user can browse freely and ask follow-up
    questions from the UI. Use this when the user wants to explore, browse or
    understand the schema visually. For a plain list of tables or columns
    inside your own reasoning, list_tables / describe_table are cheaper.

    The result includes ``browser_url``, which opens this view full-window in
    the user's browser: post it as a clickable link right under the app.

    Args:
        database: Pre-select this database (case-sensitive). Omit to start at the database list.
        table: Pre-select this table or view in `database` (case-sensitive). Requires `database`.
    """
    data: dict[str, Any] = {"databases": list_databases(), "database": database, "table": table}
    names = [d["name"] for d in data["databases"]]
    lines = [
        f"Schema Explorer opened ({len(names)} databases: "
        + ", ".join(d["name"] for d in data["databases"] if not d["system"])
        + ")."
    ]
    if table and not database:
        data["error"] = "A table was given without a database; pass both database and table."
    elif database:
        # The table list goes to the app via schema_explorer_tables rather than
        # into this model-visible result, which would make it large.
        try:
            _require_database(database)
            counts = next(d for d in data["databases"] if d["name"] == database)
            lines.append(f"Database {database}: {counts['tables']} table(s), {counts['views']} view(s), listed in the app.")
            if table:
                detail = table_detail(database, table)
                data["detail"] = detail
                lines.append(_detail_summary(detail))
        except NotFoundError as exc:
            data["error"] = str(exc)
    if data.get("error"):
        lines.append(f"Error: {data['error']}")
    summary = with_browser_link("\n".join(lines), data, "schema_explorer", {"database": database, "table": table})
    return tool_result(summary, data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_databases() -> CallToolResult:
    """List databases with table/view counts, for the Schema Explorer app."""
    dbs = list_databases()
    return tool_result(f"{len(dbs)} database(s)", {"databases": dbs})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_tables(database: str) -> CallToolResult:
    """Tables and views of one database with storage type, rows and size, for the Schema Explorer app."""
    data = list_tables(database)
    return tool_result(_tables_summary(database, data["tables"], limit=20), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_table(database: str, table: str) -> CallToolResult:
    """Columns, keys, DDL and stats of one table, for the Schema Explorer app."""
    data = table_detail(database, table)
    return tool_result(_detail_summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_preview(database: str, table: str, limit: int = PREVIEW_ROWS) -> CallToolResult:
    """First rows of a table or view, for the Schema Explorer app."""
    data = preview(database, table, limit)
    return tool_result(f"{len(data['rows'])} row(s) from {database}.{table}", data)


# --------------------------------------------------------------------------
# Skew: how evenly a table's rows and data are spread over the partitions and
# leaves. Computed on demand (a button / tab in the app), never on a tree click.
#
# Sources (read-only, as in the docs' "Detecting and Resolving Data Skew", 9.0):
# information_schema.TABLE_STATISTICS has one row per table per partition
# (ORDINAL = partition id, HOST/PORT = the leaf, PARTITION_TYPE Master /
# Slave / Reference, ROWS, MEMORY_USE); only the master copies are counted.
# On-disk columnstore size per partition comes from COLUMNAR_SEGMENTS
# (PARTITION, NODE_ID, COMPRESSED_SIZE); a partition's replica on another node
# has the same segments, so per partition the largest node's sum is taken.
# The docs' skew score is the relative standard deviation STDDEV/AVG (in %; under
# 10 % is no cause for worry); we show it next to (max - avg) / avg, which is how
# much bigger the largest partition is than an even share.
# --------------------------------------------------------------------------

SKEW_DOCS = "https://docs.singlestore.com/db/v9.0/create-a-database/detecting-and-resolving-data-skew/"
SHARD_KEY_DOCS = "https://docs.singlestore.com/db/v9.0/create-a-database/optimizing-table-data-structures/shard-keys/"
SKEW_MIN_ROWS = 10_000      # the docs only look at tables above this; skew in smaller ones doesn't matter
SKEW_OK_PCT = 10.0          # docs: under 10 % variation is no cause for worry
SKEW_HIGH_PCT = 25.0        # relative standard deviation from which we call it high
SKEW_HIGH_MAX_PCT = 100.0   # ... or the largest partition holds twice an even share
_TOP_PARTITIONS = 5


def _spread(values: list[float]) -> dict[str, Any]:
    """avg / min / max, the relative standard deviation (docs' skew score) and (max - avg) / avg, in %."""
    n = len(values)
    if not n:
        return {"avg": None, "min": None, "max": None, "rsd_pct": None, "max_over_avg_pct": None}
    avg = sum(values) / n
    var = sum((v - avg) ** 2 for v in values) / n  # population, like SingleStore's STDDEV
    if avg <= 0:
        return {"avg": 0, "min": min(values), "max": max(values), "rsd_pct": 0.0, "max_over_avg_pct": 0.0}
    return {
        "avg": round(avg, 1), "min": min(values), "max": max(values),
        "rsd_pct": round(var ** 0.5 / avg * 100, 1),
        "max_over_avg_pct": round((max(values) - avg) / avg * 100, 1),
    }


def skew_level(rows_total: float, rsd: float | None, max_over_avg: float | None) -> str:
    """"small" (too few rows to matter), "ok", "moderate" or "high"."""
    if rows_total < SKEW_MIN_ROWS or rsd is None:
        return "small"
    if rsd < SKEW_OK_PCT and (max_over_avg or 0) < 2 * SKEW_OK_PCT:
        return "ok"
    if rsd >= SKEW_HIGH_PCT or (max_over_avg or 0) >= SKEW_HIGH_MAX_PCT:
        return "high"
    return "moderate"


def skew_stats(partitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Skew of one table from its master partitions [{partition, host, rows, memory_bytes, disk_bytes}]."""
    parts = sorted(partitions, key=lambda p: p["partition"])
    for p in parts:
        p["data_bytes"] = (p.get("memory_bytes") or 0) + (p.get("disk_bytes") or 0)
    rows = [float(p["rows"] or 0) for p in parts]
    data = [float(p["data_bytes"]) for p in parts]
    total_rows, total_bytes = sum(rows), sum(data)
    for p in parts:
        p["rows_share_pct"] = round(float(p["rows"] or 0) / total_rows * 100, 1) if total_rows else 0.0
    leaves: dict[str, dict[str, Any]] = {}
    for p in parts:
        leaf = leaves.setdefault(p["host"] or "?", {"host": p["host"] or "?", "partitions": 0, "rows": 0, "data_bytes": 0})
        leaf["partitions"] += 1
        leaf["rows"] += int(p["rows"] or 0)
        leaf["data_bytes"] += int(p["data_bytes"])
    leaf_list = sorted(leaves.values(), key=lambda x: x["host"])
    by_rows = _spread(rows)
    return {
        "partitions": parts,
        "partition_count": len(parts),
        "rows_total": int(total_rows),
        "data_bytes_total": int(total_bytes),
        "rows": by_rows,
        "data": _spread(data),
        "leaves": leaf_list,
        "leaf_rows": _spread([float(x["rows"]) for x in leaf_list]),
        "largest": sorted(parts, key=lambda p: (-(p["rows"] or 0), p["partition"]))[:_TOP_PARTITIONS],
        "level": skew_level(total_rows, by_rows["rsd_pct"], by_rows["max_over_avg_pct"]),
    }


def _shard_sql(database: str, table: str, cols: list[str]) -> dict[str, str] | None:
    """Read-only checks of the shard key's values, for the user to run in the SQL Editor (not run here)."""
    if not cols:
        return None
    fq = f"{quote_identifier(database)}.{quote_identifier(table)}"
    names = ", ".join(quote_identifier(c.removesuffix(" DESC")) for c in cols)
    return {
        "top_values": f"SELECT {names}, COUNT(*) AS n FROM {fq} GROUP BY {names} ORDER BY n DESC LIMIT 20;",
        "cardinality": f"SELECT COUNT(*) AS n_rows, COUNT(DISTINCT {names}) AS n_distinct FROM {fq};",
    }


def skew_advice(stats: dict[str, Any], keys: dict[str, Any] | None, database: str, table: str) -> dict[str, Any]:
    """Plain-language reading of the numbers: what the skew means, the likely cause and what to do."""
    level = stats["level"]
    shard = (keys or {}).get("shard")
    cols = shard["columns"] if shard else []
    keyless = not cols
    notes: list[str] = []
    rsd, mx = stats["rows"]["rsd_pct"], stats["rows"]["max_over_avg_pct"]
    if level == "small":
        notes.append(f"Only {stats['rows_total']:,} rows: skew in a table this small doesn't affect performance "
                     f"(the docs look at tables over {SKEW_MIN_ROWS:,} rows).")
    elif level == "ok":
        notes.append(f"Rows are spread evenly: {rsd} % variation between partitions (under {SKEW_OK_PCT:g} % is no cause for worry).")
    else:
        notes.append(f"{'High' if level == 'high' else 'Some'} skew: {rsd} % variation between partitions; the largest "
                     f"partition holds {mx} % more rows than an even share. A query on this table is as slow as its "
                     "largest partition, and the leaf holding it fills up first.")
        if keyless:
            notes.append("The table is keyless (no shard key), so skew usually comes from INSERT … SELECT, which inserts "
                         "into the same partition the rows were read from, or is temporary after a big load or delete. "
                         "Redistribute by copying into a new table with INSERT INTO <new table> SELECT "
                         "WITH(force_random_reshuffle=1) * FROM <this table> and swapping the names.")
        else:
            key_txt = ", ".join(cols) + (" (from the PRIMARY KEY)" if shard.get("implicit") else "")
            notes.append(f"Rows are placed by the shard key ({key_txt}): every row with the same key value lands in the "
                         "same partition. Skew like this usually means a low-cardinality column (e.g. a status, country "
                         "or date) or a few very common values (NULL, 0, a default). Check the key's values with the "
                         "queries below.")
            notes.append("A shard key can't be changed with ALTER TABLE: create a new table with a more selective SHARD "
                         "KEY (as unique as possible, still the columns you join / filter on), INSERT … SELECT the data "
                         "into it and swap the names. To test a candidate key first, see the docs (\"reshard simulation\").")
    leaf = stats["leaf_rows"]
    if len(stats["leaves"]) > 1 and level not in ("small",) and (leaf["max_over_avg_pct"] or 0) >= SKEW_HIGH_MAX_PCT / 2 \
            and (stats["rows"]["rsd_pct"] or 0) < SKEW_OK_PCT:
        notes.append("Partitions are even but the leaves are not: partitions are placed unevenly over the leaves. "
                     f"An admin can check with EXPLAIN REBALANCE PARTITIONS ON {quote_identifier(database)} (read-only).")
    return {"level": level, "notes": notes, "keyless": keyless, "shard_columns": cols,
            "check_sql": _shard_sql(database, table, cols) if level in ("moderate", "high") else None,
            "docs": SKEW_DOCS, "shard_key_docs": SHARD_KEY_DOCS}


_SKEW_PART_SQL = (
    "SELECT TABLE_NAME, ORDINAL, HOST, PORT, PARTITION_TYPE, ROWS, MEMORY_USE FROM information_schema.TABLE_STATISTICS"
    " WHERE DATABASE_NAME = %s{extra} AND PARTITION_TYPE IN ('Master', 'Reference')"
)
_SKEW_DISK_SQL = (
    "SELECT TABLE_NAME, `PARTITION` AS part, NODE_ID, SUM(COMPRESSED_SIZE) AS disk"
    " FROM information_schema.COLUMNAR_SEGMENTS WHERE DATABASE_NAME = %s{extra} GROUP BY TABLE_NAME, `PARTITION`, NODE_ID"
)


def _skew_partitions(part_rows: list[dict[str, Any]], disk_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per table: {"reference": bool, "partitions": [...]} from TABLE_STATISTICS and COLUMNAR_SEGMENTS rows."""
    disk: dict[tuple[str, int], float] = {}
    for r in disk_rows:
        k = (r["TABLE_NAME"], int(r["part"]))
        disk[k] = max(disk.get(k, 0.0), float(_num(r["disk"]) or 0))  # the replica holds the same segments
    out: dict[str, dict[str, Any]] = {}
    for r in part_rows:
        t = out.setdefault(r["TABLE_NAME"], {"reference": False, "partitions": []})
        if r["PARTITION_TYPE"] == "Reference":
            t["reference"] = True
            continue
        ordinal = int(r["ORDINAL"])
        t["partitions"].append({
            "partition": ordinal,
            "host": f"{r['HOST']}:{r['PORT']}" if r.get("PORT") else r["HOST"],
            "rows": int(_num(r["ROWS"]) or 0),
            "memory_bytes": int(_num(r["MEMORY_USE"]) or 0),
            "disk_bytes": int(disk.get((r["TABLE_NAME"], ordinal), 0)),
        })
    return out


def table_skew(database: str, table: str) -> dict[str, Any]:
    params = (database, table)
    extra = " AND TABLE_NAME = %s"
    part_rows, disk_rows, (ddl, _err) = db.parallel(
        lambda: _query(_SKEW_PART_SQL.format(extra=extra), params),
        lambda: _query(_SKEW_DISK_SQL.format(extra=extra), params),
        lambda: _show_create(database, table),
    )
    keys = parse_keys(ddl) if ddl else None
    per = _skew_partitions(part_rows, disk_rows).get(table)
    base = {"database": database, "table": table, "keys": keys, "docs": SKEW_DOCS}
    if per is None:
        if ddl is None:
            _require_database(database)
            raise NotFoundError(f"Table {table!r} not found in database {database!r} (names are case-sensitive).")
        return {**base, "kind": "none", "message": "No partition statistics for this table (a view, or a table on no partitions)."}
    if per["reference"] or (keys or {}).get("reference"):
        return {**base, "kind": "reference",
                "message": "Reference table: every node holds a full copy, so it can't be skewed."}
    stats = skew_stats(per["partitions"])
    return {**base, "kind": "distributed", **stats, "advice": skew_advice(stats, keys, database, table)}


def database_skew(database: str) -> dict[str, Any]:
    """Skew of every distributed table in a database (2 queries), worst first, plus the rows per leaf."""
    part_rows, disk_rows = db.parallel(
        lambda: _query(_SKEW_PART_SQL.format(extra=""), (database,)),
        lambda: _query(_SKEW_DISK_SQL.format(extra=""), (database,)),
    )
    if not part_rows:
        _require_database(database)
    per = _skew_partitions(part_rows, disk_rows)
    tables, leaves = [], {}
    order = {"high": 0, "moderate": 1, "ok": 2, "small": 3}
    for name, t in per.items():
        if t["reference"] or not t["partitions"]:
            continue
        s = skew_stats(t["partitions"])
        tables.append({
            "table": name, "level": s["level"], "partitions": s["partition_count"], "rows_total": s["rows_total"],
            "data_bytes_total": s["data_bytes_total"], "rows_rsd_pct": s["rows"]["rsd_pct"],
            "rows_max_over_avg_pct": s["rows"]["max_over_avg_pct"], "data_rsd_pct": s["data"]["rsd_pct"],
            "largest_partition": s["largest"][0]["partition"] if s["largest"] else None,
            "largest_rows": s["largest"][0]["rows"] if s["largest"] else None,
        })
        for leaf in s["leaves"]:
            agg = leaves.setdefault(leaf["host"], {"host": leaf["host"], "rows": 0, "data_bytes": 0})
            agg["rows"] += leaf["rows"]
            agg["data_bytes"] += leaf["data_bytes"]
    tables.sort(key=lambda x: (order[x["level"]], -(x["rows_rsd_pct"] or 0), x["table"]))
    leaf_list = sorted(leaves.values(), key=lambda x: x["host"])
    return {
        "database": database, "tables": tables, "leaves": leaf_list,
        "leaf_rows": _spread([float(x["rows"]) for x in leaf_list]),
        "leaf_data": _spread([float(x["data_bytes"]) for x in leaf_list]),
        "reference_tables": sorted(n for n, t in per.items() if t["reference"]),
        "counts": {lvl: sum(t["level"] == lvl for t in tables) for lvl in order},
        "docs": SKEW_DOCS, "min_rows": SKEW_MIN_ROWS,
    }


def _skew_summary(d: dict[str, Any]) -> str:
    if "tables" in d:
        c = d["counts"]
        lines = [f"Skew in {d['database']}: {len(d['tables'])} distributed table(s); {c['high']} high, {c['moderate']} moderate, "
                 f"{c['ok']} even, {c['small']} too small to matter."]
        for t in d["tables"][:15]:
            if t["level"] in ("high", "moderate"):
                lines.append(f"- {t['table']}: {t['level']}, {t['rows_rsd_pct']} % variation, largest partition "
                             f"+{t['rows_max_over_avg_pct']} % over an even share ({t['rows_total']:,} rows)")
        return "\n".join(lines)
    if d["kind"] != "distributed":
        return f"{d['database']}.{d['table']}: {d['message']}"
    return (f"{d['database']}.{d['table']}: skew {d['level']} - {d['rows']['rsd_pct']} % variation over "
            f"{d['partition_count']} partitions, largest +{d['rows']['max_over_avg_pct']} % over an even share. "
            + " ".join(d["advice"]["notes"][:2]))


# Reshard simulation: how the rows would spread with another shard key, without changing anything. The docs'
# method ("Show the data distribution for a table if you were to reshard on another column or set of columns"):
# GROUP BY the candidate key reshuffles the groups to the partitions that key hashes to, and the outer GROUP BY
# PARTITION_ID() (run on the leaves) counts the rows that land on each. It reads the whole table, so the app only
# runs it on an explicit click; a sample (WHERE RAND() < fraction) reads every row but moves and groups fewer.
RESHARD_MAX_COLUMNS = 8
RESHARD_DOCS = SKEW_DOCS  # its section "Show the data distribution for a table if you were to reshard on ..."


def reshard_sql(database: str, table: str, columns: list[str], fraction: float | None = None) -> str:
    """The read-only simulation query for a candidate shard key (``fraction``: sample that share of the rows)."""
    if not columns:
        raise ValueError("Pick at least one column for the candidate shard key.")
    if len(columns) > RESHARD_MAX_COLUMNS:
        raise ValueError(f"A candidate shard key of at most {RESHARD_MAX_COLUMNS} columns.")
    if len(set(columns)) != len(columns):
        raise ValueError("Each column once in the candidate shard key.")
    fq = f"{quote_identifier(database)}.{quote_identifier(table)}"
    key = ", ".join(quote_identifier(c) for c in columns)
    where = ""
    if fraction is not None:
        if not 0 < fraction < 1:
            raise ValueError("The sample fraction must be between 0 and 1.")
        where = f" WHERE RAND() < {fraction:.6g}"
    return (f"SELECT WITH(leaf_pushdown=true) SUM(c) AS n_rows, COUNT(*) AS n_keys, PARTITION_ID() AS part"
            f" FROM (SELECT COUNT(*) AS c FROM {fq}{where} GROUP BY {key}) reshuffle GROUP BY PARTITION_ID()")


def reshard_result(current: dict[str, Any], rows: list[dict[str, Any]], columns: list[str],
                   fraction: float | None) -> dict[str, Any]:
    """Predicted rows per partition (scaled up from a sample), skew next to the current one, and a reading."""
    counts = {int(r["part"]): (float(_num(r["n_rows"]) or 0), int(_num(r["n_keys"]) or 0)) for r in rows}
    parts_now = {p["partition"]: p for p in current.get("partitions") or []}
    ids = sorted(set(parts_now) | set(counts))
    scale = 1 / fraction if fraction else 1.0
    predicted = [{"partition": i, "host": (parts_now.get(i) or {}).get("host"), "rows": round(counts.get(i, (0, 0))[0] * scale),
                  "keys": counts.get(i, (0, 0))[1], "current_rows": (parts_now.get(i) or {}).get("rows")} for i in ids]
    stats = skew_stats([{**p, "memory_bytes": 0, "disk_bytes": 0} for p in predicted])
    sampled_rows = int(sum(c[0] for c in counts.values()))
    keys = sum(c[1] for c in counts.values())
    level, cur_rsd, new_rsd = stats["level"], (current.get("rows") or {}).get("rsd_pct"), stats["rows"]["rsd_pct"]
    notes = []
    col_txt = ", ".join(columns)
    if keys and keys < 10 * max(1, len(ids)):
        notes.append(f"Only {keys:,} distinct value(s) of ({col_txt}){' in the sample' if fraction else ''} for "
                     f"{len(ids)} partitions: too few to spread evenly - pick more selective columns.")
    if cur_rsd is not None and new_rsd is not None:
        better = new_rsd < cur_rsd - 1
        worse = new_rsd > cur_rsd + 1
        notes.append(f"Sharding on ({col_txt}) would give {new_rsd} % variation between partitions, against {cur_rsd} % now"
                     f" ({'more even' if better else 'less even' if worse else 'about the same'}).")
    if level == "small":
        notes.append(f"The table has under {SKEW_MIN_ROWS:,} rows, so skew doesn't matter for it either way.")
    if fraction:
        notes.append(f"Estimated from a sample of about {fraction * 100:.3g} % of the rows ({sampled_rows:,} rows), scaled up.")
    return {"columns": columns, "partitions": predicted, "partition_count": len(ids), "rows": stats["rows"],
            "level": level, "rows_total": stats["rows_total"], "distinct_keys": keys, "sampled_rows": sampled_rows,
            "fraction": fraction, "largest": stats["largest"], "notes": notes,
            "current": {"level": current.get("level"), "rows": current.get("rows"), "rows_total": current.get("rows_total")}}


def reshard_simulation(database: str, table: str, columns: list[str], sample_rows: int | None = None) -> dict[str, Any]:
    """Run the reshard simulation for one candidate shard key (read-only)."""
    if not isinstance(columns, list) or not all(isinstance(c, str) and c for c in columns):
        raise ValueError("columns must be a list of column names.")
    current, cols = db.parallel(
        lambda: table_skew(database, table),
        lambda: _query("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                       (database, table)),
    )
    known = {r["COLUMN_NAME"] for r in cols}
    missing = [c for c in columns if c not in known]
    if missing:
        raise NotFoundError(f"No column {', '.join(missing)} in {database}.{table} (names are case-sensitive).")
    if current["kind"] != "distributed":
        raise ValueError(f"{database}.{table}: {current.get('message') or 'not a distributed table'} Nothing to simulate.")
    total = current["rows_total"]
    fraction = None
    if sample_rows:
        if sample_rows < 1:
            raise ValueError("The sample size must be a positive number of rows.")
        if total and sample_rows < total:
            fraction = sample_rows / total
    sql = reshard_sql(database, table, columns, fraction)
    started = time.monotonic()
    rows = _query(sql)
    out = reshard_result(current, rows, columns, fraction)
    return {"database": database, "table": table, "sql": sql, "elapsed_ms": round((time.monotonic() - started) * 1000),
            "current_shard": ((current.get("keys") or {}).get("shard") or {}).get("columns") or [],
            "docs": RESHARD_DOCS, **out}


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_reshard(database: str, table: str, columns: list[str], sample_rows: int | None = None) -> CallToolResult:
    """Reshard simulation for the Distribution tab: how a table's rows would spread over the partitions with
    another shard key (``columns``), next to the current skew - nothing is changed. Uses the docs' query (GROUP BY the
    candidate key, then GROUP BY PARTITION_ID() on the leaves), which scans the whole table: the app runs it only
    on an explicit click. ``sample_rows``: estimate from about that many rows (a random sample, scaled up).
    """
    d = reshard_simulation(database, table, columns, sample_rows)
    head = (f"{database}.{table} sharded on ({', '.join(columns)}): {d['level']}, {d['rows']['rsd_pct']} % variation over "
            f"{d['partition_count']} partitions (now {(d['current']['rows'] or {}).get('rsd_pct')} %).")
    return tool_result(" ".join([head, *d["notes"]]), d)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_skew(database: str, table: str | None = None) -> CallToolResult:
    """Data skew, for the Schema Explorer's Distribution tab / database "Check skew": rows and data per
    partition and per leaf, skew % (relative standard deviation and (max - avg) / avg), the largest
    partitions, the shard key and a plain explanation. Read-only (TABLE_STATISTICS, COLUMNAR_SEGMENTS).

    Args:
        database: Database (case-sensitive).
        table: One table (case-sensitive); omit for every table of the database.
    """
    data = table_skew(database, table) if table else database_skew(database)
    return tool_result(_skew_summary(data), data)


# --------------------------------------------------------------------------
# Clean up: leftover SAS work tables (logic in ..housekeeping)
# --------------------------------------------------------------------------


@apps.tool(resource_uri=URI, title="Clean up work tables", annotations=_READ_ONLY)
def cleanup_work_tables() -> CallToolResult:
    """Open the Schema Explorer's Clean-up panel for leftover SAS work tables.

    SAS jobs leave work tables behind in SingleStore (SAS Data Management
    ``_dm…`` tables, SAS flow ``_flw…`` tables, ``SASTMP…`` / ``_tmp…``). The
    panel scans every non-system database by name pattern (editable, and
    optionally every table older than N days) and shows per table its rows,
    size, creator, age, last use in the query history and dependent views.
    The user selects tables, confirms, and they are dropped one by one (with a
    dry-run mode and a log). Nothing is dropped by this call; only the user
    can drop from the panel. Use this when the user wants to find or clean up
    leftover, temporary or SAS work tables.

    The result includes ``browser_url``, which opens this view full-window in
    the user's browser: post it as a clickable link right under the app.
    """
    data: dict[str, Any] = {"databases": list_databases(), "mode": "cleanup",
                            "cleanup_settings": housekeeping.load_settings()}
    summary = "Schema Explorer opened in Clean-up mode; it scans for leftover work tables in the app."
    summary = with_browser_link(summary, data, "cleanup_work_tables", {})
    return tool_result(summary, data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_cleanup_scan(
    patterns: list[dict[str, str]] | None = None,
    older_than_days: int | None = None,
    recent_days: int | None = None,
    save: bool = False,
) -> CallToolResult:
    """Find leftover work tables by name pattern (and age), for the Schema Explorer's Clean-up panel.

    Args:
        patterns: [{"pattern": "_dm*", "kind": "SAS DM"}, ...]; shell-style, case-insensitive. Omit for the saved ones.
        older_than_days: Also offer every table not created/altered for this many days.
        recent_days: Tables used or created this recently are flagged as risky.
        save: Store these settings in ~/.singlestore-mcp/cleanup.json.
    """
    if save:
        housekeeping.save_settings({"patterns": patterns if patterns is not None else housekeeping.load_settings()["patterns"],
                                    "older_than_days": older_than_days, "recent_days": recent_days})
    data = housekeeping.scan(patterns, older_than_days, recent_days)
    data["settings"] = housekeeping.load_settings()
    data["log"] = housekeeping.read_log(20)
    return tool_result(housekeeping.scan_summary(data, limit=10), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_cleanup_settings(dry_run: bool | None = None) -> CallToolResult:
    """Read (or change the dry-run default of) the Clean-up settings, plus recent log entries."""
    s = housekeeping.save_settings({"dry_run": dry_run}) if dry_run is not None else housekeeping.load_settings()
    return tool_result("Clean-up settings", {"settings": s, "log": housekeeping.read_log(50)})


@apps.tool(
    resource_uri=URI,
    visibility=APP_ONLY,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
)
def schema_explorer_cleanup_drop(
    tables: list[dict[str, str]],
    views: list[dict[str, str]] | None = None,
    dry_run: bool = True,
) -> CallToolResult:
    """Drop the selected work tables one statement at a time (DROP TABLE IF EXISTS), for the Clean-up panel.

    Re-checks each table on the server; system databases are refused and a table
    with a dependent view is skipped unless that view is listed in ``views``
    (it is then dropped first). With ``dry_run`` only the statements are returned.

    Args:
        tables: [{"database": "SASDP", "name": "_dmallchars"}, ...]
        views: Dependent views to drop as well, same shape.
        dry_run: Only show the statements; drop nothing.
    """
    data = housekeeping.drop(tables, views, dry_run)
    return tool_result(housekeeping.drop_summary(data), data)


# --------------------------------------------------------------------------
# ANALYZE TABLE: refresh optimizer statistics. It changes a shared cluster, so
# there is a read-only plan first and an explicit confirmation before it runs.
#
# Where SingleStore 9.0 shows statistics: information_schema.OPTIMIZER_STATISTICS,
# one row per column (and JSON key), filled by autostats or ANALYZE: ROW_COUNT,
# AUTOSTATS_ENABLED, AUTOSTATS_OUTDATED (autostats too stale to use; the optimizer
# then falls back to the last ANALYZE), RANGE_STATS (histograms) and LAST_UPDATED
# (documented as when autostats last updated the column; on 9.0.40 ANALYZE TABLE sets
# it too, checked on SASDP.CLASS). Per the docs, ANALYZE TABLE also invalidates and
# re-optimizes plans built on stale statistics.
# --------------------------------------------------------------------------

ANALYZE_BATCH = 10
_STALE_ROW_CHANGE = 0.2  # the table's row count moved this far from the statistics' row count


def _analyze_statement(database: str, table: str) -> str:
    return f"ANALYZE TABLE {quote_identifier(database)}.{quote_identifier(table)}"


def _stats_by_table(database: str) -> dict[str, dict[str, Any]]:
    rows = _query(
        "SELECT TABLE_NAME, COUNT(DISTINCT COLUMN_NAME) AS columns_with_stats, MIN(LAST_UPDATED) AS oldest,"
        " MAX(LAST_UPDATED) AS newest, SUM(AUTOSTATS_OUTDATED) AS outdated, SUM(AUTOSTATS_ENABLED) AS autostats_on,"
        " SUM(RANGE_STATS) AS histograms, MAX(ROW_COUNT) AS stats_rows, COUNT(*) AS stat_rows"
        " FROM information_schema.OPTIMIZER_STATISTICS WHERE DATABASE_NAME = %s GROUP BY TABLE_NAME",
        (database,),
    )
    return {r["TABLE_NAME"]: r for r in rows}


def _age_days(now: Any, then: Any) -> float | None:
    """Days between two cluster timestamps (both from the cluster's clock, no time zone)."""
    try:
        a = datetime.datetime.fromisoformat(str(now))
        b = datetime.datetime.fromisoformat(str(then))
    except (TypeError, ValueError):
        return None
    return round((a - b).total_seconds() / 86400, 1)


def _table_stats(entry: dict[str, Any], n_columns: int, s: dict[str, Any] | None, now: Any) -> dict[str, Any]:
    """Statistics facts for one table and the reasons they may be stale."""
    s = s or {}
    with_stats = int(_num(s.get("columns_with_stats")) or 0)
    stat_rows = int(_num(s.get("stat_rows")) or 0)
    outdated = int(_num(s.get("outdated")) or 0)
    autostats_on = int(_num(s.get("autostats_on")) or 0)
    stats_rows = _num(s.get("stats_rows"))
    rows_now = entry.get("rows")
    stats = {
        "columns": n_columns,
        "columns_with_stats": with_stats,
        "outdated_columns": outdated,
        "autostats_columns": autostats_on,
        "histogram_columns": int(_num(s.get("histograms")) or 0),
        "stats_row_count": stats_rows,
        "current_row_count": rows_now,
        "last_updated_oldest": s.get("oldest"),
        "last_updated_newest": s.get("newest"),
        "age_days": _age_days(now, s.get("newest")) if s.get("newest") else None,
    }
    reasons = []
    if not with_stats:
        reasons.append("no statistics")
    else:
        if outdated:
            reasons.append(f"autostats out of date on {outdated} column(s)")
        if with_stats < n_columns:
            reasons.append(f"{n_columns - with_stats} of {n_columns} column(s) without statistics")
        if autostats_on < stat_rows:
            reasons.append("autostats off on some columns (the optimizer uses the last ANALYZE)")
        if rows_now is not None and stats_rows is not None:
            now_f, then_f = float(rows_now), float(stats_rows)
            if max(now_f, then_f) >= 1000 and abs(now_f - then_f) / max(then_f, 1.0) > _STALE_ROW_CHANGE:
                reasons.append(f"row count changed from {int(then_f):,} to {int(now_f):,}")
    return {"stats": stats, "stale": bool(reasons), "reasons": reasons}


def analyze_plan(database: str, tables: list[str] | None = None) -> dict[str, Any]:
    if database in SYSTEM_DATABASES:
        raise ValueError(f"{database} is a system database; ANALYZE is only for user tables.")
    entries, stats, col_rows, now_rows = db.parallel(
        lambda: _table_entries(database),
        lambda: _stats_by_table(database),
        lambda: _query("SELECT TABLE_NAME, COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s"
                       " GROUP BY TABLE_NAME", (database,)),
        lambda: _query("SELECT NOW() AS now"),
    )
    if not entries:
        _require_database(database)
    now = now_rows[0]["now"] if now_rows else None
    n_cols = {r["TABLE_NAME"]: int(_num(r["n"]) or 0) for r in col_rows}
    by_name = {e["name"]: e for e in entries}
    wanted = list(dict.fromkeys(tables)) if tables else [e["name"] for e in entries]
    out = []
    for name in wanted:
        e = by_name.get(name)
        if e is None or e["type"] != "BASE TABLE":
            continue
        out.append({
            "table": name,
            "storage": e["storage"],
            "reference": e["reference"],
            "rows": e["rows"],
            "size_bytes": e["size_bytes"],
            **_table_stats(e, n_cols.get(name, 0), stats.get(name), now),
            "statement": _analyze_statement(database, name) + ";",
        })
    return {
        "database": database,
        "cluster_now": now,
        "tables": out,
        "stale_tables": sum(t["stale"] for t in out),
        "statements": [t["statement"] for t in out],
        "missing": [t for t in wanted if t not in by_name],
        "skipped_views": [t for t in wanted if t in by_name and by_name[t]["type"] != "BASE TABLE"],
        "batch_size": ANALYZE_BATCH,
        "about": (
            "Read-only plan; nothing ran. Statistics come from information_schema.OPTIMIZER_STATISTICS (one row per "
            "column, filled by autostats or ANALYZE). last_updated_* is its LAST_UPDATED: documented as when autostats "
            "last updated the column; ANALYZE TABLE sets it too (seen on 9.0.40). A table counts as stale when it has no "
            "statistics, autostats are out of date, columns lack statistics, autostats are off on some columns (the "
            "optimizer then uses the last ANALYZE), or its row count moved more than 20% from the statistics' row count. "
            "ANALYZE TABLE reads the table to rebuild its statistics and invalidates cached plans built on stale ones."
        ),
        "next_step": (
            "After the user has seen these statements and explicitly confirmed, call schema_explorer_analyze("
            f"database={database!r}, tables=[...], confirm=True) with at most {ANALYZE_BATCH} tables per call "
            "(one table per call shows progress best)."
        ),
    }


def _analyze_plan_summary(d: dict[str, Any], limit: int = 30) -> str:
    lines = [f"ANALYZE plan for {d['database']} (read-only, nothing ran): {len(d['tables'])} table(s), "
             f"{d['stale_tables']} with stale or missing statistics."]
    for t in d["tables"][:limit]:
        s = t["stats"]
        age = f"statistics updated {s['age_days']} days ago" if s["age_days"] is not None else "no statistics"
        why = "; ".join(t["reasons"])
        lines.append(f"- {t['table']}: {age}{'; ' + why if why else ''} -> {t['statement']}")
    if len(d["tables"]) > limit:
        lines.append(f"... {len(d['tables']) - limit} more")
    if d["missing"]:
        lines.append("Not found: " + ", ".join(d["missing"]))
    if d["skipped_views"]:
        lines.append("Views skipped (no statistics): " + ", ".join(d["skipped_views"]))
    lines.append(d["next_step"])
    return "\n".join(lines)


def analyze(database: str, tables: list[str], confirm: bool = False) -> dict[str, Any]:
    if not tables:
        raise ValueError("Name the tables to analyze (schema_explorer_analyze_plan lists them).")
    tables = list(dict.fromkeys(tables))
    if len(tables) > ANALYZE_BATCH:
        raise ValueError(f"At most {ANALYZE_BATCH} tables per call; call again for the rest.")
    if database in SYSTEM_DATABASES:
        raise ValueError(f"{database} is a system database; ANALYZE is only for user tables.")
    if not confirm:
        raise ValueError(
            "Refused: ANALYZE TABLE changes statistics on the shared cluster. Show the user the plan "
            "(schema_explorer_analyze_plan) and call again with confirm=True only after they explicitly confirmed."
        )
    placeholders = ", ".join(["%s"] * len(tables))
    found_rows, before = db.parallel(
        lambda: _query("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s"
                       f" AND TABLE_NAME IN ({placeholders})", (database, *tables)),
        lambda: _stats_by_table(database),
    )
    found = {r["TABLE_NAME"]: r["TABLE_TYPE"] for r in found_rows}
    results = []
    for name in tables:
        sql = _analyze_statement(database, name)
        entry: dict[str, Any] = {"table": name, "statement": sql + ";"}
        if name not in found:
            entry.update(status="missing", error="Table not found (names are case-sensitive).")
        elif found[name] != "BASE TABLE":
            entry.update(status="skipped", error=f"A {found[name].lower()} has no statistics to analyze.")
        else:
            started = time.monotonic()
            try:
                cols, rows, _ = db.execute(sql, database=database)
                entry.update(status="analyzed", output=jsonable_rows(rows)[:20] if cols else None)
            except Exception as exc:  # noqa: BLE001 - reported per table; the others still run
                entry.update(status="error", error=str(exc))
            entry["seconds"] = round(time.monotonic() - started, 2)
        results.append(entry)
    after, now_rows = db.parallel(lambda: _stats_by_table(database), lambda: _query("SELECT NOW() AS now"))
    for e in results:
        b, a = before.get(e["table"]) or {}, after.get(e["table"]) or {}
        e["last_updated_before"] = b.get("newest")
        e["last_updated_after"] = a.get("newest")
        e["columns_with_stats_after"] = int(_num(a.get("columns_with_stats")) or 0)
        e["outdated_columns_after"] = int(_num(a.get("outdated")) or 0)
    return {
        "database": database,
        "cluster_now": now_rows[0]["now"] if now_rows else None,
        "results": results,
        "analyzed": sum(e["status"] == "analyzed" for e in results),
        "errors": sum(e["status"] == "error" for e in results),
        "about": ("Ran one ANALYZE TABLE per table, in order. last_updated_before / _after are "
                  "OPTIMIZER_STATISTICS.LAST_UPDATED (newest column) before and after."),
    }


def _analyze_summary(d: dict[str, Any]) -> str:
    lines = [f"ANALYZE in {d['database']}: {d['analyzed']} analyzed, {d['errors']} error(s)."]
    for e in d["results"]:
        took = f" in {e['seconds']} s" if "seconds" in e else ""
        lines.append(f"- {e['table']}: {e['status']}{took}{' - ' + e['error'] if e.get('error') else ''}")
    return "\n".join(lines)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=_READ_ONLY)
def schema_explorer_analyze_plan(database: str, tables: list[str] | None = None) -> CallToolResult:
    """Read-only plan for ANALYZE TABLE: per base table its statistics (age, outdated / missing columns,
    row-count drift), whether they look stale and why, and the exact statements. Runs nothing.

    Args:
        database: Database (case-sensitive).
        tables: Tables to include (case-sensitive). Omit for every base table in the database.
    """
    data = analyze_plan(database, tables)
    return tool_result(_analyze_plan_summary(data), data)


@apps.tool(
    resource_uri=URI,
    visibility=APP_ONLY,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)
def schema_explorer_analyze(database: str, tables: list[str], confirm: bool = False) -> CallToolResult:
    """Run ANALYZE TABLE on the given tables, one statement per table, and report each result.

    Refuses unless ``confirm`` is true: call it only after the user saw the plan
    (schema_explorer_analyze_plan) and explicitly confirmed. At most 10 tables per call.

    Args:
        database: Database (case-sensitive).
        tables: Base tables to analyze (case-sensitive), at most 10.
        confirm: Must be true: the user's explicit confirmation.
    """
    data = analyze(database, tables, confirm)
    return tool_result(_analyze_summary(data), data)
