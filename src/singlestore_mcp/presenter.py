"""Presenter mode: neutral stand-ins for identifying metadata on a projector.

The Studio's pages mask connection names, hosts / IPs, user names, e-mail addresses and SAS Viya addresses
when presenter mode is on (``S2.presenter`` in apps/shared.js). This module is the one place that hands out
the stand-ins, so every view of every page shows the same label for the same value ("Connection A",
"host-1", "user1", "viya-1") for as long as this server runs.

Known values are seeded from the saved SingleStore and SAS Viya connections, this computer (user and host
name) and the cluster's user list (one read of information_schema.USERS, marked like every query the server
sends for itself). Values a page finds on its own (an IP address, an e-mail address, a host name) are added
when the page asks. Nothing is stored on disk; the values never leave this machine.
"""

from __future__ import annotations

import getpass
import socket
import string
import threading
import time
import urllib.parse
from typing import Any

MARKER = "/* s2-presenter */"
KINDS = ("connection", "host", "viya", "user")
# Names that identify nobody and appear in ordinary text ("root", "admin"): never replaced inside other text.
GENERIC = {"root", "admin", "administrator", "sa", "sas", "sasdemo", "test", "user", "users", "public", "default",
           "nobody", "sys", "system", "distributed", "guest", "s2", "viya", "dba", "localhost", "127.0.0.1", "%"}
MAX_VALUES = 2000

_lock = threading.Lock()
_labels: dict[str, dict[str, str]] = {}   # lower-case value -> {value, kind, label}
_counts = {k: 0 for k in KINDS}
_seeded = False
_users_tried = -1e9


def _letters(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = string.ascii_uppercase[r] + out
    return out


def _make_label(kind: str) -> str:
    _counts[kind] += 1
    n = _counts[kind]
    return {"connection": f"Connection {_letters(n)}", "host": f"host-{n}", "viya": f"viya-{n}", "user": f"user{n}"}[kind]


def _add(value: Any, kind: str) -> None:
    if not isinstance(value, str):
        return
    v = value.strip()
    if len(v) < 2 or v.lower() in GENERIC or kind not in KINDS or len(_labels) >= MAX_VALUES:
        return
    key = v.lower()
    if key not in _labels:
        _labels[key] = {"value": v, "kind": kind, "label": _make_label(kind)}


def _url_parts(url: str | None) -> tuple[str | None, str | None]:
    """(host, user) of a URL or connection string."""
    if not url:
        return None, None
    raw = url if "://" in url else f"x://{url}"
    try:
        u = urllib.parse.urlsplit(raw)
        return u.hostname, urllib.parse.unquote(u.username) if u.username else None
    except ValueError:
        return None, None


def _seed() -> None:
    from . import connections, sas_viya

    try:
        profiles = connections.profiles()
        act = connections.active()
    except Exception:  # noqa: BLE001 - presenter mode works without them
        profiles, act = [], None
    ordered = ([act] if act else []) + [p for p in profiles if not act or p.name != act.name]
    for p in ordered:
        _add(p.name, "connection")
    try:
        viya = sas_viya.saved()
        viya_active = sas_viya.active_name()
    except Exception:  # noqa: BLE001
        viya, viya_active = [], None
    viya = sorted(viya, key=lambda c: c.get("name") != viya_active)
    for c in viya:
        _add(c.get("name"), "connection")
    for p in ordered:
        _add(p.host, "host")
        host, user = _url_parts(p.url)
        _add(host, "host")
        _add(p.user, "user")
        _add(user, "user")
    for c in viya:
        host, _ = _url_parts(c.get("url"))
        _add(host, "viya")
        try:
            _add(sas_viya.token_info(c["name"]).get("user"), "user")
        except Exception:  # noqa: BLE001 - no sign-in stored
            pass
    try:
        _add(getpass.getuser(), "user")
    except Exception:  # noqa: BLE001
        pass
    try:
        _add(socket.gethostname(), "host")
    except OSError:
        pass


def _seed_cluster_users() -> bool:
    """The cluster's user names (Security, Query History and the process list show them). False: try again later."""
    global _users_tried
    if time.monotonic() - _users_tried < 30:
        return False
    _users_tried = time.monotonic()
    try:
        from .db import db

        for r in db.execute(f"{MARKER} SELECT DISTINCT USER FROM information_schema.USERS ORDER BY USER")[1]:
            _add(r.get("USER"), "user")
        return True
    except Exception:  # noqa: BLE001 - no cluster or no access: the users found on the pages are added later
        return False


def labels(values: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """Every value known so far with its label, after adding ``values`` ([{value, kind}], kind one of KINDS)."""
    global _seeded
    with _lock:
        _seed()  # saved connections can change while presenting: cheap, local
        if not _seeded:
            _seeded = _seed_cluster_users()
        for item in (values or [])[:500]:
            if isinstance(item, dict):
                _add(item.get("value"), str(item.get("kind") or "host"))
        return [dict(x) for x in _labels.values()]


def reset() -> None:
    """Forget every label (tests; a new session starts over)."""
    global _seeded
    with _lock:
        _labels.clear()
        for k in _counts:
            _counts[k] = 0
        _seeded = False
        global _users_tried
        _users_tried = -1e9
