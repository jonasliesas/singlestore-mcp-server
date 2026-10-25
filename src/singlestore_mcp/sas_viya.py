"""SAS Viya connections for SAS cells in notebooks and SAS in the Workspace: saved connections, OAuth sign-in
and tokens.

Like the SingleStore connections, there can be several saved SAS Viya connections (name, Viya address,
compute context, client ID, certificate check, SingleStore libref, CAS server, startup code) and one of them
is active: ``settings()`` / ``token()`` without a name answer for the active one, so notebook SAS cells,
``sas_rest`` and the Workspace use it.

SAS code runs in a SAS Compute session on Viya (through ``saspy`` in the notebook kernel). Signing in uses
OAuth 2.0 Authorization Code + PKCE with Viya's built-in public client ``vscode`` (as SAS's own tools do):
the browser opens SAS Logon, which shows a code to paste once. The access token is renewed with the
refresh token from then on, so there's no password anywhere. If the SAS Viya MCP server's login cache
(``~/.sas-mcp-server/credentials.json``) or the SAS Viya CLI's (``~/.sas/credentials.json``) exists, it's
used for a connection to the same Viya, so no new sign-in is needed.

The connection list lives in ``~/.singlestore-mcp/sas.json`` (no secrets); each connection's tokens go to the
OS credential store like passwords (key ``sas-viya:<name>#token``). An old single-settings ``sas.json`` becomes
one connection (with its sign-in) the first time it's read.

    python -m singlestore_mcp.sas_viya token [<connection>]   # a valid access token (notebook kernels call this)
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from . import connections, fileio
from .paths import data_dir

_LEGACY_SECRET = "sas-viya#token"  # the one token of the old single-settings sas.json
_lock = threading.RLock()
# Token renewal / import talks to SAS Logon (0.3 s – seconds): its own lock, so reading the settings (every SAS call,
# the views' status lines) never waits behind it.
_token_lock = threading.RLock()
_pending: dict[str, Any] = {}  # PKCE state between "Sign in" and "paste the code"
_route = {"direct": False}  # True when Viya is only reachable without the configured proxy
DEFAULTS = {"url": "", "compute_context": "SAS Studio compute context", "client_id": "vscode",
            "ssl_verify": True, "libref": "S2", "cas_server": "cas-shared-default", "startup_code": "",
            # Workspace SAS session: LIBNAME <libref> on the active SingleStore connection (before the startup code)
            "auto_libname": True,
            # Start the Workspace's SAS session in the background when the Workspace opens (sas_prewarm)
            "prewarm": True,
            # SAS 9 through SAS/CONNECT: sign-on code (e.g. %include of a signon file), the remote session name
            # used in its SIGNON (server= of the remote libnames) and which remote libraries to assign (empty: all)
            "sas9_signon": "", "sas9_server": "", "sas9_libraries": ""}
FIELDS = ("name", *DEFAULTS)
MAX_STARTUP_CODE = 20_000  # characters of SAS code run when the Workspace's SAS session starts
# Same rule as SingleStore connection names (no ":" so the secret keys can't clash with theirs).
_NAME_RE = re.compile(r"^[\w .@()+-]{1,60}$")
# Other SAS tools' token caches (same shape), reused when they belong to this Viya.
_FOREIGN = [(Path.home() / ".sas-mcp-server" / "credentials.json", "vscode"),
            (Path.home() / ".sas" / "credentials.json", "sas.cli")]


class NeedsSignIn(LookupError):
    """No valid SAS Viya token: sign in to SAS Viya in the Connections window."""


def _file() -> Path:
    return data_dir() / "sas.json"


def _secret(name: str) -> str:
    return f"sas-viya:{name}#token"


# ------------------------------------------------------------------ saved connections


def _norm_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    if url and not url.startswith("https://") and not url.startswith("http://"):
        url = "https://" + url
    return url


def _fields(values: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """The connection's settings from ``values`` (a form or a partial update); keys not given keep ``cur``."""
    def get(key: str) -> Any:
        return values[key] if key in values and values[key] is not None else cur[key]

    return {
        "url": _norm_url(get("url")),
        "compute_context": str(get("compute_context") or "").strip() or DEFAULTS["compute_context"],
        "client_id": str(get("client_id") or "").strip() or DEFAULTS["client_id"],
        "ssl_verify": bool(get("ssl_verify")),
        "libref": str(get("libref") or "").strip().upper()[:8] or "S2",
        "cas_server": str(get("cas_server") or "").strip() or DEFAULTS["cas_server"],
        # Workspace SAS session startup code (e.g. %include of libname files); never a password itself.
        "startup_code": str(get("startup_code") or "")[:MAX_STARTUP_CODE],
        "auto_libname": bool(get("auto_libname")),
        "prewarm": bool(get("prewarm")),
        "sas9_signon": str(get("sas9_signon") or "")[:MAX_STARTUP_CODE],
        "sas9_server": _sas9_server(get("sas9_server")),
        "sas9_libraries": _sas9_libraries(get("sas9_libraries")),
    }


def _sas9_server(value: Any) -> str:
    server = str(value or "").strip().upper()
    if server and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,7}", server):
        raise ValueError("The SAS 9 server is the remote session name used in the SIGNON (1–8 letters, digits, _), e.g. CAUKI.")
    return server


def _sas9_libraries(value: Any) -> str:
    from .sas_sql import parse_sas9_libraries

    text = " ".join(str(value or "").split())[:2000]
    parse_sas9_libraries(text)  # ValueError for a bad name
    return text


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    return {"name": str(entry["name"]), **{k: entry.get(k, v) for k, v in DEFAULTS.items()}}


def _name_for(url: str) -> str:
    """A connection name for a migrated sas.json: the Viya host, or "SAS Viya"."""
    host = urlsplit(url).hostname or ""
    return host if _NAME_RE.match(host) else "SAS Viya"


def _migrate(old: dict[str, Any]) -> dict[str, Any]:
    """The old single-settings sas.json → one connection that keeps its sign-in (none if it had no address)."""
    url = _norm_url(old.get("url"))
    data: dict[str, Any] = {"version": 2, "active": None, "connections": []}
    if url:
        name = _name_for(url)
        data["connections"] = [{"name": name, **_fields({k: v for k, v in old.items() if k in DEFAULTS}, DEFAULTS), "url": url}]
        data["active"] = name
        raw = connections.get_password(_LEGACY_SECRET)
        if raw:
            connections.set_password(_secret(name), raw)
    fileio.write_json(_file(), data, indent=2)
    if url:
        connections.delete_password(_LEGACY_SECRET)
    return data


def _read() -> dict[str, Any]:
    # A file that exists but can't be read raises (fileio) instead of reading as empty: a save after that
    # would otherwise drop the saved connections.
    with _lock:
        raw = fileio.read_json(_file(), None)
        if isinstance(raw, dict) and isinstance(raw.get("connections"), list):
            conns = [_clean(c) for c in raw["connections"] if isinstance(c, dict) and c.get("name")]
            return {"version": 2, "active": raw.get("active"), "connections": conns}
        if isinstance(raw, dict) and "url" in raw:
            return _migrate(raw)
        return {"version": 2, "active": None, "connections": []}


def _write(data: dict[str, Any]) -> None:
    fileio.write_json(_file(), data, indent=2)


def _active_of(data: dict[str, Any]) -> dict[str, Any] | None:
    conns = data["connections"]
    return next((c for c in conns if c["name"] == data.get("active")), None) or (conns[0] if conns else None)


def saved() -> list[dict[str, Any]]:
    """All saved connections (settings only, no secrets)."""
    return [dict(c) for c in _read()["connections"]]


def active_name() -> str | None:
    act = _active_of(_read())
    return act["name"] if act else None


def settings(name: str | None = None) -> dict[str, Any]:
    """The settings of connection ``name`` (default: the active one). Without any connection: the defaults with
    an empty ``url`` and ``name`` None (callers treat that as "not set up")."""
    data = _read()
    if name is None:
        c = _active_of(data)
        return dict(c) if c else {"name": None, **DEFAULTS}
    c = next((c for c in data["connections"] if c["name"] == name), None)
    if c is None:
        raise LookupError(f"No SAS Viya connection named {name!r}.")
    return dict(c)


def save_connection(values: dict[str, Any], original_name: str | None = None) -> dict[str, Any]:
    """Create (``original_name`` None) or update a saved connection. The sign-in stays unless the address
    changes to another Viya; renaming moves it."""
    name = str(values.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("Give the SAS Viya connection a name (letters, digits, spaces and . _ - @ ( ) +).")
    with _lock:
        data = _read()
        conns = data["connections"]
        old = None
        if original_name:
            old = next((c for c in conns if c["name"] == original_name), None)
            if old is None:
                raise LookupError(f"No SAS Viya connection named {original_name!r}.")
        if any(c["name"] == name for c in conns) and name != original_name:
            raise ValueError(f"A SAS Viya connection named {name!r} already exists.")
        new = {"name": name, **_fields(values, old or DEFAULTS)}
        if not new["url"]:
            raise ValueError("Enter the SAS Viya address (https://…).")
        act = _active_of(data)  # pinned, so a new connection never changes which one is active
        data["active"] = name if act is None or (old and act["name"] == original_name) else act["name"]
        data["connections"] = sorted([c for c in conns if c["name"] != original_name and c["name"] != name] + [new],
                                     key=lambda c: c["name"].lower())
        _write(data)
        if old and original_name != name and _pending.get("name") == original_name:
            _pending["name"] = name
        if old:
            if old["url"] and new["url"] != old["url"]:
                connections.delete_password(_secret(original_name))  # another Viya: sign in again
            elif original_name != name:
                raw = connections.get_password(_secret(original_name))
                if raw:
                    connections.set_password(_secret(name), raw)
                connections.delete_password(_secret(original_name))
    return new


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    """Update the active connection with the keys given (the old single-settings API). An empty or missing
    address never blanks a saved one. Without any connection, a non-empty address creates one."""
    with _lock:
        act = _active_of(_read())
        if act is None:
            url = _norm_url(values.get("url"))
            if not url:
                raise ValueError("Enter the SAS Viya address first.")
            return save_connection({**values, "name": values.get("name") or _name_for(url), "url": url})
        given = {k: v for k, v in values.items() if k in DEFAULTS}
        if not _norm_url(given.get("url")):
            given.pop("url", None)
        return save_connection({**act, **given, "name": act["name"]}, original_name=act["name"])


def delete_connection(name: str) -> None:
    with _lock:
        data = _read()
        if not any(c["name"] == name for c in data["connections"]):
            raise LookupError(f"No SAS Viya connection named {name!r}.")
        data["connections"] = [c for c in data["connections"] if c["name"] != name]
        if data.get("active") == name:
            data["active"] = data["connections"][0]["name"] if data["connections"] else None
        _write(data)
    connections.delete_password(_secret(name))
    with _lock:
        if _pending.get("name") == name:
            _pending.clear()


def activate(name: str) -> dict[str, Any]:
    with _lock:
        data = _read()
        c = next((c for c in data["connections"] if c["name"] == name), None)
        if c is None:
            raise LookupError(f"No SAS Viya connection named {name!r}.")
        data["active"] = name
        _write(data)
        _route.update(direct=False)  # another Viya may be reached another way
        _route.pop("probed", None)
    return dict(c)


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


def _stored(name: str) -> dict[str, Any] | None:
    raw = connections.get_password(_secret(name))
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return None


def _store(name: str, tokens: dict[str, Any], client_id: str, source: str, url: str) -> dict[str, Any]:
    access = tokens["access_token"]
    exp = _claims(access).get("exp") or (time.time() + int(tokens.get("expires_in") or 3600))
    data = {"access": access, "refresh": tokens.get("refresh_token") or "", "exp": float(exp),
            "client_id": client_id, "source": source, "url": url}
    connections.set_password(_secret(name), json.dumps(data))
    return data


def foreign_import_enabled() -> bool:
    """SINGLESTORE_MCP_SAS_IMPORT=0 turns off reusing other SAS tools' sign-ins (e.g. for tests that must use their own)."""
    import os

    return os.environ.get("SINGLESTORE_MCP_SAS_IMPORT", "1").strip().lower() not in ("0", "false", "no", "off")


def _import_foreign(name: str, url: str, verify: bool) -> dict[str, Any] | None:
    """Reuse a sign-in cached by the SAS Viya MCP server or the SAS Viya CLI, if this Viya accepts it.
    (Viya tokens name http://localhost as issuer, so the only way to tell is to ask this Viya.)"""
    if not foreign_import_enabled():
        return None
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
                return _store(name, {"access_token": access, "refresh_token": refresh}, client, source, url)
        if refresh:
            try:
                fresh = _post_token(url, {"grant_type": "refresh_token", "refresh_token": refresh}, client, verify)
            except RuntimeError:
                continue
            fresh.setdefault("refresh_token", refresh)
            return _store(name, fresh, client, source, url)
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


def _not_set_up() -> NeedsSignIn:
    return NeedsSignIn("SAS Viya isn't set up: add a SAS Viya connection in the Connections window.")


def token(min_valid: int = 300, name: str | None = None) -> str:
    """A SAS Viya access token for connection ``name`` (default: the active one), valid for at least
    ``min_valid`` seconds (renewed with the refresh token)."""
    s = settings(name)
    if not s["name"] or not s["url"]:
        raise _not_set_up()
    name = s["name"]
    with _token_lock:
        t = _stored(name)
        if t and t.get("url") != s["url"]:
            t = None
        if not t:
            t = _import_foreign(name, s["url"], s["ssl_verify"])
        if not t:
            raise NeedsSignIn(f"Not signed in to SAS Viya ({name}): use Sign in under SAS Viya in the Connections window.")
        if t["exp"] - time.time() > min_valid:
            return t["access"]
        if t.get("refresh"):
            try:
                fresh = _post_token(s["url"], {"grant_type": "refresh_token", "refresh_token": t["refresh"]},
                                    t.get("client_id") or s["client_id"], s["ssl_verify"])
                fresh.setdefault("refresh_token", t["refresh"])
                return _store(name, fresh, t.get("client_id") or s["client_id"], t.get("source") or "sign-in", s["url"])["access"]
            except Exception as exc:  # noqa: BLE001 - expired / revoked refresh token
                raise NeedsSignIn(f"The SAS Viya sign-in ({name}) has expired ({exc}). Sign in again in the Connections window.") from exc
        raise NeedsSignIn(f"The SAS Viya sign-in ({name}) has expired. Sign in again in the Connections window.")


def sign_in_start(name: str | None = None) -> dict[str, Any]:
    """Open SAS Logon in the browser for a saved connection (Authorization Code + PKCE); SAS Logon then shows a
    code to paste. Saves nothing."""
    s = settings(name)
    if not s["name"] or not s["url"]:
        raise ValueError("Add a SAS Viya connection (with its address) first.")
    verifier = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~") for _ in range(96))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": s["client_id"], "response_type": "code", "code_challenge_method": "S256",
              "code_challenge": challenge, "state": secrets.token_urlsafe(16)}
    auth_url = f"{s['url']}/SASLogon/oauth/authorize?{urlencode(params)}"
    with _lock:
        _pending.clear()
        _pending.update(verifier=verifier, name=s["name"], url=s["url"], client_id=s["client_id"],
                        verify=s["ssl_verify"], started=time.time())
    try:
        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001 - the app shows the link too
        pass
    return {"authorize_url": auth_url, "name": s["name"]}


def sign_in_finish(code: str, name: str | None = None) -> dict[str, Any]:
    code = (code or "").strip()
    with _lock:
        pend = dict(_pending)
    if not pend or time.time() - pend.get("started", 0) > 900 or (name and pend.get("name") != name):
        raise ValueError("Start with Sign in first (the sign-in expires after 15 minutes).")
    if not code:
        raise ValueError("Paste the code SAS Logon shows after you sign in.")
    if settings(pend["name"])["url"] != pend["url"]:
        raise ValueError("The connection's address changed meanwhile: sign in again.")
    tokens = _post_token(pend["url"], {"grant_type": "authorization_code", "code": code,
                                       "code_verifier": pend["verifier"]}, pend["client_id"], pend["verify"])
    _store(pend["name"], tokens, pend["client_id"], "sign-in", pend["url"])
    with _lock:
        _pending.clear()
    return status(pend["name"])


def sign_out(name: str | None = None) -> None:
    s = settings(name)
    if s["name"]:
        connections.delete_password(_secret(s["name"]))


def token_info(name: str) -> dict[str, Any]:
    """Sign-in state of a saved connection from its stored token only (no network: for lists)."""
    s = settings(name)
    t = _stored(name)
    if not t or t.get("url") != s["url"]:
        return {"signed_in": False, "foreign_available": foreign_import_enabled() and any(p.is_file() for p, _ in _FOREIGN)}
    valid = t.get("exp", 0) - time.time() > 60
    c = _claims(t.get("access") or "")
    return {"signed_in": valid or bool(t.get("refresh")), "user": c.get("user_name") or c.get("sub"),
            "token_expires": t.get("exp"), "token_valid": valid, "renewable": bool(t.get("refresh")), "source": t.get("source")}


def status(name: str | None = None) -> dict[str, Any]:
    """Settings and sign-in status of connection ``name`` (default: the active one); may renew the token or
    reuse another SAS tool's sign-in over the network."""
    s = settings(name)
    out: dict[str, Any] = {**s, "signed_in": False, "active": bool(s["name"]) and s["name"] == active_name()}
    if not s["name"] or not s["url"]:
        return out
    try:
        tok = token(min_valid=60, name=s["name"])
    except NeedsSignIn as exc:
        out["problem"] = str(exc)
        return out
    except Exception as exc:  # noqa: BLE001
        out["problem"] = str(exc)[:300]
        return out
    c = _claims(tok)
    t = _stored(s["name"]) or {}
    out.update(signed_in=True, user=c.get("user_name") or c.get("sub"), token_expires=t.get("exp"),
               renewable=bool(t.get("refresh")), source=t.get("source"))
    return out


def _get(path: str, name: str | None = None) -> Any:
    s = settings(name)
    status, body = _http("GET", f"{s['url']}{path}", verify=s["ssl_verify"],
                         headers={"Authorization": f"Bearer {token(60, s['name'])}", "Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"SAS Viya answered HTTP {status} for {path}")
    return body


def compute_contexts(name: str | None = None) -> list[str]:
    items = _get("/compute/contexts?limit=200", name).get("items", [])
    return sorted(i.get("name") for i in items if i.get("name"))


def test(name: str | None = None) -> dict[str, Any]:
    """Sign-in valid, the compute context exists — without starting a SAS session."""
    started = time.time()
    try:
        ctx = settings(name)["compute_context"]
        names = compute_contexts(name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:400]}
    # Where the sign-in came from: "sign-in" (this server's own), or another SAS tool's that was reused
    # ("SAS Viya MCP server sign-in" / "SAS Viya CLI sign-in").
    signed_in_with = (_stored(settings(name)["name"]) or {}).get("source")
    return {"ok": ctx in names, "contexts": names, "ms": round((time.time() - started) * 1000), "signed_in_with": signed_in_with,
            **({} if ctx in names else {"error": f"No compute context named {ctx!r}. Pick one of the list."})}


def kernel_env() -> dict[str, str]:
    """The active connection for a new notebook kernel (the token itself comes from SINGLESTORE_MCP_SAS_TOKEN_CMD,
    fresh each time, for this same connection even if another one is made active later)."""
    s = settings()
    if not s["name"] or not s["url"]:
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
        "SINGLESTORE_MCP_SAS_CONNECTION": s["name"],
        "SINGLESTORE_MCP_SAS_URL": s["url"], "SINGLESTORE_MCP_SAS_CONTEXT": s["compute_context"],
        "SINGLESTORE_MCP_SAS_VERIFY": "1" if s["ssl_verify"] else "0", "SINGLESTORE_MCP_SAS_LIBREF": s["libref"],
        "SINGLESTORE_MCP_SAS_TOKEN_CMD": json.dumps([sys.executable, "-m", "singlestore_mcp.sas_viya", "token", s["name"]]),
    }


def _main(argv: list[str]) -> int:
    if not argv or argv[0] != "token" or len(argv) > 2:
        print("usage: python -m singlestore_mcp.sas_viya token [<connection>]", file=sys.stderr)
        return 2
    try:
        print(token(name=argv[1] if len(argv) > 1 else None))
    except Exception as exc:  # noqa: BLE001
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
