"""SAS Viya connections (sas_viya.py + the Connections app's SAS tools), offline.

SINGLESTORE_MCP_HOME is a temp folder, the credential store is a dict, nothing reaches Viya (the network
helpers are replaced) and the Workspace's SAS sessions are a counter.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from singlestore_mcp import connections as conn
from singlestore_mcp import sas_rest, sas_viya
from singlestore_mcp.apps import connections as app

URL_A = "https://viya-a.example.com"
URL_B = "https://viya-b.example.com"


def _jwt(user: str, exp: float) -> str:
    def part(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'user_name': user, 'exp': exp})}.sig"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = tmp_path / "home"
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(home))
    secrets: dict[str, str] = {}
    monkeypatch.setattr(conn, "get_password", lambda name: secrets.get(name))
    monkeypatch.setattr(conn, "set_password", lambda name, pw: secrets.__setitem__(name, pw))
    monkeypatch.setattr(conn, "delete_password", lambda name: secrets.pop(name, None))
    monkeypatch.setattr(conn, "password_store", lambda: "test store")
    monkeypatch.setattr(sas_viya, "_FOREIGN", [])  # never reuse the real SAS Viya MCP server / CLI sign-in

    def no_network(*a, **k):
        raise AssertionError("no network in offline tests")

    monkeypatch.setattr(sas_viya, "_http", no_network)
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})  # kernel_env: no proxy route to probe
    monkeypatch.setitem(sas_viya._route, "direct", False)
    resets: list[str | None] = []
    monkeypatch.setattr(sas_rest, "reset", lambda name=None: resets.append(name))
    sas_viya._pending.clear()
    return {"home": home, "secrets": secrets, "resets": resets, "file": home / "sas.json"}


def _token(user: str = "jonas", url: str = URL_A, refresh: str = "r1", exp_in: float = 3600) -> str:
    exp = time.time() + exp_in
    return json.dumps({"access": _jwt(user, exp), "refresh": refresh, "exp": exp, "client_id": "vscode",
                       "source": "sign-in", "url": url})


def _conn(name: str, url: str, **kw) -> dict:
    return {"name": name, "url": url, **kw}


# ------------------------------------------------------------------ migration


def test_old_settings_become_one_connection_with_its_sign_in(env) -> None:
    env["home"].mkdir(parents=True)
    env["file"].write_text(json.dumps({"url": URL_A + "/", "compute_context": "Ctx", "startup_code": "%put hi;", "libref": "s2x"}))
    env["secrets"]["sas-viya#token"] = _token()
    s = sas_viya.settings()
    assert s["name"] == "viya-a.example.com" and s["url"] == URL_A and s["compute_context"] == "Ctx"
    assert s["startup_code"] == "%put hi;" and s["libref"] == "S2X"
    assert "sas-viya#token" not in env["secrets"] and "sas-viya:viya-a.example.com#token" in env["secrets"]
    saved = json.loads(env["file"].read_text())
    assert saved["active"] == "viya-a.example.com" and len(saved["connections"]) == 1
    assert sas_viya.token_info("viya-a.example.com")["signed_in"]
    assert sas_viya.token(name="viya-a.example.com")  # the migrated token is valid: no network needed


def test_old_settings_with_empty_url_give_an_empty_list(env) -> None:
    env["home"].mkdir(parents=True)
    env["file"].write_text(json.dumps({"url": "", "compute_context": "SAS Studio compute context", "startup_code": ""}))
    assert sas_viya.saved() == [] and sas_viya.active_name() is None
    assert sas_viya.settings()["url"] == "" and sas_viya.settings()["name"] is None
    assert json.loads(env["file"].read_text())["connections"] == []
    st = app.sas_connections_state().structured_content
    assert st["connections"] == [] and st["active"] is None
    with pytest.raises(sas_viya.NeedsSignIn):
        sas_viya.token()
    assert sas_viya.kernel_env() == {}


def test_no_file_is_no_connection(env) -> None:
    assert sas_viya.saved() == [] and sas_viya.status()["signed_in"] is False


# ------------------------------------------------------------------ connections


def test_save_activate_and_settings_follow_the_active_one(env) -> None:
    app.sas_connection_save(_conn("A", URL_A))
    assert sas_viya.active_name() == "A"  # the first one is active
    app.sas_connection_save(_conn("B", URL_B, compute_context="Other"))
    assert sas_viya.active_name() == "A"  # adding one doesn't switch
    assert sas_viya.settings()["url"] == URL_A
    assert env["resets"] == []
    state = app.sas_connection_activate("B").structured_content
    assert state["active"] == "B" and [c["active"] for c in state["connections"]] == [False, True]
    assert sas_viya.settings()["url"] == URL_B and sas_viya.settings()["compute_context"] == "Other"
    assert env["resets"] == []  # every connection has its own SAS sessions: nothing restarts
    app.sas_connection_activate("B")
    with pytest.raises(LookupError):
        app.sas_connection_activate("nope")
    assert env["resets"] == []
    assert sas_viya.kernel_env()["SINGLESTORE_MCP_SAS_URL"] == URL_B
    assert json.loads(sas_viya.kernel_env()["SINGLESTORE_MCP_SAS_TOKEN_CMD"])[-1] == "B"


def test_save_with_activate_and_duplicate_names(env) -> None:
    app.sas_connection_save(_conn("A", URL_A))
    app.sas_connection_save(_conn("B", URL_B), activate=True)
    assert sas_viya.active_name() == "B" and env["resets"] == []
    with pytest.raises(ValueError, match="already exists"):
        sas_viya.save_connection(_conn("A", URL_B))
    with pytest.raises(ValueError, match="name"):
        sas_viya.save_connection(_conn("bad:name", URL_B))


def test_tokens_are_per_connection(env) -> None:
    sas_viya.save_connection(_conn("A", URL_A))
    sas_viya.save_connection(_conn("B", URL_B))
    env["secrets"]["sas-viya:A#token"] = _token("alice", URL_A)
    a, b = sas_viya.token_info("A"), sas_viya.token_info("B")
    assert a["signed_in"] and a["user"] == "alice" and a["renewable"] and a["token_valid"]
    assert not b["signed_in"]
    assert sas_viya.token(name="A")
    with pytest.raises(sas_viya.NeedsSignIn, match="B"):
        sas_viya.token(name="B")
    # A token stored for another address doesn't count.
    env["secrets"]["sas-viya:B#token"] = _token("bob", URL_A)
    assert not sas_viya.token_info("B")["signed_in"]
    # Sign out forgets only that connection's sign-in.
    app.sas_viya_sign_out("A")
    assert "sas-viya:A#token" not in env["secrets"] and "sas-viya:B#token" in env["secrets"]


def test_rename_moves_the_token_and_the_active_mark(env) -> None:
    sas_viya.save_connection(_conn("A", URL_A))
    env["secrets"]["sas-viya:A#token"] = _token()
    sas_viya.save_connection({**sas_viya.settings("A"), "name": "Prod"}, original_name="A")
    assert "sas-viya:A#token" not in env["secrets"] and "sas-viya:Prod#token" in env["secrets"]
    assert sas_viya.active_name() == "Prod" and [c["name"] for c in sas_viya.saved()] == ["Prod"]


def test_address_change_drops_the_sign_in_other_edits_keep_it(env) -> None:
    sas_viya.save_connection(_conn("A", URL_A))
    env["secrets"]["sas-viya:A#token"] = _token()
    sas_viya.save_connection({**sas_viya.settings("A"), "compute_context": "Ctx2", "startup_code": "%put x;"}, original_name="A")
    assert "sas-viya:A#token" in env["secrets"]
    sas_viya.save_connection({**sas_viya.settings("A"), "url": URL_A + "/"}, original_name="A")  # same address
    assert "sas-viya:A#token" in env["secrets"]
    sas_viya.save_connection({**sas_viya.settings("A"), "url": URL_B}, original_name="A")
    assert "sas-viya:A#token" not in env["secrets"]


def test_delete_removes_the_sign_in_and_moves_active(env) -> None:
    app.sas_connection_save(_conn("A", URL_A))
    app.sas_connection_save(_conn("B", URL_B))
    env["secrets"]["sas-viya:A#token"] = _token()
    state = app.sas_connection_delete("A").structured_content
    assert "sas-viya:A#token" not in env["secrets"]
    assert state["active"] == "B" and [c["name"] for c in state["connections"]] == ["B"]
    assert env["resets"] == ["A"]  # its sessions end with it
    app.sas_connection_delete("B")
    assert sas_viya.saved() == [] and sas_viya.active_name() is None
    with pytest.raises(LookupError):
        sas_viya.delete_connection("B")


def test_editing_the_active_connection_restarts_the_sessions_only_when_needed(env) -> None:
    app.sas_connection_save(_conn("A", URL_A))
    app.sas_connection_save(_conn("B", URL_B))
    app.sas_connection_save({**sas_viya.settings("B"), "startup_code": "%put b;"}, original_name="B")
    assert env["resets"] == ["B"]  # B's own sessions (a tab may use B without it being active)
    app.sas_connection_save({**sas_viya.settings("A"), "libref": "S2"}, original_name="A")
    assert env["resets"] == ["B"]  # nothing the session depends on
    app.sas_connection_save({**sas_viya.settings("A"), "startup_code": "%put a;"}, original_name="A")
    assert env["resets"] == ["B", "A"]


# ------------------------------------------------------------------ the original bug


def test_an_empty_form_can_not_wipe_the_saved_address(env) -> None:
    """The old Connections form saved its (still empty) fields when Sign in was clicked before the state had
    loaded: sas.json lost its address and the sign-in was deleted."""
    sas_viya.save_connection(_conn("A", URL_A, startup_code="%include x;"))
    env["secrets"]["sas-viya:A#token"] = _token()
    empty_form = {"url": "", "compute_context": "SAS Studio compute context", "libref": "S2", "ssl_verify": True,
                  "cas_server": "cas-shared-default", "startup_code": ""}
    app.sas_viya_save(empty_form)  # the old tool, as the old form called it
    s = sas_viya.settings()
    assert s["url"] == URL_A and "sas-viya:A#token" in env["secrets"]
    with pytest.raises(ValueError, match="address"):
        sas_viya.save_connection({**empty_form, "name": "A"}, original_name="A")
    assert sas_viya.settings("A")["url"] == URL_A
    with pytest.raises(ValueError, match="address"):
        sas_viya.save_connection({**empty_form, "name": "New"})


def test_partial_save_keeps_the_rest(env) -> None:
    app.sas_viya_save({"url": URL_A})  # no connection yet: an address creates one
    assert sas_viya.active_name() == "viya-a.example.com"
    app.sas_viya_save({"startup_code": "%put hi;"})
    s = sas_viya.settings()
    assert s["url"] == URL_A and s["startup_code"] == "%put hi;"


def test_partial_save_without_connection_needs_an_address(env) -> None:
    with pytest.raises(ValueError, match="address"):
        sas_viya.save_settings({"url": "", "startup_code": "%put hi;"})
    assert sas_viya.saved() == []


def test_sign_in_saves_nothing(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sas_viya.webbrowser, "open", lambda url: True)
    sas_viya.save_connection(_conn("A", URL_A, startup_code="%put a;"))
    sas_viya.save_connection(_conn("B", URL_B))
    before = env["file"].read_text()
    res = app.sas_viya_sign_in(name="B").structured_content
    assert res["authorize_url"].startswith(URL_B + "/SASLogon/oauth/authorize?") and res["name"] == "B"
    assert env["file"].read_text() == before
    exp = time.time() + 3600
    monkeypatch.setattr(sas_viya, "_post_token", lambda url, data, client, verify: {
        "access_token": _jwt("bob", exp), "refresh_token": "r"} if url == URL_B and data["code"] == "abc" else {})
    res = app.sas_viya_sign_in(code="abc", name="B").structured_content
    assert res["name"] == "B" and res["user"] == "bob" and res["signed_in"]
    assert "sas-viya:B#token" in env["secrets"] and "sas-viya:A#token" not in env["secrets"]
    assert env["file"].read_text() == before
    for c in res["state"]["connections"]:
        assert "access" not in json.dumps(c) and _jwt("bob", exp) not in json.dumps(c)  # tokens never reach the app


def test_sign_in_needs_a_saved_connection(env) -> None:
    with pytest.raises(ValueError, match="connection"):
        sas_viya.sign_in_start()
    with pytest.raises(LookupError):
        sas_viya.sign_in_start("nope")


def test_unreadable_file_raises_instead_of_reading_as_empty(env, monkeypatch: pytest.MonkeyPatch) -> None:
    sas_viya.save_connection(_conn("A", URL_A))

    def locked(path):
        raise PermissionError("in use")

    monkeypatch.setattr(sas_viya.fileio, "read_text", locked)
    with pytest.raises(OSError):
        sas_viya.settings()
    with pytest.raises(OSError):
        sas_viya.save_settings({"startup_code": "x"})


# ------------------------------------------------------------------ other SAS tools' sign-ins (and the tests' gating)

def test_foreign_sign_in_is_reused_unless_turned_off(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Why the SAS tests also pass without S2_TEST_SAS_SECRETS: with no sign-in of its own, token() reuses the SAS
    Viya MCP server's cached one (when this Viya accepts it). SINGLESTORE_MCP_SAS_IMPORT=0 turns that off."""
    cache = tmp_path / ".sas-mcp-server" / "credentials.json"
    cache.parent.mkdir()
    access = _jwt("jonas", time.time() + 3600)
    cache.write_text(json.dumps({"Default": {"access-token": access, "refresh-token": "r"}}), encoding="utf-8")
    monkeypatch.setattr(sas_viya, "_FOREIGN", [(cache, "vscode")])
    monkeypatch.setattr(sas_viya, "_http", lambda method, url, **k: (200, {"id": "jonas"}))  # @currentUser accepts it
    sas_viya.save_connection(_conn("Zeus", URL_A))
    monkeypatch.setenv("SINGLESTORE_MCP_SAS_IMPORT", "0")
    assert sas_viya.token_info("Zeus")["foreign_available"] is False
    with pytest.raises(sas_viya.NeedsSignIn):
        sas_viya.token(name="Zeus")
    monkeypatch.delenv("SINGLESTORE_MCP_SAS_IMPORT")
    assert sas_viya.token(name="Zeus") == access
    assert json.loads(env["secrets"]["sas-viya:Zeus#token"])["source"] == "SAS Viya MCP server sign-in"


def test_test_suite_sas_sign_in_modes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import support

    for k in ("S2_TEST_SAS_SIGNIN", "S2_TEST_SAS_SECRETS", "SINGLESTORE_MCP_SAS_IMPORT"):
        monkeypatch.delenv(k, raising=False)
    assert support.sas_signin_mode() == "any" and not support._copy_secrets()
    assert "SINGLESTORE_MCP_SAS_IMPORT" not in support.server_env(tmp_path, tmp_path)
    monkeypatch.setenv("S2_TEST_SAS_SECRETS", "1")
    assert support._copy_secrets()
    monkeypatch.setenv("S2_TEST_SAS_SIGNIN", "import")
    assert not support._copy_secrets()
    monkeypatch.setenv("S2_TEST_SAS_SIGNIN", "own")
    monkeypatch.delenv("S2_TEST_SAS_SECRETS")
    assert support._copy_secrets() and support.server_env(tmp_path, tmp_path)["SINGLESTORE_MCP_SAS_IMPORT"] == "0"
    monkeypatch.setenv("S2_TEST_SAS_SIGNIN", "bogus")
    with pytest.raises(ValueError):
        support.sas_signin_mode()
