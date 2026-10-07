"""Saved SingleStore connections ("profiles") and the active one.

Profiles (name, host, port, user, default database, TLS) live in
``~/.singlestore-mcp/connections.json``. Passwords are never written there:
they go to the operating system's credential store through ``keyring``
(Windows Credential Manager, macOS Keychain, Secret Service on Linux). Where
no such store exists (e.g. a headless Linux server), they go to an encrypted
file next to the profiles, with a key file only this user can read. Secrets
too large for the OS store (Entra ID tokens on Windows) go to an encrypted
file whose key is kept in the OS store.

The SINGLESTORE_* environment variables, if set, appear as the built-in,
read-only profile "Environment variables", so existing setups keep working.
One profile is active at a time; db.py, notebook kernels and the in-app
assistant all connect with it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import data_dir

ENV_PROFILE = "Environment variables"
AUTH_TYPES = ("password", "jwt", "browser_sso", "entra")
# Secrets kept per connection: the password / pasted token, then tokens and caches of the sign-in kinds.
_SECRET_SUFFIXES = ("", "#sso", "#entra", "#entra-cache", "#entra-mode")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
# Entra ID without an app registration of your own: Microsoft's Azure CLI public client (consented in every
# tenant, also used by Microsoft's Azure SDKs) and the "Azure OSS database" resource that Azure Database for
# MySQL / PostgreSQL use for Entra logins. Its tokens are v1 JWTs (user name in "upn") signed with the tenant's keys.
ENTRA_DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
ENTRA_DEFAULT_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
_TENANT_RE = re.compile(r"^([0-9a-fA-F-]{36}|[\w.-]+\.[a-zA-Z]{2,}|organizations|common)$")


class NeedsSignIn(LookupError):
    """A token connection has no valid (unexpired) token: sign in / paste a new one."""
_SERVICE = "singlestore-mcp"
_NAME_RE = re.compile(r"^[\w .@()+-]{1,60}$")
_lock = threading.RLock()
_listeners: list[Any] = []


@dataclass
class Profile:
    name: str
    host: str = ""
    port: int = 3306
    user: str = "root"
    database: str | None = None
    ssl_disabled: bool = False
    url: str | None = None          # full connection string instead of host/user/…
    ssl_ca: str | None = None       # CA certificate (PEM) to verify the server, e.g. Helios' singlestore_bundle.pem
    ssl_cert: str | None = None     # client certificate (PEM), if the cluster requires one
    ssl_key: str | None = None      # client private key (PEM)
    ssl_verify: bool = True         # verify the server's certificate against ssl_ca
    auth: str = "password"          # password | jwt (pasted token) | browser_sso (Helios sign-in) | entra
    entra_tenant: str | None = None     # Entra ID: tenant ID or domain; default: the work account's own tenant
    entra_client_id: str | None = None  # Entra ID: own app registration; default: Microsoft's Azure CLI app
    entra_scope: str | None = None      # Entra ID: scope; default api://<own client>/.default or the OSS database one
    builtin: bool = False           # the Environment variables profile

    def public(self) -> dict[str, Any]:
        """For the UI and the model: everything except the password."""
        return {
            "name": self.name, "host": self.host, "port": self.port, "user": self.user,
            "database": self.database, "ssl_disabled": self.ssl_disabled, "builtin": self.builtin,
            "ssl_ca": self.ssl_ca, "ssl_cert": self.ssl_cert, "ssl_key": self.ssl_key, "ssl_verify": self.ssl_verify,
            "auth": self.auth, **token_info(self),
            "entra_tenant": self.entra_tenant, "entra_client_id": self.entra_client_id, "entra_scope": self.entra_scope,
            "url": _mask_url(self.url) if self.url else None,
            "has_password": bool(self.url) or (self.builtin and bool(os.environ.get("SINGLESTORE_PASSWORD")))
                            or (not self.builtin and get_password(self.name) is not None),
        }


def _mask_url(url: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]*@|^([^:/@]+:)[^@]*@", lambda m: (m.group(1) or m.group(2)) + "•••@", url)


# ------------------------------------------------------------------ password store


class _FileSecrets:
    """Encrypted secrets file (Fernet). Without an OS credential store it's the store itself (key file readable
    by this user only); with one, it holds the secrets too large for it (key kept in the OS store)."""

    def __init__(self, kr=None) -> None:
        self.kr = kr
        self.key_file = data_dir() / "secret.key"
        self.store = data_dir() / ("secrets-large.enc" if kr else "secrets.enc")

    def _fernet(self):
        from cryptography.fernet import Fernet

        if self.kr:
            key = self.kr.get_password(_SERVICE, "__secrets_file_key__")
            if not key:
                key = Fernet.generate_key().decode()
                self.kr.set_password(_SERVICE, "__secrets_file_key__", key)
            return Fernet(key.encode())
        if not self.key_file.exists():
            self.key_file.write_bytes(Fernet.generate_key())
            try:
                os.chmod(self.key_file, 0o600)
            except OSError:
                pass
        return Fernet(self.key_file.read_bytes())

    def _load(self) -> dict[str, str]:
        if not self.store.exists():
            return {}
        return json.loads(self._fernet().decrypt(self.store.read_bytes()))

    def _save(self, data: dict[str, str]) -> None:
        self.store.write_bytes(self._fernet().encrypt(json.dumps(data).encode()))
        try:
            os.chmod(self.store, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def set(self, name: str, password: str) -> None:
        data = self._load()
        data[name] = password
        self._save(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is not None:
            self._save(data)


def _keyring():
    """The OS credential store, or None if there's no usable one."""
    try:
        import keyring

        # keyring's placeholders when the OS has no store: keyring.backends.fail / .null
        if type(keyring.get_keyring()).__module__.rsplit(".", 1)[-1] in ("fail", "null"):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - no keyring: use the file store
        return None


def password_store() -> str:
    kr = _keyring()
    if kr:
        backend = type(kr.get_keyring()).__name__
        friendly = {"WinVaultKeyring": "Windows Credential Manager", "Keyring": "macOS Keychain"}.get(backend)
        if "SecretService" in type(kr.get_keyring()).__module__:
            friendly = "Secret Service (Linux keyring)"
        return friendly or f"OS keyring ({backend})"
    return f"encrypted file in {data_dir()}"


# Windows Credential Manager holds at most 2560 bytes (UTF-16: ~1280 characters) per entry; Entra tokens and
# MSAL's token cache are bigger, so those go to the encrypted file instead.
_KEYRING_MAX = 1200 if sys.platform == "win32" else 100_000


def get_password(name: str) -> str | None:
    kr = _keyring()
    try:
        value = kr.get_password(_SERVICE, name) if kr else None
        if value is None and (not kr or _FileSecrets(kr).store.exists()):
            value = _FileSecrets(kr).get(name)
        return value
    except Exception:  # noqa: BLE001
        return None


def set_password(name: str, password: str) -> None:
    kr = _keyring()
    if kr and len(password) <= _KEYRING_MAX:
        try:
            kr.set_password(_SERVICE, name, password)
            if _FileSecrets(kr).store.exists():
                _FileSecrets(kr).delete(name)
            return
        except Exception:  # noqa: BLE001 - too big for this store: use the file
            pass
    _FileSecrets(kr).set(name, password)
    if kr:
        try:
            kr.delete_password(_SERVICE, name)
        except Exception:  # noqa: BLE001 - nothing stored
            pass


def delete_password(name: str) -> None:
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(_SERVICE, name)
        except Exception:  # noqa: BLE001 - nothing stored
            pass
    try:
        if not kr or _FileSecrets(kr).store.exists():
            _FileSecrets(kr).delete(name)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ profiles


def _file() -> Path:
    return data_dir() / "connections.json"


def _env_profile() -> Profile | None:
    if os.environ.get("SINGLESTORE_URL"):
        return Profile(name=ENV_PROFILE, url=os.environ["SINGLESTORE_URL"], builtin=True)
    if not os.environ.get("SINGLESTORE_HOST"):
        return None
    return Profile(
        name=ENV_PROFILE,
        host=os.environ["SINGLESTORE_HOST"],
        port=int(os.environ.get("SINGLESTORE_PORT", "3306")),
        user=os.environ.get("SINGLESTORE_USER", "root"),
        database=os.environ.get("SINGLESTORE_DATABASE") or None,
        ssl_disabled=os.environ.get("SINGLESTORE_SSL_DISABLED", "").lower() in ("1", "true", "yes"),
        ssl_ca=os.environ.get("SINGLESTORE_SSL_CA") or None,
        ssl_cert=os.environ.get("SINGLESTORE_SSL_CERT") or None,
        ssl_key=os.environ.get("SINGLESTORE_SSL_KEY") or None,
        ssl_verify=os.environ.get("SINGLESTORE_SSL_VERIFY", "1").lower() not in ("0", "false", "no"),
        auth="jwt" if os.environ.get("SINGLESTORE_CREDENTIAL_TYPE", "").lower() == "jwt" else "password",
        builtin=True,
    )


def _read() -> dict[str, Any]:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": None, "profiles": []}


def _write(data: dict[str, Any]) -> None:
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, _file())


def profiles() -> list[Profile]:
    with _lock:
        out = [p for p in [_env_profile()] if p]
        for d in _read().get("profiles", []):
            out.append(Profile(**{k: v for k, v in d.items() if k in Profile.__dataclass_fields__ and k != "builtin"}))
        return out


def get(name: str) -> Profile:
    for p in profiles():
        if p.name == name:
            return p
    raise LookupError(f"No connection named {name!r}.")


def active() -> Profile | None:
    with _lock:
        name = _read().get("active")
        all_ = profiles()
        return next((p for p in all_ if p.name == name), None) or (all_[0] if all_ else None)


def save(profile: dict[str, Any], password: str | None = None, original_name: str | None = None) -> Profile:
    """Create or update a profile. ``password`` None keeps the stored one; "" removes it."""
    name = str(profile.get("name", "")).strip()
    if not _NAME_RE.match(name):
        raise ValueError("Give the connection a name (letters, digits, spaces and . _ - @ ( ) +).")
    if name == ENV_PROFILE:
        raise ValueError(f"{ENV_PROFILE!r} comes from the SINGLESTORE_* settings and can't be edited here.")
    url = (profile.get("url") or "").strip() or None
    if url == "__keep__":  # editing: keep the saved URL (never sent to the app)
        url = next((d.get("url") for d in _read().get("profiles", []) if d["name"] == original_name), None)
    host = str(profile.get("host", "")).strip()
    if not url and not host:
        raise ValueError("Enter a host (or a connection URL).")
    p = Profile(
        name=name, host=host, port=int(profile.get("port") or 3306), user=str(profile.get("user") or "root").strip(),
        database=(str(profile.get("database") or "").strip() or None), ssl_disabled=bool(profile.get("ssl_disabled")),
        url=url, auth=_auth_type(profile), **_tls_fields(profile), **_entra_fields(profile),
    )
    with _lock:
        data = _read()
        others = [d for d in data.get("profiles", []) if d["name"] not in (name, original_name)]
        if any(d["name"] == name for d in data.get("profiles", [])) and original_name != name:
            raise ValueError(f"A connection named {name!r} already exists.")
        old = next((d for d in data.get("profiles", []) if d["name"] == (original_name or name)), None)
        entry = {k: v for k, v in p.__dict__.items() if k != "builtin"}
        data["profiles"] = sorted(others + [entry], key=lambda d: d["name"].lower())
        if original_name and original_name != name:
            for suffix in _SECRET_SUFFIXES:  # the password and any cached sign-in move with the name
                old_secret = get_password(original_name + suffix)
                delete_password(original_name + suffix)
                if old_secret is not None and (suffix or password is None):
                    set_password(name + suffix, old_secret)
            if data.get("active") == original_name:
                data["active"] = name
        if old and any(old.get(k) != entry[k] for k in ("auth", "user", "entra_tenant", "entra_client_id", "entra_scope")):
            for suffix in ("#sso", "#entra", "#entra-cache", "#entra-mode"):  # another identity: sign in again
                delete_password(name + suffix)
        _write(data)
    if password:
        set_password(name, password)
    elif password == "":
        delete_password(name)
    return p


def _auth_type(profile: dict[str, Any]) -> str:
    auth = str(profile.get("auth") or "password")
    if auth not in AUTH_TYPES:
        raise ValueError(f"Unknown authentication {auth!r}.")
    if auth != "password" and profile.get("ssl_disabled"):
        raise ValueError("Token logins (JWT / SSO) need TLS: turn 'TLS off' off.")
    return auth


def _entra_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """Tenant, client ID and scope of an Entra ID connection (checked); None for other kinds."""
    if profile.get("auth") != "entra":
        return {"entra_tenant": None, "entra_client_id": None, "entra_scope": None}
    tenant = str(profile.get("entra_tenant") or "").strip() or None
    client = str(profile.get("entra_client_id") or "").strip() or None
    scope = " ".join(str(profile.get("entra_scope") or "").split()) or None
    if tenant and not _TENANT_RE.match(tenant):
        raise ValueError("The Entra tenant is a tenant ID (a GUID) or a domain such as contoso.onmicrosoft.com; "
                         "leave it empty to use your work account's tenant.")
    if client and not _GUID_RE.match(client):
        raise ValueError("The client ID is an app registration's application ID (a GUID); leave it empty to use none.")
    return {"entra_tenant": tenant, "entra_client_id": client, "entra_scope": scope}


def _tls_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """ssl_ca / ssl_cert / ssl_key paths (must exist) and ssl_verify from a form."""
    out: dict[str, Any] = {"ssl_verify": profile.get("ssl_verify") is not False}
    for key, label in (("ssl_ca", "CA certificate"), ("ssl_cert", "Client certificate"), ("ssl_key", "Client key")):
        value = str(profile.get(key) or "").strip().strip('"') or None
        if value and not Path(value).expanduser().is_file():
            raise ValueError(f"{label} file not found: {value}")
        out[key] = str(Path(value).expanduser()) if value else None
    if bool(out["ssl_cert"]) != bool(out["ssl_key"]):
        raise ValueError("A client certificate needs its key file too (and the other way round).")
    return out


def delete(name: str) -> None:
    if name == ENV_PROFILE:
        raise ValueError(f"{ENV_PROFILE!r} comes from the SINGLESTORE_* settings; remove those to drop it.")
    with _lock:
        data = _read()
        before = len(data.get("profiles", []))
        data["profiles"] = [d for d in data.get("profiles", []) if d["name"] != name]
        if len(data["profiles"]) == before:
            raise LookupError(f"No connection named {name!r}.")
        if data.get("active") == name:
            data["active"] = None
        _write(data)
    for suffix in _SECRET_SUFFIXES:
        delete_password(name + suffix)
    _notify()


def activate(name: str) -> Profile:
    p = get(name)
    with _lock:
        data = _read()
        data["active"] = name
        _write(data)
    _notify()
    return p


def on_change(callback) -> None:
    """Call ``callback()`` after the active connection changes (db.py resets its pool)."""
    _listeners.append(callback)


def _notify() -> None:
    for cb in list(_listeners):
        try:
            cb()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ tokens (JWT / browser SSO)
# A pasted JWT is stored like a password. A browser-SSO token is fetched once
# with SingleStore's sign-in page and cached (OS store, key "<name>#sso"), so
# the pool, notebook kernels and the assistant all reuse one sign-in.


def _token_expiry(token: str | None) -> float | None:
    if not token:
        return None
    try:
        import jwt

        exp = jwt.decode(token, options={"verify_signature": False}).get("exp")
        return float(exp) if exp else None
    except Exception:  # noqa: BLE001 - not a JWT we can read: no expiry shown
        return None


def _stored_token(p: Profile) -> str | None:
    if p.builtin:
        return os.environ.get("SINGLESTORE_PASSWORD") if p.auth == "jwt" else None
    return get_password(p.name + {"browser_sso": "#sso", "entra": "#entra"}.get(p.auth, ""))


def token_info(p: Profile) -> dict[str, Any]:
    if p.auth == "password":
        return {}
    import time

    exp = _token_expiry(_stored_token(p))
    return {"token_expires": exp, "token_valid": bool(_stored_token(p)) and (exp is None or exp > time.time() + 30)}


def token_for(p: Profile) -> str:
    """The token to log in with; NeedsSignIn when there's none or it has expired."""
    import time

    token = _stored_token(p)
    exp = _token_expiry(token)
    if p.auth == "entra" and (not token or (exp is not None and exp <= time.time() + 300)):
        renewed = _entra_silent(p)  # renew a few minutes early, without a browser; None = sign in needed
        if renewed:
            token, exp = renewed, _token_expiry(renewed)
    if not token or (exp is not None and exp <= time.time() + 30):
        what = "Paste a new JWT token" if p.auth == "jwt" else "Sign in again"
        raise NeedsSignIn(f"The token for connection {p.name!r} {'has expired' if token else 'is missing'}. "
                          f"{what} in the Connections window.")
    return token


def sso_sign_in(name: str, timeout: int = 120) -> dict[str, Any]:
    """Browser SSO (Helios): opens SingleStore's sign-in page, waits for the token, caches it."""
    from singlestoredb import auth as s2auth

    p = get(name)
    if p.auth == "entra":
        return entra_sign_in(p, timeout)
    if p.auth != "browser_sso":
        raise ValueError(f"{name!r} doesn't use browser SSO.")
    if not p.user or "@" not in p.user:
        raise ValueError("For browser SSO, enter your SingleStore login email as the user.")
    info = s2auth.get_jwt(p.user, databases=p.database, timeout=timeout)
    set_password(name + "#sso", str(info.token))
    _notify()
    return token_info(p)


# Microsoft Entra ID: on Windows (and macOS) MSAL first asks the operating system's sign-in broker (WAM), so
# on an Entra-joined laptop the account you're signed in to Windows with gets a token without any prompt.
# Otherwise it signs in once in the browser (SSO, MFA, conditional access) and keeps a refresh token in its
# cache (stored like a password, "<name>#entra-cache"). Access tokens ("<name>#entra") are then renewed
# silently; "<name>#entra-mode" remembers which of the two ("broker" / "browser") signed in. The cluster checks them against Entra's keys (jwks_endpoint) and takes the user name from
# jwks_username_field, normally preferred_username.


def _entra_scopes(p: Profile) -> list[str]:
    if p.entra_scope:
        return p.entra_scope.split()
    return [f"api://{p.entra_client_id}/.default"] if p.entra_client_id else [ENTRA_DEFAULT_SCOPE]


def _broker_available() -> bool:
    if sys.platform not in ("win32", "darwin"):
        return False
    try:
        import pymsalruntime  # noqa: F401 - msal[broker]
        return True
    except ImportError:
        return False


def _entra_app(p: Profile, broker: bool = False):
    import msal

    cache = msal.SerializableTokenCache()
    blob = get_password(p.name + "#entra-cache")
    if blob:
        try:
            cache.deserialize(blob)
        except ValueError:
            pass
    app = msal.PublicClientApplication(p.entra_client_id or ENTRA_DEFAULT_CLIENT_ID,
                                       authority=f"https://login.microsoftonline.com/{p.entra_tenant or 'organizations'}",
                                       token_cache=cache, timeout=15,
                                       enable_broker_on_windows=broker, enable_broker_on_mac=broker)
    return app, cache


def _entra_keep(p: Profile, cache, result: dict[str, Any] | None) -> str | None:
    if cache.has_state_changed:
        set_password(p.name + "#entra-cache", cache.serialize())
    token = (result or {}).get("access_token")
    if token:
        set_password(p.name + "#entra", token)
    return token


def _entra_silent(p: Profile) -> str | None:
    """A fresh access token from the cached sign-in, or None if the user has to sign in again."""
    if not get_password(p.name + "#entra-cache"):
        return None
    try:
        with _lock:
            app, cache = _entra_app(p, broker=get_password(p.name + "#entra-mode") == "broker" and _broker_available())
            accounts = app.get_accounts(username=p.user) or app.get_accounts()
            result = app.acquire_token_silent(_entra_scopes(p), account=accounts[0]) if accounts else None
            return _entra_keep(p, cache, result)
    except Exception:  # noqa: BLE001 - offline / refresh token revoked: sign in again
        return None


def entra_sign_in(p: Profile, timeout: int = 120) -> dict[str, Any]:
    """Sign in to Entra ID: first with the account signed in to Windows / macOS (the OS broker, usually no
    prompt; needs redirect URI ms-appx-web://Microsoft.AAD.BrokerPlugin/<client-id> on Windows), else in the
    browser (needs redirect URI http://localhost)."""
    broker_error = None
    result: dict[str, Any] = {}
    if _broker_available():
        try:
            app, cache = _entra_app(p, broker=True)
            result = app.acquire_token_interactive(_entra_scopes(p), login_hint=p.user or None, timeout=timeout,
                                                   parent_window_handle=app.CONSOLE_WINDOW_HANDLE)
        except Exception as exc:  # noqa: BLE001 - no broker after all: use the browser
            result = {"error": str(exc)}
        if "access_token" in result:
            mode = "broker"
        else:
            broker_error = result.get("error_description") or result.get("error")
    if "access_token" not in result:
        app, cache = _entra_app(p)
        result = app.acquire_token_interactive(_entra_scopes(p), login_hint=p.user or None, timeout=timeout,
                                               prompt="select_account")
        mode = "browser"
    if "access_token" not in result:
        raise RuntimeError(f"Entra sign-in failed: {result.get('error_description') or result.get('error') or result}"
                           + (f" (Windows sign-in: {broker_error})" if broker_error else ""))
    set_password(p.name + "#entra-mode", mode)
    _entra_keep(p, cache, result)
    _notify()
    import jwt

    claims = jwt.decode(result["access_token"], options={"verify_signature": False})
    # The claim the cluster reads the user name from (jwks_username_field): preferred_username in v2 tokens,
    # upn in v1 tokens (the default OSS database resource).
    claim = "preferred_username" if claims.get("ver") == "2.0" and claims.get("preferred_username") else "upn"
    name = claims.get(claim) or claims.get("unique_name")
    info = {**token_info(p), "token_user": name, "username_claim": claim, "tenant": claims.get("tid"),
            "token_version": claims.get("ver"), "audience": claims.get("aud"),
            "signed_in_with": "your Windows / macOS sign-in" if mode == "broker" else "the browser"}
    if broker_error:
        info["broker_error"] = str(broker_error)[:300]
    if not name:
        info["warning"] = "The token has no user name (upn / preferred_username): the cluster can't map it to a user."
    elif p.user and name.lower() != p.user.lower():
        info["warning"] = f"The token is for {name}, but the connection's user is {p.user}."
    elif name != p.user:
        # SingleStore matches the token's user name exactly, case included: log in with its spelling.
        _set_user(p.name, name)
        info["user_corrected"] = name
    return info


def _set_user(name: str, user: str) -> None:
    """Change a saved connection's user name without the sign-in reset that save() does for a new identity."""
    with _lock:
        data = _read()
        for d in data.get("profiles", []):
            if d["name"] == name:
                d["user"] = user
        _write(data)
    _notify()


# ------------------------------------------------------------------ using a profile


def password_for(p: Profile) -> str:
    if p.auth != "password":
        return token_for(p)
    if p.builtin:
        return os.environ.get("SINGLESTORE_PASSWORD", "")
    return get_password(p.name) or ""


def connect_kwargs(p: Profile, password: str | None = None) -> dict[str, Any]:
    if p.url:
        return {"host": p.url, "results_type": "dict"}
    kw: dict[str, Any] = {
        "host": p.host, "port": p.port, "user": p.user,
        "password": password if password is not None else password_for(p),
        "results_type": "dict", "ssl_disabled": p.ssl_disabled,
    }
    if p.database:
        kw["database"] = p.database
    # Token logins (JWT / SSO): the token is the password. The server asks for
    # mysql_clear_password, which the driver answers over TLS; no extra option
    # is needed (singlestoredb 1.17's driver rejects credential_type).
    if not p.ssl_disabled:
        if p.ssl_ca:
            kw["ssl_ca"] = p.ssl_ca
            kw["ssl_verify_cert"] = p.ssl_verify
        if p.ssl_cert and p.ssl_key:
            kw["ssl_cert"], kw["ssl_key"] = p.ssl_cert, p.ssl_key
    return kw


def env_for(p: Profile | None) -> dict[str, str]:
    """SINGLESTORE_* variables describing a profile, for child processes (kernels, assistant)."""
    if not p:
        return {}
    if p.url:
        return {"SINGLESTORE_URL": p.url}
    env = {
        "SINGLESTORE_HOST": p.host, "SINGLESTORE_PORT": str(p.port), "SINGLESTORE_USER": p.user,
        "SINGLESTORE_PASSWORD": _password_or_empty(p), "SINGLESTORE_SSL_DISABLED": "1" if p.ssl_disabled else "",
        "SINGLESTORE_DATABASE": p.database or "",
        "SINGLESTORE_SSL_CA": p.ssl_ca or "", "SINGLESTORE_SSL_CERT": p.ssl_cert or "",
        "SINGLESTORE_SSL_KEY": p.ssl_key or "", "SINGLESTORE_SSL_VERIFY": "1" if p.ssl_verify else "0",
        "SINGLESTORE_CREDENTIAL_TYPE": "jwt" if p.auth != "password" else "",
    }
    return env


def _password_or_empty(p: Profile) -> str:
    try:
        return password_for(p)
    except NeedsSignIn:
        return ""


def apply_env(env: dict[str, str], p: Profile | None = None) -> dict[str, str]:
    """``env`` with the active (or given) profile's SINGLESTORE_* settings in place of the old ones."""
    p = p or active()
    if not p:
        return env
    out = {k: v for k, v in env.items() if not k.startswith("SINGLESTORE_") or k.startswith("SINGLESTORE_MCP")}
    out.update(env_for(p))
    return out


def test(profile: dict[str, Any] | None = None, password: str | None = None, name: str | None = None) -> dict[str, Any]:
    """Try a connection: a saved one by ``name``, or unsaved settings from the form."""
    import time

    import singlestoredb as s2

    if name and not profile:
        p = get(name)
        try:
            pw = password_for(p)
        except NeedsSignIn as exc:
            return {"ok": False, "error": str(exc), "needs_sign_in": True}
    else:
        base = get(profile["original_name"]) if profile and profile.get("original_name") else None
        if profile and profile.get("url") == "__keep__":
            profile = {**profile, "url": base.url if base else None}
        try:
            tls = _tls_fields(profile or {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        p = Profile(
            name=str((profile or {}).get("name") or "test"), host=str((profile or {}).get("host") or "").strip(),
            port=int((profile or {}).get("port") or 3306), user=str((profile or {}).get("user") or "root").strip(),
            database=(str((profile or {}).get("database") or "").strip() or None),
            ssl_disabled=bool((profile or {}).get("ssl_disabled")), url=((profile or {}).get("url") or "").strip() or None,
            auth=str((profile or {}).get("auth") or "password"), **tls,
        )
        if p.auth in ("browser_sso", "entra") and not password:
            if not base or base.auth != p.auth:
                return {"ok": False, "error": "Save the connection, then use Sign in to get a token, then Test."}
            p = base
        try:
            pw = password if password else (password_for(base) if base else "")
        except NeedsSignIn as exc:
            return {"ok": False, "error": str(exc), "needs_sign_in": True}
    started = time.perf_counter()
    try:
        conn = s2.connect(**connect_kwargs(p, pw), autocommit=True, connect_timeout=8)
        try:
            cur = conn.cursor()
            cur.execute("SELECT @@memsql_version AS v, CURRENT_USER() AS u")
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - shown in the app
        error = str(exc)[:400]
        if "No SSL detected" in error or "SSL" in error and not p.ssl_ca:
            error += " → Set a CA certificate (for Helios: 'Use SingleStore Helios CA') and test again."
        return {"ok": False, "error": error}
    return {"ok": True, "version": row["v"], "user": row["u"], "ms": round((time.perf_counter() - started) * 1000)}


def _main(argv: list[str]) -> int:
    """``python -m singlestore_mcp.connections token <name>``: print a valid token for a token connection
    (renewed silently when it can be). Notebook kernels call it when their token is about to expire."""
    if len(argv) != 2 or argv[0] != "token":
        print("usage: python -m singlestore_mcp.connections token <connection name>", file=sys.stderr)
        return 2
    try:
        print(token_for(get(argv[1])))
    except (LookupError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
