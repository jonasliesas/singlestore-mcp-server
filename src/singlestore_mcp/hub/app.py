"""The hub's HTTP server: sign-in, the user's Studio behind ``/studio/``, account, admin and health pages.

Routes
  GET  /healthz              liveness/readiness (no sign-in; counts only)
  GET  /                     -> /studio/ when signed in, else /login
  GET  /login, /login/start  sign-in page; start SAS Logon (redirect, or the paste-the-code page)
  GET  /oauth/callback       SAS Logon's redirect back (own client)
  POST /login/code           the pasted code (vscode client)
  GET  /account, POST /logout
  GET  /admin, POST /admin/stop   (STUDIO_ADMINS only)
  *    /studio/...           proxied to the signed-in user's own Studio process

Isolation: the user's Studio is found from the session cookie only - no user name, port or token is in any
URL - so one user's browser can't address another user's Studio. Upstream requests carry no cookies or
Authorization header; the path token and port are the hub's secret.
CSRF: cookies are SameSite=Lax; every POST must come from the hub's own origin (Origin header, or
Sec-Fetch-Site: same-origin when a browser leaves Origin out); the hub's own forms also carry a per-session token.
"""

from __future__ import annotations

import http.client
import json
import secrets
import signal
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import oauth, pages
from .audit import Audit
from .config import HubConfig
from .sessions import PENDING_TTL_S, Session, SessionStore
from .spawner import PUBLIC_BASE, Spawner, StudioFailed, StudioFull, rss_bytes

LOGIN_COOKIE = "studio_login"
_MAX_BODY = 2_000_000
_HUB_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
# Headers passed to the Studio (nothing that identifies or authenticates the browser).
_FORWARD = ("content-type", "accept", "accept-language", "cache-control", "if-none-match")
_BACK = ("content-type", "cache-control", "content-disposition", "etag", "last-modified")


class Hub:
    def __init__(self, cfg: HubConfig, audit: Audit | None = None) -> None:
        self.cfg = cfg
        self.audit = audit or Audit(cfg.hub_dir() / "audit.jsonl")
        self.sessions = SessionStore(cfg.session_hours * 3600)
        self.spawner = Spawner(cfg, self.audit)
        self.httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ lifecycle
    def serve(self, block: bool = True) -> int:
        handler = _handler(self)
        self.httpd = ThreadingHTTPServer((self.cfg.bind, self.cfg.port), handler)
        self.httpd.daemon_threads = True
        self.spawner.start_reaper()
        port = self.httpd.server_address[1]
        self.audit.record("hub_start", port=port, mode=self.cfg.oauth_mode, viya=self.cfg.viya_url,
                          max_users=self.cfg.max_users, idle_minutes=self.cfg.idle_minutes)
        if block:
            try:
                self.httpd.serve_forever()
            finally:
                self.close()
        else:
            threading.Thread(target=self.httpd.serve_forever, name="hub", daemon=True).start()
        return port

    def close(self) -> None:
        self.spawner.stop_all()
        if self.httpd is not None:
            httpd, self.httpd = self.httpd, None
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001 - serve_forever already ended
                pass
            httpd.server_close()
            self.audit.record("hub_stop")

    # ------------------------------------------------------------ sign-in
    def finish_sign_in(self, tokens: dict[str, Any], ip: str) -> Session:
        who = oauth.current_user(self.cfg, tokens["access_token"])
        if not self.cfg.may_sign_in(who["id"]):
            self.audit.record("login_denied", user=who["id"], ip=ip, reason="not in STUDIO_ALLOWED_USERS")
            raise oauth.SignInError(f"{who['id']} isn't allowed to use this Studio. Ask the Studio admin.")
        self.spawner.ensure(who["id"], tokens, ip=ip)  # StudioFull when max_users run already
        session = self.sessions.create(who["id"], who["name"], ip)
        self.audit.record("login", user=who["id"], ip=ip, mode=self.cfg.oauth_mode)
        return session


def _handler(hub: Hub) -> type[BaseHTTPRequestHandler]:
    cfg = hub.cfg

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "studio-hub"
        sys_version = ""

        def log_message(self, *args: Any) -> None:  # request lines can carry codes: the audit log is the record
            pass

        # -------------------------------------------------------- helpers
        def _ip(self) -> str:
            if cfg.trust_forwarded:
                fwd = self.headers.get("X-Forwarded-For", "")
                if fwd:
                    return fwd.split(",")[0].strip()[:64]
            return self.client_address[0]

        def _cookies(self) -> dict[str, str]:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except Exception:  # noqa: BLE001 - a malformed cookie header: no cookies
                return {}
            return {k: m.value for k, m in jar.items()}

        def _session(self) -> Session | None:
            return hub.sessions.get(self._cookies().get(cfg.cookie_name))

        def _cookie(self, name: str, value: str, max_age: int) -> str:
            parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
            if cfg.secure:
                parts.append("Secure")
            return "; ".join(parts)

        def _security_headers(self, csp: str | None) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            # same-origin: the hub's own form posts carry Origin (no-referrer makes it "null"); nothing leaks to SAS Logon.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            if csp:
                self.send_header("Content-Security-Policy", csp)
            if cfg.secure:
                self.send_header("Strict-Transport-Security", "max-age=31536000")

        def _send(self, status: int, body: bytes | str, ctype: str = "text/html; charset=utf-8",
                  headers: list[tuple[str, str]] | None = None, csp: str | None = _HUB_CSP) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers(csp)
            for k, v in headers or []:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data), "application/json", csp=None)

        def _redirect(self, location: str, headers: list[tuple[str, str]] | None = None) -> None:
            self._send(303, b"", "text/plain", headers=[("Location", location), *(headers or [])])

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is not None and origin != "null":
                return origin == cfg.origin
            # No Origin, or "null" (a form post under a strict referrer policy): the browser's own Fetch Metadata.
            return self.headers.get("Sec-Fetch-Site") == "same-origin"

        def _body(self) -> bytes | None:
            """The request body (read once), or None when it's too large or its length is bad."""
            if hasattr(self, "_raw"):
                return self._raw
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_BODY:
                self.close_connection = True
                self._raw: bytes | None = None
            else:
                self._raw = self.rfile.read(length) if length else b""
            return self._raw

        def _form(self) -> dict[str, str]:
            raw = self._body() or b""
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

        # -------------------------------------------------------- dispatch
        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            parts = urllib.parse.urlsplit(self.path)
            path, query = parts.path, urllib.parse.parse_qs(parts.query)
            try:
                if path == "/healthz":
                    return self._json(200, {"ok": True, "studios": len(hub.spawner.running()), "sessions": len(hub.sessions.all())})
                if path == "/studio":
                    return self._redirect(PUBLIC_BASE)
                if path.startswith(PUBLIC_BASE):
                    return self._studio(path[len(PUBLIC_BASE):], parts.query)
                if path == "/":
                    return self._redirect(PUBLIC_BASE if self._session() else "/login")
                if path == "/login":
                    note = "You're signed out." if "out" in query else None
                    return self._send(200, pages.login(cfg.viya_url, note=note))
                if path == "/login/start":
                    return self._login_start()
                if path == "/oauth/callback":
                    return self._callback(query)
                if path == "/account":
                    return self._account()
                if path == "/logout":  # a link (e.g. from Studio) lands on the account page's Sign out button
                    return self._account()
                if path == "/admin":
                    return self._admin()
                return self._send(404, pages.message("Not found", "There's nothing here.", ("Open Studio", "/")))
            except Exception as exc:  # noqa: BLE001 - never a stack trace (or a secret) to the browser
                print(f"hub: GET {path}: {type(exc).__name__}", file=sys.stderr, flush=True)
                return self._send(500, pages.message("Something went wrong", "The hub couldn't answer. Try again.", ("Start page", "/"), True))

        def do_POST(self) -> None:  # noqa: N802
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path
            self.__dict__.pop("_raw", None)  # one handler serves every request of a kept-alive connection
            self._body()  # read before any answer (a refusal with unread data resets the connection on Windows)
            if not self._same_origin():
                hub.audit.record("csrf_blocked", ip=self._ip(), path=path[:80], origin=self.headers.get("Origin"))
                return self._send(403, pages.message("Forbidden", "This request didn't come from Studio itself.", ("Start page", "/"), True))
            try:
                if path.startswith(PUBLIC_BASE):
                    return self._studio(path[len(PUBLIC_BASE):], parts.query)
                if path == "/login/code":
                    return self._login_code()
                if path == "/logout":
                    return self._logout()
                if path == "/admin/stop":
                    return self._admin_stop()
                return self._send(404, pages.message("Not found", "There's nothing here.", ("Open Studio", "/")))
            except Exception as exc:  # noqa: BLE001
                print(f"hub: POST {path}: {type(exc).__name__}", file=sys.stderr, flush=True)
                return self._send(500, pages.message("Something went wrong", "The hub couldn't answer. Try again.", ("Start page", "/"), True))

        # -------------------------------------------------------- sign-in
        def _login_start(self) -> None:
            verifier, challenge = oauth.new_pkce()
            pend = hub.sessions.start_pending(verifier)
            url = oauth.authorize_url(cfg, pend.state, challenge)
            cookie = ("Set-Cookie", self._cookie(LOGIN_COOKIE, pend.pid, PENDING_TTL_S))
            if cfg.oauth_mode == "redirect":
                return self._redirect(url, [cookie])
            return self._send(200, pages.paste_code(url, pend.csrf), headers=[cookie])

        def _complete(self, tokens: dict[str, Any]) -> None:
            try:
                session = hub.finish_sign_in(tokens, self._ip())
            except StudioFull as exc:
                return self._send(503, pages.message("Studio is full", str(exc), ("Try again", "/login"), True))
            except oauth.SignInError as exc:
                return self._send(403, pages.message("Sign-in failed", str(exc), ("Try again", "/login"), True))
            old = self._cookies().get(cfg.cookie_name)
            if old:
                hub.sessions.drop(old)  # a new session id at every sign-in (no session fixation)
            self._redirect(PUBLIC_BASE, [("Set-Cookie", self._cookie(cfg.cookie_name, session.sid, cfg.session_hours * 3600)),
                                         ("Set-Cookie", self._cookie(LOGIN_COOKIE, "", 0))])

        def _callback(self, query: dict[str, list[str]]) -> None:
            if cfg.oauth_mode != "redirect":
                return self._send(404, pages.message("Not found", "There's nothing here.", ("Sign in", "/login")))
            pend = hub.sessions.take_pending(self._cookies().get(LOGIN_COOKIE))
            state = (query.get("state") or [""])[0]
            if query.get("error"):
                hub.audit.record("login_failed", ip=self._ip(), reason=(query.get("error") or [""])[0][:60])
                return self._send(403, pages.message("Sign-in failed", "SAS Logon didn't sign you in.", ("Try again", "/login"), True))
            if pend is None or not state or not secrets.compare_digest(state, pend.state):
                hub.audit.record("login_failed", ip=self._ip(), reason="state mismatch or expired")
                return self._send(400, pages.message("Sign-in expired", "Start the sign-in again from this browser.", ("Sign in", "/login"), True))
            try:
                tokens = oauth.exchange_code(cfg, (query.get("code") or [""])[0], pend.verifier)
            except oauth.SignInError as exc:
                hub.audit.record("login_failed", ip=self._ip(), reason="code refused")
                return self._send(403, pages.message("Sign-in failed", str(exc), ("Try again", "/login"), True))
            return self._complete(tokens)

        def _login_code(self) -> None:
            if cfg.oauth_mode != "paste":
                return self._send(404, pages.message("Not found", "There's nothing here.", ("Sign in", "/login")))
            form = self._form()
            pid = self._cookies().get(LOGIN_COOKIE)
            pend = hub.sessions.peek_pending(pid)
            if pend is None or not secrets.compare_digest(form.get("csrf", ""), pend.csrf):
                hub.audit.record("login_failed", ip=self._ip(), reason="sign-in expired or csrf")
                return self._send(400, pages.message("Sign-in expired", "Start the sign-in again from this browser.", ("Sign in", "/login"), True))
            code = form.get("code", "").strip()
            if not code:
                return self._redirect("/login/start")
            hub.sessions.take_pending(pid)  # one try per sign-in: a refused code means starting again
            try:
                tokens = oauth.exchange_code(cfg, code, pend.verifier)
            except oauth.SignInError as exc:
                hub.audit.record("login_failed", ip=self._ip(), reason="code refused")
                return self._send(403, pages.message("Sign-in failed", f"{exc} Start again and paste the new code.", ("Sign in again", "/login/start"), True))
            return self._complete(tokens)

        # -------------------------------------------------------- account / admin
        def _account(self) -> None:
            s = self._session()
            if s is None:
                return self._redirect("/login")
            return self._send(200, pages.account(s.name, s.user, s.csrf, cfg.is_admin(s.user), hub.spawner.get(s.user) is not None))

        def _logout(self) -> None:
            s = self._session()
            form = self._form()
            if s is not None:
                if not secrets.compare_digest(form.get("csrf", ""), s.csrf):
                    return self._send(403, pages.message("Forbidden", "Sign out from the account page.", ("Account", "/account"), True))
                hub.sessions.drop(s.sid)
                hub.spawner.stop(s.user, "logout")
                hub.audit.record("logout", user=s.user, ip=self._ip())
            self._redirect("/login?out=1", [("Set-Cookie", self._cookie(cfg.cookie_name, "", 0))])

        def _admin_session(self) -> Session | None:
            s = self._session()
            if s is None or not cfg.is_admin(s.user):
                return None
            return s

        def _admin(self) -> None:
            s = self._admin_session()
            if s is None:
                if self._session() is None:
                    return self._redirect("/login")
                return self._send(403, pages.message("Forbidden", "The admin page is for Studio admins.", ("Account", "/account"), True))
            rows = [{"user": st.user, "pid": st.pid, "state": "running" if st.alive() and st.port else "starting" if not st.ready.is_set() else "stopped",
                     "started": st.started, "last_activity": st.last_activity, "rss": rss_bytes(st.pid)} for st in hub.spawner.running()]
            sess = [{"user": x.user, "name": x.name, "ip": x.ip, "created": x.created, "last_seen": x.last_seen} for x in hub.sessions.all()]
            limits = {"users": f"{len(rows)} of {cfg.max_users}", "idle stop": f"{cfg.idle_minutes} min",
                      "sign-in": cfg.oauth_mode, "notebooks": "on" if cfg.notebooks else "off", "Ask Claude": "on" if cfg.assistant else "off"}
            return self._send(200, pages.admin(rows, sess, hub.audit.tail(50), s.csrf, limits))

        def _admin_stop(self) -> None:
            s = self._admin_session()
            form = self._form()
            if s is None or not secrets.compare_digest(form.get("csrf", ""), s.csrf):
                return self._send(403, pages.message("Forbidden", "The admin page is for Studio admins.", ("Account", "/account"), True))
            target = form.get("user", "")
            stopped = hub.spawner.stop(target, "admin", by=s.user)
            dropped = hub.sessions.drop_user(target) if target != s.user else 0
            hub.audit.record("admin_stop", user=target, by=s.user, stopped=stopped, sessions=dropped)
            return self._redirect("/admin")

        # -------------------------------------------------------- the user's Studio
        def _studio(self, rest: str, query: str) -> None:
            s = self._session()
            if s is None:
                if self.command == "GET" and rest == "":
                    return self._redirect("/login")
                return self._json(401, {"error": "Signed out: open Studio again to sign in."})
            if rest.startswith("/") or ".." in rest.split("/") or "\\" in rest:
                return self._json(404, {"error": "Not found"})
            if rest == "" and not query and self.command == "GET":
                return self._redirect(PUBLIC_BASE + "?" + urllib.parse.urlencode({"tool": "sql_editor", "args": "{}"}))
            body = self._body() if self.command == "POST" else b""
            if body is None:
                return self._json(413, {"error": "Too large"})
            st = hub.spawner.get(s.user)
            if st is None:
                if rest == "ping":  # a page's heartbeat doesn't start a Studio that was stopped (idle, admin)
                    return self._json(503, {"error": "Studio isn't running", "stopped": True})
                try:
                    st = hub.spawner.ensure(s.user, ip=self._ip())
                except StudioFull as exc:
                    return self._send(503, pages.message("Studio is full", str(exc), ("Try again", PUBLIC_BASE), True))
            try:
                hub.spawner.wait_ready(st)
            except StudioFailed as exc:
                return self._send(503, pages.message("Studio didn't start", str(exc), ("Try again", PUBLIC_BASE), True))
            if rest != "ping":
                hub.spawner.touch(st)
            return self._forward(st, rest, query, body)

        def _forward(self, st: Any, rest: str, query: str, body: bytes) -> None:
            target = st.upstream(rest) + (f"?{query}" if query else "")
            origin = f"http://127.0.0.1:{st.port}"
            headers = {k: v for k, v in ((h, self.headers.get(h)) for h in _FORWARD) if v}
            headers["Host"] = f"127.0.0.1:{st.port}"
            if self.command == "POST":
                headers["Origin"] = origin  # the hub checked the browser's Origin already
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection("127.0.0.1", st.port, timeout=600)
            try:
                conn.request(self.command if self.command != "HEAD" else "GET", target, body=body or None, headers=headers)
                res = conn.getresponse()
                data = res.read()
            except OSError:
                return self._json(502, {"error": "Studio didn't answer. It may have stopped: reload the page."})
            finally:
                conn.close()
            self.send_response(res.status)
            for h in _BACK:
                v = res.getheader(h)
                if v:
                    self.send_header(h, v)
            self.send_header("Content-Length", str(len(data)))
            self._security_headers("frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

    return Handler


def main() -> None:
    from .config import load

    cfg = load()
    hub = Hub(cfg)

    def stop(*_: Any) -> None:
        threading.Thread(target=hub.close, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    print(f"SAS SpeedyStore Studio hub on http://{cfg.bind}:{cfg.port} (public {cfg.public_url}); "
          f"SAS Logon {cfg.viya_url}, sign-in: {cfg.oauth_mode}", file=sys.stderr, flush=True)
    try:
        hub.serve()
    except KeyboardInterrupt:
        pass
    finally:
        hub.close()
        time.sleep(0.2)
