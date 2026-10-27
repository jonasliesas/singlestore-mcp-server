"""The Workspace keeps working through a network break, without a restart: a test server of its own whose cluster
connection goes through a local fault proxy (``faultproxy``); the proxy drops every connection and refuses new ones
(VPN gone), the Workspace shows "Connection to SingleStore lost – retrying…", and once the proxy lets connections
through again the line goes away by itself ("Reconnected to SingleStore") and the SQL Editor runs queries again.
Read-only on the cluster."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from faultproxy import FaultProxy
from support import DB, McpSession, server_env
from uihelpers import find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]
WS = "SAS SpeedyStore Studio · Workspace"


def _home_through(proxy_port: int, test_home: Path, home: Path) -> None:
    """A server home whose active connection is the test home's, but reached through the proxy (same name, so the
    password still comes from the OS credential store)."""
    data = json.loads((test_home / "connections.json").read_text(encoding="utf-8"))
    for p in data.get("profiles", []):
        if p["name"] == data.get("active"):
            p["host"], p["port"] = "127.0.0.1", proxy_port
    (home / "connections.json").write_text(json.dumps(data), encoding="utf-8")


def _run(sql_frame: Any, sql: str) -> None:
    sql_frame.evaluate("(d) => window.dispatchEvent(new CustomEvent('s2:set-sql', {detail: d}))",
                       {"sql": sql, "run": True, "database": DB})


def test_workspace_shows_the_outage_and_recovers_by_itself(ui, test_home: Path, sql_dir: Path, tmp_path: Path) -> None:
    page, errors = ui
    data = json.loads((test_home / "connections.json").read_text(encoding="utf-8"))
    active = next((p for p in data.get("profiles", []) if p["name"] == data.get("active")), None)
    if not active or active.get("url") or active.get("auth", "password") != "password":
        pytest.skip("needs an active saved password connection with a host")
    proxy = FaultProxy(active["host"], int(active.get("port") or 3306))
    home = tmp_path / "home"
    home.mkdir()
    _home_through(proxy.port, test_home, home)
    env = server_env(home, sql_dir, {"SINGLESTORE_MCP_NO_STANDBY": "1", "SINGLESTORE_MCP_CONNECT_TIMEOUT": "3",
                                     "SINGLESTORE_MCP_CONNECT_DEADLINE": "6", "SINGLESTORE_MCP_PING_TIMEOUT": "2",
                                     "SINGLESTORE_MCP_PROBE_EVERY": "1"})
    s = McpSession(env, read_timeout=120)
    try:
        url = s.ok("browser_link", {"tool": "sql_editor", "arguments": {"database": DB, "view": "sql", "new_tab": True,
                                                                        "sql": "-- network"}})["url"]
        page.goto(url, wait_until="domcontentloaded")
        ws = find_frame(page, WS, 40)
        sql = find_frame(page, "SQL Editor", 40)
        assert wait_js(sql, "document.querySelector('.cm-content') && document.querySelector('.sq-run')", 30)
        _run(sql, "SELECT 1 AS before_outage")
        assert wait_js(sql, "/before_outage/i.test(document.body.innerText)", 40), text(sql)[-400:]

        # The network goes away: every connection dropped, new ones refused.
        proxy.drop()
        proxy.refuse()
        r = s.call("run_sql", {"sql": "SELECT 1"}, timeout=60)
        assert r.is_error and "reconnects by itself" in r.text, r.text[:300]
        ws.evaluate("dispatchEvent(new Event('online'))")  # the page checks at once (else on its next poll)
        assert wait_js(ws, "!document.querySelector('.ws-net').hidden && "
                           "/Connection to SingleStore lost/.test(document.querySelector('.ws-net').innerText)", 30), \
            ws.evaluate("document.querySelector('.ws-net')?.outerHTML")

        # Back: the line goes away by itself, the views hear it, and the editor runs queries again.
        proxy.restore()
        assert wait_js(ws, "document.querySelector('.ws-net').hidden", 30), ws.evaluate("document.querySelector('.ws-net')?.innerText")
        assert wait_js(ws, "/Reconnected to SingleStore/.test(document.body.innerText)", 10)
        _run(sql, "SELECT 2 AS after_outage")
        assert wait_js(sql, "/after_outage/i.test(document.body.innerText)", 40), text(sql)[-400:]
        assert not s.call("run_sql", {"sql": "SELECT 1"}).is_error
        crashes = [e for e in errors.drain() if e.startswith("pageerror")]
        assert not crashes, crashes
    finally:
        s.close()
        proxy.close()
