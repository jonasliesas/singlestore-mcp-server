"""Alerts app: the alert rules, the alerts the background checker raised, acknowledge / clear.

The checker itself (``singlestore_mcp.alerts``) runs in the server process; this module only shows its
state and edits its rules. Reading the state never queries the cluster, so the workspace can poll the
unacknowledged count cheaply for its rail badge.
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import alerts as checker
from ._core import APP_ONLY, apps, register_app, tool_result, with_browser_link

URI = "ui://singlestore/alerts.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

register_app(
    URI,
    "alerts.html",
    name="Alerts",
    description="Alerts on long-running and failed queries, pipeline failures, memory, disk and nodes",
)


def _summary(st: dict[str, Any]) -> str:
    lines = [f"{st['unacknowledged']} unacknowledged alert(s), {st['active']} still active, {st['total']} in total "
             f"(checked every {st['settings']['interval_s']} s; shown in the Alerts app)."]
    for a in st.get("alerts", [])[:10]:
        lines.append(f"- [{a.get('severity')}] {a.get('title')}{' (active)' if a.get('active') else ''}"
                     f"{'' if a.get('acked') else ' (unacknowledged)'}: {str(a.get('detail') or '')[:160]}")
    return "\n".join(lines)


@apps.tool(resource_uri=URI, title="Alerts", annotations=READ_ONLY)
def alerts() -> CallToolResult:
    """Open the Alerts view: alerts the server raised while watching the SingleStore cluster in the background
    (queries running too long, slow or failed queries, failed pipeline batches / pipeline errors, node memory
    and disk thresholds, nodes not online), with the rules to edit and acknowledge / clear buttons.

    The result includes ``browser_url``: post it as a clickable link right under the app.
    """
    checker.start()
    st = checker.state()
    return tool_result(with_browser_link(_summary(st), st, "alerts", {}), st)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def alerts_data() -> CallToolResult:
    """Alerts, rules and checker status for the Alerts view (no cluster queries)."""
    checker.start()
    st = checker.state()
    return tool_result(_summary(st), st)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def alerts_badge() -> CallToolResult:
    """Unacknowledged alert count for the workspace rail (in-memory state only; cheap to poll)."""
    checker.start()
    st = checker.state(include_alerts=False)
    data = {k: st[k] for k in ("unacknowledged", "active", "total", "newest", "newest_unacked")}
    data["notify"] = st["settings"]["notify"]
    return tool_result(f"{st['unacknowledged']} unacknowledged alert(s).", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=LOCAL_WRITE)
def alerts_update(rules: dict[str, Any] | None = None, settings: dict[str, Any] | None = None) -> CallToolResult:
    """Change alert rules ({name: {enabled, seconds, percent}}) and settings ({interval_s, notify})."""
    st = checker.update_rules(rules, settings)
    return tool_result("Alert rules saved.", st)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=LOCAL_WRITE)
def alerts_ack(ids: list[str] | None = None, all: bool = False) -> CallToolResult:  # noqa: A002 - tool argument name
    """Acknowledge alerts by id, or all of them."""
    if not all and not ids:
        raise ValueError("Pass ids or all=true.")
    n = checker.acknowledge(None if all else list(ids or []))
    return tool_result(f"{n} alert(s) acknowledged.", {"acknowledged": n})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=LOCAL_WRITE)
def alerts_clear(acknowledged_only: bool = False) -> CallToolResult:
    """Remove alerts from the list (all, or only the acknowledged ones)."""
    n = checker.clear(only_acknowledged=acknowledged_only)
    return tool_result(f"{n} alert(s) cleared.", {"cleared": n})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def alerts_check_now() -> CallToolResult:
    """Run every enabled check now (read-only queries) and return the new state."""
    result = checker.check_once()
    st = checker.state()
    return tool_result(_summary(st), {**st, "check": result})
