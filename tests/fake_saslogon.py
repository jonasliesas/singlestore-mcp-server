"""A fake SAS Logon (OAuth Authorization Code + PKCE, refresh tokens, @currentUser) on top of FakeViya, for the hub tests.

Nobody types a password here: a test "signs in" by opening the authorize URL with ``login_as=<user>`` added, which
stands for the user signing in on SAS Logon's own page. Then, as SAS Logon does:
- a client with a registered redirect URI gets a 302 to ``redirect_uri?code=…&state=…``;
- the public ``vscode`` client (no redirect URI) gets a page that shows the code to paste.

Tokens are opaque test strings (``fake-access-…``), never real JWTs. Clients: ``vscode`` (public, no secret) and
``studio-hub`` (secret ``test-client-secret``, redirect URI set with ``register_redirect``).
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode

from fake_viya import FakeViya

TEST_CLIENT_SECRET = "test-client-secret"  # noqa: S105 - a fake client's secret, only valid against this fake


class FakeSasLogon(FakeViya):
    def __init__(self) -> None:
        self.clients: dict[str, dict[str, object]] = {"vscode": {"secret": "", "redirect": None},
                                                       "studio-hub": {"secret": TEST_CLIENT_SECRET, "redirect": None}}
        self.users = {"alice": "Alice Andersen", "bob": "Bob Berg"}
        self.codes: dict[str, dict[str, object]] = {}
        self.access: dict[str, dict[str, object]] = {}
        self.refresh: dict[str, str] = {}
        self.access_ttl = 3600
        self.token_requests: list[dict[str, str]] = []  # grant types and clients (no secrets kept)
        self._n = itertools.count(1)
        self._logon_lock = threading.Lock()
        super().__init__()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def register_redirect(self, client_id: str, redirect_uri: str) -> None:
        self.clients[client_id]["redirect"] = redirect_uri

    def _new_access(self, user: str, client: str) -> dict[str, object]:
        n = next(self._n)
        access, refresh = f"fake-access-{n}-{secrets.token_hex(8)}", f"fake-refresh-{n}-{secrets.token_hex(8)}"
        with self._logon_lock:
            self.access[access] = {"user": user, "exp": time.time() + self.access_ttl, "client": client}
            self.refresh[refresh] = user
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": self.access_ttl}

    def _client_ok(self, headers: object, form: dict[str, str]) -> str | None:
        client = form.get("client_id", "")
        auth = headers.get("Authorization", "") if headers is not None else ""  # type: ignore[union-attr]
        secret = ""
        if auth.startswith("Basic "):
            cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            client = client or cid
        reg = self.clients.get(client)
        if reg is None or reg["secret"] != secret:
            return None
        return client

    def route_full(self, method: str, path: str, query: str, body: bytes, headers: object) -> tuple:
        js = "application/json"
        q = {k: v[0] for k, v in parse_qs(query).items()}
        if method == "GET" and path == "/SASLogon/oauth/authorize":
            client = self.clients.get(q.get("client_id", ""))
            user = q.get("login_as")
            if client is None:
                return 400, {"error": "invalid_client"}, js
            if not user or user not in self.users:
                return 200, "<html><body>SAS Logon sign-in page (the test adds login_as=…)</body></html>", "text/html"
            if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
                return 400, {"error": "invalid_request", "error_description": "PKCE required"}, js
            redirect = q.get("redirect_uri")
            if redirect and redirect != client["redirect"]:
                return 400, {"error": "invalid_request", "error_description": "redirect_uri not registered"}, js
            code = secrets.token_urlsafe(12)
            with self._logon_lock:
                self.codes[code] = {"user": user, "client": q["client_id"], "challenge": q["code_challenge"],
                                    "redirect": redirect, "at": time.time()}
            if redirect:
                return 302, None, "text/plain", {"Location": f"{redirect}?{urlencode({'code': code, 'state': q.get('state', '')})}"}
            return 200, f'<html><body><p>Your code:</p><code id="code">{code}</code></body></html>', "text/html"
        if method == "POST" and path == "/SASLogon/oauth/token":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            client = self._client_ok(headers, form)
            self.token_requests.append({"grant_type": form.get("grant_type", ""), "client": client or "?"})
            if client is None:
                return 401, {"error": "unauthorized", "error_description": "Bad credentials"}, js
            if form.get("grant_type") == "authorization_code":
                with self._logon_lock:
                    entry = self.codes.pop(form.get("code", ""), None)  # one use only
                if entry is None or entry["client"] != client:
                    return 400, {"error": "invalid_grant", "error_description": "Invalid authorization code"}, js
                verifier = form.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if challenge != entry["challenge"]:
                    return 400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}, js
                if entry["redirect"] and form.get("redirect_uri") != entry["redirect"]:
                    return 400, {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, js
                return 200, self._new_access(str(entry["user"]), client), js
            if form.get("grant_type") == "refresh_token":
                user = self.refresh.get(form.get("refresh_token", ""))
                if user is None:
                    return 400, {"error": "invalid_grant", "error_description": "Invalid refresh token"}, js
                return 200, self._new_access(user, client), js
            return 400, {"error": "unsupported_grant_type"}, js
        if path.startswith("/identities/") or path.startswith("/compute/"):
            auth = headers.get("Authorization", "") if headers is not None else ""  # type: ignore[union-attr]
            tok = self.access.get(auth.removeprefix("Bearer ").strip())
            if tok is None or float(tok["exp"]) < time.time():  # type: ignore[arg-type]
                return 401, {"error": "invalid_token"}, js
            if method == "GET" and path == "/identities/users/@currentUser":
                user = str(tok["user"])
                return 200, {"id": user, "name": self.users[user], "type": "user"}, js
        return self.route(method, path)

    def sign_in(self, authorize_url: str, user: str) -> str:
        """The authorize URL with the user "signed in": the redirect Location, or the code SAS Logon shows."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return None

        opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
        sep = "&" if "?" in authorize_url else "?"
        try:
            with opener.open(f"{authorize_url}{sep}login_as={user}", timeout=10) as res:
                page = res.read().decode()
                return page.split('<code id="code">', 1)[1].split("</code>", 1)[0]
        except urllib.error.HTTPError as exc:  # the 302
            if exc.code == 302:
                return exc.headers["Location"]
            raise
