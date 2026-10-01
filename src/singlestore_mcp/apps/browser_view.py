"""Open an app in a normal browser window ("Open in browser").

Hosts like Claude render apps inside the chat column. This serves the same
app HTML from a small HTTP server inside the MCP server process, wrapped in a
minimal MCP Apps host page (browser_host.html) that forwards the app's tool
calls to this server in-process, so the app gets the whole browser window.

Security: listens on 127.0.0.1 only, every URL carries a random token, POSTs
must come from the page's own origin and Host must match (blocks other sites
and DNS rebinding), and only app tools plus a few pipeline actions the apps
use are callable.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ._core import page_html

HOST_NAME = "singlestore-browser-view"
_HOST_PAGE = Path(__file__).with_name("browser_host.html")
# Plain (non-app) tools the apps' buttons call.
_EXTRA_TOOLS = {"start_pipeline", "stop_pipeline", "test_pipeline", "browser_link"}
_MAX_BODY = 1_000_000


class BrowserView:
    def __init__(self, mcp: Any) -> None:
        self._mcp = mcp
        self._token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._tools: dict[str, dict[str, Any]] = {}

    def url(self, tool: str, arguments: dict[str, Any]) -> str:
        self._ensure_started()
        if tool not in self._tools or "resourceUri" not in self._tools[tool].get("_meta", {}).get("ui", {}):
            raise ValueError(f"{tool!r} is not an app tool")
        port = self._httpd.server_address[1]  # type: ignore[union-attr]
        query = urllib.parse.urlencode({"tool": tool, "args": json.dumps(arguments)})
        return f"http://127.0.0.1:{port}/{self._token}/?{query}"

    def _ensure_started(self) -> None:
        with self._lock:
            if self._httpd is not None:
                return
            tools = asyncio.run(self._mcp.list_tools())
            for t in tools:
                d = t.model_dump(by_alias=True, exclude_none=True, mode="json")
                if "ui" in d.get("_meta", {}) or t.name in _EXTRA_TOOLS:
                    self._tools[t.name] = d
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
            self._httpd.daemon_threads = True
            threading.Thread(target=self._httpd.serve_forever, name="browser-view", daemon=True).start()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            return _error_result(f"Tool {name!r} is not available in the browser view")
        try:
            result = asyncio.run(self._mcp.call_tool(name, arguments))
        except ToolError as exc:
            return _error_result(str(exc))
        return result.model_dump(by_alias=True, exclude_none=True, mode="json")

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        view = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep stdio (the MCP channel) clean
                pass

            def _origin(self) -> str:
                return f"http://127.0.0.1:{self.server.server_address[1]}"

            def _authorized(self) -> tuple[bool, str]:
                parts = urllib.parse.urlsplit(self.path)
                prefix = f"/{view._token}/"
                if self.headers.get("Host") != self._origin().removeprefix("http://"):
                    return False, ""
                if not secrets.compare_digest(parts.path[: len(prefix)], prefix):
                    return False, ""
                return True, parts.path[len(prefix):]

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data: Any) -> None:
                self._send(status, json.dumps(data).encode(), "application/json")

            def do_GET(self) -> None:  # noqa: N802
                ok, route = self._authorized()
                if not ok:
                    return self._send(404, b"Not found", "text/plain")
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if route == "":
                    return self._send(200, _HOST_PAGE.read_bytes(), "text/html; charset=utf-8")
                if route == "tool":
                    tool = view._tools.get(query.get("name", [""])[0])
                    return self._json(200, tool) if tool else self._json(404, {"error": "unknown tool"})
                if route == "resource":
                    html = page_html(query.get("uri", [""])[0])
                    if html is None:
                        return self._send(404, b"Not found", "text/plain")
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                return self._send(404, b"Not found", "text/plain")

            def do_POST(self) -> None:  # noqa: N802
                ok, route = self._authorized()
                if not ok or route != "call" or self.headers.get("Origin") != self._origin():
                    return self._send(403, b"Forbidden", "text/plain")
                length = int(self.headers.get("Content-Length") or 0)
                if length > _MAX_BODY:
                    return self._send(413, b"Too large", "text/plain")
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    name, arguments = body["name"], body.get("arguments") or {}
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"error": "expected {name, arguments}"})
                self._json(200, view.call(name, arguments))

        return Handler


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}
