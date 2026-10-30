"""The Studio hub (server pilot), offline: sign-in with a fake SAS Logon, cookies and CSRF, per-user routing and
isolation, idle stop, limits, admin page, audit log without secrets.

The users' Studios are small fake upstream servers here (FakeSpawner) that echo what reached them, so these tests
run in a second; test_hub_e2e.py runs real Studio processes. No real password, token or SAS Logon anywhere.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fake_saslogon import TEST_CLIENT_SECRET, FakeSasLogon
from hub_client import Browser

from singlestore_mcp.hub.app import Hub
from singlestore_mcp.hub.audit import Audit
from singlestore_mcp.hub.config import HubConfig
from singlestore_mcp.hub.sessions import SessionStore
from singlestore_mcp.hub.spawner import Spawner, Studio, safe_name

MASTER = b"test-master-key-not-a-real-one-0123456789"


# ----------------------------------------------------------------------------- fake Studios

class FakeProc:
    def __init__(self, server: ThreadingHTTPServer) -> None:
        self.server, self.pid, self.returncode = server, os.getpid(), None
        self.stdin = self

    def close(self) -> None:
        if self.returncode is None:
            self.returncode = 0
            self.server.shutdown()
            self.server.server_close()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.close()


class FakeSpawner(Spawner):
    """Starts an echo server instead of a Studio process: it answers only under its own path token."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.seeds: list[tuple[str, dict[str, Any]]] = []
        self.starts = 0

    def _start(self, st: Studio, tokens: dict[str, Any] | None) -> None:
        token = st.token

        class Echo(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _h(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if not self.path.startswith(f"/{token}/"):
                    data, status = b"not found", 404
                else:
                    status = 200
                    data = json.dumps({"user": st.user, "path": self.path[len(token) + 2:], "method": self.command,
                                       "host": self.headers.get("Host"), "origin": self.headers.get("Origin"),
                                       "cookie": self.headers.get("Cookie"), "authorization": self.headers.get("Authorization"),
                                       "body": body.decode()}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Set-Cookie", "upstream=should-not-pass")
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _h

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        st.proc = FakeProc(srv)  # type: ignore[assignment]
        st.port = srv.server_address[1]
        self.seeds.append((st.user, self.seed(st.user, tokens)))
        self.starts += 1
        st.ready.set()
        self.audit.record("studio_start", user=st.user, pid=st.pid)


@pytest.fixture()
def logon():
    fake = FakeSasLogon()
    yield fake
    fake.close()


def make_hub(tmp_path: Path, logon: FakeSasLogon, **over: Any) -> Hub:
    values: dict[str, Any] = dict(viya_url=logon.url, data_dir=tmp_path, master_key=MASTER, oauth_mode="redirect",
                                  client_id="studio-hub", client_secret=TEST_CLIENT_SECRET, port=0, admins=("alice",))
    values.update(over)
    cfg = HubConfig(**values)
    hub = Hub(cfg, Audit(tmp_path / "audit.jsonl", echo=False))
    hub.spawner = FakeSpawner(cfg, hub.audit)
    port = hub.serve(block=False)
    cfg.public_url = f"http://127.0.0.1:{port}"
    logon.register_redirect("studio-hub", cfg.redirect_uri)
    return hub


@pytest.fixture()
def hub(tmp_path: Path, logon: FakeSasLogon):
    h = make_hub(tmp_path, logon)
    yield h
    h.close()


def sign_in(hub: Hub, logon: FakeSasLogon, user: str, browser: Browser | None = None) -> Browser:
    b = browser or Browser(hub.cfg.public_url)
    r = b.get("/login/start")
    if hub.cfg.oauth_mode == "redirect":
        assert r.status == 303 and r.location.startswith(f"{logon.url}/SASLogon/oauth/authorize?")
        back = logon.sign_in(r.location, user)
        assert back.startswith(hub.cfg.redirect_uri)
        r = b.get(back)
    else:
        assert r.status == 200
        url = re.search(r'href="([^"]+/SASLogon/oauth/authorize[^"]+)"', r.text).group(1).replace("&amp;", "&")
        code = logon.sign_in(url, user)
        r = b.post_form("/login/code", {"csrf": r.hidden("csrf"), "code": code})
    assert r.status == 303 and r.location == "/studio/", r.text[:300]
    return b


def ping(b: Browser, path: str = "session") -> dict[str, Any]:
    r = b.get(f"/studio/{path}")
    assert r.status == 200, r.text[:300]
    return r.json()


# ----------------------------------------------------------------------------- unit

def test_safe_name_and_user_keys() -> None:
    assert safe_name("norjni") == "norjni"
    assert safe_name("Jonas.Nielsen@sas.com") != safe_name("jonas.nielsen_sas.com")
    assert "/" not in safe_name("../../etc") and ".." not in safe_name("../../etc")
    cfg = HubConfig(viya_url="https://viya.example", master_key=MASTER)
    assert cfg.user_key("alice") == cfg.user_key("ALICE")  # same user, any case
    assert cfg.user_key("alice") != cfg.user_key("bob")
    other = HubConfig(viya_url="https://viya.example", master_key=MASTER + b"x")
    assert other.user_key("alice") != cfg.user_key("alice")


def test_audit_drops_secret_fields(tmp_path: Path) -> None:
    audit = Audit(tmp_path / "a.jsonl", echo=False)
    audit.record("login", user="alice", access_token="SEKRIT1", code="SEKRIT2", client_secret="SEKRIT3", password="SEKRIT4")
    text = (tmp_path / "a.jsonl").read_text()
    assert "alice" in text and "SEKRIT" not in text


def test_sessions_expire_and_pending_is_single_use() -> None:
    store = SessionStore(max_age_s=0.2)
    s = store.create("alice", "Alice")
    assert store.get(s.sid) is s
    time.sleep(0.3)
    assert store.get(s.sid) is None
    p = store.start_pending("verifier")
    assert store.take_pending(p.pid) is p
    assert store.take_pending(p.pid) is None


# ----------------------------------------------------------------------------- sign-in

def test_health_needs_no_sign_in(hub: Hub) -> None:
    r = Browser(hub.cfg.public_url).get("/healthz")
    assert r.status == 200 and r.json()["ok"] is True


def test_studio_needs_sign_in(hub: Hub) -> None:
    b = Browser(hub.cfg.public_url)
    assert b.get("/studio/").location == "/login"
    assert b.get("/").location == "/login"
    assert b.call("sql_editor_state").status == 401


def test_redirect_sign_in_sets_a_session_cookie(hub: Hub, logon: FakeSasLogon) -> None:
    b = Browser(hub.cfg.public_url)
    r = b.get("/login/start")
    login_cookie = next(c for c in r.set_cookies() if c.startswith("studio_login="))
    assert "HttpOnly" in login_cookie and "SameSite=Lax" in login_cookie
    r = b.get(logon.sign_in(r.location, "alice"))
    session_cookie = next(c for c in r.set_cookies() if c.startswith("studio_session="))
    assert "HttpOnly" in session_cookie and "SameSite=Lax" in session_cookie and "Path=/" in session_cookie
    assert ping(b)["user"] == "alice"
    # The Studio got the user's Viya sign-in with its seed; the hub's client secret goes to the process separately.
    user, seed = hub.spawner.seeds[-1]
    assert user == "alice" and seed["viya"]["tokens"]["access_token"].startswith("fake-access-")
    assert seed["viya"]["url"] == logon.url and seed["viya"]["client_id"] == "studio-hub"


def test_secure_cookie_names_behind_https() -> None:
    cfg = HubConfig(viya_url="https://viya.example", master_key=MASTER, public_url="https://studio.example.com")
    assert cfg.secure and cfg.cookie_name == "__Host-studio"
    assert cfg.redirect_uri == "https://studio.example.com/oauth/callback"


def test_paste_sign_in_with_the_vscode_client(tmp_path: Path, logon: FakeSasLogon) -> None:
    hub = make_hub(tmp_path, logon, oauth_mode="paste", client_id="vscode", client_secret=None)
    try:
        b = sign_in(hub, logon, "bob")
        assert ping(b)["user"] == "bob"
        assert {"grant_type": "authorization_code", "client": "vscode"} in logon.token_requests
    finally:
        hub.close()


def test_callback_from_another_browser_is_refused(hub: Hub, logon: FakeSasLogon) -> None:
    started = Browser(hub.cfg.public_url)
    r = started.get("/login/start")
    back = logon.sign_in(r.location, "alice")
    other = Browser(hub.cfg.public_url)  # no login cookie: e.g. a link someone else sent
    r = other.get(back)
    assert r.status == 400 and not other.cookies
    # Tampered state from the right browser: refused too.
    r = started.get(back.replace("state=", "state=x"))
    assert r.status == 400


def test_refused_code_does_not_sign_in(hub: Hub) -> None:
    b = Browser(hub.cfg.public_url)
    b.get("/login/start")
    pid = b.cookies["studio_login"]
    state = hub.sessions.peek_pending(pid).state
    r = b.get(f"/oauth/callback?code=made-up&state={state}")
    assert r.status == 403 and "studio_session" not in b.cookies


def test_allowed_users(tmp_path: Path, logon: FakeSasLogon) -> None:
    hub = make_hub(tmp_path, logon, allowed_users=("alice",))
    try:
        b = Browser(hub.cfg.public_url)
        r = b.get("/login/start")
        r = b.get(logon.sign_in(r.location, "bob"))
        assert r.status == 403 and "studio_session" not in b.cookies
        assert any(e["event"] == "login_denied" for e in hub.audit.tail())
    finally:
        hub.close()


# ----------------------------------------------------------------------------- isolation and CSRF

def test_each_user_reaches_only_their_own_studio(hub: Hub, logon: FakeSasLogon) -> None:
    alice, bob = sign_in(hub, logon, "alice"), sign_in(hub, logon, "bob")
    a, b = ping(alice), ping(bob)
    assert a["user"] == "alice" and b["user"] == "bob"
    # Nothing that identifies the browser reaches the Studio, and the Studio can't set cookies on the hub.
    assert a["cookie"] is None and a["authorization"] is None
    assert alice.get("/studio/ping").set_cookies() == []
    # The Studio sees its own loopback address as Host, never the hub's or another user's.
    st_a = hub.spawner.get("alice")
    assert a["host"] == f"127.0.0.1:{st_a.port}"
    # No URL addresses another user's Studio: user names, ports and tokens aren't part of any route.
    st_b = hub.spawner.get("bob")
    for path in (f"/studio/../{st_b.token}/session", f"/{st_b.token}/session", "/studio/bob/session", "/user/bob/studio/"):
        r = alice.get(path)
        assert r.status != 200 or r.json().get("user") != "bob", path
    # Swapping in another user's cookie value is the only way, and that is their session (HttpOnly keeps it from scripts).
    assert alice.cookies["studio_session"] != bob.cookies["studio_session"]


def test_posts_must_come_from_the_hub_origin(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    assert b.call("sql_editor_state", origin="https://evil.example").status == 403
    assert b.call("sql_editor_state", origin=None).status == 403  # no Origin, no Sec-Fetch-Site
    assert b.call("sql_editor_state", origin="null").status == 403
    # A form post under a strict referrer policy says Origin: null; the browser's Fetch Metadata then decides.
    r = b.request("POST", "/studio/call", b"{}", {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}, origin="null")
    assert r.status == 200
    r = b.request("POST", "/studio/call", b"{}", {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, origin="null")
    assert r.status == 403
    r = b.request("POST", "/studio/call", b"{}", {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}, origin=None)
    assert r.status == 200
    r = b.call("sql_editor_state", {"tab_id": None})
    echo = r.json()
    assert r.status == 200 and echo["method"] == "POST" and echo["path"] == "call"
    assert echo["origin"] == f"http://127.0.0.1:{hub.spawner.get('alice').port}"  # rewritten for the Studio's own check
    assert json.loads(echo["body"])["name"] == "sql_editor_state"
    # The hub's own forms need the session's CSRF token as well.
    assert b.post_form("/logout", {"csrf": "wrong"}).status == 403
    assert any(e["event"] == "csrf_blocked" for e in hub.audit.tail())


def test_security_headers(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    login = Browser(hub.cfg.public_url).get("/login")
    assert "frame-ancestors 'none'" in login.headers["Content-Security-Policy"]
    assert login.headers["X-Frame-Options"] == "DENY" and login.headers["X-Content-Type-Options"] == "nosniff"
    assert login.headers["Referrer-Policy"] == "same-origin"  # no-referrer would make form posts say Origin: null
    # A link, not a form: CSP form-action 'self' would block a form's redirect on to SAS Logon.
    assert 'href="/login/start"' in login.text and 'action="/login/start"' not in login.text
    studio = b.get("/studio/session")
    assert "frame-ancestors 'none'" in studio.headers["Content-Security-Policy"]


# ----------------------------------------------------------------------------- lifecycle

def test_idle_stop_and_restart(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    ping(b)
    st = hub.spawner.get("alice")
    before = st.last_activity
    time.sleep(0.05)
    assert b.get("/studio/ping").status == 200
    assert st.last_activity == before  # the pages' heartbeat isn't use
    st.last_activity = time.time() - hub.cfg.idle_minutes * 60 - 5
    assert hub.spawner.reap_once() == ["alice"]
    assert hub.spawner.get("alice") is None
    assert any(e["event"] == "studio_stop" and e.get("reason") == "idle" for e in hub.audit.tail())
    # A heartbeat doesn't bring it back (the page says it stopped); using it does.
    r = b.get("/studio/ping")
    assert r.status == 503 and hub.spawner.get("alice") is None
    assert ping(b)["user"] == "alice" and hub.spawner.starts == 2


def test_logout_stops_the_studio(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    ping(b)
    account = b.get("/account")
    assert account.status == 200 and "alice" in account.text
    r = b.post_form("/logout", {"csrf": account.hidden("csrf")})
    assert r.status == 303 and r.location == "/login?out=1"
    assert "studio_session" not in b.cookies
    assert hub.spawner.get("alice") is None
    assert b.get("/studio/").location == "/login"
    events = [e["event"] for e in hub.audit.tail()]
    assert "logout" in events and "studio_stop" in events


def test_max_users(tmp_path: Path, logon: FakeSasLogon) -> None:
    hub = make_hub(tmp_path, logon, max_users=1)
    try:
        sign_in(hub, logon, "alice")
        b = Browser(hub.cfg.public_url)
        r = b.get("/login/start")
        r = b.get(logon.sign_in(r.location, "bob"))
        assert r.status == 503 and "full" in r.text.lower()
        assert any(e["event"] == "studio_full" for e in hub.audit.tail())
    finally:
        hub.close()


def test_admin_page_and_stop(hub: Hub, logon: FakeSasLogon) -> None:
    alice, bob = sign_in(hub, logon, "alice"), sign_in(hub, logon, "bob")
    ping(bob)
    assert bob.get("/admin").status == 403  # not an admin
    page = alice.get("/admin")
    assert page.status == 200 and "bob" in page.text and "Memory" in page.text
    r = alice.post_form("/admin/stop", {"csrf": page.hidden("csrf"), "user": "bob"})
    assert r.status == 303
    assert hub.spawner.get("bob") is None
    assert bob.get("/studio/").location == "/login"  # bob's sessions went with it
    assert any(e["event"] == "admin_stop" and e.get("by") == "alice" for e in hub.audit.tail())


def test_no_secrets_in_the_audit_log(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    ping(b)
    account = b.get("/account")
    b.post_form("/logout", {"csrf": account.hidden("csrf")})
    text = (Path(hub.cfg.data_dir) / "audit.jsonl").read_text()
    for secret in [*logon.access, *logon.refresh, TEST_CLIENT_SECRET, MASTER.decode(), hub.cfg.user_key("alice")]:
        assert secret not in text
    assert "login" in text and "logout" in text
