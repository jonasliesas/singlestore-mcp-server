"""SAS SpeedyStore Studio as an HTTP app on a server: one process per user, started by the hub.

    python -m singlestore_mcp.studio_server --stdin-config      # started by the hub (singlestore_mcp.hub)
    python -m singlestore_mcp.studio_server                     # one user, settings from the environment

The same Workspace as ``workspace_app`` (the in-process browser view of the MCP server), without Claude, stdio,
an Edge window or the restart supervisor. Server mode (``server_mode``) is switched on: notebooks and the Claude
assistants are off unless switched on, secrets go to the encrypted file store (no OS keyring), and the file
browser's "This computer" is the user's own Studio folder (``HOME``).

Started by the hub, it reads one JSON line from stdin before anything else::

    {"token": "<path token>", "secret_key": "<Fernet key>", "user": "<SAS user id>",
     "public_base": "/studio/", "sas_client": {"id": "...", "secret": "..."},
     "seed": {"viya": {...}, "singlestore": {...}}}

so nothing secret is in its command line or environment (another process of the same OS user could read those).
It then prints ``STUDIO-READY <port>`` and serves until its stdin closes (the hub stopped it or went away).
The hub reaches it on 127.0.0.1:<port>/<token>/ only; the user's browser never sees the port or token.

Environment (the hub sets these): SINGLESTORE_MCP_HOME (state), HOME / USERPROFILE (the user's files),
SINGLESTORE_MCP_SQL_DIR, SINGLESTORE_STUDIO_BIND (default 127.0.0.1), SINGLESTORE_STUDIO_PORT (default 0: any),
SINGLESTORE_MCP_NOTEBOOKS / SINGLESTORE_MCP_ASSISTANT (default off). Standalone (no --stdin-config):
SINGLESTORE_STUDIO_TOKEN (default: a new one, printed), SINGLESTORE_STUDIO_PUBLIC_BASE,
SINGLESTORE_STUDIO_ALLOWED_HOSTS / _ORIGINS (comma-separated; behind your own reverse proxy),
SINGLESTORE_MCP_SECRET_KEY (Fernet key of the secrets file; default: a key file in the state folder).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any


def _csv(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(name, "").split(",") if x.strip())


def apply_seed(seed: dict[str, Any]) -> dict[str, Any]:
    """Put the hub's sign-in and defaults into this user's state: the SAS Viya connection with the user's token
    (from the hub's SAS Logon sign-in), and a default SingleStore connection (host/port/database, no password)
    if the user has none of that name yet. Returns what was done (no secrets)."""
    from . import connections, sas_viya

    done: dict[str, Any] = {}
    viya = seed.get("viya") or {}
    if viya.get("url") and viya.get("name"):
        name, url = str(viya["name"]), str(viya["url"]).rstrip("/")
        values = {"name": name, "url": url, "client_id": viya.get("client_id") or "vscode",
                  "ssl_verify": bool(viya.get("ssl_verify", True))}
        if viya.get("compute_context"):
            values["compute_context"] = viya["compute_context"]
        existing = next((c for c in sas_viya.saved() if c["name"] == name), None)
        if existing is None:
            sas_viya.save_connection(values)
        elif existing["url"] != url or existing["client_id"] != values["client_id"]:
            sas_viya.save_connection({**existing, **values}, original_name=name)
        tokens = viya.get("tokens") or {}
        if tokens.get("access_token"):
            sas_viya._store(name, tokens, values["client_id"], "SAS Logon (Studio sign-in)", url)
            done["viya_token"] = True
        if not sas_viya.active_name() or viya.get("activate"):
            sas_viya.activate(name)
        done["viya"] = name
    s2 = seed.get("singlestore") or {}
    if s2.get("host") and s2.get("name"):
        if not any(p.name == s2["name"] for p in connections.profiles()):
            fresh = not [p for p in connections.profiles() if not p.builtin]
            connections.save({"name": s2["name"], "host": s2["host"], "port": int(s2.get("port") or 3306),
                              "user": s2.get("user") or "", "database": s2.get("database") or None,
                              "ssl_disabled": bool(s2.get("ssl_disabled"))})
            if fresh:
                connections.activate(s2["name"])
            done["singlestore"] = s2["name"]
    return done


def _quiet(fn: Any) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001 - the app shows connection problems itself
        pass


def _watch_stdin(view: Any) -> None:
    """The hub keeps stdin open while this process should run: EOF (stopped, or the hub died) ends it."""
    def run() -> None:
        try:
            while sys.stdin.readline():
                pass
        except (OSError, ValueError):
            pass
        view.shutdown_requested = True

    threading.Thread(target=run, name="studio-stdin", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SAS SpeedyStore Studio, server mode (one user).")
    parser.add_argument("--stdin-config", action="store_true", help="read the hub's settings (one JSON line) from stdin")
    opts = parser.parse_args(argv)
    os.environ["SINGLESTORE_MCP_SERVER_MODE"] = "1"
    os.environ.setdefault("SINGLESTORE_MCP_SAS_IMPORT", "0")  # never another tool's sign-in from the server's folders

    from . import server_mode

    cfg: dict[str, Any] = {}
    if opts.stdin_config:
        line = sys.stdin.readline()
        cfg = json.loads(line) if line.strip() else {}
    token = cfg.get("token") or os.environ.get("SINGLESTORE_STUDIO_TOKEN") or None
    key = cfg.get("secret_key") or os.environ.get("SINGLESTORE_MCP_SECRET_KEY") or None
    server_mode.set_secret_key(key)
    server_mode.set_user(cfg.get("user") or os.environ.get("SINGLESTORE_STUDIO_USER") or None)
    sas_client = cfg.get("sas_client") or {}
    server_mode.set_sas_client(sas_client.get("id"), sas_client.get("secret"))
    public_base = cfg.get("public_base") or os.environ.get("SINGLESTORE_STUDIO_PUBLIC_BASE") or None
    bind = os.environ.get("SINGLESTORE_STUDIO_BIND", "127.0.0.1")
    port = int(os.environ.get("SINGLESTORE_STUDIO_PORT", "0") or 0)

    # Importing the server registers every tool and app page; no MCP client needed.
    from . import server_impl
    from .apps._core import set_browser_url_factory
    from .apps.browser_view import BrowserView
    from .server_impl import mcp

    seed_report: dict[str, Any] = {}
    if cfg.get("seed"):
        try:
            seed_report = apply_seed(cfg["seed"])
        except Exception as exc:  # noqa: BLE001 - the user can still set things up in Connections
            print(f"studio: couldn't apply the hub's settings: {type(exc).__name__}", file=sys.stderr, flush=True)

    from .db import db

    threading.Thread(target=lambda: _quiet(db.warm), name="db-warm", daemon=True).start()
    from .browser_address import new_token

    token = token or new_token()
    view = BrowserView(mcp, port=port, token=token, bind=bind, public_base=public_base,
                       allowed_hosts=_csv("SINGLESTORE_STUDIO_ALLOWED_HOSTS"), allowed_origins=_csv("SINGLESTORE_STUDIO_ALLOWED_ORIGINS"))
    view.extra_routes["hub/seed"] = lambda body: {"ok": True, "done": apply_seed(body)}
    view.url("sql_editor", {})  # starts the HTTP server
    server_impl.browser_view = view
    set_browser_url_factory(view.url)
    # No "Reset browser links" here: the address is the hub's business (set_browser_reset is left unset).
    from . import alerts, metrics_store

    alerts.start()
    metrics_store.start()
    print(f"STUDIO-READY {view.port}", flush=True)
    if not opts.stdin_config:
        base = public_base or f"http://{bind}:{view.port}/{token}/"
        print(f"Studio: {base}", flush=True)
        if seed_report:
            print(f"Studio: applied {sorted(seed_report)}", flush=True)
    else:
        _watch_stdin(view)
    try:
        idle = float(os.environ.get("SINGLESTORE_STUDIO_IDLE_MINUTES", "0") or 0) * 60
        while not view.shutdown_requested and not (idle and time.time() - view.last_activity > idle):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        alerts.stop(timeout=2.0)
        metrics_store.stop(timeout=2.0)
        view.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
