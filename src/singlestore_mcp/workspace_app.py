"""Standalone SingleStore Workspace: the workspace in a browser window, without Claude.

    pythonw -m singlestore_mcp.workspace_app [--database SASDP] [--view sql] [--browser]

Starts the app server (the same in-process "browser view" the MCP server
uses for "Open in browser") on 127.0.0.1 and opens the workspace in an Edge
app window (or the default browser with --browser). A second launch reuses
the running server and just opens another window. The server shuts down a
few minutes after the last window closes (open pages ping it every 30 s).

Its address (port and token) is kept in ``workspace.json`` and reused by the
next start, so a window left open while the server was replaced (newer code)
reconnects by itself. "Reset browser links" (Connections) gives it a new token.

Reads the same SINGLESTORE_* environment variables as the MCP server. The
editor's chat works through the in-editor assistant (Claude Code); "send to
the Claude chat" isn't available here.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import fileio
from .paths import data_dir

DEFAULT_PORT = 8790
IDLE_SHUTDOWN_SECONDS = 300
_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def code_fingerprint() -> str:
    """Changes whenever a file of this package changes (so a running server can be replaced)."""
    import hashlib

    h = hashlib.sha1()
    root = Path(__file__).parent
    for p in sorted(root.rglob("*")):
        if p.suffix in (".py", ".html", ".js", ".css", ".json", ".md") and "__pycache__" not in p.parts:
            st = p.stat()
            h.update(f"{p.relative_to(root)}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()[:12]


def _stop(state: dict) -> None:
    """Stop a running workspace server (outdated code): ask it, then end the process."""
    base = f"http://127.0.0.1:{state['port']}/{state['token']}"
    try:
        urllib.request.urlopen(f"{base}/shutdown", timeout=2).read()
    except OSError:
        pass
    for _ in range(30):
        time.sleep(0.2)
        try:
            urllib.request.urlopen(f"{base}/ping", timeout=1)
        except OSError:
            return
    try:
        os.kill(int(state["pid"]), 9)
    except (OSError, ValueError, KeyError):
        pass
    time.sleep(0.5)


def _state_file() -> Path:
    return data_dir() / "workspace.json"


def _previous() -> dict:
    """The address the last workspace server used (kept after it stopped), or {}."""
    try:
        state = json.loads(_state_file().read_text(encoding="utf-8"))
        port, token = state.get("port"), state.get("token")
        if isinstance(port, int) and isinstance(token, str) and len(token) >= 16:
            return {"port": port, "token": token}
    except (OSError, ValueError, AttributeError):
        pass
    return {}


class _StateStore:
    """Keeps a new token (Reset browser links) in workspace.json."""

    def __init__(self, state: dict) -> None:
        self.state = state

    def load(self) -> None:
        return None

    def save(self, port: int, token: str) -> None:
        self.state.update(port=port, token=token)
        fileio.write_text(_state_file(), json.dumps(self.state))


def _running() -> dict | None:
    """The state of a workspace server that's already running, if any."""
    try:
        state = json.loads(_state_file().read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{state['port']}/{state['token']}/ping"
        with urllib.request.urlopen(url, timeout=2) as res:
            return state if res.status == 200 else None
    except (OSError, ValueError, KeyError):
        return None


def _workspace_url(state: dict, args: dict) -> str:
    query = urllib.parse.urlencode({"tool": "sql_editor", "args": json.dumps(args)})
    return f"http://127.0.0.1:{state['port']}/{state['token']}/?{query}"


def _open_window(url: str, use_browser: bool) -> None:
    edge = next((p for p in _EDGE_PATHS if Path(p).exists()), None) or shutil.which("msedge")
    if edge and not use_browser:
        # --app: its own window without tabs or address bar, like a desktop app.
        subprocess.Popen([edge, f"--app={url}"], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        webbrowser.open(url)


def _free_port(preferred: int) -> int:
    """``preferred`` if nothing listens there, else any free port (checked with an exclusive bind)."""
    import socket

    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise OSError("No free port")


def _warm_database() -> None:
    try:
        from .db import db

        db.warm()
    except Exception:  # noqa: BLE001 - the app shows connection problems itself
        pass


_SPLASH = """<!doctype html><html><head><meta charset="utf-8"><title>SingleStore Workspace</title>
<style>html,body{height:100%;margin:0}body{display:grid;place-items:center;font:14px system-ui,'Segoe UI',sans-serif;
color:#555;background:#fff}@media(prefers-color-scheme:dark){body{background:#1e1e1e;color:#aaa}}
.s{width:18px;height:18px;border:2px solid #ccc;border-top-color:#7c3aed;border-radius:50%;
display:inline-block;vertical-align:-4px;margin-right:8px;animation:r .8s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
</style></head><body><div id="m"><span class="s"></span>Starting SingleStore Workspace…</div>
<script>
const target = __TARGET__, ping = target.split("?")[0] + "ping", t0 = Date.now();
(function poll() {
  fetch(ping + "?t=" + Date.now(), { mode: "no-cors", cache: "no-store" })
    .then(() => location.replace(target))
    .catch(() => {
      if (Date.now() - t0 > 90000) document.getElementById("m").textContent =
        "The workspace didn't start. Close this window and try the shortcut again.";
      else setTimeout(poll, 250);
    });
})();
</script></body></html>"""


def _splash(target: str) -> Path:
    """A local "Starting…" page that switches to ``target`` once the server answers."""
    path = data_dir() / "starting.html"
    path.write_text(_SPLASH.replace("__TARGET__", json.dumps(target)), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Open the SingleStore Workspace in a browser window.")
    parser.add_argument("--database", help="database to start in")
    parser.add_argument("--view", default="sql", choices=["sql", "notebook", "schema", "pipelines", "cluster", "history", "security", "config", "alerts", "workload", "connections"])
    parser.add_argument("--port", type=int, default=None, help=f"default: the last one used, else {DEFAULT_PORT}")
    parser.add_argument("--browser", action="store_true", help="open in the default browser instead of an Edge app window")
    parser.add_argument("--no-open", action="store_true", help="start the server and print the URL, without opening a window")
    opts = parser.parse_args()
    args = {k: v for k, v in {"database": opts.database, "view": None if opts.view == "sql" else opts.view}.items() if v}

    build = code_fingerprint()
    state = _running()
    if state and state.get("build") == build:
        if not opts.no_open:
            _open_window(_workspace_url(state, args), opts.browser)
        return
    if state:
        _stop(state)  # running an older version of the code: replace it

    # Start-up work runs in parallel: the window opens at once on a "Starting…"
    # page, the database connections open in the background, and the server
    # code loads meanwhile (the slowest part: the MCP SDK). The page switches
    # to the workspace as soon as the server answers.
    # The last server's address again (open windows reconnect), unless another port is asked for.
    previous = _previous()
    preferred = opts.port or previous.get("port") or DEFAULT_PORT
    token = previous.get("token") if previous.get("port") == preferred else None
    token = token or secrets.token_urlsafe(24)
    port = _free_port(preferred)
    state = {"port": port, "token": token, "pid": os.getpid(), "build": build}
    if opts.no_open:
        pass
    else:
        _open_window(_splash(_workspace_url(state, args)).as_uri(), opts.browser)
    threading.Thread(target=_warm_database, name="db-warm-early", daemon=True).start()

    # Importing the server registers every tool and app page; no MCP client needed.
    from . import server_impl
    from .apps._core import set_browser_reset, set_browser_url_factory
    from .apps.browser_view import BrowserView
    from .server_impl import mcp

    store = _StateStore(state)
    try:
        view = BrowserView(mcp, port=port, token=token, store=store)
        view.url("sql_editor", {})  # starts the HTTP server
    except OSError:  # the port was taken in the meantime: use any free port (the splash can't follow; reopen)
        view = BrowserView(mcp, port=0, token=token, store=store)
        state["port"] = urllib.parse.urlsplit(view.url("sql_editor", {})).port
        if not opts.no_open:
            _open_window(_workspace_url(state, args), opts.browser)
    view.allow_shutdown = True
    # Browser links and "Reset browser links" in this process use this view (not a second server on an MCP slot).
    server_impl.browser_view = view
    set_browser_url_factory(view.url)
    set_browser_reset(view.rotate_token)
    from . import alerts

    alerts.start()  # background alert checks while the workspace server runs
    from . import metrics_store

    metrics_store.start()  # metrics history for the Cluster Monitor (SINGLESTORE_MCP_METRICS=0 turns it off)
    fileio.write_text(_state_file(), json.dumps(state))
    if opts.no_open:
        print(_workspace_url(state, args), flush=True)

    try:
        while time.time() - view.last_activity < IDLE_SHUTDOWN_SECONDS and not view.shutdown_requested:
            time.sleep(1)
    finally:
        # The file stays (its port and token are reused by the next start); only the pid goes.
        try:
            kept = json.loads(_state_file().read_text(encoding="utf-8"))
            if kept.get("pid") == os.getpid():
                kept.pop("pid", None)
                fileio.write_text(_state_file(), json.dumps(kept))
        except (OSError, ValueError, AttributeError):
            pass
        from . import assistant

        alerts.stop(timeout=2.0)
        metrics_store.stop(timeout=2.0)
        assistant._close_all()


if __name__ == "__main__":
    main()
