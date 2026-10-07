"""SAS Viya connection for SAS cells in notebooks: settings, OAuth sign-in and tokens.

SAS code runs in a SAS Compute session on Viya (through ``saspy`` in the notebook kernel). Signing in uses
OAuth 2.0 Authorization Code + PKCE with Viya's built-in public client ``vscode`` (as SAS's own tools do):
the browser opens SAS Logon, which shows a code to paste once. The access token is renewed with the
refresh token from then on, so there's no password anywhere. If the SAS Viya MCP server's login cache
(``~/.sas-mcp-server/credentials.json``) or the SAS Viya CLI's (``~/.sas/credentials.json``) exists, it's
used, so no new sign-in is needed.

Settings live in ``~/.singlestore-mcp/sas.json``; tokens go to the OS credential store like passwords.

    python -m singlestore_mcp.sas_viya token   # a valid access token (notebook kernels call this)
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import connections
from .paths import data_dir

_SECRET = "sas-viya#token"
_lock = threading.RLock()
_pending: dict[str, Any] = {}  # PKCE state between "Sign in" and "paste the code"
_route = {"direct": False}  # True when Viya is only reachable without the configured proxy
DEFAULTS = {"url": "", "compute_context": "SAS Studio compute context", "client_id": "vscode",
            "ssl_verify": True, "libref": "S2"}
# Other SAS tools' token caches (same shape), reused when they belong to this Viya.
_FOREIGN = [(Path.home() / ".sas-mcp-server" / "credentials.json", "vscode"),
            (Path.home() / ".sas" / "credentials.json", "sas.cli")]


class NeedsSignIn(LookupError):
    """No valid SAS Viya token: sign in to SAS Viya in the Connections window."""


def _file() -> Path:
    return data_dir() / "sas.json"


def settings() -> dict[str, Any]:
    try:
        saved = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    cur = settings()
    url = str(values.get("url", cur["url"]) or "").strip().rstrip("/")
    if url and not url.startswith("https://") and not url.startswith("http://"):
        url = "https://" + url
    new = {
        "url": url,
        "compute_context": str(values.get("compute_context") or cur["compute_context"]).strip(),
        "client_id": str(values.get("client_id") or cur["client_id"]).strip(),
        "ssl_verify": bool(values.get("ssl_verify", cur["ssl_verify"])),
        "libref": (str(values.get("libref") or cur["libref"]).strip().upper()[:8]) or "S2",
    }
    if new["url"] != cur["url"]:
        connections.delete_password(_SECRET)  # another Viya: sign in again
    _file().write_text(json.dumps(new, indent=2), encoding="utf-8")
    return new


# ------------------------------------------------------------------ tokens


def _claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _http(method: str, url: str, *, data: dict[str, str] | None = None, headers: dict[str, str] | None = None,
          verify: bool = True, timeout: float = 20.0) -> tuple[int, Any]:
    """Small HTTP helper on the standard library (status, parsed JSON or text)."""
    import ssl
    import urllib.error
    import urllib.request

    body = urlencode(data).encode() if data is not None else None
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # The configured proxy first (a corporate proxy may be required), then directly: a proxy that doesn't
    # allow an internal Viya host (e.g. one a host app sets for its own processes) must not block it.
    routes = [None, {}] if not _route["direct"] else [{}, None]
    last: Exception | None = None
    for proxies in routes:
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if proxies is not None:
            handlers.append(urllib.request.ProxyHandler(proxies))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with opener.open(req, timeout=timeout) as resp:
                status, raw = resp.status, resp.read()
            _route["direct"] = proxies == {} and bool(urllib.request.getproxies().get("https"))
            break
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
            break
        except (urllib.error.URLError, OSError) as exc:
            last = exc
    else:
        base = url.split("/SASLogon")[0].split("/compute")[0].split("/identities")[0]
        raise RuntimeError(f"Couldn't reach SAS Viya at {base}: {getattr(last, 'reason', last)}") from last
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def _stored() -> dict[str, Any] | None:
    raw = connections.get_password(_SECRET)
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return None


def _store(tokens: dict[str, Any], client_id: str, source: str, url: str) -> dict[str, Any]:
    access = tokens["access_token"]
    exp = _claims(access).get("exp") or (time.time() + int(tokens.get("expires_in") or 3600))
    data = {"access": access, "refresh": tokens.get("refresh_token") or "", "exp": float(exp),
            "client_id": client_id, "source": source, "url": url}
    connections.set_password(_SECRET, json.dumps(data))
    return data


def _import_foreign(url: str, verify: bool) -> dict[str, Any] | None:
    """Reuse a sign-in cached by the SAS Viya MCP server or the SAS Viya CLI, if this Viya accepts it.
    (Viya tokens name http://localhost as issuer, so the only way to tell is to ask this Viya.)"""
    for path, client in _FOREIGN:
        try:
            creds = json.loads(path.read_text())["Default"]
        except (OSError, ValueError, KeyError):
            continue
        access, refresh = creds.get("access-token") or "", creds.get("refresh-token") or ""
        source = "SAS Viya MCP server sign-in" if ".sas-mcp-server" in str(path) else "SAS Viya CLI sign-in"
        if access and (_claims(access).get("exp") or 0) > time.time() + 60:
            status, _ = _http("GET", f"{url}/identities/users/@currentUser",
                              headers={"Authorization": f"Bearer {access}", "Accept": "application/json"}, verify=verify)
            if status == 200:
                return _store({"access_token": access, "refresh_token": refresh}, client, source, url)
        if refresh:
            try:
                fresh = _post_token(url, {"grant_type": "refresh_token", "refresh_token": refresh}, client, verify)
            except RuntimeError:
                continue
            fresh.setdefault("refresh_token", refresh)
            return _store(fresh, client, source, url)
    return None


def _post_token(url: str, data: dict[str, str], client_id: str, verify: bool) -> dict[str, Any]:
    basic = base64.b64encode(f"{client_id}:".encode()).decode()
    status, body = _http("POST", f"{url}/SASLogon/oauth/token", data={**data, "client_id": client_id}, verify=verify,
                         headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}",
                                  "Accept": "application/json"})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        detail = (body.get("error_description") or body.get("error")) if isinstance(body, dict) else str(body)[:200]
        raise RuntimeError(f"SAS Logon refused the token request (HTTP {status}): {detail}")
    return body


def token(min_valid: int = 300) -> str:
    """A SAS Viya access token valid for at least ``min_valid`` seconds (renewed with the refresh token)."""
    s = settings()
    if not s["url"]:
        raise NeedsSignIn("SAS Viya isn't set up: enter its address in the Connections window (SAS Viya).")
    with _lock:
        t = _stored()
        if t and t.get("url") != s["url"]:
            t = None
        if not t:
            t = _import_foreign(s["url"], s["ssl_verify"])
        if not t:
            raise NeedsSignIn("Not signed in to SAS Viya: use Sign in under SAS Viya in the Connections window.")
        if t["exp"] - time.time() > min_valid:
            return t["access"]
        if t.get("refresh"):
            try:
                fresh = _post_token(s["url"], {"grant_type": "refresh_token", "refresh_token": t["refresh"]},
                                    t.get("client_id") or s["client_id"], s["ssl_verify"])
                fresh.setdefault("refresh_token", t["refresh"])
                return _store(fresh, t.get("client_id") or s["client_id"], t.get("source") or "sign-in", s["url"])["access"]
            except Exception as exc:  # noqa: BLE001 - expired / revoked refresh token
                raise NeedsSignIn(f"The SAS Viya sign-in has expired ({exc}). Sign in again in the Connections window.") from exc
        raise NeedsSignIn("The SAS Viya sign-in has expired. Sign in again in the Connections window.")


def sign_in_start() -> dict[str, Any]:
    """Open SAS Logon in the browser (Authorization Code + PKCE); SAS Logon then shows a code to paste."""
    s = settings()
    if not s["url"]:
        raise ValueError("Enter the SAS Viya address first.")
    verifier = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~") for _ in range(96))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": s["client_id"], "response_type": "code", "code_challenge_method": "S256",
              "code_challenge": challenge, "state": secrets.token_urlsafe(16)}
    auth_url = f"{s['url']}/SASLogon/oauth/authorize?{urlencode(params)}"
    with _lock:
        _pending.clear()
        _pending.update(verifier=verifier, url=s["url"], client_id=s["client_id"], started=time.time())
    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001 - the app shows the link too
        pass
    return {"authorize_url": auth_url}


def sign_in_finish(code: str) -> dict[str, Any]:
    code = (code or "").strip()
    with _lock:
        pend = dict(_pending)
    if not pend or time.time() - pend.get("started", 0) > 900:
        raise ValueError("Start with Sign in first (the sign-in expires after 15 minutes).")
    if not code:
        raise ValueError("Paste the code SAS Logon shows after you sign in.")
    s = settings()
    tokens = _post_token(pend["url"], {"grant_type": "authorization_code", "code": code,
                                       "code_verifier": pend["verifier"]}, pend["client_id"], s["ssl_verify"])
    _store(tokens, pend["client_id"], "sign-in", pend["url"])
    with _lock:
        _pending.clear()
    return status()


def sign_out() -> None:
    connections.delete_password(_SECRET)


def status() -> dict[str, Any]:
    s = settings()
    out: dict[str, Any] = {**s, "signed_in": False}
    if not s["url"]:
        return out
    try:
        tok = token(min_valid=60)
    except NeedsSignIn as exc:
        out["problem"] = str(exc)
        return out
    except Exception as exc:  # noqa: BLE001
        out["problem"] = str(exc)[:300]
        return out
    c = _claims(tok)
    t = _stored() or {}
    out.update(signed_in=True, user=c.get("user_name") or c.get("sub"), token_expires=t.get("exp"),
               renewable=bool(t.get("refresh")), source=t.get("source"))
    return out


def _get(path: str) -> Any:
    s = settings()
    status, body = _http("GET", f"{s['url']}{path}", verify=s["ssl_verify"],
                         headers={"Authorization": f"Bearer {token(60)}", "Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"SAS Viya answered HTTP {status} for {path}")
    return body


def compute_contexts() -> list[str]:
    items = _get("/compute/contexts?limit=200").get("items", [])
    return sorted(i.get("name") for i in items if i.get("name"))


def test() -> dict[str, Any]:
    """Sign-in valid, the compute context exists — without starting a SAS session."""
    started = time.time()
    try:
        names = compute_contexts()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:400]}
    ctx = settings()["compute_context"]
    return {"ok": ctx in names, "contexts": names, "ms": round((time.time() - started) * 1000),
            **({} if ctx in names else {"error": f"No compute context named {ctx!r}. Pick one of the list."})}


def kernel_env() -> dict[str, str]:
    """Settings for notebook kernels (the token itself comes from SINGLESTORE_MCP_SAS_TOKEN_CMD, fresh each time)."""
    s = settings()
    if not s["url"]:
        return {}
    extra: dict[str, str] = {}
    import urllib.request

    if urllib.request.getproxies().get("https") and not _route.get("probed"):
        _route["probed"] = True
        try:  # learn whether Viya is reachable through the proxy (cheap, unauthenticated)
            _http("GET", f"{s['url']}/SASLogon/", verify=s["ssl_verify"], timeout=8)
        except RuntimeError:
            pass
    if _route["direct"]:
        # Viya answered only without the proxy: keep the kernel's HTTP clients (swat, sasctl) off it for this host.
        import os

        host = s["url"].split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        extra = {"NO_PROXY": ",".join(p for p in (current, host) if p), "no_proxy": ",".join(p for p in (current, host) if p)}
    return {
        **extra,
        "SINGLESTORE_MCP_SAS_URL": s["url"], "SINGLESTORE_MCP_SAS_CONTEXT": s["compute_context"],
        "SINGLESTORE_MCP_SAS_VERIFY": "1" if s["ssl_verify"] else "0", "SINGLESTORE_MCP_SAS_LIBREF": s["libref"],
        "SINGLESTORE_MCP_SAS_TOKEN_CMD": json.dumps([sys.executable, "-m", "singlestore_mcp.sas_viya", "token"]),
    }


def _main(argv: list[str]) -> int:
    if argv != ["token"]:
        print("usage: python -m singlestore_mcp.sas_viya token", file=sys.stderr)
        return 2
    try:
        print(token())
    except Exception as exc:  # noqa: BLE001
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
