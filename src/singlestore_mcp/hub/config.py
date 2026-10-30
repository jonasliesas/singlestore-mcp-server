"""Hub settings, all from environment variables (a Kubernetes ConfigMap + Secret, or a local shell).

Required: STUDIO_VIYA_URL (the SAS Viya whose SAS Logon signs users in). Everything else has a default.
Secrets (STUDIO_MASTER_KEY, STUDIO_OAUTH_CLIENT_SECRET) come from a Secret, never from the repo or a file in it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

_TRUE = ("1", "true", "yes", "on")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    return default if not raw else raw.lower() in _TRUE


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _csv(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in _env(name).split(",") if x.strip())


@dataclass
class HubConfig:
    viya_url: str
    public_url: str = "http://127.0.0.1:8800"
    bind: str = "127.0.0.1"
    port: int = 8800
    data_dir: Path = field(default_factory=lambda: Path("/data"))
    viya_name: str = "SAS Viya"
    viya_verify: bool = True
    viya_compute_context: str = ""
    # OAuth: "redirect" (own client registered in SAS Logon with redirect URI <public_url>/oauth/callback) or
    # "paste" (SAS Logon shows a code that the user pastes; works with the built-in public "vscode" client).
    oauth_mode: str = "paste"
    client_id: str = "vscode"
    client_secret: str | None = None
    scope: str = ""                       # empty: SAS Logon grants the client's registered scopes (as for vscode)
    master_key: bytes = b""
    idle_minutes: int = 30
    session_hours: int = 10
    max_users: int = 20
    start_timeout_s: int = 90
    admins: tuple[str, ...] = ()
    allowed_users: tuple[str, ...] = ()  # empty: every SAS user who can sign in
    trust_forwarded: bool = False        # take the client address from X-Forwarded-For (behind the ingress)
    notebooks: bool = False
    assistant: bool = False
    metrics: bool = True
    default_s2: dict[str, str] = field(default_factory=dict)
    python: str = ""                      # Python that runs the Studio processes (default: this one)

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/oauth/callback"

    @property
    def cookie_name(self) -> str:
        # __Host-: the browser only accepts it Secure, Path=/ and without Domain (no subdomain can set or read it).
        return "__Host-studio" if self.secure else "studio_session"

    def is_admin(self, user: str) -> bool:
        return user.lower() in {a.lower() for a in self.admins}

    def may_sign_in(self, user: str) -> bool:
        return not self.allowed_users or user.lower() in {a.lower() for a in self.allowed_users}

    def user_key(self, user: str) -> str:
        """The Fernet key of one user's secrets file: derived from the master key, never stored."""
        raw = hmac.new(self.master_key, b"studio-user-secrets:" + user.lower().encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw).decode()

    def users_dir(self) -> Path:
        return self.data_dir / "users"

    def hub_dir(self) -> Path:
        path = self.data_dir / "hub"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _master_key(data_dir: Path) -> bytes:
    """STUDIO_MASTER_KEY (a Secret), else a key file the hub makes once in its own folder (0600) - fine for a
    local test, but then a copy of the volume holds the key too: set STUDIO_MASTER_KEY on a server."""
    raw = _env("STUDIO_MASTER_KEY")
    if raw:
        return raw.encode()
    path = data_dir / "hub" / "master.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip().encode()


def load() -> HubConfig:
    viya = _env("STUDIO_VIYA_URL").rstrip("/")
    if not viya:
        raise SystemExit("Set STUDIO_VIYA_URL to the SAS Viya address (https://…) whose SAS Logon signs users in.")
    if not re.match(r"^https?://", viya):
        viya = "https://" + viya
    data_dir = Path(_env("STUDIO_DATA_DIR") or "/data").expanduser()
    port = _int("STUDIO_HUB_PORT", 8800)
    client_id = _env("STUDIO_OAUTH_CLIENT_ID")
    mode = (_env("STUDIO_OAUTH_MODE") or ("redirect" if client_id and client_id != "vscode" else "paste")).lower()
    if mode not in ("redirect", "paste"):
        raise SystemExit("STUDIO_OAUTH_MODE is redirect (own SAS Logon client) or paste (the vscode client's code).")
    default_s2 = {k: _env(f"STUDIO_DEFAULT_S2_{k.upper()}") for k in ("name", "host", "port", "database")}
    default_s2 = {k: v for k, v in default_s2.items() if v}
    if default_s2.get("host"):
        default_s2.setdefault("name", "SingleStore")
    cfg = HubConfig(
        viya_url=viya,
        public_url=(_env("STUDIO_PUBLIC_URL") or f"http://127.0.0.1:{port}").rstrip("/"),
        bind=_env("STUDIO_HUB_BIND") or "127.0.0.1",
        port=port,
        data_dir=data_dir,
        viya_name=_env("STUDIO_VIYA_NAME") or (urlsplit(viya).hostname or "SAS Viya").split(".")[0],
        viya_verify=_flag("STUDIO_VIYA_VERIFY", True),
        viya_compute_context=_env("STUDIO_VIYA_COMPUTE_CONTEXT"),
        oauth_mode=mode,
        client_id=client_id or "vscode",
        client_secret=_env("STUDIO_OAUTH_CLIENT_SECRET") or None,
        scope=_env("STUDIO_OAUTH_SCOPE"),
        master_key=_master_key(data_dir),
        idle_minutes=max(1, _int("STUDIO_IDLE_MINUTES", 30)),
        session_hours=max(1, _int("STUDIO_SESSION_HOURS", 10)),
        max_users=max(1, _int("STUDIO_MAX_USERS", 20)),
        start_timeout_s=max(10, _int("STUDIO_START_TIMEOUT", 90)),
        admins=_csv("STUDIO_ADMINS"),
        allowed_users=_csv("STUDIO_ALLOWED_USERS"),
        trust_forwarded=_flag("STUDIO_TRUST_FORWARDED", False),
        notebooks=_flag("STUDIO_NOTEBOOKS", False),
        assistant=_flag("STUDIO_ASSISTANT", False),
        metrics=_flag("STUDIO_METRICS", True),
        default_s2=default_s2,
        python=_env("STUDIO_PYTHON"),
    )
    if cfg.oauth_mode == "redirect" and cfg.client_id == "vscode":
        raise SystemExit("STUDIO_OAUTH_MODE=redirect needs the hub's own SAS Logon client (STUDIO_OAUTH_CLIENT_ID).")
    if len(cfg.master_key) < 32:
        raise SystemExit("STUDIO_MASTER_KEY must be at least 32 characters (e.g. python -c \"import secrets; print(secrets.token_urlsafe(48))\").")
    return cfg
