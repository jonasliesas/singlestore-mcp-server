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
Authorization header; the path token and port are the hub's secret. The hub's own routes into a Studio
(``hub/seed``) can't be reached through ``/studio/``.
CSRF: cookies are SameSite=Lax; every POST must come from the hub's own origin (Origin header, or
Sec-Fetch-Site: same-origin when a browser leaves Origin out); the hub's own forms also carry a per-session token.
A link from another site (Sec-Fetch-Site: cross-site / same-site) opens plain Studio: it can't pick the tool or its
arguments that the Studio page runs on opening.
Framing: requests with Transfer-Encoding or several Content-Length headers are refused and a GET's body is read
(no request smuggling behind the ingress's kept-alive connections); a client gets ``Handler.timeout`` seconds per read.
Sign-in starts and code exchanges are rate limited per client address (``STUDIO_LOGIN_RATE`` a minute).
Robustness: a Studio that crashed or hangs is dropped and started again; the browser gets "Your Studio is
restarting" (a page that reloads itself, or ``X-Studio-Restarting: 1`` on its calls, which the Studio page waits out).
"""

from __future__ import annotations

import http.client
import json
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import access, oauth, pages
from .audit import Audit
from .config import NOTEBOOKS_GATEWAY_NOTE, NOTEBOOKS_LOCAL_NOTE, HubConfig
from .sessions import PENDING_TTL_S, RateLimiter, Session, SessionStore
from .spawner import PUBLIC_BASE, Spawner, Studio, StudioFailed, StudioFull, rss_bytes

_MAX_BODY = 2_000_000
_PAGE_WAIT_S = 20     # a page request waits this long for a starting Studio, then gets a page that reloads itself
_HUB_ONLY = ("hub", "shutdown")  # Studio routes only the hub itself may call (never proxied from a browser)
_DEFAULT_QUERY = urllib.parse.urlencode({"tool": "sql_editor", "args": "{}"})
_HUB_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
# Headers passed to the Studio (nothing that identifies or authenticates the browser).
_FORWARD = ("content-type", "accept", "accept-language", "cache-control", "if-none-match")
_BACK = ("content-type", "cache-control", "content-disposition", "etag", "last-modified")


class _HubServer(ThreadingHTTPServer):
    daemon_threads = True
    # Python's default listen backlog is 5: every user's page opens with several calls at once, and a connection
    # beyond the backlog is retried by the client's TCP only after 1 s (Linux) / 0.5 s (Windows).
    request_queue_size = 128


class Hub:
    def __init__(self, cfg: HubConfig, audit: Audit | None = None) -> None:
        self.cfg = cfg
        self.audit = audit or Audit(cfg.hub_dir() / "audit.jsonl")
        self.sessions = SessionStore(cfg.session_hours * 3600)
        self.spawner = Spawner(cfg, self.audit)
        self.spawner.on_access = self.studio_access
        self.broker = None
        if cfg.gateway:
            from .gateway import GatewaySettings, KernelBroker
            from .spawner import safe_name

            self.broker = KernelBroker(GatewaySettings(url=cfg.gateway_url, token=cfg.gateway_token,
                                                       kernels_per_user=cfg.kernels_per_user, kernels_total=cfg.kernels_total,
                                                       relay_bind=cfg.relay_bind, relay_port=cfg.relay_port,
                                                       relay_url=cfg.relay_url, verify=cfg.gateway_verify),
                                       self.audit, allowed=self._kernel_access, folder=safe_name)
            self.spawner.broker = self.broker
        self.collector = None
        if cfg.collector:
            from .collector import CollectorProcess

            self.collector = CollectorProcess(cfg, self.audit)
            self.spawner.collector = self.collector
            self.collector.on_ready = self._collector_ready
        self.login_limit = RateLimiter(cfg.login_rate, 60.0)
        self.httpd: ThreadingHTTPServer | None = None
        self._close_lock = threading.Lock()
        self._closed = False

    def _collector_ready(self) -> None:
        """The collector (re)started: running Studios get its address (it has a new port)."""
        for st in self.spawner.running():
            if st.ready.is_set() and st.port and not st.stopping:
                self.spawner.push_seed(st, {"collector": self.spawner.collector_seed(st.user)})

    def _kernel_access(self, user: str) -> bool:
        """Gateway kernels only for users whose sign-in check (or a later re-check) gave notebooks."""
        acc = self.spawner.access.get(user)
        return acc is not None and acc.app and acc.notebooks

    # ------------------------------------------------------------ lifecycle
    def serve(self, block: bool = True) -> int:
        handler = _handler(self)
        self.httpd = _HubServer((self.cfg.bind, self.cfg.port), handler)
        if self.broker is not None:
            self.broker.serve()
        if self.collector is not None:
            self.collector.start()
            self.collector.wait_ready(20)  # Studios started before it is up keep their own store until a reseed
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
        """Stop taking requests, then stop every Studio (all at once). Safe to call twice (signal + finally): a
        second call waits until the first is done."""
        with self._close_lock:
            if self._closed:
                return
            httpd, self.httpd = self.httpd, None
            if httpd is not None:
                try:
                    httpd.shutdown()
                except Exception:  # noqa: BLE001 - serve_forever already ended
                    pass
            self.spawner.stop_all()
            if self.broker is not None:
                self.broker.close()
            if self.collector is not None:
                self.collector.stop()
            if httpd is not None:
                httpd.server_close()
                self.audit.record("hub_stop")
            self._closed = True

    # ------------------------------------------------------------ sign-in
    def check_access(self, user: str, access_token: str) -> access.Access:
        """App / notebook access from the SAS groups (read with the user's own token), STUDIO_ALLOWED_USERS and
        STUDIO_NOTEBOOKS. The identities service down: access refused (a new sign-in waits until it answers)."""
        rules = self.cfg.rules
        groups: list[dict[str, str]] | None = []
        if rules.wants_groups:
            try:
                groups = access.fetch_groups(lambda path: oauth.get(self.cfg, path, access_token), user)
            except (access.GroupCheckFailed, oauth.SignInError):
                groups = None
        return access.decide(user, rules, groups)

    def studio_access(self, st: Any, report: dict[str, Any]) -> None:
        """A Studio re-checked its user's groups after renewing the Viya token (studio_server.recheck_access).
        Only ever narrows access: the hub's own sign-in check is what grants it."""
        if report.get("check") == "failed":
            self.audit.record("access_check_failed", user=st.user)  # running sessions stay; a new sign-in is refused
            return
        old = self.spawner.access.get(st.user)
        if old is None or not old.app:
            return
        new = access.Access(app=bool(report.get("app")), app_via=str(report.get("app_via") or "")[:120],
                            notebooks=bool(report.get("notebooks")) and old.notebooks,
                            notebooks_via=str(report.get("notebooks_via") or "")[:120],
                            monitor=bool(report.get("monitor")) and old.monitor,
                            monitor_via=str(report.get("monitor_via") or "")[:120])
        if (old.app, old.notebooks, old.monitor) == (new.app, new.notebooks, new.monitor):
            return
        self.spawner.access[st.user] = new
        self.audit.record("access_changed", user=st.user, app=new.app, app_via=new.app_via, notebooks=new.notebooks,
                          notebooks_via=new.notebooks_via, monitor=new.monitor)
        if old.monitor and not new.monitor and self.collector is not None and new.app:
            # Out of the monitor group: a new repository token that shows only their own queries.
            threading.Thread(target=self.spawner.push_seed, args=(st, {"collector": self.spawner.collector_seed(st.user)}),
                             daemon=True).start()
        if self.broker is not None and old.notebooks and not new.notebooks:  # gateway kernels stop with the access
            threading.Thread(target=self.broker.forget_studio, args=(st.user, "notebook access removed"), daemon=True).start()
        if not new.app:
            dropped = self.sessions.drop_user(st.user)
            threading.Thread(target=self.spawner.stop, args=(st.user, "access removed"), daemon=True).start()
            self.audit.record("access_revoked", user=st.user, reason=new.app_via, sessions=dropped)

    def finish_sign_in(self, tokens: dict[str, Any], ip: str) -> Session:
        who = oauth.current_user(self.cfg, tokens["access_token"])
        acc = self.check_access(who["id"], tokens["access_token"])
        if not acc.app:
            self.audit.record("login_denied", user=who["id"], ip=ip, reason=acc.app_via)
            raise oauth.SignInError(acc.reason)
        self.spawner.access[who["id"]] = acc
        # StudioFull when max_users run already (an admin may start one more, to reach /admin)
        self.spawner.ensure(who["id"], tokens, ip=ip, admin=self.cfg.is_admin(who["id"]))
        session = self.sessions.create(who["id"], who["name"], ip)
        self.audit.record("login", user=who["id"], ip=ip, mode=self.cfg.oauth_mode, app_via=acc.app_via, notebooks=acc.notebooks,
                          monitor=acc.monitor)
        return session


def repository_summary(hub: Hub) -> str:
    """One line for the admin page: the Studio repository's state."""
    if hub.collector is None:
        return "off (each Studio keeps its own history; set STUDIO_COLLECTOR_PASSWORD)"
    st = hub.collector.status()
    if not st.get("running"):
        return f"{hub.cfg.repository_db}: collector not running - {st.get('error') or 'starting'}"
    repo = st.get("repository") or {}
    parts = [f"{hub.cfg.repository_db} on {st.get('source')}"]
    if repo.get("last_flush"):
        parts.append(f"last write {time.strftime('%H:%M:%S', time.localtime(repo['last_flush']))}")
    if repo.get("pending"):
        parts.append(f"{repo['pending']} rows waiting for the cluster")
    m = st.get("metrics") or {}
    if m.get("last_sample"):
        parts.append(f"last sample {time.strftime('%H:%M:%S', time.localtime(m['last_sample']))}")
    for err in (repo.get("last_error"), st.get("last_error"), m.get("last_error"), st.get("error")):
        if err:
            parts.append(f"error: {err}")
            break
    return " · ".join(parts)


def _handler(hub: Hub) -> type[BaseHTTPRequestHandler]:
    cfg = hub.cfg

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "studio-hub"
        sys_version = ""
        timeout = 120  # seconds per read from the browser (a slow or idle client doesn't hold a thread for ever)

        def parse_request(self) -> bool:
            self.__dict__.pop("_raw", None)  # one handler serves every request of a kept-alive connection
            if not super().parse_request():
                return False
            # Only Content-Length framing, once: a chunked body (which this server doesn't read) or two lengths
            # would leave bytes that the next request on the connection is read from (request smuggling).
            if self.headers.get_all("Transfer-Encoding") or len(self.headers.get_all("Content-Length") or []) > 1:
                self.send_error(400, "Bad request framing")  # also closes the connection
                return False
            return True

        def log_message(self, *args: Any) -> None:  # request lines can carry codes: the audit log is the record
            pass

        # -------------------------------------------------------- helpers
        def _ip(self) -> str:
            if cfg.trust_forwarded:
                # The last address is the one the ingress (the trusted proxy in front) added; anything before it
                # came from the client and could be made up.
                hops = [h.strip() for h in self.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
                if hops:
                    return hops[-1][:64]
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

        def _too_many(self) -> bool:
            """Sign-in rate limit per client address: answers 429 and returns True when used up."""
            ip = self._ip()
            if hub.login_limit.allow(ip):
                return False
            if hub.login_limit.first_refusal(ip):  # once per window: a flood doesn't fill the audit log
                hub.audit.record("login_rate_limited", ip=ip, limit=cfg.login_rate)
            self._send(429, pages.message("Too many sign-ins", "Too many sign-in attempts from your address. "
                                          "Wait a minute, then try again.", ("Sign in", "/login"), True),
                       headers=[("Retry-After", str(hub.login_limit.retry_after(ip)))])
            return True

        def _form(self) -> dict[str, str]:
            raw = self._body() or b""
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

        # -------------------------------------------------------- dispatch
        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            parts = urllib.parse.urlsplit(self.path)
            path, query = parts.path, urllib.parse.parse_qs(parts.query)
            self._body()  # a GET with a body: read it (or close the connection), never parse it as the next request
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
            if self._too_many():
                return None
            verifier, challenge = oauth.new_pkce()
            pend = hub.sessions.start_pending(verifier)
            url = oauth.authorize_url(cfg, pend.state, challenge)
            cookie = ("Set-Cookie", self._cookie(cfg.login_cookie_name, pend.pid, PENDING_TTL_S))
            if cfg.oauth_mode == "redirect":
                return self._redirect(url, [cookie])
            return self._send(200, pages.paste_code(url, pend.csrf), headers=[cookie])

        def _complete(self, tokens: dict[str, Any]) -> None:
            try:
                session = hub.finish_sign_in(tokens, self._ip())
            except StudioFull as exc:
                return self._send(503, pages.message("Studio is full", str(exc), ("Try again", "/login"), True))
            except StudioFailed as exc:  # keeps crashing, or the hub is stopping
                return self._send(503, pages.message("Studio didn't start", str(exc), ("Try again", "/login"), True))
            except oauth.SignInError as exc:
                return self._send(403, pages.message("Sign-in failed", str(exc), ("Try again", "/login"), True))
            old = self._cookies().get(cfg.cookie_name)
            if old:
                hub.sessions.drop(old)  # a new session id at every sign-in (no session fixation)
            self._redirect(PUBLIC_BASE, [("Set-Cookie", self._cookie(cfg.cookie_name, session.sid, cfg.session_hours * 3600)),
                                         ("Set-Cookie", self._cookie(cfg.login_cookie_name, "", 0))])

        def _callback(self, query: dict[str, list[str]]) -> None:
            if cfg.oauth_mode != "redirect":
                return self._send(404, pages.message("Not found", "There's nothing here.", ("Sign in", "/login")))
            if self._too_many():
                return None
            pend = hub.sessions.take_pending(self._cookies().get(cfg.login_cookie_name))
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
            if self._too_many():
                return None
            form = self._form()
            pid = self._cookies().get(cfg.login_cookie_name)
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
            def acc(user: str) -> dict[str, Any]:
                a = hub.spawner.access.get(user)
                return {"app": a.app, "app_via": a.app_via, "notebooks": a.notebooks, "notebooks_via": a.notebooks_via} if a else {}

            rows = [{**acc(st.user), "user": st.user, "pid": st.pid, "state": "running" if st.alive() and st.port else "starting" if not st.ready.is_set() else "stopped",
                     "started": st.started, "last_activity": st.last_activity, "rss": rss_bytes(st.pid)} for st in hub.spawner.running()]
            sess = [{**acc(x.user), "user": x.user, "name": x.name, "ip": x.ip, "created": x.created, "last_seen": x.last_seen} for x in hub.sessions.all()]
            limits = {"users": f"{len(rows)} of {cfg.max_users}", "idle stop": f"{cfg.idle_minutes} min",
                      "sign-in": cfg.oauth_mode, "notebooks": cfg.notebooks_mode, "Ask Claude": "on" if cfg.assistant else "off",
                      "users group": cfg.users_group or "none", "notebook group": cfg.notebook_group or "none",
                      "monitor group": cfg.monitor_group or "none", "repository": repository_summary(hub)}
            warning = NOTEBOOKS_LOCAL_NOTE if cfg.notebooks_mode == "local" else None
            kernels = None
            if hub.broker is not None:
                warning = NOTEBOOKS_GATEWAY_NOTE
                counts = hub.broker.counts()
                for r in rows:
                    r["kernels"] = counts.get(r["user"], 0)
                limits.update({"kernels": f"{sum(counts.values())} of {cfg.kernels_total} ({cfg.kernels_per_user} per user)",
                               "package installs": cfg.pip_mode + (f" ({cfg.pip_index})" if cfg.pip_mode == "mirror" else "")})
                kernels = [{"user": k.user, "id": k.id[:8], "purpose": k.purpose, "started": k.created,
                            "last_activity": k.last_activity, "channels": k.channels} for k in hub.broker.kernels()]
                kernels.append({"gateway": cfg.gateway_url, "healthy": hub.broker.healthy()})
            return self._send(200, pages.admin(rows, sess, hub.audit.tail(50), s.csrf, limits, warning, kernels))

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
            if rest.split("/", 1)[0] in _HUB_ONLY:  # e.g. hub/seed: the hub's own call into a Studio, not the browser's
                return self._json(404, {"error": "Not found"})
            page = self.command in ("GET", "HEAD") and rest == ""
            if page and query and query != _DEFAULT_QUERY and self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                # A link from another site: plain Studio. The tool and arguments in the address are what the page
                # runs on opening, so another site must not choose them (e.g. ?tool=stop_pipeline&args=...).
                hub.audit.record("cross_site_link", user=s.user, ip=self._ip())
                return self._redirect(PUBLIC_BASE + "?" + _DEFAULT_QUERY)
            if rest == "" and not query and self.command == "GET":
                return self._redirect(PUBLIC_BASE + "?" + _DEFAULT_QUERY)
            body = self._body() if self.command == "POST" else b""
            if body is None:
                return self._json(413, {"error": "Too large"})
            st = hub.spawner.get(s.user)
            if st is None:
                # A page's heartbeat doesn't start a Studio that was stopped (idle, admin, sign-out) - but it does
                # bring back one that crashed or hung, so the open page reconnects by itself.
                if rest == "ping" and not hub.spawner.crashed_recently(s.user):
                    return self._json(503, {"error": "Studio isn't running", "stopped": True})
                try:
                    st = hub.spawner.ensure(s.user, ip=self._ip(), admin=cfg.is_admin(s.user))
                except StudioFull as exc:
                    return self._send(503, pages.message("Studio is full", str(exc), ("Try again", PUBLIC_BASE), True))
                except StudioFailed as exc:
                    return self._send(503, pages.message("Studio didn't start", str(exc), ("Try again", PUBLIC_BASE), True))
            if not st.ready.is_set():
                if rest == "ping":
                    return self._restarting(st, page=False)
                if page and not st.ready.wait(_PAGE_WAIT_S):
                    return self._restarting(st, page=True)
            try:
                hub.spawner.wait_ready(st)
            except StudioFailed as exc:
                return self._send(503, pages.message("Studio didn't start", str(exc), ("Try again", PUBLIC_BASE), True))
            if rest == "ping":
                return self._forward(st, rest, query, body)
            hub.spawner.touch(st)
            with hub.spawner.using(st):  # the idle stop waits for calls in flight
                return self._forward(st, rest, query, body)

        def _restarting(self, st: Studio, page: bool) -> None:
            """The user's Studio is (re)starting: a page that reloads itself, or for the Studio page's own requests
            a 503 with X-Studio-Restarting, which the page waits out like a server restart."""
            hdrs = [("X-Studio-Restarting", "1"), ("Retry-After", "3")]
            if page:
                return self._send(503, pages.waiting(st.restart), headers=hdrs)
            text = "Your Studio is restarting. Try again in a moment." if st.restart else "Your Studio is starting."
            return self._send(503, json.dumps({"error": text, "restarting": True}), "application/json", headers=hdrs, csp=None)

        def _lost(self, st: Studio, rest: str, hung: bool) -> None:
            """The Studio crashed (or hangs): drop it, start a new one at once, and say "restarting"."""
            try:
                st.proc.wait(timeout=0.5)  # type: ignore[union-attr] - a crash: the process is about to be gone
            except (subprocess.TimeoutExpired, AttributeError):
                pass
            hub.spawner.crashed(st, "hung" if hung and st.alive() else "exited" if not st.alive() else "not answering")
            try:
                fresh = hub.spawner.ensure(st.user, ip=self._ip(), admin=cfg.is_admin(st.user))
            except (StudioFull, StudioFailed) as exc:
                return self._json(503, {"error": str(exc)})
            return self._restarting(fresh, page=self.command in ("GET", "HEAD") and rest == "")

        def _forward(self, st: Studio, rest: str, query: str, body: bytes) -> None:
            target = st.upstream(rest) + (f"?{query}" if query else "")
            origin = f"http://127.0.0.1:{st.port}"
            headers = {k: v for k, v in ((h, self.headers.get(h)) for h in _FORWARD) if v}
            headers["Host"] = f"127.0.0.1:{st.port}"
            if self.command == "POST":
                headers["Origin"] = origin  # the hub checked the browser's Origin already
                headers["Content-Length"] = str(len(body))
            # Connecting over loopback is instant; the answer may take as long as a call may run.
            conn = http.client.HTTPConnection("127.0.0.1", st.port, timeout=5)
            try:
                conn.connect()
                conn.sock.settimeout(10 if rest == "ping" else cfg.proxy_timeout_s)  # type: ignore[union-attr]
                conn.request(self.command if self.command != "HEAD" else "GET", target, body=body or None, headers=headers)
                res = conn.getresponse()
                data = res.read()
            except TimeoutError:
                conn.close()
                if hub.spawner.ping(st):  # it answers: just a long call
                    return self._json(504, {"error": f"Studio didn't answer within {cfg.proxy_timeout_s} s. "
                                                     "The call may still be running: check, then try again."})
                return self._lost(st, rest, hung=True)
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                # Refused: nothing listens on its port any more (no need to ask). Else maybe a one-off broken
                # connection while the Studio is fine.
                if not isinstance(exc, ConnectionRefusedError) and hub.spawner.ping(st):
                    return self._json(502, {"error": "Studio didn't answer. Try again."})
                return self._lost(st, rest, hung=False)
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
