"""Query History extras: the Trends tab (local durable history) and the Advisor's before/after check.

App-only tools for the Query History page (``query_history.URI``); kept apart from query_history.py.
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import compare, history_store
from ..db import db
from ._core import APP_ONLY, apps, jsonable, tool_result
from .query_history import URI

READ_ONLY = ToolAnnotations(readOnlyHint=True)
# The comparison runs the user's own SELECT queries (read-only, but real work on the cluster).
RUNS_QUERIES = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_trends(days: int = 30) -> CallToolResult:
    """Daily / hourly query counts, runtime, failures and p95 from the local copy of the history, plus query
    shapes that got slower than the week before."""
    synced = history_store.safe_sync(force=True)
    now = db.execute(f"{history_store.MARKER_PREFIX}trends */ SELECT NOW() AS t")[1][0]["t"]
    data = history_store.trends(days, now=str(jsonable(now)).replace("T", " "))
    data["sync"] = synced
    lines = [f"{data['stored']} runs stored locally for {data['source']} (since {data['oldest']})."]
    for s in data["slower"][:5]:
        lines.append(f"- slower: {s['before_ms'] / 1000:.1f} s -> {s['median_ms'] / 1000:.1f} s ({s['runs']} runs): {s['sample'][:120]}")
    return tool_result("\n".join(lines), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_trend_runs(bucket: str, failed_only: bool = False) -> CallToolResult:
    """The queries behind one Trends bar (a day "YYYY-MM-DD" or an hour "YYYY-MM-DD HH"), slowest first."""
    data = history_store.bucket_runs(bucket, failed_only)
    return tool_result(f"{data['total']} {'failed ' if failed_only else ''}runs in {bucket}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_compare_candidates(table: str, new_table: str, limit: int = 8) -> CallToolResult:
    """The heaviest read-only queries in the history that read ``table`` (database.table), with the table name
    replaced by ``new_table`` (nothing is run)."""
    data = compare.candidates(table, new_table, limit)
    return tool_result(data.get("message") or f"{len(data['queries'])} candidate queries.", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=RUNS_QUERIES)
def query_history_compare_start(table: str, new_table: str, shapes: list[str], runs: int = 3,
                                timeout_s: float = 120) -> CallToolResult:
    """Run the selected candidate queries against the original and the new table (wrapped in COUNT(*)),
    alternating, ``runs`` times each. Poll query_history_compare_status."""
    data = compare.start(table, new_table, shapes, runs, timeout_s)
    return tool_result(f"Comparison started: {data['total_runs']} runs.", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_compare_status(job_id: str) -> CallToolResult:
    """Progress and results of a before/after comparison."""
    data = compare.status(job_id)
    return tool_result(_compare_summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def query_history_compare_cancel(job_id: str) -> CallToolResult:
    """Stop a running comparison (the statement running now is cancelled too)."""
    data = compare.cancel(job_id)
    return tool_result("Stopping the comparison.", data)


def _compare_summary(job: dict[str, Any]) -> str:
    lines = [f"Comparison {job['table']} vs {job['new_table']}: {job['state']}, {job['done_runs']}/{job['total_runs']} runs."]
    for q in job["queries"]:
        if q.get("old_median_ms") is not None and q.get("new_median_ms") is not None:
            lines.append(f"- {q['old_median_ms']} ms -> {q['new_median_ms']} ms (x{q.get('speedup')}), rows "
                         f"{'match' if q.get('rows_match') else 'DIFFER'}: {' '.join(q['sql'].split())[:100]}")
        elif q.get("errors"):
            lines.append(f"- error: {q['errors'][0]}: {' '.join(q['sql'].split())[:100]}")
    return "\n".join(lines)
