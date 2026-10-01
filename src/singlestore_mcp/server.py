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

Run directly over stdio:

    python -m singlestore_mcp.server

or, once installed, via the ``singlestore-mcp-server`` console script.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .apps import apps
from .apps._core import set_browser_url_factory, surface_errors
from .apps.browser_view import BrowserView
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


def _exec(sql: str, database: str | None = None, fetch: bool = True) -> dict[str, Any]:
    columns, rows, rowcount = db.execute(sql, database=database, fetch=fetch)
    return _result(columns, rows, rowcount)


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
    result = _exec(sql, database=database)
    if len(result["rows"]) > max_rows:
        result["rows"] = result["rows"][:max_rows]
        result["truncated"] = True
    return result


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
        tool: The app tool: pipeline_monitor, query_grid or schema_explorer.
        arguments: That tool's arguments, e.g. {"sql": "...", "database": "SASDP"}
            for query_grid or {"database": "SASDP", "table": "CARS"} for
            schema_explorer.
    """
    return {"url": browser_view.url(tool, arguments or {})}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
