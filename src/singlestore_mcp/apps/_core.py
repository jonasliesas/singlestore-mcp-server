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
import hashlib
import math
import os
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
from mcp.server.apps import Apps, ResourceCsp, ResourcePermissions
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from ..netguard import Unreachable
from . import splash

_F = TypeVar("_F", bound=Callable[..., Any])

# Failures the caller can act on: bad arguments, missing objects, SQL errors, a network that's away.
_EXPECTED_ERRORS = (ValueError, LookupError, s2.Error, Unreachable)


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
    # The ids let the Workspace rebuild this head from its own copy (see app_page), so a
    # view's page needn't carry the ~0.45 MB ext-apps bundle through the host again.
    css = (_HERE / "shared.css").read_text(encoding="utf-8")
    js = (_HERE / "shared.js").read_text(encoding="utf-8")
    return (
        f'<style id="s2-shared-css">\n{css}\n</style>\n'
        f'<script type="module" id="s2-ext-apps">\n{_ext_apps_script()}\n</script>\n'
        f'<script type="module" id="s2-shared-js">\n{js}\n</script>'
    )


@cache
def head_hash() -> str:
    return hashlib.sha1(_head().encode("utf-8")).hexdigest()[:12]


# Stands for the shared head in a page sent without it (app_page with a matching ``head``).
SHARED_HEAD_MARKER = "<!--S2:SHARED-HEAD-->"


_VENDOR_MARKER = re.compile(r"<!--S2:VENDOR:([\w.-]+)-->")
# Stands for a vendor block the caller already has (app_page with matching ``vendors``).
VENDOR_HAVE_MARKER = "<!--S2:VENDOR-HAVE:{name}-->"
# name -> (hash, the inlined block): CodeMirror (~0.5 MB) is in both the SQL Editor's and the Notebook's page,
# so the Workspace keeps the blocks it got once and asks for later pages without them.
_VENDOR_BLOCKS: dict[str, tuple[str, str]] = {}


def _vendor_inline(match: re.Match[str]) -> str:
    """Inline a file from vendor/: a .js file as a classic script, a .json file as a data block.

    The block is marked ``data-s2-vendor`` / ``data-s2-hash`` (first attributes, so the Workspace can find it)."""
    name = match.group(1)
    if name in _VENDOR_BLOCKS:
        return _VENDOR_BLOCKS[name][1]
    path = _HERE / "vendor" / name
    if name.endswith(".json") and not path.exists():
        text = "[]"  # optional data (e.g. the function list); the app copes without it
    else:
        text = path.read_text(encoding="utf-8")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    mark = f'data-s2-vendor="{name}" data-s2-hash="{digest}"'
    if name.endswith(".json"):
        # "<\/" is the same JSON string but can't close the <script> element.
        block_id = "vendor-" + name.rsplit(".", 1)[0]
        block = f'<script {mark} type="application/json" id="{block_id}">{text.replace("</", "<\\/")}</script>'
    else:
        if "</script" in text.lower():
            raise RuntimeError(f"vendor/{name} contains '</script' and can't be inlined")
        block = f"<script {mark}>\n{text}\n</script>"
    _VENDOR_BLOCKS[name] = (digest, block)
    return block


def without_vendors(html: str, have: dict[str, str] | None) -> tuple[str, list[str]]:
    """``html`` with the vendor blocks the caller already has (name -> hash, matching) replaced by
    VENDOR_HAVE_MARKER; returns (html, names left out)."""
    omitted = []
    for name, digest in (have or {}).items():
        known = _VENDOR_BLOCKS.get(name)
        if known and known[0] == digest and known[1] in html:
            html = html.replace(known[1], VENDOR_HAVE_MARKER.format(name=name), 1)
            omitted.append(name)
    return html, omitted


# Server mode: the file browser's local files are the user's own folder in Studio on the server, not "this computer".
_LOCAL_FILES = ('label: "This computer", title: "Files on this computer (the SQL folder and your user folder)"',
                'label: "My files", title: "Your own files in Studio on the server (the SQL folder is inside)"')


def _server_wording(html: str) -> str:
    from .. import server_mode

    return html.replace(*_LOCAL_FILES) if server_mode.enabled() else html


def build_page(template: str) -> str:
    html = (_HERE / template).read_text(encoding="utf-8")
    if _HEAD_MARKER not in html:
        raise RuntimeError(f"{template} is missing the {_HEAD_MARKER} marker")
    html = _VENDOR_MARKER.sub(_vendor_inline, html)
    html = splash.expand(html)  # the Studio splash (the Workspace)
    html = html.replace(_HEAD_MARKER, _head(), 1)
    html = _server_wording(html)
    build = hashlib.sha1(html.encode("utf-8")).hexdigest()[:8]
    meta = f'<meta name="s2-build" content="{build}">\n<meta name="s2-head" content="{head_hash()}">'
    return html.replace("<head>", f"<head>\n{meta}", 1)


_browser_url_factory: Callable[[str, dict[str, Any]], str] | None = None


def set_browser_url_factory(factory: Callable[[str, dict[str, Any]], str]) -> None:
    global _browser_url_factory
    _browser_url_factory = factory


_browser_reset: Callable[[], Any] | None = None


def set_browser_reset(reset: Callable[[], Any]) -> None:
    """How "Reset browser links" gives the browser view a new token (the process's BrowserView)."""
    global _browser_reset
    _browser_reset = reset


def reset_browser_links() -> bool:
    """New browser-view token: every open browser tab and earlier link stops working. False when there's no view."""
    if _browser_reset is None:
        return False
    _browser_reset()
    return True


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


# Hosts allowed to serve images into apps that render user content (notebook
# text cells). Hosts like Claude enforce this CSP on the app's sandbox.
IMAGE_DOMAINS = [d.strip() for d in os.environ.get(
    "SINGLESTORE_MCP_IMAGE_DOMAINS",
    "https://raw.githubusercontent.com,https://*.githubusercontent.com,https://github.com",
).split(",") if d.strip()]


def register_app(uri: str, template: str, *, name: str, description: str, external_images: bool = False) -> None:
    _PAGES[uri] = build_page(template)
    apps.add_html_resource(
        uri,
        _PAGES[uri],
        name=name,
        description=description,
        prefers_border=True,
        csp=ResourceCsp(resource_domains=IMAGE_DOMAINS) if external_images else None,
        # Lets "Copy" put the browser link on the clipboard where the host allows it.
        permissions=ResourcePermissions(clipboard_write={}),
    )


def page_html(uri: str) -> str | None:
    return _PAGES.get(uri)


_BUILD_RE = re.compile(r'<meta name="s2-build" content="([0-9a-f]+)">')
_LITE: dict[str, tuple[str, str]] = {}


def page_parts(uri: str) -> tuple[str, str, str] | None:
    """(build, full page, page with the shared head replaced by SHARED_HEAD_MARKER), or None."""
    html = _PAGES.get(uri)
    if html is None:
        return None
    if uri not in _LITE:
        match = _BUILD_RE.search(html)
        _LITE[uri] = (match.group(1) if match else "", html.replace(_head(), SHARED_HEAD_MARKER, 1))
    build, lite = _LITE[uri]
    return build, html, lite


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
