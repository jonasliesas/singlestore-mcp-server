"""SingleStore MCP server.

Exposes general SQL execution plus first-class tools for SingleStore
Pipelines (create/alter/start/stop/drop/test/inspect), built on:

  * the official MCP Python SDK (``mcp``, modelcontextprotocol/python-sdk)
    for the stdio server/tool-registration plumbing, and
  * SingleStore's own official Python client (``singlestoredb``,
    singlestore-labs/singlestoredb-python) for the database connection.

Works against SingleStore Helios (cloud) and self-managed SingleStore
clusters identically -- both are plain MySQL-wire-protocol endpoints, so the
only inputs needed are host/port/user/password (see db.py / README.md).

The tools, prompts and apps live here; ``server.py`` is the lightweight
entry point (``singlestore-mcp-server`` / ``python -m singlestore_mcp.server``)
that starts this module behind the restart supervisor.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .apps import apps
from .apps._core import set_browser_url_factory, surface_errors
from .apps.browser_view import BrowserView
from .apps.sql_editor import deliver_reply
from .db import InvalidIdentifierError, db, quote_identifier

mcp = MCPServer("singlestore", extensions=[apps])


def tool(**kwargs: Any):
    """``mcp.tool`` that reports SQL/validation errors to the model verbatim."""

    def decorator(fn):
        mcp.tool(**kwargs)(surface_errors(fn))
        return fn

    return decorator


_CREATE_PIPELINE_RE = re.compile(r"^\s*CREATE\s+(OR\s+REPLACE\s+)?PIPELINE\b", re.IGNORECASE)
_ALTER_PIPELINE_RE = re.compile(r"^\s*ALTER\s+PIPELINE\b", re.IGNORECASE)
_DEFAULT_MAX_ROWS = 200


def _result(columns: list[str], rows: list[dict[str, Any]], rowcount: int) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, "row_count": rowcount}


def _exec(sql: str, database: str | None = None, fetch: bool = True, fit: bool = True) -> dict[str, Any]:
    columns, rows, rowcount = db.execute(sql, database=database, fetch=fetch)
    result = _result(columns, rows, rowcount)
    if fit:
        _fit_result(result)
    return result


def _pipeline_name_clause(pipeline_name: str) -> str:
    try:
        return quote_identifier(pipeline_name)
    except InvalidIdentifierError as exc:
        raise ValueError(str(exc)) from exc


# --------------------------------------------------------------------------
# General SQL
# --------------------------------------------------------------------------


@tool()
def run_sql(sql: str, database: str | None = None, max_rows: int = _DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """Run one arbitrary SQL statement against SingleStore and return the results.

    Use this for SELECT/DML/DDL that isn't pipeline-specific. For creating,
    altering, starting, stopping, dropping or inspecting Pipelines, prefer
    the dedicated pipeline tools -- they validate the statement type and are
    easier to call correctly.

    Args:
        sql: The statement to execute.
        database: Database to run it against (defaults to the connection's
            configured database).
        max_rows: Truncate returned rows to this many (does not affect how
            many rows the statement itself processes).
    """
    result = _exec(sql, database=database, fit=False)
    if len(result["rows"]) > max_rows:
        result["rows"] = result["rows"][:max_rows]
        result["truncated"] = True
    _fit_result(result)
    return result


# Claude rejects tool results over ~50,000 characters; keep the SQL tools' results well below that.
_MAX_RESULT_CHARS = 30_000  # compact JSON; the text Claude sees is indented, ~1.4x


def _fit_result(result: dict[str, Any]) -> None:
    """Drop trailing rows until the result fits ``_MAX_RESULT_CHARS`` (wide rows can blow past it)."""
    rows = result["rows"]
    used = len(json.dumps(result["columns"], default=str)) + 200
    for i, row in enumerate(rows):
        used += len(json.dumps(row, default=str, ensure_ascii=False)) + 2
        if used > _MAX_RESULT_CHARS:
            result["rows"] = rows[:i]
            result["truncated"] = True
            result["note"] = (f"Showing {i} of {len(rows)} rows: the result is too large for one tool response. "
                              "Select fewer columns, add a LIMIT, or open it in query_grid.")
            return


@tool()
def list_databases() -> dict[str, Any]:
    """List all databases visible to the connected user."""
    return _exec("SHOW DATABASES")


@tool()
def list_tables(database: str | None = None) -> dict[str, Any]:
    """List tables (and views) in a database.

    Args:
        database: Database to list tables from (defaults to the connection's
            configured database).
    """
    return _exec("SHOW TABLES", database=database)


@tool()
def describe_table(table: str, database: str | None = None) -> dict[str, Any]:
    """Show column definitions for a table.

    Args:
        table: Table name.
        database: Database the table lives in (defaults to the connection's
            configured database).
    """
    return _exec(f"DESCRIBE {quote_identifier(table)}", database=database)


# --------------------------------------------------------------------------
# Pipelines
# --------------------------------------------------------------------------


@tool()
def list_pipelines(database: str | None = None) -> dict[str, Any]:
    """List all pipelines in a database and their current state (Running/Stopped/Error).

    Args:
        database: Database to list pipelines from (defaults to the
            connection's configured database).
    """
    return _exec("SHOW PIPELINES", database=database)


@tool()
def pipeline_status(pipeline_name: str, database: str | None = None) -> dict[str, Any]:
    """Get the current state of one pipeline by name.

    Equivalent to SHOW PIPELINES filtered down to a single pipeline. Returns
    an empty row list if no pipeline with that name exists.

    Args:
        pipeline_name: Name of the pipeline to look up.
        database: Database the pipeline lives in (defaults to the
            connection's configured database).
    """
    result = _exec("SHOW PIPELINES", database=database)
    if not result["columns"]:
        return result
    name_column = result["columns"][0]
    result["rows"] = [r for r in result["rows"] if r.get(name_column) == pipeline_name]
    result["row_count"] = len(result["rows"])
    return result


@tool()
def get_pipeline_ddl(pipeline_name: str, database: str | None = None) -> dict[str, Any]:
    """Get the full CREATE PIPELINE statement that reproduces an existing pipeline.

    Args:
        pipeline_name: Name of the pipeline.
        database: Database the pipeline lives in (defaults to the
            connection's configured database).
    """
    clause = _pipeline_name_clause(pipeline_name)
    return _exec(f"SHOW CREATE PIPELINE {clause}", database=database)


@tool()
def create_pipeline(create_pipeline_sql: str, database: str | None = None) -> dict[str, Any]:
    """Create a new pipeline from a full CREATE PIPELINE statement.

    Pipeline definitions vary a lot by source (S3, Kafka, Azure Blob, GCS,
    filesystem, ...), format (CSV/JSON/Avro/Parquet) and optional transforms,
    so this tool takes the complete statement text rather than trying to
    model every variant as separate parameters. It only checks that the
    statement actually starts with CREATE [OR REPLACE] PIPELINE before
    running it. Creating a pipeline does not start it -- call start_pipeline
    afterwards, or include FOREGROUND handling via start_pipeline.

    Example create_pipeline_sql:
        CREATE PIPELINE my_pipeline AS
        LOAD DATA S3 's3://my-bucket/path/'
        CONFIG '{"region": "us-east-1"}'
        CREDENTIALS '{"aws_access_key_id": "...", "aws_secret_access_key": "..."}'
        INTO TABLE my_table
        FIELDS TERMINATED BY ',';

    Args:
        create_pipeline_sql: The full CREATE PIPELINE ... statement.
        database: Database to create the pipeline in (defaults to the
            connection's configured database).
    """
    if not _CREATE_PIPELINE_RE.match(create_pipeline_sql):
        raise ValueError(
            "create_pipeline_sql must start with CREATE PIPELINE or "
            "CREATE OR REPLACE PIPELINE. Use run_sql for other statements."
        )
    return _exec(create_pipeline_sql, database=database, fetch=False)


@tool()
def alter_pipeline(alter_pipeline_sql: str, database: str | None = None) -> dict[str, Any]:
    """Alter an existing pipeline from a full ALTER PIPELINE statement.

    Commonly used to change the connection string/credentials or reset
    offsets. Takes the full statement for the same reason as create_pipeline:
    the set of alterable clauses is source-specific.

    Args:
        alter_pipeline_sql: The full ALTER PIPELINE ... statement.
        database: Database the pipeline lives in (defaults to the
            connection's configured database).
    """
    if not _ALTER_PIPELINE_RE.match(alter_pipeline_sql):
        raise ValueError(
            "alter_pipeline_sql must start with ALTER PIPELINE. Use run_sql "
            "for other statements."
        )
    return _exec(alter_pipeline_sql, database=database, fetch=False)


@tool()
def start_pipeline(
    pipeline_name: str,
    database: str | None = None,
    foreground: bool = False,
    limit_batches: int | None = None,
    if_not_running: bool = True,
) -> dict[str, Any]:
    """Start a pipeline so it begins (or resumes) loading data.

    Args:
        pipeline_name: Name of the pipeline to start.
        database: Database the pipeline lives in (defaults to the
            connection's configured database).
        foreground: If true, run synchronously and report rows loaded /
            errors in the result instead of returning immediately. Useful
            for a one-off load or for testing a pipeline end to end.
        limit_batches: Only valid with foreground=True: stop after this many
            batches instead of running indefinitely.
        if_not_running: Add IF NOT RUNNING so starting an already-running
            pipeline is a no-op instead of an error.
    """
    if limit_batches is not None and not foreground:
        raise ValueError("limit_batches requires foreground=True")
    clause = _pipeline_name_clause(pipeline_name)
    sql = "START PIPELINE "
    if if_not_running:
        sql += "IF NOT RUNNING "
    sql += clause
    if foreground:
        sql += " FOREGROUND"
        if limit_batches is not None:
            sql += f" LIMIT {int(limit_batches)} BATCHES"
    return _exec(sql, database=database, fetch=foreground)


@tool()
def stop_pipeline(pipeline_name: str, database: str | None = None) -> dict[str, Any]:
    """Stop a running pipeline.

    Args:
        pipeline_name: Name of the pipeline to stop.
        database: Database the pipeline lives in (defaults to the
            connection's configured database).
    """
    clause = _pipeline_name_clause(pipeline_name)
    return _exec(f"STOP PIPELINE {clause}", database=database, fetch=False)


@tool()
def drop_pipeline(pipeline_name: str, database: str | None = None, if_exists: bool = True) -> dict[str, Any]:
    """Delete a pipeline. Running pipelines are stopped automatically before being dropped.

    Args:
        pipeline_name: Name of the pipeline to drop.
        database: Database the pipeline lives in (defaults to the
            connection's configured database).
        if_exists: Add IF EXISTS so dropping a nonexistent pipeline is a
            no-op instead of an error.
    """
    clause = _pipeline_name_clause(pipeline_name)
    sql = "DROP PIPELINE "
    if if_exists:
        sql += "IF EXISTS "
    sql += clause
    return _exec(sql, database=database, fetch=False)


@tool()
def test_pipeline(pipeline_name: str, database: str | None = None, limit: int | None = None) -> dict[str, Any]:
    """Test an existing pipeline: extract and transform data without loading it into the table.

    The pipeline must already exist and must be stopped first (SingleStore
    errors if you test a running pipeline) -- call stop_pipeline before this
    if needed. Nothing is written to the destination table; this is purely
    for validating that the source/format/transform config works.

    Args:
        pipeline_name: Name of the pipeline to test.
        database: Database the pipeline lives in (defaults to the
            connection's configured database).
        limit: Only pull this many rows/messages instead of testing the
            whole batch.
    """
    clause = _pipeline_name_clause(pipeline_name)
    sql = f"TEST PIPELINE {clause}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return _exec(sql, database=database)


browser_view = BrowserView(mcp)
set_browser_url_factory(browser_view.url)


@tool(annotations=ToolAnnotations(readOnlyHint=True))
def browser_link(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get a link that opens one of the interactive apps full-window in the user's web browser.

    Use this when the user wants an app bigger than the chat allows or in
    their browser. Give the user the returned URL as a clickable link. It
    works on this machine only, until the MCP server restarts.

    Args:
        tool: The app tool: pipeline_monitor, query_grid, schema_explorer, cluster_monitor or sql_editor.
        arguments: That tool's arguments, e.g. {"sql": "...", "database": "SASDP"}
            for query_grid or {"database": "SASDP", "table": "CARS"} for
            schema_explorer.
    """
    return {"url": browser_view.url(tool, arguments or {})}


@tool(annotations=ToolAnnotations(readOnlyHint=True))
def sql_editor_reply(editor_id: str, message: str) -> dict[str, Any]:
    """Send an answer to the chat panel of an open SQL Editor app.

    Use this to answer a message that arrived from the SQL Editor (it starts
    with "[SQL Editor <editor_id>]"), or when the user asks you to put SQL into
    their open editor (the editor reports its id in the model context). The
    message is plain text; put SQL in ```sql fenced blocks - each block gets
    Replace / Insert / Copy buttons in the editor. Keep explanations short.
    After calling this, reply briefly in the chat as well.

    Args:
        editor_id: The editor's id, e.g. "e-4f9a2c".
        message: The answer, with SQL in ```sql fenced blocks.
    """
    seq = deliver_reply(editor_id, message)
    return {"delivered": True, "editor_id": editor_id, "seq": seq}


# --------------------------------------------------------------------------
# Connections (saved SingleStore connections; one is active)
# --------------------------------------------------------------------------


@tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_connections() -> dict[str, Any]:
    """List the saved SingleStore connections (no passwords) and which one is active.

    All tools and apps use the active connection. The user manages them in the
    Connections window (connections_window tool, or the workspace's Connect view).
    """
    from .apps.connections import _state

    return _state()


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def use_connection(name: str) -> dict[str, Any]:
    """Switch the active SingleStore connection; all tools, apps and new notebook kernels then use it.

    Args:
        name: Name of a saved connection (see list_connections).
    """
    from . import connections
    from .apps.connections import _state

    p = connections.activate(name)
    check = connections.test(name=name)
    message = (f"Now connected to {p.name} (SingleStore {check['version']} as {check['user']})." if check["ok"]
               else f"Switched to {p.name}, but the connection test failed: {check['error']}")
    return {"message": message, "test": check, **_state()}


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def restart_server() -> dict[str, Any]:
    """Restart the SingleStore MCP server so code and app changes load, without reconnecting.

    Open apps keep working after a reload of the app; browser links made
    before the restart stop working.
    """
    # The supervisor (singlestore_mcp.supervisor) answers this call itself;
    # reaching this function means the server runs without it.
    return {
        "restarted": False,
        "message": "This server runs without the restart supervisor "
        "(SINGLESTORE_MCP_NO_SUPERVISOR is set, or it was started directly); reconnect it instead.",
    }


# --------------------------------------------------------------------------
# Prompts: shown in Claude Code as /singlestore:<name> slash commands
# --------------------------------------------------------------------------


@mcp.prompt(title="Open the SingleStore workspace")
def workspace(database: str = "", view: str = "sql") -> str:
    """Open the SingleStore Workspace (SQL editor, schema, pipelines, cluster)."""
    db = f' on database "{database}"' if database else ""
    return (
        f"Open the SingleStore Workspace{db} with the sql_editor tool, view \"{view or 'sql'}\". "
        "Then post the browser link from the result as a clickable link under the app."
    )


@mcp.prompt(title="Explain and tune a query")
def explain_query(sql: str, database: str = "") -> str:
    """Explain a SingleStore query and suggest how to make it faster."""
    db = f" in database {database}" if database else ""
    return (
        f"Explain this SingleStore query{db} and how to make it faster.\n\n```sql\n{sql}\n```\n\n"
        "Check the tables' columns, shard and sort keys and sizes (describe_table, information_schema), "
        "run EXPLAIN with run_sql, and say what it shows. Then give an improved query (and any key or "
        "index change as a separate statement for the user to run), with a short reason for each change. "
        "Don't run statements that change data or schema."
    )


@mcp.prompt(title="Create a pipeline")
def create_pipeline(source: str, table: str = "", database: str = "") -> str:
    """Create a SingleStore pipeline from an S3 path, Kafka topic or other source."""
    target = f" into {database + '.' if database else ''}{table}" if table else ""
    return (
        f"Create a SingleStore pipeline that loads {source}{target}.\n"
        "1. Inspect the source format (sample a file or message) and propose the target table DDL "
        "(shard key, sort key) if the table doesn't exist.\n"
        "2. Write the CREATE PIPELINE (use pipeline_source_file() where it helps), show it to me, "
        "and wait for my OK before creating anything.\n"
        "3. After creating it, run test_pipeline with a small LIMIT, then start it and open the "
        "pipeline monitor."
    )


@mcp.prompt(title="Check pipeline health")
def pipeline_health(database: str = "") -> str:
    """Check all pipelines for errors, stalls and lag."""
    scope = f"in database {database}" if database else "in all databases"
    return (
        f"Check the health of the SingleStore pipelines {scope}: state, latest batches, recent errors, "
        "files not yet loaded and Kafka lag (pipeline_status, information_schema.PIPELINES_ERRORS / "
        "PIPELINES_FILES / PIPELINES_CURSORS). Summarize per pipeline (OK / needs attention, and why), "
        "suggest fixes, and open the pipeline monitor app."
    )


@mcp.prompt(title="Table report")
def table_report(table: str, database: str = "") -> str:
    """Summarize a table: size, storage, keys and data profile."""
    where = f"{database}.{table}" if database else table
    return (
        f"Give me a report on the SingleStore table {where} (names are case-sensitive): row count, "
        "storage type and size, shard and sort keys, columns with types, and a short profile of the data "
        "(nulls, distinct counts, min/max for key columns), using cheap queries. Point out anything "
        "unusual, such as skew or leading spaces in values. Then open it in the schema explorer."
    )


@mcp.prompt(title="Restart the server")
def restart() -> str:
    """Restart the SingleStore MCP server to load code and app changes."""
    return (
        "Restart the SingleStore MCP server with the restart_server tool and tell me the result. "
        "If any SingleStore app is open, mention that reloading or reopening it picks up app changes."
    )


def run_stdio() -> None:
    """Serve MCP over stdio in this process (the supervisor's child, or unsupervised)."""
    # Connect in the background once the MCP handshake is under way.
    threading.Timer(1.0, db.warm).start()
    from . import alerts

    alerts.start()  # background alert checks while the server runs (SINGLESTORE_MCP_ALERTS=0 turns them off)
    try:
        mcp.run(transport="stdio")
    finally:
        alerts.stop(timeout=2.0)


if __name__ == "__main__":
    run_stdio()
