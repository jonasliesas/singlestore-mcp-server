"""Standalone SingleStore Workspace: the workspace in a browser window, without Claude.

    pythonw -m singlestore_mcp.workspace_app [--database SASDP] [--view sql] [--browser]

Starts the app server (the same in-process "browser view" the MCP server
uses for "Open in browser") on 127.0.0.1 and opens the workspace in an Edge
app window (or the default browser with --browser). A second launch reuses
the running server and just opens another window. The server shuts down a
few minutes after the last window closes (open pages ping it every 30 s).

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
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8790
IDLE_SHUTDOWN_SECONDS = 300
_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _state_file() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "singlestore-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "workspace.json"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Open the SingleStore Workspace in a browser window.")
    parser.add_argument("--database", help="database to start in")
    parser.add_argument("--view", default="sql", choices=["sql", "schema", "pipelines", "cluster"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--browser", action="store_true", help="open in the default browser instead of an Edge app window")
    parser.add_argument("--no-open", action="store_true", help="start the server and print the URL, without opening a window")
    opts = parser.parse_args()
    args = {k: v for k, v in {"database": opts.database, "view": None if opts.view == "sql" else opts.view}.items() if v}

    state = _running()
    if state:
        if not opts.no_open:
            _open_window(_workspace_url(state, args), opts.browser)
        return

    # Importing the server registers every tool and app page; no MCP client needed.
    from .apps.browser_view import BrowserView
    from .db import db
    from .server_impl import mcp

    db.warm()

    token = secrets.token_urlsafe(24)
    try:
        view = BrowserView(mcp, port=opts.port, token=token)
        url = view.url("sql_editor", {})  # starts the HTTP server
    except OSError:  # port taken by something else: use any free port
        view = BrowserView(mcp, port=0, token=token)
        url = view.url("sql_editor", {})
    state = {"port": urllib.parse.urlsplit(url).port, "token": token, "pid": os.getpid()}
    _state_file().write_text(json.dumps(state), encoding="utf-8")
    if opts.no_open:
        print(_workspace_url(state, args), flush=True)
    else:
        _open_window(_workspace_url(state, args), opts.browser)

    try:
        while time.time() - view.last_activity < IDLE_SHUTDOWN_SECONDS:
            time.sleep(10)
    finally:
        try:
            if json.loads(_state_file().read_text(encoding="utf-8")).get("pid") == os.getpid():
                _state_file().unlink()
        except (OSError, ValueError):
            pass
        from . import assistant

        assistant._close_all()


if __name__ == "__main__":
    main()
