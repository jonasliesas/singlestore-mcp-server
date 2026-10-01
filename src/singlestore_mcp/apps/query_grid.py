"""Query Results Grid app: run a read-only query and browse the result.

The UI re-runs edited SQL by calling ``query_grid`` itself, so the read-only
guard below protects both the model's calls and the UI's Run button.
"""

from __future__ import annotations

import re
import time
from typing import Any

import singlestoredb as s2
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ToolAnnotations

from ..db import db
from ._core import (
    APP_ONLY,
    apps,
    jsonable_rows,
    register_app,
    rows_as_text,
    stash,
    tool_result,
    unstash,
    with_browser_link,
)

URI = "ui://singlestore/query-grid.html"
MAX_ROWS_LIMIT = 10_000
DEFAULT_MAX_ROWS = 1_000

register_app(
    URI,
    "query_grid.html",
    name="Query Results Grid",
    description="Sortable, filterable, exportable grid of a read-only SQL query's results",
)

_ALLOWED_FIRST = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
# Keywords that would make a SELECT/WITH/EXPLAIN write something (SELECT ...
# INTO OUTFILE/KAFKA/S3/@var, a data-modifying statement after WITH, EXPLAIN
# ANALYZE, ...). Checked only outside strings, quoted identifiers and comments,
# and not when used as a function call (REPLACE(...), INSERT(...), TRUNCATE(...)).
_WRITE_WORDS = {
    "INTO", "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
    "TRUNCATE", "CALL", "ANALYZE",
}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# String literals, quoted identifiers and comments, in the order MySQL lexes them.
_LEXEME = re.compile(
    r"""
      (?P<comment>/\*.*?\*/|--[ \t][^\n]*|--$|--\n|\#[^\n]*)
    | (?P<str>'(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.|"")*")
    | (?P<ident>`(?:[^`]|``)*`)
    """,
    re.VERBOSE | re.DOTALL | re.MULTILINE,
)


# Raised as ToolError so the message always reaches the model and the UI (the
# SDK masks other exception types unless _core's wrapper translates them).
class ReadOnlyViolation(ToolError, ValueError):
    pass


def _db_error_text(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 2 and isinstance(args[0], int):
        return f"SingleStore error {args[0]}: {args[1]}"
    return f"SingleStore error: {exc}"


def _reject(reason: str) -> None:
    raise ReadOnlyViolation(
        f"query_grid only runs read-only statements (SELECT, WITH, SHOW, DESCRIBE, DESC, EXPLAIN): {reason}. "
        "Use run_sql for anything that changes data or schema."
    )


def check_read_only(sql: str) -> None:
    """Raise ReadOnlyViolation unless ``sql`` is a single read-only statement."""
    if "/*!" in sql:
        _reject("MySQL executable comments (/*! ... */) are not allowed")
    # Blank out comments, string literals and quoted identifiers so keyword
    # checks only look at real SQL tokens.
    code = _LEXEME.sub(lambda m: " " if m.group("comment") else " ? ", sql)
    if "'" in code or '"' in code or "`" in code:
        _reject("unterminated quote")
    statement = code.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if ";" in statement:
        _reject("only one statement per call")
    statement = statement.lstrip("( \t\r\n")  # allow "(SELECT ...) UNION (SELECT ...)"
    words = list(_WORD.finditer(statement))
    if not words or words[0].start() != 0:
        _reject("the statement must start with a keyword")
    first = words[0].group().upper()
    if first not in _ALLOWED_FIRST:
        _reject(f"{first} statements are not allowed")
    if first in ("SELECT", "WITH", "EXPLAIN"):
        bad = sorted(
            {
                w.group().upper()
                for w in words
                if w.group().upper() in _WRITE_WORDS
                and not statement[w.end():].lstrip().startswith("(")
            }
        )
        if bad:
            _reject(f"found {', '.join(bad)}")


def run_query(sql: str, database: str | None, max_rows: int) -> dict[str, Any]:
    check_read_only(sql)
    max_rows = max(1, min(int(max_rows), MAX_ROWS_LIMIT))
    started = time.perf_counter()
    # One extra row tells us whether the result was cut off. An explicit LIMIT
    # in the SQL overrides sql_select_limit, so slice in Python as well.
    try:
        columns, rows, _ = db.execute(sql, database=database, max_rows=max_rows + 1)
    except s2.Error as exc:
        raise ToolError(_db_error_text(exc)) from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    truncated = len(rows) > max_rows
    rows = jsonable_rows(rows[:max_rows])
    # Rows are dicts; singlestoredb keys a repeated column name as "table.col"
    # (or ".col"), so the row keys are the reliable column list when present.
    warnings = []
    unique_columns = list(dict.fromkeys(columns))
    if len(unique_columns) != len(columns):
        if rows and len(rows[0]) == len(columns):
            unique_columns = list(rows[0].keys())
        dupes = sorted({c for c in columns if columns.count(c) > 1})
        warnings.append(f"Duplicate column names ({', '.join(dupes)}); alias them for clearer headers.")
    return {
        "sql": sql,
        "database": database,
        "columns": unique_columns,
        "has_result_set": bool(columns),
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "max_rows": max_rows,
        "elapsed_ms": elapsed_ms,
        "warnings": warnings,
    }


def _summary(data: dict[str, Any]) -> str:
    if not data["has_result_set"]:
        return f"Statement ran in {data['elapsed_ms']} ms and returned no result set."
    head = f"{data['row_count']} row(s) x {len(data['columns'])} column(s) in {data['elapsed_ms']} ms"
    if data["truncated"]:
        head += f"; TRUNCATED at max_rows={data['max_rows']} (more rows exist)"
    head += ". Full result is shown in the Query Results Grid app."
    parts = [head, *data["warnings"], rows_as_text(data["columns"], data["rows"], limit=20)]
    return "\n".join(parts)


@apps.tool(resource_uri=URI, title="Query Results Grid", annotations=ToolAnnotations(readOnlyHint=True))
def query_grid(sql: str, database: str | None = None, max_rows: int = DEFAULT_MAX_ROWS) -> CallToolResult:
    """Run a read-only SQL query and show the results in an interactive grid.

    Use this when the user wants to see, browse, sort, filter or export
    (CSV) query results, or when a result is too large to read as text. The
    user can also edit and re-run the query from the grid. You receive only
    a summary with the first 20 rows; the user sees all fetched rows.

    Only read-only statements are accepted: SELECT, WITH, SHOW, DESCRIBE,
    DESC, EXPLAIN (one statement, no SELECT ... INTO). For writes, DDL or when
    you just need a value for your own reasoning, use run_sql instead.

    The result includes ``browser_url``, which opens this view full-window in
    the user's browser: post it as a clickable link right under the app.

    Args:
        sql: A single read-only statement.
        database: Database to run it against (defaults to the connection's
            configured database).
        max_rows: Maximum rows to fetch, 1-10000 (default 1000). The result
            is flagged as truncated when more rows exist.
    """
    data = run_query(sql, database, max_rows)
    # Rows go to the app via query_grid_rows, not into this (model-visible) result.
    lean = {k: v for k, v in data.items() if k != "rows"}
    lean["result_id"] = stash(data["rows"])
    summary = with_browser_link(
        _summary(data), lean, "query_grid", {"sql": sql, "database": database, "max_rows": data["max_rows"]}
    )
    return tool_result(summary, lean)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=True))
def query_grid_rows(result_id: str) -> CallToolResult:
    """Full rows of an earlier query_grid result, for the Query Results Grid app."""
    rows = unstash(result_id)
    return tool_result(f"{len(rows)} row(s)", {"result_id": result_id, "rows": rows})
