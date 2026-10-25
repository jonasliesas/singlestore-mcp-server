"""Start-up shortcuts (no cluster): the standby's pre-opened connections (singlestore_mcp.preconnect) and how the
pool takes them over, and the SQL Editor schema read ahead by sql_editor."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import db as db_mod
from singlestore_mcp import preconnect


class FakeConn:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.closed = False
        self.pings = 0
        self.fail_ping = False

    def ping(self, reconnect: bool = True) -> None:
        if self.fail_ping:
            raise OSError("gone")
        self.pings += 1

    def close(self) -> None:
        self.closed = True


class Profile:
    def __init__(self, auth: str = "password", database: str | None = "SASDP") -> None:
        self.auth, self.database, self.name = auth, database, "test"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import singlestoredb

    from singlestore_mcp import connections

    state: dict[str, Any] = {"profile": Profile(), "kwargs": {"host": "h", "password": "p", "database": "SASDP"},
                             "opened": []}

    def connect(**kw: Any) -> FakeConn:
        c = FakeConn(**kw)
        state["opened"].append(c)
        return c

    monkeypatch.setattr(singlestoredb, "connect", connect)
    monkeypatch.setattr(connections, "active", lambda: state["profile"])
    monkeypatch.setattr(connections, "connect_kwargs", lambda p: dict(state["kwargs"]))
    monkeypatch.setattr(db_mod.s2, "connect", connect)
    sys.modules.pop(preconnect.HOLDER_MODULE, None)
    yield state
    sys.modules.pop(preconnect.HOLDER_MODULE, None)


def _ready(p: preconnect.Preconnected) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not p._conns:
        time.sleep(0.01)


def test_standby_connections_are_taken_over_by_the_pool(fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(preconnect.ENV, "3")
    p = preconnect.start()
    assert p is not None
    _ready(p)
    sys.modules[preconnect.HOLDER_MODULE] = p.handover()
    assert all(c.kw.get("autocommit") is True for c in fake["opened"]) and len(fake["opened"]) == 3
    pool = db_mod.Database()
    pool.warm(connections=3)  # takes the three over: nothing new to open
    assert len(pool._idle) == 3 and pool._open == 3 and pool._default_db == "SASDP"
    assert len(fake["opened"]) == 3 and not any(c.closed for c in fake["opened"])
    assert preconnect.HOLDER_MODULE not in sys.modules  # taken once


def test_other_settings_meanwhile_close_them(fake: dict[str, Any]) -> None:
    p = preconnect.Preconnected(2)
    _ready(p)
    sys.modules[preconnect.HOLDER_MODULE] = p.handover()
    fake["kwargs"] = {**fake["kwargs"], "password": "changed"}  # the active connection was edited meanwhile
    pool = db_mod.Database()
    pool._adopt_preconnected()
    assert not pool._idle and pool._open == 0
    assert all(c.closed for c in fake["opened"])


def test_token_logins_and_turned_off_open_nothing(fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(preconnect.ENV, "0")
    assert preconnect.start() is None
    fake["profile"] = Profile(auth="entra")
    p = preconnect.Preconnected(2)
    p._thread.join(5)
    assert fake["opened"] == [] and p.handover().take(fake["kwargs"]) == []


def test_stale_connections_are_not_handed_over(fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    p = preconnect.Preconnected(2)
    _ready(p)
    p._last_ok = time.monotonic() - preconnect.FRESH_S - 1  # pings stopped working a while ago
    assert p.handover().take(fake["kwargs"]) == []
    assert all(c.closed for c in fake["opened"])


def test_pings_drop_dead_connections_and_keep_the_rest(fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preconnect, "PING_S", 0.05)
    p = preconnect.Preconnected(2)
    _ready(p)
    fake["opened"][0].fail_ping = True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(p._conns) != 1:
        time.sleep(0.02)
    got = p.handover().take(fake["kwargs"])
    assert got == [fake["opened"][1]] and fake["opened"][0].closed and fake["opened"][1].pings >= 1


def test_a_connect_still_running_at_go_is_abandoned(fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import singlestoredb

    gate = threading.Event()
    slow: list[FakeConn] = []

    def connect(**kw: Any) -> FakeConn:
        gate.wait(5)
        c = FakeConn(**kw)
        slow.append(c)
        return c

    monkeypatch.setattr(singlestoredb, "connect", connect)
    p = preconnect.Preconnected(1)
    assert p.handover(wait=0.05).take(fake["kwargs"]) == []
    gate.set()
    p._thread.join(5)
    assert slow and all(c.closed for c in slow)  # the late connection closed itself


# ----------------------------------------------------------------------------- schema read ahead by sql_editor


@pytest.fixture
def editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    from singlestore_mcp.apps import sql_editor as se

    calls: list[str] = []

    def schema(database: str) -> dict[str, Any]:
        calls.append(database)
        if database == "gone":
            raise LookupError("Database 'gone' not found")
        return {"database": database, "tables": [{"name": f"t{len(calls)}", "view": False, "columns": []}], "routines": []}

    monkeypatch.setattr(se, "_schema_data", schema)
    monkeypatch.setattr(se, "_prefetched", {})
    return se, calls


def test_first_schema_call_gets_the_prefetch_later_ones_query(editor) -> None:
    se, calls = editor
    se._prefetch_schema("SASDP")
    se._prefetch_schema("SASDP")  # once per database
    first = se.sql_editor_schema("SASDP").structured_content
    again = se.sql_editor_schema("SASDP").structured_content  # the tree's refresh: fresh
    assert calls == ["SASDP", "SASDP"] and first["tables"][0]["name"] == "t1" and again["tables"][0]["name"] == "t2"


def test_prefetch_is_dropped_after_a_connection_switch_or_when_old(editor, monkeypatch: pytest.MonkeyPatch) -> None:
    se, calls = editor
    se._prefetch_schema("SASDP")
    se.db._generation += 1  # another active connection meanwhile
    try:
        assert se.sql_editor_schema("SASDP").structured_content["tables"][0]["name"] == "t2"
    finally:
        se.db._generation -= 1
    se._prefetch_schema("SASDP")
    monkeypatch.setattr(se, "PREFETCH_TTL_S", -1.0)
    se.sql_editor_schema("SASDP")
    assert len(calls) == 4


def test_failed_prefetch_reports_like_a_normal_call(editor) -> None:
    se, calls = editor
    se._prefetch_schema("gone")
    with pytest.raises(Exception, match="not found"):
        se.sql_editor_schema("gone")
    assert calls == ["gone", "gone"]


def test_start_database_is_the_active_tabs_draft(editor) -> None:
    se, _ = editor
    from singlestore_mcp import sql_editor_state as st

    assert se._start_database(None) is None
    st.save_draft({"sql": "SELECT 1", "database": "SASDP"}, st.resolve_tab(None))
    assert se._start_database(None) == "SASDP"
