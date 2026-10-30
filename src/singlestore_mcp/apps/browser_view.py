"""Open an app in a normal browser window ("Open in browser").

Hosts like Claude render apps inside the chat column. This serves the same
app HTML from a small HTTP server inside the MCP server process, wrapped in a
minimal MCP Apps host page (browser_host.html) that forwards the app's tool
calls to this server in-process, so the app gets the whole browser window.

Security: listens on 127.0.0.1 only, every URL carries a random token, POSTs
must come from the page's own origin and Host must match (blocks other sites
and DNS rebinding), and only app tools plus a few pipeline actions the apps
use are callable.

Stable address: the port and token are kept in the server's home folder
(``browser_address``) and reused by the next server process of the same MCP
client (``restart_server``, the standby, a crash restart), which listens on
them again right at start. An open tab then reconnects by itself
(browser_host.html retries for a while). The token changes only on request
(``rotate_token``, the Connections window's "Reset browser links").
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError

from ..browser_address import new_token
from . import splash
from ._core import page_html

HOST_NAME = "singlestore-browser-view"
_HOST_PAGE = Path(__file__).with_name("browser_host.html")
# Plain (non-app) tools the apps' buttons call.
_EXTRA_TOOLS = {"start_pipeline", "stop_pipeline", "test_pipeline", "browser_link", "workload_status", "workload_stop"}
# Calls the page may send again after the connection dropped mid-call (besides read-only / idempotent tools):
# an autosave just writes the latest text again.
_REPEATABLE = {"sql_editor_save_draft"}
_MAX_BODY = 1_000_000


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


# How long a new server waits for its predecessor (same slot) to let go of the port.
BIND_WAIT_S = _env_float("SINGLESTORE_MCP_BROWSER_BIND_WAIT", 15)
# How long an open page keeps trying to reach the server before it shows "Session ended".
RECONNECT_S = _env_float("SINGLESTORE_MCP_BROWSER_RECONNECT", 60)


class AddressStore(Protocol):
    def load(self) -> tuple[int, str] | None: ...
    def save(self, port: int, token: str) -> None: ...


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """Owns its port exclusively. Python's default SO_REUSEADDR lets two servers
    bind the same port on Windows, so a stale standalone workspace would keep
    answering requests meant for the new one."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class BrowserView:
    """``port``/``token``: a fixed address (the standalone workspace). ``store``: where the address is kept
    across restarts (or a function returning that store, called at start); its address is reused."""

    def __init__(self, mcp: Any, port: int = 0, token: str | None = None,
                 store: AddressStore | Callable[[], AddressStore | None] | None = None, *,
                 bind: str = "127.0.0.1", public_base: str | None = None,
                 allowed_hosts: tuple[str, ...] = (), allowed_origins: tuple[str, ...] = ()) -> None:
        """Server mode (``studio_server``): ``bind`` is the listen address, ``public_base`` the address the user's
        browser opens (e.g. "/studio/" behind the hub: links say that instead of 127.0.0.1:<port>/<token>/), and
        ``allowed_hosts`` / ``allowed_origins`` the Host and Origin values accepted besides the listen address."""
        self._mcp = mcp
        self._bind_host = bind
        self._public_base = public_base
        self._allowed_hosts = tuple(allowed_hosts)
        self._allowed_origins = tuple(allowed_origins)
        self.extra_routes: dict[str, Callable[[Any], Any]] = {}  # POST <token>/<route> -> fn(json body) -> json
        self._port = port
        self._token = token or new_token()
        self._fixed = bool(port or token)
        self._store_arg = store
        self._store: AddressStore | None = None
        self._resolved = False
        self._kept = False  # the address came from the store: wait for a predecessor to release the port
        self._kept_changed = False
        self.last_activity = time.time()  # any request; open pages ping every 30 s
        self.allow_shutdown = False        # standalone workspace: /shutdown stops it
        self.shutdown_requested = False
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._tools: dict[str, dict[str, Any]] = {}
        self._repeatable: list[str] = []

    # ------------------------------------------------------------ address
    def _resolve(self) -> None:
        """Find the store and its kept address (once; under the lock)."""
        if self._resolved:
            return
        self._resolved = True
        store = self._store_arg
        if callable(store) and not hasattr(store, "load"):
            try:
                store = store()
            except Exception:  # noqa: BLE001 - no kept address: a random one, as before
                store = None
        self._store = store  # type: ignore[assignment]
        if self._store is not None and not self._fixed:
            kept = self._store.load()
            if kept:
                self._port, self._token = kept
                self._kept = True

    def has_kept_address(self) -> bool:
        with self._lock:
            self._resolve()
            return self._kept

    def start_early(self) -> None:
        """Listen right away (in the background) when this server has a kept address, so a tab left open
        across a restart reaches the new server; otherwise the first browser link starts it."""
        def go() -> None:
            try:
                if self.has_kept_address():
                    self._ensure_started()
            except Exception:  # noqa: BLE001 - the first browser link tries again
                pass

        threading.Thread(target=go, name="browser-view-start", daemon=True).start()

    def rotate_token(self) -> str:
        """A new token: every open tab and link made before stops working (shows "Session ended")."""
        with self._lock:
            self._resolve()
            self._token = new_token()
            if self._httpd is not None and self._store is not None:
                self._store.save(self._httpd.server_address[1], self._token)
            else:
                self._kept_changed = True  # saved once it listens
        return self._token

    def close(self) -> None:
        """Stop listening (tests; a server process just exits)."""
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    @property
    def port(self) -> int | None:
        return self._httpd.server_address[1] if self._httpd is not None else None

    # ------------------------------------------------------------ serving
    def url(self, tool: str, arguments: dict[str, Any]) -> str:
        self._ensure_started()
        if tool not in self._tools or "resourceUri" not in self._tools[tool].get("_meta", {}).get("ui", {}):
            raise ValueError(f"{tool!r} is not an app tool")
        port = self._httpd.server_address[1]  # type: ignore[union-attr]
        query = urllib.parse.urlencode({"tool": tool, "args": json.dumps(arguments)})
        if self._public_base is not None:
            return f"{self._public_base}?{query}"
        return f"http://127.0.0.1:{port}/{self._token}/?{query}"

    def _ensure_started(self) -> None:
        with self._lock:
            if self._httpd is not None:
                return
            self._resolve()
            if not self._tools:
                tools = asyncio.run(self._mcp.list_tools())
                for t in tools:
                    d = t.model_dump(by_alias=True, exclude_none=True, mode="json")
                    if "ui" in d.get("_meta", {}) or t.name in _EXTRA_TOOLS:
                        self._tools[t.name] = d
                        hints = d.get("annotations") or {}
                        if hints.get("readOnlyHint") or hints.get("idempotentHint") or t.name in _REPEATABLE:
                            self._repeatable.append(t.name)
            self._httpd = self._bind()
            self._httpd.daemon_threads = True
            threading.Thread(target=self._httpd.serve_forever, name="browser-view", daemon=True).start()
            port = self._httpd.server_address[1]
            if self._store is not None and not self._fixed and (not self._kept or port != self._port or self._kept_changed):
                # First start, or the kept port was taken by another program: keep this one from now on.
                try:
                    self._store.save(port, self._token)
                except OSError:
                    pass
            self._port = port

    def _bind(self) -> ThreadingHTTPServer:
        handler = self._handler()
        if self._port:
            # A kept port: the previous server of this client may still be stopping (restart_server stops it
            # while this one starts), so wait a little for it to let go.
            deadline = time.monotonic() + (BIND_WAIT_S if self._kept else 0)
            while True:
                try:
                    return _ExclusiveHTTPServer((self._bind_host, self._port), handler)
                except OSError:
                    if time.monotonic() >= deadline:
                        if not self._kept:
                            raise  # a fixed port (standalone workspace): the caller picks another
                        break
                    time.sleep(0.2)
        # A new port: not one another client's slot keeps (that client would find it taken when it restarts).
        others = getattr(self._store, "other_ports", lambda: set())()
        for _ in range(5):
            httpd = _ExclusiveHTTPServer((self._bind_host, 0), handler)
            if httpd.server_address[1] not in others:
                break
            httpd.server_close()
        return httpd

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

            def _hosts(self) -> tuple[str, ...]:
                port = self.server.server_address[1]
                own = {f"127.0.0.1:{port}", f"{view._bind_host}:{port}"}
                return (*own, *view._allowed_hosts)

            def _origins(self) -> tuple[str, ...]:
                return (*(f"http://{h}" for h in self._hosts()), *view._allowed_origins)

            def _authorized(self) -> tuple[bool, str]:
                parts = urllib.parse.urlsplit(self.path)
                prefix = f"/{view._token}/"
                if self.headers.get("Host") not in self._hosts():
                    return False, ""
                if not secrets.compare_digest(parts.path[: len(prefix)], prefix):
                    return False, ""
                view.last_activity = time.time()
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
                    page = splash.expand(_HOST_PAGE.read_text(encoding="utf-8"))
                    if view._public_base is not None:  # server mode: the hub's account page (sign out) next to it
                        page = page.replace("<!--S2:SERVER-->", '<script>window.S2_SERVER = { account: "../account" };</script>', 1)
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                if route == "ping":
                    return self._json(200, {"ok": True})
                if route == "session":  # what the page needs to reconnect on its own
                    return self._json(200, {"repeatable": view._repeatable, "reconnect_s": RECONNECT_S})
                if route == "shutdown" and view.allow_shutdown:
                    view.shutdown_requested = True
                    return self._json(200, {"ok": True})
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
                extra = view.extra_routes.get(route) if ok else None
                length = int(self.headers.get("Content-Length") or 0)
                if length > _MAX_BODY:
                    self.close_connection = True
                    return self._send(413, b"Too large", "text/plain")
                # Read the body before answering, even a refusal: on Windows, closing a socket with unread data
                # resets the connection, and the client sees an abort instead of the 403.
                raw = self.rfile.read(length) if length else b""
                if not ok or (route != "call" and extra is None) or self.headers.get("Origin") not in self._origins():
                    return self._send(403, b"Forbidden", "text/plain")
                if extra is not None:  # server mode: the hub's own calls (e.g. a fresh SAS Viya sign-in)
                    try:
                        return self._json(200, extra(json.loads(raw or b"{}")))
                    except (ValueError, TypeError, KeyError) as exc:
                        return self._json(400, {"error": str(exc)[:300]})
                try:
                    body = json.loads(raw or b"{}")
                    name, arguments = body["name"], body.get("arguments") or {}
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"error": "expected {name, arguments}"})
                self._json(200, view.call(name, arguments))

        return Handler


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}
