"""The browser view's stable address (offline): the port and token are kept per slot and reused by the next
server of the same MCP client, a predecessor's port is waited for, a port another program holds is given up
for a new one (kept from then on), one client's slot is never another's, and the token is checked and only
changes on request."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from singlestore_mcp import browser_address
from singlestore_mcp.apps import browser_view as bv


class _Tool:
    def __init__(self, name: str, ui: bool, read_only: bool) -> None:
        self.name = name
        self._d = {"name": name, "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": read_only},
                   **({"_meta": {"ui": {"resourceUri": f"ui://test/{name}.html"}}} if ui else {})}

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self._d


class _Result:
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"ok": True}}


class FakeMcp:
    async def list_tools(self) -> list[_Tool]:
        return [_Tool("app", True, True), _Tool("app_write", True, False), _Tool("run_sql", False, False)]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _Result:
        return _Result()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    monkeypatch.delenv(browser_address.ENV_SLOT, raising=False)
    monkeypatch.delenv("SINGLESTORE_MCP_SUPERVISED", raising=False)
    browser_address.release_slot()
    yield tmp_path
    browser_address.release_slot()


def _parts(url: str) -> tuple[int, str]:
    u = urllib.parse.urlsplit(url)
    return u.port, u.path.strip("/").split("/")[0]


def _get(port: int, token: str, route: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/{token}/{route}", timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _post(port: int, token: str, origin: str | None = "same") -> tuple[int, Any]:
    origin = f"http://127.0.0.1:{port}" if origin == "same" else origin
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{token}/call", method="POST",
                                 data=json.dumps({"name": "app", "arguments": {}}).encode(),
                                 headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def test_address_is_kept_and_reused_by_the_next_server(home) -> None:
    store = browser_address.SlotStore(0)
    first = bv.BrowserView(FakeMcp(), store=store)
    url = first.url("app", {"x": 1})
    port, token = _parts(url)
    assert store.load() == (port, token)
    first.close()

    second = bv.BrowserView(FakeMcp(), store=browser_address.SlotStore(0))
    assert second.has_kept_address()
    assert second.url("app", {"x": 1}) == url  # the same link keeps working
    assert _get(port, token, "ping") == 200
    second.close()


def test_start_early_listens_only_with_a_kept_address(home) -> None:
    fresh = bv.BrowserView(FakeMcp(), store=browser_address.SlotStore(3))
    fresh.start_early()
    time.sleep(0.5)
    assert fresh.port is None  # nothing kept yet: lazy, as before (the first link starts it)
    port, token = _parts(fresh.url("app", {}))
    fresh.close()

    again = bv.BrowserView(FakeMcp(), store=browser_address.SlotStore(3))
    again.start_early()  # a tab left open finds the server without anyone asking for a link
    deadline = time.monotonic() + 10
    while again.port is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert again.port == port and _get(port, token, "ping") == 200
    again.close()


def test_new_server_waits_for_its_predecessor_to_release_the_port(home, monkeypatch) -> None:
    monkeypatch.setattr(bv, "BIND_WAIT_S", 10)
    old = bv.BrowserView(FakeMcp(), store=browser_address.SlotStore(0))
    port, token = _parts(old.url("app", {}))
    new = bv.BrowserView(FakeMcp(), store=browser_address.SlotStore(0))
    started = threading.Thread(target=new.url, args=("app", {}))
    started.start()  # restart_server: the new server starts while the old one still stops
    time.sleep(0.8)
    assert new.port is None
    old.close()
    started.join(15)
    assert new.port == port and _get(port, token, "ping") == 200
    new.close()


def test_port_taken_by_another_program_gives_a_new_port_and_keeps_it(home, monkeypatch) -> None:
    monkeypatch.setattr(bv, "BIND_WAIT_S", 0.3)
    store = browser_address.SlotStore(0)
    first = bv.BrowserView(FakeMcp(), store=store)
    port, token = _parts(first.url("app", {}))
    first.close()
    squatter = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    squatter.bind(("127.0.0.1", port))
    squatter.listen()
    try:
        view = bv.BrowserView(FakeMcp(), store=store)
        new_port, new_token = _parts(view.url("app", {}))
        assert new_port != port and new_token == token
        assert store.load() == (new_port, token)  # kept from now on (no waiting next time)
        view.close()
    finally:
        squatter.close()


def test_fresh_port_avoids_other_slots_ports(home, monkeypatch) -> None:
    other = browser_address.SlotStore(1)
    view = bv.BrowserView(FakeMcp(), store=other)
    other_port, _ = _parts(view.url("app", {}))
    view.close()
    assert browser_address.SlotStore(0).other_ports() == {other_port}
    assert browser_address.SlotStore(1).other_ports() == set()


def test_token_is_checked_and_rotates_only_on_request(home) -> None:
    store = browser_address.SlotStore(0)
    view = bv.BrowserView(FakeMcp(), store=store)
    port, token = _parts(view.url("app", {}))
    assert _get(port, token, "ping") == 200
    assert _get(port, "x" * 32, "ping") == 404
    assert _post(port, "x" * 32)[0] == 403
    assert _post(port, token, origin=None)[0] == 403
    assert _post(port, token, origin="http://evil.example")[0] == 403
    status, body = _post(port, token)
    assert status == 200 and body["structuredContent"] == {"ok": True}
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/{token}/session", timeout=10) as r:
        session = json.loads(r.read())
    assert set(session["repeatable"]) == {"app"} and session["reconnect_s"] > 0  # read-only app tools only

    new = view.rotate_token()
    assert new != token and store.load() == (port, new)
    assert _get(port, token, "ping") == 404 and _get(port, new, "ping") == 200
    view.close()


def test_fixed_address_is_not_written_to_the_store_and_raises_when_taken(home) -> None:
    class Store:
        saved: list = []

        def load(self) -> None:
            return None

        def save(self, port: int, token: str) -> None:
            self.saved.append((port, token))

    store = Store()
    first = bv.BrowserView(FakeMcp(), port=0, token="t" * 32, store=store)
    port, _ = _parts(first.url("app", {}))
    assert store.saved == []
    with pytest.raises(OSError):  # the standalone workspace then picks another port itself
        bv.BrowserView(FakeMcp(), port=port, token="t" * 32).url("app", {})
    first.rotate_token()
    assert store.saved and store.saved[-1][0] == port
    first.close()


def test_slots_one_per_client(home) -> None:
    assert browser_address.claim_slot() == 0
    assert browser_address.claim_slot() == 0  # the same process keeps its slot
    code = "from singlestore_mcp import browser_address as b; print(b.claim_slot())"
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert other.stdout.strip() == "1"  # a second client while this one holds slot 0
    browser_address.release_slot()
    again = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert again.stdout.strip() == "0"  # free again once its holder is gone (the OS drops the lock with the process)


def test_supervised_server_uses_its_supervisors_slot(home, monkeypatch) -> None:
    monkeypatch.setenv("SINGLESTORE_MCP_SUPERVISED", "1")
    assert browser_address.store_for_this_process() is None  # no slot from the supervisor: random, not kept
    monkeypatch.setenv(browser_address.ENV_SLOT, "2")
    store = browser_address.store_for_this_process()
    assert store is not None and store.slot == 2
    assert not browser_address._held  # the supervisor holds the lock, not the server
