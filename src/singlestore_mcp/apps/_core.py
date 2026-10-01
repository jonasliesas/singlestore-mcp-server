"""Shared plumbing for this server's MCP Apps (interactive UIs).

Server side uses the official Python SDK's built-in MCP Apps extension
(``mcp.server.apps.Apps``). Browser side uses the official
``@modelcontextprotocol/ext-apps`` client bundle, vendored under ``vendor/``
and inlined into every page so apps work without CDN access or CSP
exceptions (self-managed clusters are often on networks without internet).

Each app is one HTML template in this directory. ``register_app`` expands its
``<!--S2:HEAD-->`` marker into: shared.css, the ext-apps bundle (exposed as
``globalThis.McpExtApps``) and shared.js (exposed as ``globalThis.S2``).
"""

from __future__ import annotations

import datetime
import decimal
import functools
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

import singlestoredb as s2
from mcp.server.apps import Apps, ResourcePermissions
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

_F = TypeVar("_F", bound=Callable[..., Any])

# Failures the caller can act on: bad arguments, missing objects, SQL errors.
_EXPECTED_ERRORS = (ValueError, LookupError, s2.Error)


def surface_errors(fn: _F) -> _F:
    """Report expected failures to the model with their real message.

    The SDK replaces the text of any exception that isn't a ToolError with a
    generic "Error executing tool X", which hides SQL errors and validation
    messages the model needs in order to correct its call.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


class _Apps(Apps):
    def tool(self, **kwargs: Any) -> Callable[[_F], _F]:
        register = super().tool(**kwargs)

        def decorator(fn: _F) -> _F:
            register(surface_errors(fn))
            return fn

        return decorator


apps = _Apps()

APP_ONLY = ["app"]

_HERE = Path(__file__).parent
_EXT_APPS_BUNDLE = _HERE / "vendor" / "ext-apps-app-2.0.3.js"
_HEAD_MARKER = "<!--S2:HEAD-->"


@cache
def _ext_apps_script() -> str:
    # The bundle is an ES module ending in `export{x as App,...};`. Inline
    # module scripts can't be imported from, so publish the exports on
    # globalThis for the scripts that follow it.
    src = _EXT_APPS_BUNDLE.read_text(encoding="utf-8")
    match = re.search(r"export\s*\{([^}]*)\}\s*;?\s*$", src)
    if not match:
        raise RuntimeError(f"Unexpected format in {_EXT_APPS_BUNDLE.name}: no trailing export list")
    pairs = []
    for part in match.group(1).split(","):
        local, _, exported = part.strip().partition(" as ")
        pairs.append(f"{(exported or local).strip()}:{local.strip()}")
    return src[: match.start()] + "globalThis.McpExtApps={" + ",".join(pairs) + "};"


@cache
def _head() -> str:
    css = (_HERE / "shared.css").read_text(encoding="utf-8")
    js = (_HERE / "shared.js").read_text(encoding="utf-8")
    return (
        f"<style>\n{css}\n</style>\n"
        f'<script type="module">\n{_ext_apps_script()}\n</script>\n'
        f'<script type="module">\n{js}\n</script>'
    )


def build_page(template: str) -> str:
    html = (_HERE / template).read_text(encoding="utf-8")
    if _HEAD_MARKER not in html:
        raise RuntimeError(f"{template} is missing the {_HEAD_MARKER} marker")
    return html.replace(_HEAD_MARKER, _head(), 1)


_browser_url_factory: Callable[[str, dict[str, Any]], str] | None = None


def set_browser_url_factory(factory: Callable[[str, dict[str, Any]], str]) -> None:
    global _browser_url_factory
    _browser_url_factory = factory


def browser_url(tool: str, arguments: dict[str, Any]) -> str | None:
    """Full-window browser link for an app view, or None if unavailable."""
    if _browser_url_factory is None:
        return None
    try:
        return _browser_url_factory(tool, {k: v for k, v in arguments.items() if v is not None})
    except Exception:  # noqa: BLE001 - the link is a convenience; never fail the tool over it
        return None


def with_browser_link(summary: str, data: dict[str, Any], tool: str, arguments: dict[str, Any]) -> str:
    """Add the view's browser link to ``data`` and the summary."""
    url = browser_url(tool, arguments)
    if url is None:
        return summary
    data["browser_url"] = url
    return f"{summary}\nFull-window browser link for this view (post it under the app): {url}"


_PAGES: dict[str, str] = {}


def register_app(uri: str, template: str, *, name: str, description: str) -> None:
    _PAGES[uri] = build_page(template)
    apps.add_html_resource(
        uri,
        _PAGES[uri],
        name=name,
        description=description,
        prefers_border=True,
        # Lets "Copy" put the browser link on the clipboard where the host allows it.
        permissions=ResourcePermissions(clipboard_write={}),
    )


def page_html(uri: str) -> str | None:
    return _PAGES.get(uri)


def tool_result(summary: str, data: dict[str, Any]) -> CallToolResult:
    """Text summary plus structured data for the UI.

    Some hosts (e.g. Claude Code) show the model ``structuredContent`` rather
    than the text, so keep ``data`` small for model-visible tools: return bulk
    data through app-only tools or ``stash`` instead. The summary is repeated
    in ``data`` so the model gets it either way.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent={**data, "summary": summary},
    )


_STASH: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_STASH_LOCK = threading.Lock()
_STASH_MAX_ENTRIES = 8
_STASH_TTL_SECONDS = 3600


def stash(value: Any) -> str:
    """Keep a large result server-side for an app to fetch; returns its id."""
    key = uuid.uuid4().hex
    now = time.monotonic()
    with _STASH_LOCK:
        _STASH[key] = (now, value)
        while len(_STASH) > _STASH_MAX_ENTRIES:
            _STASH.popitem(last=False)
    return key


def unstash(key: str) -> Any:
    with _STASH_LOCK:
        entry = _STASH.get(key)
        if entry is None or time.monotonic() - entry[0] > _STASH_TTL_SECONDS:
            _STASH.pop(key, None)
            raise LookupError("This result has expired; re-run the query.")
        _STASH.move_to_end(key)
        return entry[1]


def jsonable(value: Any) -> Any:
    """Convert a value from singlestoredb into something JSON can carry."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        as_float = float(value)
        return as_float if decimal.Decimal(repr(as_float)) == value else str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return "0x" + raw[:64].hex() + ("…" if len(raw) > 64 else "")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)


def jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: jsonable(v) for k, v in row.items()} for row in rows]


def rows_as_text(columns: list[str], rows: list[dict[str, Any]], limit: int = 20) -> str:
    """Compact pipe-separated rendering of the first rows, for the model."""
    if not columns:
        return "(no result set)"
    lines = [" | ".join(columns)]
    for row in rows[:limit]:
        cells = []
        for col in columns:
            cell = row.get(col)
            text = "NULL" if cell is None else str(cell)
            cells.append(text if len(text) <= 60 else text[:57] + "...")
        lines.append(" | ".join(cells))
    if len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more rows (shown in the app)")
    return "\n".join(lines)
