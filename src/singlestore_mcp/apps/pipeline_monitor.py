"""Pipeline Monitor app: live status of SingleStore pipelines with controls.

The UI starts/stops/tests pipelines by calling the server's regular
start_pipeline / stop_pipeline / test_pipeline tools, and refreshes through
the app-only ``pipeline_monitor_data`` tool.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from ..db import db
from ._core import APP_ONLY, apps, jsonable_rows, register_app, tool_result, with_browser_link

URI = "ui://singlestore/pipeline-monitor.html"
RECENT_BATCHES = 20
READ_ONLY = ToolAnnotations(readOnlyHint=True)

register_app(
    URI,
    "pipeline_monitor.html",
    name="Pipeline Monitor",
    description="Live status of SingleStore pipelines with start/stop/test controls",
)


def _where(database: str | None, column: str = "DATABASE_NAME") -> tuple[str, tuple[Any, ...]]:
    return (f" WHERE {column} = %s", (database,)) if database else ("", ())


def _query(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return jsonable_rows(db.execute(sql, params)[1])


def collect(database: str | None) -> dict[str, Any]:
    where, params = _where(database)
    # Only selected CONFIG_JSON fields: the full document contains credentials.
    pipelines = _query(
        "SELECT DATABASE_NAME, PIPELINE_NAME, STATE,"
        " UNIX_TIMESTAMP(CREATE_TIME) AS CREATE_UNIX, UNIX_TIMESTAMP(ALTER_TIME) AS ALTER_UNIX,"
        " JSON_EXTRACT_STRING(CONFIG_JSON, 'source_type') AS SOURCE_TYPE,"
        " JSON_EXTRACT_STRING(CONFIG_JSON, 'connection_string') AS SOURCE,"
        " JSON_EXTRACT_STRING(CONFIG_JSON, 'table') AS TARGET_TABLE"
        f" FROM information_schema.PIPELINES{where} ORDER BY DATABASE_NAME, PIPELINE_NAME",
        params,
    )
    last_batches = _query(
        "SELECT DATABASE_NAME, PIPELINE_NAME, BATCH_ID, BATCH_STATE,"
        " UNIX_TIMESTAMP(START_TIME) AS START_UNIX, BATCH_TIME,"
        " ROWS_INSERTED, ROWS_UPDATED, ROWS_DELETED, ROWS_PER_SEC, MB_STREAMED"
        f" FROM information_schema.PIPELINES_BATCHES_SUMMARY{where}",
        params,
    )
    recent = _query(
        "SELECT DATABASE_NAME, PIPELINE_NAME, BATCH_ID, BATCH_STATE, BATCH_ROWS_WRITTEN,"
        " BATCH_TIME, BATCH_START_UNIX_TIMESTAMP FROM ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, PIPELINE_NAME ORDER BY BATCH_ID DESC) AS rn"
        f"  FROM information_schema.PIPELINES_BATCHES_METADATA{where}"
        f") t WHERE rn <= {RECENT_BATCHES} ORDER BY BATCH_ID",
        params,
    )
    errors = _query(
        "SELECT DATABASE_NAME, PIPELINE_NAME, n, ERROR_UNIX_TIMESTAMP, ERROR_TYPE, ERROR_MESSAGE FROM ("
        "  SELECT DATABASE_NAME, PIPELINE_NAME, ERROR_UNIX_TIMESTAMP, ERROR_TYPE, ERROR_MESSAGE,"
        "   COUNT(*) OVER (PARTITION BY DATABASE_NAME, PIPELINE_NAME) AS n,"
        "   ROW_NUMBER() OVER (PARTITION BY DATABASE_NAME, PIPELINE_NAME ORDER BY ERROR_UNIX_TIMESTAMP DESC) AS rn"
        f"  FROM information_schema.PIPELINES_ERRORS{where}"
        ") t WHERE rn = 1",
        params,
    )

    # Progress through the source: file counts for file sources, offset lag for Kafka.
    files = _query(
        "SELECT DATABASE_NAME, PIPELINE_NAME, FILE_STATE, COUNT(*) AS n, SUM(FILE_SIZE) AS bytes"
        f" FROM information_schema.PIPELINES_FILES{where} GROUP BY 1, 2, 3",
        params,
    )
    kafka = _query(
        "SELECT DATABASE_NAME, PIPELINE_NAME, COUNT(*) AS partitions,"
        " SUM(GREATEST(LATEST_OFFSET - CURSOR_OFFSET, 0)) AS lag, MAX(UPDATED_UNIX_TIMESTAMP) AS updated_unix"
        f" FROM information_schema.PIPELINES_CURSORS{where}{' AND' if where else ' WHERE'} SOURCE_TYPE = 'KAFKA'"
        " GROUP BY 1, 2",
        params,
    )

    key = lambda r: (r["DATABASE_NAME"], r["PIPELINE_NAME"])  # noqa: E731
    files_by: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"kind": "files", "total": 0, "loaded": 0, "skipped": 0, "pending": 0, "bytes_total": 0, "bytes_loaded": 0}
    )
    for r in files:
        f = files_by[key(r)]
        n, size = int(r["n"] or 0), int(r["bytes"] or 0)
        f["total"] += n
        f["bytes_total"] += size
        state = (r["FILE_STATE"] or "").lower()
        if state == "loaded":
            f["loaded"] += n
            f["bytes_loaded"] += size
        elif state == "skipped":
            f["skipped"] += n
        else:
            f["pending"] += n
    kafka_by = {
        key(r): {
            "kind": "kafka",
            "partitions": int(r["partitions"] or 0),
            "lag": int(r["lag"] or 0),
            "updated_unix": r["updated_unix"],
        }
        for r in kafka
    }

    last_by = {key(r): r for r in last_batches}
    errors_by = {key(r): r for r in errors}
    recent_by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in recent:
        recent_by[key(r)].append(
            {
                "batch_id": r["BATCH_ID"],
                "state": r["BATCH_STATE"],
                "rows": r["BATCH_ROWS_WRITTEN"],
                "seconds": r["BATCH_TIME"],
                "start_unix": r["BATCH_START_UNIX_TIMESTAMP"],
            }
        )

    items = []
    for p in pipelines:
        k = key(p)
        last = last_by.get(k)
        err = errors_by.get(k)
        items.append(
            {
                "database": p["DATABASE_NAME"],
                "name": p["PIPELINE_NAME"],
                "state": p["STATE"],
                "source_type": p["SOURCE_TYPE"],
                "source": p["SOURCE"],
                "target_table": p["TARGET_TABLE"],
                "created_unix": p["CREATE_UNIX"],
                "altered_unix": p["ALTER_UNIX"],
                "last_batch": None
                if last is None
                else {
                    "batch_id": last["BATCH_ID"],
                    "state": last["BATCH_STATE"],
                    "start_unix": last["START_UNIX"],
                    "seconds": last["BATCH_TIME"],
                    "rows_inserted": last["ROWS_INSERTED"],
                    "rows_updated": last["ROWS_UPDATED"],
                    "rows_deleted": last["ROWS_DELETED"],
                    "rows_per_sec": last["ROWS_PER_SEC"],
                    "mb_streamed": last["MB_STREAMED"],
                },
                "recent_batches": recent_by.get(k, []),
                "progress": kafka_by.get(k) if p["SOURCE_TYPE"] == "KAFKA" else (dict(files_by[k]) if k in files_by else None),
                "error_count": err["n"] if err else 0,
                "last_error": None
                if err is None
                else {"unix": err["ERROR_UNIX_TIMESTAMP"], "type": err["ERROR_TYPE"], "message": err["ERROR_MESSAGE"]},
            }
        )
    return {
        "database": database,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pipelines": items,
    }


def _summary(data: dict[str, Any]) -> str:
    pipelines = data["pipelines"]
    scope = f"database {data['database']}" if data["database"] else "all databases"
    if not pipelines:
        return f"No pipelines in {scope}."
    lines = [f"{len(pipelines)} pipeline(s) in {scope} (shown in the Pipeline Monitor app):"]
    for p in pipelines:
        line = f"- {p['database']}.{p['name']}: {p['state']} ({p['source_type']})"
        prog = p["progress"]
        if prog and prog["kind"] == "files":
            line += f", {prog['loaded']}/{prog['total']} files loaded"
            if prog["skipped"]:
                line += f" ({prog['skipped']} skipped)"
        elif prog and prog["kind"] == "kafka":
            line += ", caught up" if prog["lag"] == 0 else f", {prog['lag']:,} messages behind"
        if p["last_batch"]:
            line += f", last batch {p['last_batch']['state']}"
        if p["error_count"]:
            line += f", {p['error_count']} error(s); latest: {(p['last_error']['message'] or '')[:160]}"
        lines.append(line)
    return "\n".join(lines)


@apps.tool(resource_uri=URI, title="Pipeline Monitor", annotations=READ_ONLY)
def pipeline_monitor(database: str | None = None) -> CallToolResult:
    """Open an interactive monitor of SingleStore pipelines.

    Shows every pipeline's state, source, latest batch and recent errors, with
    buttons to start, stop and test pipelines and an auto-refresh. Use this
    when the user wants to see or manage pipelines visually.

    The result includes ``browser_url``, which opens this view full-window in
    the user's browser: post it as a clickable link right under the app.

    Args:
        database: Limit to one database. Omit to show pipelines in all databases.
    """
    data = collect(database)
    summary = with_browser_link(_summary(data), data, "pipeline_monitor", {"database": database})
    return tool_result(summary, data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def pipeline_monitor_data(database: str | None = None) -> CallToolResult:
    """Refresh data for the Pipeline Monitor app."""
    data = collect(database)
    return tool_result(_summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def pipeline_errors(pipeline_name: str, database: str, limit: int = 25) -> CallToolResult:
    """Most recent errors for one pipeline, for the Pipeline Monitor app."""
    rows = _query(
        "SELECT ERROR_UNIX_TIMESTAMP, ERROR_TYPE, ERROR_CODE, ERROR_KIND, ERROR_MESSAGE, BATCH_ID,"
        " LOAD_DATA_LINE_NUMBER, LOAD_DATA_LINE, HOST, PARTITION"
        " FROM information_schema.PIPELINES_ERRORS WHERE DATABASE_NAME = %s AND PIPELINE_NAME = %s"
        f" ORDER BY ERROR_UNIX_TIMESTAMP DESC LIMIT {max(1, min(int(limit), 200))}",
        (database, pipeline_name),
    )
    return tool_result(f"{len(rows)} recent error(s) for {database}.{pipeline_name}", {"errors": rows})
