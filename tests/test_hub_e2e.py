"""End to end: the hub with real Studio processes (python -m singlestore_mcp.studio_server) and the fake SAS Logon.

Two simulated users sign in (redirect flow with the hub's own client and secret), each gets their own Studio
process and folder, the user's SAS Viya sign-in is pre-loaded (and renewed with the client secret), one user's
editor text and secrets never show up for the other, and signing out stops the process. No cluster, no real
SAS Logon, no passwords. Takes ~20-40 s (each Studio imports the server).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fake_saslogon import TEST_CLIENT_SECRET, FakeSasLogon
from hub_client import Browser
from test_hub import MASTER, sign_in

from singlestore_mcp.hub.app import Hub
from singlestore_mcp.hub.audit import Audit
from singlestore_mcp.hub.config import HubConfig


@pytest.fixture(scope="module")
def logon():
    fake = FakeSasLogon()
    fake.access_ttl = 40  # short: the Studio renews the token on its first SAS call (needs the client secret)
    yield fake
    fake.close()


@pytest.fixture(scope="module")
def hub(tmp_path_factory: pytest.TempPathFactory, logon: FakeSasLogon):
    data = tmp_path_factory.mktemp("hubdata")
    cfg = HubConfig(viya_url=logon.url, data_dir=data, master_key=MASTER, oauth_mode="redirect", client_id="studio-hub",
                    client_secret=TEST_CLIENT_SECRET, port=0, admins=("alice",), max_users=5, metrics=False,
                    viya_name="Zeus")
    h = Hub(cfg, Audit(data / "hub" / "audit.jsonl", echo=False))
    port = h.serve(block=False)
    cfg.public_url = f"http://127.0.0.1:{port}"
    logon.register_redirect("studio-hub", cfg.redirect_uri)
    yield h
    h.close()


def open_page(b: Browser, path: str = "/studio/"):
    """The Studio page; while the Studio starts the hub answers with a page that reloads itself: wait like it."""
    deadline = time.time() + 90
    while True:
        r = b.follow(b.get(path))
        if r.status != 503 or r.headers.get("X-Studio-Restarting") != "1" or time.time() > deadline:
            return r
        time.sleep(1)


def call(b: Browser, name: str, arguments: dict | None = None) -> dict:
    r = b.call(name, arguments)
    assert r.status == 200, r.text[:300]
    res = r.json()
    assert not res.get("isError"), res
    return res.get("structuredContent") or {}


@pytest.fixture(scope="module")
def users(hub: Hub, logon: FakeSasLogon) -> dict[str, Browser]:
    return {"alice": sign_in(hub, logon, "alice"), "bob": sign_in(hub, logon, "bob")}


def test_studio_page_and_features(users: dict[str, Browser]) -> None:
    b = users["alice"]
    r = open_page(b)
    assert r.status == 200 and "SAS SpeedyStore Studio" in r.text
    assert 'window.S2_SERVER = { account: "../account" }' in r.text
    tool = b.get("/studio/tool?name=sql_editor").json()
    assert tool["name"] == "sql_editor"
    data = call(b, "sql_editor", {})
    assert data["features"]["server"] is True and data["features"]["notebooks"] is False
    assert data["features"]["assistant"] is False and data["features"]["user"] == "alice"
    assert data["browser_url"].startswith("/studio/?")  # links point at the hub, not 127.0.0.1:<port>/<token>
    assert call(b, "sql_editor_assistant")["disabled"] is True


def test_viya_sign_in_is_preloaded_and_renewed(users: dict[str, Browser], logon: FakeSasLogon) -> None:
    state = call(users["alice"], "sas_viya_state")
    assert state["name"] == "Zeus" and state["url"] == logon.url and state["client_id"] == "studio-hub"
    assert state["signed_in"] is True and state["source"] == "SAS Logon (Studio sign-in)"
    # The token lives 40 s, so that call renewed it - with the hub's client and its secret.
    assert {"grant_type": "refresh_token", "client": "studio-hub"} in logon.token_requests


def test_users_have_separate_state(users: dict[str, Browser], hub: Hub) -> None:
    alice, bob = users["alice"], users["bob"]
    a_tab = call(alice, "sql_editor_state")["tab_id"]
    call(alice, "sql_editor_save_draft", {"sql": "select 'alice-only' as secret_draft", "tab_id": a_tab})
    b_state = call(bob, "sql_editor_state")
    assert "alice-only" not in json.dumps(b_state)
    assert "alice-only" in json.dumps(call(alice, "sql_editor_state", {"tab_id": a_tab}))
    users_dir = Path(hub.cfg.data_dir) / "users"
    assert (users_dir / "alice" / "state").is_dir() and (users_dir / "bob" / "state").is_dir()
    bob_files = " ".join(p.read_text("utf-8", "replace") for p in (users_dir / "bob").rglob("*") if p.is_file() and p.suffix in (".json", ".log"))
    assert "alice-only" not in bob_files
    # Two processes, two ports.
    a, b = hub.spawner.get("alice"), hub.spawner.get("bob")
    assert a.pid != b.pid and a.port != b.port and a.token != b.token


def test_secrets_are_encrypted_with_the_hubs_key(users: dict[str, Browser], hub: Hub, logon: FakeSasLogon) -> None:
    state = Path(hub.cfg.data_dir) / "users" / "alice" / "state"
    enc = state / "secrets.enc"
    assert enc.is_file() and not (state / "secret.key").exists()  # the key isn't on the volume
    raw = enc.read_bytes()
    assert not any(t.encode() in raw for t in [*logon.access, *logon.refresh])
    from cryptography.fernet import Fernet

    data = json.loads(Fernet(hub.cfg.user_key("alice").encode()).decrypt(raw))
    assert any(k.startswith("sas-viya:Zeus") for k in data)
    with pytest.raises(Exception):  # noqa: B017 - bob's key doesn't open alice's file
        Fernet(hub.cfg.user_key("bob").encode()).decrypt(raw)


def test_no_tokens_in_logs(users: dict[str, Browser], hub: Hub, logon: FakeSasLogon) -> None:
    logs = [Path(hub.cfg.data_dir) / "hub" / "audit.jsonl", *(Path(hub.cfg.data_dir) / "users").rglob("studio.log")]
    text = " ".join(p.read_text("utf-8", "replace") for p in logs if p.exists())
    for secret in [*logon.access, *logon.refresh, TEST_CLIENT_SECRET, MASTER.decode()]:
        assert secret not in text
    for user in ("alice", "bob"):
        st = hub.spawner.get(user)
        assert st.token not in text


def test_sign_out_stops_only_that_users_studio(users: dict[str, Browser], hub: Hub) -> None:
    bob = users["bob"]
    proc = hub.spawner.get("bob").proc
    account = bob.get("/account")
    bob.post_form("/logout", {"csrf": account.hidden("csrf")})
    deadline = time.time() + 15
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    assert proc.poll() is not None
    assert hub.spawner.get("alice") is not None and call(users["alice"], "sql_editor_state")["tab_id"]


def test_a_killed_studio_comes_back_with_its_state(users: dict[str, Browser], hub: Hub) -> None:
    alice = users["alice"]
    tab = call(alice, "sql_editor_state")["tab_id"]
    call(alice, "sql_editor_save_draft", {"sql": "select 'kept across a crash' as x", "tab_id": tab})
    old = hub.spawner.get("alice")
    old.proc.kill()  # a crash (out of memory, a bug): no clean stop
    old.proc.wait(10)
    deadline = time.time() + 90
    while True:  # the page's own calls: "restarting" (503 + X-Studio-Restarting) until the new process answers
        r = alice.call("sql_editor_state", {"tab_id": tab})
        if r.status == 200 or time.time() > deadline:
            break
        assert r.status == 503 and r.headers.get("X-Studio-Restarting") == "1", r.text[:300]
        time.sleep(0.5)
    assert r.status == 200 and "kept across a crash" in json.dumps(r.json())
    new = hub.spawner.get("alice")
    assert new is not old and new.pid != old.pid and new.port
    assert any(e["event"] == "studio_stop" and e.get("user") == "alice" and e.get("reason") == "exited" for e in hub.audit.tail())


def test_hub_close_stops_every_studio_process(users: dict[str, Browser], hub: Hub) -> None:
    procs = [st.proc for st in hub.spawner.running()]
    assert procs
    t0 = time.time()
    hub.close()
    assert all(p.poll() is not None for p in procs)
    assert time.time() - t0 < 25  # inside the pod's 30 s termination grace period


def test_removed_from_the_sas_group_signs_out_at_the_next_token_renewal(tmp_path_factory: pytest.TempPathFactory,
                                                                      logon: FakeSasLogon) -> None:
    """Real Studio: the user's Viya token (40 s) is renewed on the next SAS call, the Studio re-checks the groups with
    it, reports to the hub, and the hub signs the user out and stops the Studio."""
    data = tmp_path_factory.mktemp("hubgroups")
    cfg = HubConfig(viya_url=logon.url, data_dir=data, master_key=MASTER, oauth_mode="redirect", client_id="studio-hub",
                    client_secret=TEST_CLIENT_SECRET, port=0, metrics=False, viya_name="Zeus",
                    users_group="SpeedyStoreStudioUsers", notebook_group="SpeedyStoreStudioNotebooks")
    h = Hub(cfg, Audit(data / "hub" / "audit.jsonl", echo=False))
    port = h.serve(block=False)
    cfg.public_url = f"http://127.0.0.1:{port}"
    logon.register_redirect("studio-hub", cfg.redirect_uri)
    logon.memberships["alice"] = ["SpeedyStoreStudioUsers"]
    try:
        alice = sign_in(h, logon, "alice")
        data_ = call(alice, "sql_editor", {})
        assert data_["features"]["notebooks"] is False  # STUDIO_NOTEBOOKS off: no notebooks for anyone
        logon.memberships["alice"] = []  # the Viya admin takes alice out of the group
        call(alice, "sas_viya_state")  # a SAS call renews the token -> the Studio re-checks
        deadline = time.time() + 30
        while h.spawner.get("alice") is not None and time.time() < deadline:
            time.sleep(0.2)
        assert h.spawner.get("alice") is None
        assert alice.get("/studio/").location == "/login"
        assert any(e["event"] == "access_revoked" and e.get("user") == "alice" for e in h.audit.tail())
    finally:
        h.close()
        logon.memberships.pop("alice", None)


def _notebook_python() -> Path | None:
    """A notebook environment on this machine (the desktop one), or SINGLESTORE_MCP_NOTEBOOK_PYTHON."""
    import os

    configured = os.environ.get("SINGLESTORE_MCP_NOTEBOOK_PYTHON")
    if configured and Path(configured).exists():
        return Path(configured)
    env = Path.home() / ".singlestore-mcp" / "notebook-env"
    exe = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.exists() and (env / ".s2-ready").exists() else None


def _run_cell(b: Browser, nb: str, source: str) -> str:
    run_id = call(b, "notebook_run", {"notebook_id": nb, "cell_type": "python", "source": source})["run_id"]
    deadline = time.time() + 120
    while True:
        res = call(b, "notebook_poll", {"run_id": run_id})
        if res["done"] or time.time() > deadline:
            break
        time.sleep(0.3)
    assert res["done"] and res["status"] == "ok", res
    return "".join(o.get("text", "") for o in res["outputs"] if o.get("output_type") == "stream")


@pytest.mark.notebook
def test_local_notebooks_for_the_notebook_group_only(tmp_path_factory: pytest.TempPathFactory, logon: FakeSasLogon) -> None:
    """STUDIO_NOTEBOOKS=local: a member of SpeedyStoreStudioNotebooks runs cells in a kernel of their own Studio, in
    their own folder, with their own SAS token; a member of SpeedyStoreStudioUsers only is refused by the tools."""
    from singlestore_mcp.hub.spawner import rss_bytes

    python = _notebook_python()
    if python is None:
        pytest.skip("no notebook environment on this machine (set SINGLESTORE_MCP_NOTEBOOK_PYTHON)")
    data = tmp_path_factory.mktemp("hubnotebooks")
    cfg = HubConfig(viya_url=logon.url, data_dir=data, master_key=MASTER, oauth_mode="redirect", client_id="studio-hub",
                    client_secret=TEST_CLIENT_SECRET, port=0, metrics=False, viya_name="Zeus", notebooks=True,
                    notebook_python=str(python), users_group="SpeedyStoreStudioUsers",
                    notebook_group="SpeedyStoreStudioNotebooks")
    h = Hub(cfg, Audit(data / "hub" / "audit.jsonl", echo=False))
    port = h.serve(block=False)
    cfg.public_url = f"http://127.0.0.1:{port}"
    logon.register_redirect("studio-hub", cfg.redirect_uri)
    logon.memberships.update(alice=["SpeedyStoreStudioNotebooks"], bob=["SpeedyStoreStudioUsers"])
    try:
        alice, bob = sign_in(h, logon, "alice"), sign_in(h, logon, "bob")
        assert call(alice, "sql_editor", {})["features"]["notebooks"] is True
        assert call(bob, "sql_editor", {})["features"]["notebooks"] is False
        before = rss_bytes(h.spawner.get("alice").pid)
        out = _run_cell(alice, "nb-e2e", "import os, json, subprocess\n"
                                         "cmd = json.loads(os.environ['SINGLESTORE_MCP_SAS_TOKEN_CMD'])\n"
                                         "tok = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()\n"
                                         "print(os.getcwd()); print(tok.startswith('fake-access-'))\n"
                                         "print('STUDIO_MASTER_KEY' in os.environ, 'SINGLESTORE_MCP_SECRET_KEY' in os.environ)")
        cwd, own_token, leaked = out.strip().splitlines()
        assert Path(cwd).resolve() == (data / "users" / "alice" / "files").resolve()
        assert own_token == "True" and leaked == "False False"
        time.sleep(3)
        with_kernel = rss_bytes(h.spawner.get("alice").pid)  # the whole tree: Studio + bridge + kernel
        assert with_kernel > before + 30 * 1048576
        print(f"\nmemory: alice's Studio {before / 1048576:.0f} MB, with one idle kernel {with_kernel / 1048576:.0f} MB")
        r = bob.call("notebook_run", {"notebook_id": "nb-bob", "cell_type": "python", "source": "print(1)"})
        assert r.status == 200 and r.json().get("isError") and "Notebooks are off" in json.dumps(r.json())
        page = alice.get("/admin")  # alice isn't an admin here: the warning is checked on the hub's own page below
        assert page.status == 403
    finally:
        h.close()
        for u in ("alice", "bob"):
            logon.memberships.pop(u, None)
