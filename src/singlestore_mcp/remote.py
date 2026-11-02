"""Server mode with the Studio repository: this Studio's stored history comes from the hub's collector.

The collector (``collector``, one process next to the hub) samples the metrics, syncs the query history and keeps
the plan cache for every user, with the service account, into the repository database in SingleStore. A Studio
started by the hub gets the collector's address, a token the hub signed (SAS user, monitor group, admin) and the
cluster the collector watches (``configure``). Then the stores' read functions (``history_store``,
``metrics_store``) ask the collector instead of the local SQLite files, and the Studio's own sampler and syncs
stay off.

``call`` returns ``LOCAL`` when there is nothing to ask: no collector (the desktop, or a server without the
repository), or this Studio's active connection is to another cluster than the collector's - then the stores
work on the local files as before. Stdlib only, cheap to import.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
import urllib.error
import urllib.request
from typing import Any

LOCAL = object()   # "no collector for this: use the local store"
MARKER = "/* s2-remote */"
TIMEOUT_S = 30

_lock = threading.Lock()
_cfg: dict[str, Any] = {"url": None, "token": None, "source": None}
_who: dict[tuple[str, str, str], str | None] = {}   # (connection, user, cluster) -> the SingleStore user it signs in as
_here = threading.local()   # the collector answering a call: its stores read their own files


class CollectorError(RuntimeError):
    """The collector didn't answer or refused the call (the message is safe to show)."""


def configure(url: str | None, token: str | None, source: str | None) -> None:
    """From the hub (the Studio's stdin config or a reseed): ``url`` of the collector, this Studio's ``token``, and
    the cluster it watches (``source``, host:port). None switches the collector off."""
    with _lock:
        _cfg.update(url=(url or "").rstrip("/") or None, token=token or None, source=source or None)
        _who.clear()


def configured() -> bool:
    return bool(_cfg["url"] and _cfg["token"]) and not getattr(_here, "serving", False)


@contextmanager
def serving() -> Any:
    """The collector answers a Studio's call in this thread: the stores read the local files, never ask a collector."""
    _here.serving = True
    try:
        yield
    finally:
        _here.serving = False


def active() -> bool:
    """A collector answers for the active connection's cluster."""
    if not configured():
        return False
    from . import history_store

    try:
        return history_store.source_id() == _cfg["source"]
    except Exception:  # noqa: BLE001 - no connection: nothing to ask about
        return False


def _signed_in_as() -> str | None:
    """The SingleStore user the active connection signs in as (CURRENT_USER(), once per connection): the collector
    shows users outside the monitor group only that user's queries. None when the cluster can't be reached."""
    from . import connections, history_store
    from .db import db

    p = connections.active()
    key = (p.name if p else "", p.user if p else "", history_store.source_id())
    with _lock:
        if key in _who:
            return _who[key]
    try:
        rows = db.execute(f"{MARKER} SELECT CURRENT_USER() AS u")[1]
        who = str(rows[0]["u"]).rsplit("@", 1)[0] if rows else None
    except Exception:  # noqa: BLE001 - not cached: asked again next time
        return None
    with _lock:
        _who[key] = who
    return who


def call(name: str, **kwargs: Any) -> Any:
    """Ask the collector (``LOCAL`` when there is no collector for the active cluster)."""
    if not active():
        return LOCAL
    body = json.dumps({"fn": name, "args": kwargs, "source": _cfg["source"], "s2_user": _signed_in_as()}).encode()
    req = urllib.request.Request(f"{_cfg['url']}/call", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {_cfg['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - the hub's own collector on 127.0.0.1
            data = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read() or b"{}").get("error")
        except ValueError:
            msg = None
        raise CollectorError(msg or f"The Studio repository answered HTTP {exc.code}.") from None
    except (OSError, ValueError):
        raise CollectorError("The Studio repository (the hub's collector) isn't answering. Try again in a minute; "
                             "the Studio admin can check it on the admin page.") from None
    if data.get("local"):
        return LOCAL
    return data.get("result")


def status() -> dict[str, Any]:
    """For the apps: is the history from the shared repository (no secrets)."""
    return {"collector": configured(), "active": active(), "source": _cfg["source"]}
