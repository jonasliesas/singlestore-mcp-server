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
"""

from __future__ import annotations

import re
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

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


def list_databases() -> list[dict[str, Any]]:
    schemata = _query("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
    counts = _query(
        "SELECT TABLE_SCHEMA,"
        " SUM(TABLE_TYPE = 'BASE TABLE') AS n_tables,"
        " SUM(TABLE_TYPE <> 'BASE TABLE') AS n_views"
        " FROM information_schema.TABLES GROUP BY TABLE_SCHEMA"
    )
    by_db = {r["TABLE_SCHEMA"]: r for r in counts}
    out = []
    for r in schemata:
        name = r["SCHEMA_NAME"]
        c = by_db.get(name, {})
        out.append(
            {
                "name": name,
                "system": name in SYSTEM_DATABASES,
                "tables": int(_num(c.get("n_tables")) or 0),
                "views": int(_num(c.get("n_views")) or 0),
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
    tables = _query(_TABLES_SQL.format(extra=extra), params)
    if not tables:
        return []
    stats = {r["TABLE_NAME"]: r for r in _query(_STATS_SQL.format(extra=extra), params)}
    disk = {r["TABLE_NAME"]: r for r in _query(_DISK_SQL.format(extra=extra), params)}
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


def table_detail(database: str, table: str) -> dict[str, Any]:
    entries = _table_entries(database, table)
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
        for r in _query(
            "SELECT ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE, COLUMN_KEY,"
            " COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (database, table),
        )
    ]
    ddl = None
    ddl_error = None
    try:
        cols, rows, _ = db.execute(f"SHOW CREATE TABLE {quote_identifier(database)}.{quote_identifier(table)}")
        if rows and len(cols) > 1:
            ddl = rows[0][cols[1]]
    except Exception as exc:  # noqa: BLE001 - DDL is optional; show the reason in the UI
        ddl_error = str(exc)
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
