"""Pre-warming the Workspace's SAS session, offline (Viya replaced by fakes): one warm-up per connection, skipped
quietly when not signed in or unreachable, a run that comes meanwhile waits for the same warm-up (no second
session), a connection switch warms the new one, the keep-alive restarts a session Viya ended, and the setting /
SINGLESTORE_MCP_SAS_PREWARM turn it off."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from singlestore_mcp import sas_prewarm, sas_rest, sas_runs, sas_viya
from singlestore_mcp.apps import connections as conn_app
from singlestore_mcp.apps import sas_workspace as ws


class SlowCompute(sas_rest.Compute):
    """A Compute session that takes a while to start (``gate``) and counts how often one was started."""

    def __init__(self, conn: str) -> None:
        super().__init__(conn)
        self.gate = threading.Event()
        self.gate.set()
        self.created = 0
        self.fail: BaseException | None = None
        self.auto_checks = 0

    def session(self) -> str:
        with self.lock:
            if self.sid:
                return self.sid
            if self.fail is not None:
                raise self.fail
            assert self.gate.wait(5), "test gate never opened"
            self.created += 1
            self.sid = f"S{self.created}"
            self.startup = {"ran": False}
            return self.sid

    def ensure_auto(self) -> None:
        self.auto_checks += 1

    def run(self, code: str, *, listing: bool = False, ctl: Any = None) -> dict[str, Any]:
        with self.lock:
            self.session()
            return {"state": "completed", "log": [{"line": "NOTE: done", "type": "note"}], "listing": "", "ms": 1}

    def rows(self, libref: str, table: str, limit: int, start: int = 0) -> dict[str, Any]:
        return {"columns": ["N"], "column_meta": [{"name": "N", "type": "num"}], "rows": [{"N": 1}], "total_rows": 1}


class FakeCas(sas_rest.Cas):
    def __init__(self, conn: str) -> None:
        super().__init__(conn)
        self.created = 0

    def session(self) -> str:
        with self.lock:
            if not self.sid:
                self.created += 1
                self.sid = f"C{self.created}"
            return self.sid


SETTINGS: dict[str, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(sas_prewarm.ENV, "1")
    SETTINGS.clear()
    for name in ("Zeus", "Other"):
        SETTINGS[name] = {"name": name, "url": f"https://{name.lower()}.example", "compute_context": "ctx",
                          "cas_server": "cas-shared-default", "libref": "S2", "auto_libname": False, "startup_code": "",
                          "prewarm": True}
    active = {"name": "Zeus"}
    monkeypatch.setattr(sas_viya, "active_name", lambda: active["name"])
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: dict(SETTINGS[name or active["name"]]))
    monkeypatch.setattr(sas_viya, "activate", lambda name: active.update(name=name) or dict(SETTINGS[name]))
    monkeypatch.setattr(sas_viya, "token_info", lambda name: {"signed_in": True})
    pairs: dict[str, tuple[SlowCompute, FakeCas]] = {}

    def sessions(conn: str | None = None) -> tuple[SlowCompute, FakeCas]:
        name = conn or active["name"]
        if name not in pairs:
            pairs[name] = (SlowCompute(name), FakeCas(name))
        return pairs[name]

    monkeypatch.setattr(sas_rest, "sessions", sessions)
    monkeypatch.setattr(sas_prewarm, "_state", {})
    monkeypatch.setattr(sas_prewarm, "_threads", {})
    monkeypatch.setattr(ws.editor_state, "add_history", lambda *a, **k: None)
    sas_runs._runs.clear()
    yield {"sessions": sessions, "active": active}
    for name, (c, _k) in pairs.items():
        c.gate.set()
        sas_prewarm.wait(name, 5)


def test_prewarm_starts_the_session_once(fake) -> None:
    c, k = fake["sessions"]("Zeus")
    c.gate.clear()
    first = sas_prewarm.prewarm()
    again = sas_prewarm.prewarm()  # the Workspace and a view asked at the same time
    assert first["state"] == again["state"] == "starting" and first["connection"] == "Zeus"
    c.gate.set()
    st = sas_prewarm.wait()
    assert st["state"] == "ready" and c.created == 1 and k.created == 1
    assert "compute_ms" in st and "ms" in st
    # Asked again soon after: nothing to do (no call to Viya, no second session).
    assert sas_prewarm.prewarm()["state"] == "ready"
    assert sas_prewarm.wait()["state"] == "ready" and c.created == 1 and c.auto_checks == 0


def test_skips_quietly_when_not_signed_in_or_unreachable(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sas_viya, "token_info", lambda name: {"signed_in": False, "foreign_available": False})
    st = sas_prewarm.prewarm()
    c, _ = fake["sessions"]("Zeus")
    assert st["state"] == "not_signed_in" and "sign in" in st["note"].lower() and c.created == 0
    assert not sas_prewarm._threads  # nothing started

    # Signed in, but the sign-in expired on the way (renewal refused): still no prompt, just the reason.
    monkeypatch.setattr(sas_viya, "token_info", lambda name: {"signed_in": True})
    c.fail = sas_viya.NeedsSignIn("The SAS Viya sign-in (Zeus) has expired. Sign in again in the Connections window.")
    sas_prewarm.prewarm()
    assert sas_prewarm.wait()["state"] == "not_signed_in"

    # VPN off: unreachable, and not tried again at once.
    sas_prewarm._state.clear()
    c.fail = sas_rest.SasError("Couldn't reach SAS Viya at https://zeus.example: [Errno 11001] getaddrinfo failed")
    sas_prewarm.prewarm()
    st = sas_prewarm.wait()
    assert st["state"] == "unreachable" and "getaddrinfo" in st["note"]
    assert sas_prewarm.prewarm()["state"] == "unreachable" and not sas_prewarm._threads


def test_no_secrets_in_the_status(fake) -> None:
    c, _ = fake["sessions"]("Zeus")
    c.fail = sas_rest.SasError("startup failed: libname x sstore password=Secret123 user=me")
    sas_prewarm.prewarm()
    st = sas_prewarm.wait()
    assert st["state"] == "error" and "Secret123" not in str(st)


def test_a_run_during_the_warm_up_waits_for_it(fake) -> None:
    c, _ = fake["sessions"]("Zeus")
    c.gate.clear()
    sas_prewarm.prewarm()
    time.sleep(0.1)
    r = ws.sas_query("procsql", "select 1 as a from sashelp.class", run_id="s_warm_1", wait_ms=300, tab_id="T1", tab="untitled 1")
    d = r.structured_content
    assert d["running"] and d["state"] == "queued" and d["waiting_for"].get("warming") is True
    assert "start" in r.content[0].text.lower()  # "Waiting for the SAS session to start"
    c.gate.set()
    d = ws.sas_run_status("s_warm_1", wait_ms=5000).structured_content
    assert not d["running"] and d["run_state"] == "done"
    assert c.created == 1  # the run used the warmed session, no second one


def test_library_list_during_the_warm_up_waits_for_it(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = fake["sessions"]("Zeus")
    c.gate.clear()
    sas_prewarm.prewarm()
    time.sleep(0.1)
    out: dict[str, Any] = {}
    t = threading.Thread(target=lambda: out.update(libs=sas_rest.libraries(conn="Zeus")))
    monkeypatch.setattr(sas_rest, "library_rows", lambda rows: [{"name": "WORK", "singlestore": False, "sas9": False}])
    t.start()
    time.sleep(0.2)
    assert t.is_alive()  # waiting on the session lock held by the warm-up
    c.gate.set()
    t.join(5)
    assert out["libs"][0]["name"] == "WORK" and c.created == 1


def test_switching_the_active_connection_warms_the_new_one(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conn_app, "_sas_state", lambda: {"active": fake["active"]["name"]})
    conn_app.sas_connection_activate("Other")
    st = sas_prewarm.wait("Other")
    assert st["state"] == "ready" and fake["sessions"]("Other")[0].created == 1
    assert fake["sessions"]("Zeus")[0].created == 0  # the other connection isn't touched


def test_tabs_pinned_to_another_connection_are_not_warmed(fake) -> None:
    st = ws.sas_prewarm(connection="Other", start=False).structured_content
    assert st["state"] == "idle" and fake["sessions"]("Other")[0].created == 0 and not sas_prewarm._threads


def test_off_by_setting_or_environment(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    SETTINGS["Zeus"]["prewarm"] = False
    st = sas_prewarm.prewarm()
    assert st["state"] == "off" and st["skipped"] and fake["sessions"]("Zeus")[0].created == 0
    SETTINGS["Zeus"]["prewarm"] = True
    monkeypatch.setenv(sas_prewarm.ENV, "0")
    st = sas_prewarm.prewarm()
    assert st["state"] == "off" and sas_prewarm.ENV in st["skipped"] and not sas_prewarm._threads
    # Switching connections doesn't warm either.
    monkeypatch.setattr(conn_app, "_sas_state", lambda: {})
    conn_app.sas_connection_activate("Other")
    assert fake["sessions"]("Other")[0].created == 0 and not sas_prewarm._threads


def test_keepalive_checks_the_session_and_restarts_one_viya_ended(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    c, k = fake["sessions"]("Zeus")
    sas_prewarm.prewarm()
    sas_prewarm.wait()
    seen: list[tuple[str, str]] = []
    answer = {"compute": (200, "idle"), "cas": (200, {})}

    def request(method: str, url: str, **kw: Any) -> tuple[int, Any, dict[str, str]]:
        seen.append((method, url))
        return (*answer["cas" if "/cas/" in url else "compute"], {})

    monkeypatch.setattr(sas_rest, "request", request)
    sas_prewarm._state["Zeus"]["checked"] = time.time() - sas_prewarm.KEEPALIVE_S - 1
    sas_prewarm.prewarm()
    assert sas_prewarm.wait()["state"] == "ready" and c.created == 1 and c.auto_checks == 1
    assert ("GET", "https://zeus.example/compute/sessions/S1/state") in seen  # a state GET, no SAS code
    assert not any(m == "POST" for m, _ in seen)

    # Viya ended the idle session meanwhile: a new one is started in the background, not at the next run.
    answer["compute"] = (404, {"errorCode": 5837, "message": "A session with the ID S1 could not be found."})
    answer["cas"] = (404, {})
    sas_prewarm._state["Zeus"]["checked"] = 0
    sas_prewarm.prewarm()
    st = sas_prewarm.wait()
    assert st["state"] == "ready" and c.sid == "S2" and c.created == 2 and k.created == 2


def test_the_prewarm_setting_is_saved_and_defaults_on() -> None:
    assert sas_viya.DEFAULTS["prewarm"] is True
    cur = {"name": "x", **sas_viya.DEFAULTS, "url": "https://v.example"}
    assert sas_viya._fields({"prewarm": False}, cur)["prewarm"] is False
    assert sas_viya._fields({}, cur)["prewarm"] is True
