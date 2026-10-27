"""The Workspace's connection-health line: "Connection to SingleStore lost – retrying…" while the cluster or SAS
Viya can't be reached, and gone again (with a "Reconnected" message) once they answer (see ``netguard``)."""

from __future__ import annotations

from mcp.types import CallToolResult, ToolAnnotations

from .. import netguard
from ._core import APP_ONLY, apps, tool_result
from .sql_editor import WORKSPACE_URI

_NAMES = {"singlestore": "SingleStore", "sas": "SAS Viya"}


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=True))
def connection_health(probe: str = "down", after_notice: int = 0) -> CallToolResult:
    """Whether SingleStore and SAS Viya were reachable on the last calls (in memory: no round trip on the caller's
    thread). ``probe``: "down" checks the services that are down again in the background (at most every few
    seconds), "all" (the page came back online) every service used so far, "none" nothing. ``notices``: one-time
    messages with an id above ``after_notice`` (e.g. a SAS session restarted after a connection loss)."""
    if probe == "all":
        netguard.probe_all(force=True)
    elif probe == "down":
        for svc in netguard.down_services():
            netguard.probe(svc)
    snap = netguard.snapshot()
    services = {}
    for svc, h in snap["services"].items():
        services[svc] = {"name": _NAMES.get(svc, svc), "state": h.get("state"), "since": h.get("since"),
                         "error": h.get("error"), "recovered": h.get("recovered"), "down_s": h.get("down_s")}
    notices = [n for n in snap["notices"] if n["id"] > int(after_notice or 0)]
    down = [s["name"] for s in services.values() if s["state"] == "down"]
    text = f"Can't reach {', '.join(down)}: retrying." if down else "Connections OK."
    return tool_result(text, {"services": services, "notices": notices, "events": snap["events"]})
