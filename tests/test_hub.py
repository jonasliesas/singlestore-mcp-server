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
import urllib.parse
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
                if self.server.hang_s:  # type: ignore[attr-defined] - a hung Studio: nothing answers in time
                    time.sleep(self.server.hang_s)  # type: ignore[attr-defined]
                if self.path.endswith("/slow"):  # a long call (the Studio itself still answers pings)
                    time.sleep(2.5)
                if not self.path.startswith(f"/{token}/"):
                    data, status = b"not found", 404
                else:
                    status = 200
                    data = json.dumps({"user": st.user, "path": self.path[len(token) + 2:], "method": self.command,
                                       "host": self.headers.get("Host"), "origin": self.headers.get("Origin"),
                                       "cookie": self.headers.get("Cookie"), "authorization": self.headers.get("Authorization"),
                                       "headers": {k.lower(): v for k, v in self.headers.items()},
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
        srv.hang_s = 0  # type: ignore[attr-defined]
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
    assert cfg.login_cookie_name == "__Host-studio_login"  # the sign-in cookie too: no subdomain can plant one
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


# ----------------------------------------------------------------------------- hardening

def _raw(port: int, data: bytes, wait: float = 1.0) -> bytes:
    """Send raw bytes on one connection and read what comes back until the server goes quiet."""
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=wait) as sock:
        sock.sendall(data)
        out = b""
        try:
            while chunk := sock.recv(65536):
                out += chunk
        except TimeoutError:
            pass
    return out


def test_request_smuggling_is_refused(hub: Hub) -> None:
    port = hub.httpd.server_address[1]
    inner = b"GET /login HTTP/1.1\r\nHost: x\r\n\r\n"
    # A chunked body isn't read by this server: refused, connection closed (the body is never a request).
    out = _raw(port, b"POST /logout HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
               + f"{len(inner):x}\r\n".encode() + inner + b"\r\n0\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 400") and out.count(b"HTTP/1.1") == 1
    out = _raw(port, b"POST /logout HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nContent-Length: 33\r\n\r\n" + inner)
    assert out.startswith(b"HTTP/1.1 400") and out.count(b"HTTP/1.1") == 1
    # A GET's body is read and dropped: the bytes in it are not answered as a second request.
    out = _raw(port, b"GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: " + str(len(inner)).encode() + b"\r\n\r\n" + inner)
    assert out.startswith(b"HTTP/1.1 200") and out.count(b"HTTP/1.1") == 1 and b"Sign in" not in out


def test_sign_in_is_rate_limited_per_address(tmp_path: Path, logon: FakeSasLogon) -> None:
    hub = make_hub(tmp_path, logon, login_rate=3, trust_forwarded=True)
    try:
        b = Browser(hub.cfg.public_url)
        # Only the last X-Forwarded-For hop (the one the ingress adds) counts: a made-up first hop doesn't help.
        hops = [f"203.0.113.{n}, 10.9.8.7" for n in range(8)]
        codes = [b.get("/login/start", headers={"X-Forwarded-For": h}).status for h in hops]
        assert codes[:3] == [303, 303, 303] and set(codes[3:]) == {429}
        r = b.get("/login/start", headers={"X-Forwarded-For": "10.9.8.7"})
        assert r.status == 429 and int(r.headers["Retry-After"]) >= 1
        # The code exchange counts too (it calls SAS Logon).
        assert b.get("/oauth/callback?code=x&state=y", headers={"X-Forwarded-For": "10.9.8.7"}).status == 429
        # Another address isn't affected; the refusals are noted once, not per request.
        assert b.get("/login/start", headers={"X-Forwarded-For": "10.1.1.1"}).status == 303
        limited = [e for e in hub.audit.tail() if e["event"] == "login_rate_limited"]
        assert len(limited) == 1 and limited[0]["ip"] == "10.9.8.7"
    finally:
        hub.close()


def test_hub_only_studio_routes_are_not_proxied(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    r = b.request("POST", "/studio/hub/seed", json.dumps({"viya": {"name": "x", "url": "https://evil.example"}}).encode(),
                  {"Content-Type": "application/json"})
    assert r.status == 404 and "user" not in r.json()  # the Studio never saw it
    assert b.get("/studio/shutdown").status == 404
    assert b.get("/studio/hub/seed").status == 404


def test_a_link_from_another_site_opens_plain_studio(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    link = "/studio/?" + urllib.parse.urlencode({"tool": "stop_pipeline", "args": json.dumps({"name": "orders"})})
    for site in ("cross-site", "same-site"):
        r = b.get(link, headers={"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "navigate"})
        assert r.status == 303 and r.location == "/studio/?tool=sql_editor&args=%7B%7D", site
    # ...and that plain Studio page opens (no redirect loop for a cross-site redirect chain).
    r = b.get(r.location, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
    assert r.status == 200 and r.json()["path"] == "?tool=sql_editor&args=%7B%7D"
    # Studio's own links and a typed address / bookmark keep their tool.
    for site in ("same-origin", "none", None):
        r = b.get(link, headers={"Sec-Fetch-Site": site} if site else {})
        assert r.status == 200 and "stop_pipeline" in r.json()["path"], site
    assert any(e["event"] == "cross_site_link" for e in hub.audit.tail())


def test_hop_by_hop_and_identity_headers_stay_at_the_hub(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    r = b.get("/studio/session", headers={"Connection": "Upgrade", "Upgrade": "websocket", "Authorization": "Bearer x",
                                          "Proxy-Authorization": "Basic x", "X-Forwarded-For": "1.2.3.4",
                                          "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Accept": "application/json"})
    assert r.status == 200  # never a 101: no websocket through the hub
    seen = r.json()["headers"]
    for h in ("upgrade", "authorization", "proxy-authorization", "x-forwarded-for", "sec-websocket-key", "cookie"):
        assert h not in seen, h
    assert seen["accept"] == "application/json" and seen.get("connection", "").lower() != "upgrade"
    assert "Set-Cookie" not in r.headers and r.headers.get("Upgrade") is None


def _crash(hub: Hub, user: str, *, exited: bool = True) -> Studio:
    """Make the user's (fake) Studio stop answering: exited (the process is gone) or just its port closed."""
    st = hub.spawner.get(user)
    if exited:
        st.proc.close()
    else:
        st.proc.server.shutdown()
        st.proc.server.server_close()
    return st


def test_a_crashed_studio_is_restarted_on_the_next_request(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    ping(b)
    old = _crash(hub, "alice")
    assert ping(b)["user"] == "alice"  # found dead before forwarding: started again, the call goes through
    assert hub.spawner.starts == 2 and hub.spawner.get("alice") is not old
    assert any(e["event"] == "studio_stop" and e.get("reason") == "exited" for e in hub.audit.tail())


def test_a_studio_that_dies_during_a_call_says_restarting(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    ping(b)
    _crash(hub, "alice", exited=False)  # the process looks alive but nothing answers on its port
    r = b.call("sql_editor_state")
    assert r.status == 503 and r.headers["X-Studio-Restarting"] == "1" and r.json()["restarting"] is True
    assert "restarting" in r.json()["error"]
    assert ping(b)["user"] == "alice" and hub.spawner.starts == 2  # already started again
    # The Studio page itself gets a page that reloads by itself.
    _crash(hub, "alice", exited=False)
    r = b.get("/studio/?tool=sql_editor&args=%7B%7D")
    assert r.status == 503 and 'http-equiv="refresh"' in r.text and "restarting" in r.text
    assert "<script" not in r.text


def test_a_heartbeat_brings_back_a_crashed_studio_but_not_a_stopped_one(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    ping(b)
    _crash(hub, "alice")
    hub.spawner.reap_once()  # the reaper noticed first
    assert hub.spawner.get("alice") is None
    r = b.get("/studio/ping")  # starts it again; the page keeps "reconnecting" while it starts
    assert r.status == 200 or r.headers["X-Studio-Restarting"] == "1"
    deadline = time.time() + 5
    while b.get("/studio/ping").status != 200 and time.time() < deadline:
        time.sleep(0.05)
    assert hub.spawner.starts == 2 and b.get("/studio/ping").status == 200
    hub.spawner.stop("alice", "admin")
    r = b.get("/studio/ping")
    assert r.status == 503 and r.json()["stopped"] is True and "X-Studio-Restarting" not in r.headers


def test_a_studio_that_keeps_crashing_is_not_restarted_for_a_while(hub: Hub, logon: FakeSasLogon) -> None:
    from singlestore_mcp.hub import spawner as sp

    b = sign_in(hub, logon, "alice")
    for _ in range(sp.CRASH_LIMIT):
        ping(b)
        _crash(hub, "alice")
        hub.spawner.reap_once()
    r = b.get("/studio/session")
    assert r.status == 503 and "stopped unexpectedly 3 times" in r.text
    assert hub.spawner.starts == sp.CRASH_LIMIT
    # A deliberate stop (admin, sign-out) starts the count again.
    hub.spawner.stop("alice", "admin")
    assert ping(b)["user"] == "alice"


def test_a_hung_studio_is_dropped_and_restarted(tmp_path: Path, logon: FakeSasLogon, monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp.hub import spawner as sp

    monkeypatch.setattr(sp, "PING_TIMEOUT_S", 0.3)
    hub = make_hub(tmp_path, logon, proxy_timeout_s=1)
    try:
        b = sign_in(hub, logon, "alice")
        ping(b)
        # A long call while the Studio still answers its ping: 504, the Studio stays.
        t0 = time.time()
        r = b.get("/studio/slow")
        assert r.status == 504 and time.time() - t0 < 2.4 and hub.spawner.starts == 1
        # Nothing answers any more: after the call's timeout and a failed ping, it's dropped and started again.
        hub.spawner.get("alice").proc.server.hang_s = 30
        r = b.call("sql_editor_state")
        assert r.status == 503 and r.headers["X-Studio-Restarting"] == "1"
        assert ping(b)["user"] == "alice" and hub.spawner.starts == 2
        assert any(e["event"] == "studio_stop" and e.get("reason") == "hung" for e in hub.audit.tail())
    finally:
        hub.close()


def test_health_check_finds_a_hung_studio(hub: Hub, logon: FakeSasLogon, monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp.hub import spawner as sp

    monkeypatch.setattr(sp, "PING_TIMEOUT_S", 0.3)
    alice, bob = sign_in(hub, logon, "alice"), sign_in(hub, logon, "bob")
    ping(alice)
    ping(bob)
    hub.spawner.get("alice").proc.server.hang_s = 30
    assert [hub.spawner.check_health() for _ in range(sp.HUNG_PINGS)] == [[]] * (sp.HUNG_PINGS - 1) + [["alice"]]
    assert hub.spawner.get("alice") is None and hub.spawner.get("bob") is not None


def test_idle_stop_waits_for_a_call_in_flight(hub: Hub, logon: FakeSasLogon) -> None:
    b = sign_in(hub, logon, "alice")
    ping(b)
    st = hub.spawner.get("alice")
    with hub.spawner.using(st):  # e.g. a 40-minute SAS job with no other request meanwhile
        st.last_activity = time.time() - hub.cfg.idle_minutes * 60 - 5
        assert hub.spawner.reap_once() == []
    assert time.time() - st.last_activity < 5  # its end counts as use
    assert hub.spawner.reap_once() == [] and hub.spawner.get("alice") is st


def test_a_crashed_studio_does_not_hold_a_place(tmp_path: Path, logon: FakeSasLogon) -> None:
    hub = make_hub(tmp_path, logon, max_users=1, admins=())
    try:
        ping(sign_in(hub, logon, "alice"))
        _crash(hub, "alice")
        assert ping(sign_in(hub, logon, "bob"))["user"] == "bob"
    finally:
        hub.close()


def test_an_admin_can_sign_in_when_studio_is_full(tmp_path: Path, logon: FakeSasLogon) -> None:
    hub = make_hub(tmp_path, logon, max_users=1)  # alice is the admin
    try:
        ping(sign_in(hub, logon, "bob"))
        alice = sign_in(hub, logon, "alice")
        assert alice.get("/admin").status == 200 and len(hub.spawner.running()) == 2
    finally:
        hub.close()


def test_close_stops_every_studio_and_takes_no_new_ones(hub: Hub, logon: FakeSasLogon) -> None:
    from singlestore_mcp.hub.spawner import StudioFailed

    sign_in(hub, logon, "alice")
    sign_in(hub, logon, "bob")
    procs = [hub.spawner.wait_ready(hub.spawner.get(u)).proc for u in ("alice", "bob")]
    hub.close()
    hub.close()  # twice (signal handler + finally): fine
    assert all(p.poll() is not None for p in procs) and hub.spawner.running() == []
    stops = [e for e in hub.audit.tail() if e["event"] == "studio_stop"]
    assert {e["user"] for e in stops} == {"alice", "bob"} and all(e["reason"] == "hub stopped" for e in stops)
    with pytest.raises(StudioFailed):
        hub.spawner.ensure("alice")


def test_a_studio_stopped_while_starting_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp.hub import spawner as sp

    class Proc:
        pid, returncode, stdout = 4242, None, iter(())

        def __init__(self, *a: Any, **kw: Any) -> None:
            self.stdin = self

        def close(self) -> None:
            self.returncode = 0

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int | None:
            return self.returncode

    monkeypatch.setattr(sp.subprocess, "Popen", Proc)
    cfg = HubConfig(viya_url="https://viya.example", master_key=MASTER, data_dir=tmp_path)
    spawner = Spawner(cfg, Audit(tmp_path / "a.jsonl", echo=False))
    st = Studio(user="alice", token="t")
    st.stopping = True  # signed out (or the hub stopped) while the process was being created
    spawner._start(st, None)
    assert st.proc.returncode == 0 and st.ready.is_set() and st.error


def test_rate_limiter_window() -> None:
    from singlestore_mcp.hub.sessions import RateLimiter

    rl = RateLimiter(2, window_s=10)
    assert rl.allow("a", now=0) and rl.allow("a", now=1) and not rl.allow("a", now=2)
    assert rl.first_refusal("a", now=2) and not rl.first_refusal("a", now=3)
    assert rl.retry_after("a", now=2) >= 8
    assert rl.allow("b", now=2)  # per address
    assert rl.allow("a", now=10.5)  # the first hit left the window


def test_memory_of_a_studio_counts_its_child_processes() -> None:
    """On Windows a venv's python.exe is a launcher; the Studio's memory is in its child (admin page, sizing)."""
    import subprocess
    import sys

    from singlestore_mcp.hub import spawner as sp

    assert sp.rss_bytes(None) is None and sp.rss_bytes(os.getpid()) > 1_000_000
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        deadline = time.time() + 10
        while sp.rss_bytes(child.pid) is None and time.time() < deadline:
            time.sleep(0.1)
        assert sp.rss_bytes(os.getpid()) >= sp._rss_one(os.getpid())
        if sys.platform == "win32":
            assert child.pid in sp._win_children(os.getpid())
    finally:
        child.kill()
        child.wait()


# ----------------------------------------------------------------------------- SAS groups (app / notebooks)

USERS_GROUP, NOTEBOOK_GROUP = "SpeedyStoreStudioUsers", "SpeedyStoreStudioNotebooks"


def group_hub(tmp_path: Path, logon: FakeSasLogon, **over: Any) -> Hub:
    values = dict(users_group=USERS_GROUP, notebook_group=NOTEBOOK_GROUP)
    values.update(over)
    return make_hub(tmp_path, logon, **values)


def seeds(hub: Hub) -> dict[str, Any]:
    for st in hub.spawner.running():
        hub.spawner.wait_ready(st)  # the (fake) start thread records the seed
    return dict(hub.spawner.seeds)


def denied(hub: Hub, logon: FakeSasLogon, user: str):
    b = Browser(hub.cfg.public_url)
    r = b.get("/login/start")
    r = b.get(logon.sign_in(r.location, user))
    assert r.status == 403 and "studio_session" not in b.cookies and hub.spawner.get(user) is None
    return r


def test_users_group_member_signs_in_and_others_are_refused(tmp_path: Path, logon: FakeSasLogon) -> None:
    logon.memberships = {"alice": ["SASAdministrators", "Nordics", USERS_GROUP]}  # 3rd item: on the second page
    hub = group_hub(tmp_path, logon)
    try:
        alice = sign_in(hub, logon, "alice")
        assert ping(alice)["user"] == "alice"
        a = hub.spawner.access["alice"]
        assert a.app and a.app_via == f"group {USERS_GROUP}" and not a.notebooks and a.notebooks_via == "off on this server"
        r = denied(hub, logon, "bob")
        assert USERS_GROUP in r.text and "Ask the Studio admin" in r.text
        e = next(e for e in hub.audit.tail() if e["event"] == "login_denied")
        assert e["user"] == "bob" and USERS_GROUP in e["reason"]
        page = alice.get("/admin")
        assert f"yes (group {USERS_GROUP})" in page.text and "no (off on this server)" in page.text
        assert logon.membership_requests >= 2  # paged
    finally:
        hub.close()


def test_group_matches_by_name_too(tmp_path: Path, logon: FakeSasLogon) -> None:
    logon.memberships = {"alice": [USERS_GROUP]}
    hub = group_hub(tmp_path, logon, users_group="speedy storestudiousers")  # the fake's display name, any case
    try:
        assert ping(sign_in(hub, logon, "alice"))["user"] == "alice"
    finally:
        hub.close()


def test_notebook_group_gives_notebooks_and_the_app(tmp_path: Path, logon: FakeSasLogon) -> None:
    logon.memberships = {"alice": [USERS_GROUP], "bob": [NOTEBOOK_GROUP]}
    hub = group_hub(tmp_path, logon, notebooks=True)  # STUDIO_NOTEBOOKS=1: the server-wide switch
    try:
        sign_in(hub, logon, "alice")
        sign_in(hub, logon, "bob")
        a, b = hub.spawner.access["alice"], hub.spawner.access["bob"]
        assert a.app and not a.notebooks and a.notebooks_via == f"not in {NOTEBOOK_GROUP}"
        assert b.app and b.app_via == f"group {NOTEBOOK_GROUP}" and b.notebooks
        seeds_ = seeds(hub)
        assert seeds_["alice"]["access"]["notebooks"] is False and seeds_["bob"]["access"]["notebooks"] is True
        assert seeds_["bob"]["access"]["rules"]["notebook_group"] == NOTEBOOK_GROUP  # the Studio re-checks with these
        assert hub.spawner._env("alice")["SINGLESTORE_MCP_NOTEBOOKS"] == "0"
        assert hub.spawner._env("bob")["SINGLESTORE_MCP_NOTEBOOKS"] == "1"
    finally:
        hub.close()


def test_notebook_group_alone_doesnt_switch_notebooks_on(tmp_path: Path, logon: FakeSasLogon) -> None:
    logon.memberships = {"bob": [NOTEBOOK_GROUP]}
    hub = group_hub(tmp_path, logon)  # STUDIO_NOTEBOOKS off (step 1)
    try:
        sign_in(hub, logon, "bob")
        b = hub.spawner.access["bob"]
        assert b.app and not b.notebooks and seeds(hub)["bob"]["access"]["notebooks"] is False
    finally:
        hub.close()


def test_allowed_users_is_an_extra_list(tmp_path: Path, logon: FakeSasLogon) -> None:
    logon.memberships = {"alice": [USERS_GROUP]}
    hub = group_hub(tmp_path, logon, allowed_users=("bob",))
    try:
        sign_in(hub, logon, "bob")
        assert hub.spawner.access["bob"].app_via == "STUDIO_ALLOWED_USERS"
        assert hub.spawner.access["bob"].notebooks is False
    finally:
        hub.close()


def test_identities_unavailable_refuses_new_sign_ins_but_keeps_sessions(tmp_path: Path, logon: FakeSasLogon) -> None:
    logon.memberships = {"alice": [USERS_GROUP], "bob": [USERS_GROUP]}
    hub = group_hub(tmp_path, logon)
    try:
        alice = sign_in(hub, logon, "alice")
        ping(alice)
        logon.identities_down = True
        r = denied(hub, logon, "bob")
        assert "check your SAS group membership" in r.text
        assert any(e["event"] == "login_denied" and e["reason"] == "group check failed" for e in hub.audit.tail())
        hub.studio_access(hub.spawner.get("alice"), {"check": "failed"})  # alice's Studio couldn't re-check either
        assert ping(alice)["user"] == "alice" and hub.spawner.access["alice"].app
        assert any(e["event"] == "access_check_failed" for e in hub.audit.tail())
    finally:
        hub.close()


def test_removed_from_the_group_at_token_refresh(tmp_path: Path, logon: FakeSasLogon) -> None:
    logon.memberships = {"alice": [USERS_GROUP, NOTEBOOK_GROUP]}
    hub = group_hub(tmp_path, logon, notebooks=True)
    try:
        alice = sign_in(hub, logon, "alice")
        ping(alice)
        st = hub.spawner.get("alice")
        # The Studio's report can narrow access, never widen it: notebooks off first...
        hub.studio_access(st, {"app": True, "app_via": f"group {USERS_GROUP}", "notebooks": False, "notebooks_via": "x"})
        assert hub.spawner.access["alice"].notebooks is False and hub.spawner.get("alice") is st
        hub.studio_access(st, {"app": True, "app_via": "x", "notebooks": True, "notebooks_via": "x"})
        assert hub.spawner.access["alice"].notebooks is False
        # ...then out of every group: signed out, Studio stopped.
        hub.studio_access(st, {"app": False, "app_via": f"not in {USERS_GROUP}", "notebooks": False, "notebooks_via": ""})
        deadline = time.time() + 5
        while hub.spawner.get("alice") is not None and time.time() < deadline:
            time.sleep(0.05)
        assert hub.spawner.get("alice") is None and alice.get("/studio/").location == "/login"
        e = next(e for e in hub.audit.tail() if e["event"] == "access_revoked")
        assert e["user"] == "alice" and USERS_GROUP in e["reason"]
    finally:
        hub.close()


def test_access_rules_without_groups_are_as_before() -> None:
    from singlestore_mcp.hub.access import Rules, decide

    a = decide("alice", Rules(), [])
    assert a.app and a.app_via == "everyone who can sign in" and not a.notebooks
    assert not decide("alice", Rules(notebooks=True), []).notebooks  # local notebooks never for "everyone"
    assert decide("alice", Rules(notebooks=True, allowed_users=("alice",)), []).notebooks
    assert not decide("bob", Rules(allowed_users=("alice",)), []).app
    assert not decide("alice", Rules(users_group=USERS_GROUP), None).app  # check failed: refused


def test_admin_page_warns_about_local_notebooks(tmp_path: Path, logon: FakeSasLogon) -> None:
    hub = make_hub(tmp_path, logon, notebooks=True)
    try:
        page = sign_in(hub, logon, "alice").get("/admin")
        assert "no isolation between users&#x27; kernels" in page.text and "step 2a" in page.text
        assert "notebooks: local" in page.text
    finally:
        hub.close()
    hub = make_hub(tmp_path / "off", logon)
    try:
        assert "no isolation" not in sign_in(hub, logon, "alice").get("/admin").text
    finally:
        hub.close()


def test_studio_notebooks_is_off_local_or_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp.hub import config

    for raw, mode in (("", "off"), ("off", "off"), ("0", "off"), ("local", "local"), ("LOCAL", "local"), ("1", "local"),
                      ("gateway", "gateway")):
        monkeypatch.setenv("STUDIO_NOTEBOOKS", raw)
        assert config._notebooks_mode() == mode, raw
    monkeypatch.setenv("STUDIO_NOTEBOOKS", "kubernetes")  # step 2b isn't a mode of its own: a clear stop, not a silent off
    with pytest.raises(SystemExit):
        config._notebooks_mode()
    assert config.HubConfig(viya_url="https://v", notebooks=True).notebooks_mode == "local"  # as before step 2a
    assert config.HubConfig(viya_url="https://v", notebooks_mode="gateway").notebooks
