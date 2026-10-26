"""Presenter mode's labels for the Studio's pages (see ``singlestore_mcp.presenter``)."""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import presenter
from ._core import APP_ONLY, apps, tool_result
from .sql_editor import WORKSPACE_URI


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=True))
def presenter_labels(values: list[dict[str, Any]] | None = None) -> CallToolResult:
    """Presenter mode: the neutral stand-in of every identifying value known so far (connection names, hosts,
    user names, SAS Viya addresses), after adding ``values`` ([{value, kind}], kind connection / host / viya /
    user). The same value always gets the same label while this server runs."""
    items = presenter.labels(values)
    return tool_result(f"{len(items)} label(s)", {"labels": items})
