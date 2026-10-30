"""Sign-in with SAS Logon (OAuth 2.0 Authorization Code + PKCE S256).

Two ways, chosen by the admin (``HubConfig.oauth_mode``):

- **redirect** (preferred): the hub's own client, registered in SAS Logon with the redirect URI
  ``<public_url>/oauth/callback``. SAS Logon sends the browser back with the code; one click after the password.
- **paste** (fallback, no registration needed): SAS Viya's built-in public client ``vscode``. SAS Logon shows a
  code after the sign-in, the user pastes it into the hub's page (like the Connections window does today).

The password is only ever typed into SAS Logon. The hub then learns who signed in by asking Viya
(``/identities/users/@currentUser`` with the new token), so the identity is Viya's, not a decoded JWT.
Never logged: codes, verifiers, tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import ssl
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from .config import HubConfig


class SignInError(RuntimeError):
    """SAS Logon refused (bad or expired code, wrong client) or couldn't be reached. Safe to show."""


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(cfg: HubConfig, state: str, challenge: str) -> str:
    params = {"client_id": cfg.client_id, "response_type": "code", "code_challenge_method": "S256",
              "code_challenge": challenge, "state": state}
    if cfg.oauth_mode == "redirect":
        params["redirect_uri"] = cfg.redirect_uri
        if cfg.scope:
            params["scope"] = cfg.scope
    return f"{cfg.viya_url}/SASLogon/oauth/authorize?{urlencode(params)}"


def _http(cfg: HubConfig, method: str, url: str, *, data: dict[str, str] | None = None,
          headers: dict[str, str] | None = None, timeout: float = 20.0) -> tuple[int, Any]:
    ctx = ssl.create_default_context()
    if not cfg.viya_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    body = urlencode(data).encode() if data is not None else None
    # The configured proxy first (if any), then directly - as sas_viya does: an internal Viya may be reachable
    # only one of the two ways.
    routes: list[dict[str, str] | None] = [None, {}] if urllib.request.getproxies() else [{}]
    last: Exception | None = None
    for proxies in routes:
        handlers: list[Any] = [urllib.request.HTTPSHandler(context=ctx)]
        if proxies is not None:
            handlers.append(urllib.request.ProxyHandler(proxies))
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as resp:
                status, raw = resp.status, resp.read()
            break
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
            break
        except (urllib.error.URLError, OSError) as exc:
            last = exc
    else:
        raise SignInError(f"Couldn't reach SAS Logon at {cfg.viya_url}: {getattr(last, 'reason', last)}") from None
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def _token_request(cfg: HubConfig, data: dict[str, str]) -> dict[str, Any]:
    basic = base64.b64encode(f"{cfg.client_id}:{cfg.client_secret or ''}".encode()).decode()
    status, body = _http(cfg, "POST", f"{cfg.viya_url}/SASLogon/oauth/token", data={**data, "client_id": cfg.client_id},
                         headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                                  "Authorization": f"Basic {basic}"})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        detail = (body.get("error_description") or body.get("error")) if isinstance(body, dict) else ""
        raise SignInError(f"SAS Logon refused the sign-in (HTTP {status}{': ' + str(detail)[:200] if detail else ''}).")
    return body


def exchange_code(cfg: HubConfig, code: str, verifier: str) -> dict[str, Any]:
    data = {"grant_type": "authorization_code", "code": code.strip(), "code_verifier": verifier}
    if cfg.oauth_mode == "redirect":
        data["redirect_uri"] = cfg.redirect_uri
    tokens = _token_request(cfg, data)
    tokens["obtained"] = time.time()
    return tokens


def refresh(cfg: HubConfig, refresh_token: str) -> dict[str, Any]:
    tokens = _token_request(cfg, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    tokens.setdefault("refresh_token", refresh_token)
    tokens["obtained"] = time.time()
    return tokens


def current_user(cfg: HubConfig, access_token: str) -> dict[str, str]:
    """Who the token belongs to, as Viya says: {"id": "norjni", "name": "Jonas …"}."""
    status, body = _http(cfg, "GET", f"{cfg.viya_url}/identities/users/@currentUser",
                         headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    if status != 200 or not isinstance(body, dict) or not body.get("id"):
        raise SignInError(f"SAS Viya didn't accept the new sign-in (HTTP {status}).")
    return {"id": str(body["id"]), "name": str(body.get("name") or body["id"])}
