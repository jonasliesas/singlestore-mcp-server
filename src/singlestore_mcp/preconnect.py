"""Database connections a standby process opens before it becomes the server.

``restart_server`` hands the server to a *standby* (see ``supervisor``): a process that already loaded the MCP SDK
and the driver. Its first tool call still had to wait for a database connection (~1–1.5 s over the WAN: TCP, TLS,
login and the driver's session settings, ~8 round trips). So while the standby waits, it also opens the
connections a start would open to the active saved connection, checks them with a ping (COM_PING: no statement,
nothing in the plan cache) every minute, and on "go" leaves them in ``sys.modules[HOLDER_MODULE]``; the fresh
server's pool (``db.Database``) takes them if they were opened with the active connection's current settings, else
closes them.

- Only for password logins (token logins renew their token in the server; they connect as before).
- ``SINGLESTORE_MCP_STANDBY_CONNECT`` connections (default 4, as many as a start warms up; 0 turns this off),
  kept at most ``MAX_AGE_S`` (an idle standby then closes them and the server connects at start, as before).
- A connection whose last ping is older than ``FRESH_S`` (or a ping that hangs) is never handed over.

This module is imported by the standby *before* "go" and purged from ``sys.modules`` afterwards, so the server
loads every module of this package fresh from disk; the holder object only needs ``take`` to keep working.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

HOLDER_MODULE = "_singlestore_mcp_preconnected"
ENV = "SINGLESTORE_MCP_STANDBY_CONNECT"
PING_S = 60.0
FRESH_S = 90.0
MAX_AGE_S = 3600.0


def count() -> int:
    try:
        return max(0, min(4, int(os.environ.get(ENV, "4"))))
    except ValueError:
        return 4


def supported(profile: Any) -> bool:
    return getattr(profile, "auth", "password") == "password"


def _close(conns: list[Any]) -> None:
    for c in conns:
        try:
            c.close()
        except Exception:  # noqa: BLE001 - it's broken anyway
            pass


def _ping(conn: Any) -> None:
    # Bounded (netguard.PING_TIMEOUT_S): after a VPN switch or sleep a ping on a dead connection would otherwise hang.
    from . import netguard

    netguard.ping(getattr(conn, "_conn", conn))


class Preconnected:
    """Opens ``n`` connections in a background thread and keeps them alive until ``handover`` (or ``MAX_AGE_S``)."""

    def __init__(self, n: int) -> None:
        self._n = n
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._kwargs: dict[str, Any] | None = None
        self._conns: list[Any] = []
        self._last_ok = 0.0
        self._abandoned = False
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="s2-preconnect", daemon=True)
        self._thread.start()

    # -- in the standby
    def _run(self) -> None:
        try:
            import singlestoredb as s2

            from . import connections

            profile = connections.active()
            if profile is None or not supported(profile):
                return
            kwargs = connections.connect_kwargs(profile)
            opened: list[Any] = []
            errors: list[BaseException] = []

            def one() -> None:
                try:
                    opened.append(s2.connect(**kwargs, autocommit=True))
                except BaseException as exc:  # noqa: BLE001 - no connections then: the server connects itself
                    errors.append(exc)

            threads = [threading.Thread(target=one, daemon=True) for _ in range(self._n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            with self._lock:
                if self._abandoned:
                    _close(opened)  # handed over (empty) while connecting
                    return
                self._kwargs, self._conns, self._last_ok = kwargs, opened, time.monotonic()
            while not self._stop.wait(PING_S):
                with self._lock:
                    conns = list(self._conns)
                if time.monotonic() - self._started > MAX_AGE_S:
                    with self._lock:
                        self._conns = []
                    _close(conns)
                    return
                alive = []
                for c in conns:
                    try:
                        _ping(c)
                        alive.append(c)
                    except Exception:  # noqa: BLE001 - dropped (VPN, idle timeout): not handed over
                        _close([c])
                with self._lock:
                    if self._abandoned:
                        _close(alive)
                        return
                    self._conns, self._last_ok = alive, time.monotonic()
        except Exception:  # noqa: BLE001 - nothing to hand over
            with self._lock:
                conns, self._conns = self._conns, []
            _close(conns)

    def handover(self, wait: float = 1.5) -> Preconnected:
        """Stop pinging (waiting a little for a connect or ping in progress) and keep only fresh connections."""
        self._stop.set()
        self._thread.join(wait)
        stale: list[Any] = []
        with self._lock:
            if self._thread.is_alive():
                # Still connecting, or a ping hangs (network gone): hand nothing over; the thread closes what it has.
                self._abandoned = True
                self._conns, self._kwargs = [], None
            elif time.monotonic() - self._last_ok > FRESH_S:
                stale, self._conns, self._kwargs = self._conns, [], None
        _close(stale)
        return self

    # -- in the server
    def take(self, kwargs: dict[str, Any] | None) -> list[Any]:
        """The connections, if they were opened with exactly these connect arguments; else none (and they're closed)."""
        with self._lock:
            conns, self._conns = self._conns, []
            same = kwargs is not None and kwargs == self._kwargs
            self._kwargs = None
        if not same:
            _close(conns)
            return []
        return conns


def start() -> Preconnected | None:
    """In the standby: start opening connections (None when turned off)."""
    n = count()
    return Preconnected(n) if n else None


def pop_holder() -> Any:
    """In the server: the standby's holder (once), or None."""
    return sys.modules.pop(HOLDER_MODULE, None)
