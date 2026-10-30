"""Browser sessions of the hub (in memory: a hub restart signs everyone out, and their Studios stop with it).

The cookie holds only a random session id (``secrets.token_urlsafe(32)``); everything else stays on the server.
Cookies are HttpOnly, SameSite=Lax, Secure (and ``__Host-`` prefixed) behind HTTPS. Each session has its own
CSRF token for the hub's own forms; Studio calls are checked by Origin instead (see app.py).

Sign-ins in progress (PKCE verifier + state) live in ``pending`` under a separate short-lived cookie, so a
callback only completes the sign-in that this browser started (no login CSRF).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

PENDING_TTL_S = 900  # a sign-in started more than 15 minutes ago must be started again


@dataclass
class Session:
    sid: str
    user: str
    name: str
    csrf: str
    created: float
    last_seen: float
    ip: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Pending:
    pid: str
    state: str
    verifier: str
    created: float
    csrf: str


class SessionStore:
    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s
        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ sessions
    def create(self, user: str, name: str, ip: str = "") -> Session:
        now = time.time()
        s = Session(sid=secrets.token_urlsafe(32), user=user, name=name, csrf=secrets.token_urlsafe(24),
                    created=now, last_seen=now, ip=ip)
        with self._lock:
            self._sessions[s.sid] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if time.time() - s.created > self.max_age_s:
                self._sessions.pop(sid, None)
                return None
            s.last_seen = time.time()
            return s

    def drop(self, sid: str | None) -> Session | None:
        with self._lock:
            return self._sessions.pop(sid or "", None)

    def drop_user(self, user: str) -> int:
        with self._lock:
            gone = [k for k, s in self._sessions.items() if s.user == user]
            for k in gone:
                self._sessions.pop(k, None)
        return len(gone)

    def all(self) -> list[Session]:
        now = time.time()
        with self._lock:
            for k in [k for k, s in self._sessions.items() if now - s.created > self.max_age_s]:
                self._sessions.pop(k, None)
            return list(self._sessions.values())

    # ------------------------------------------------------------ sign-ins in progress
    def start_pending(self, verifier: str) -> Pending:
        now = time.time()
        p = Pending(pid=secrets.token_urlsafe(24), state=secrets.token_urlsafe(24), verifier=verifier, created=now,
                    csrf=secrets.token_urlsafe(24))
        with self._lock:
            for k in [k for k, v in self._pending.items() if now - v.created > PENDING_TTL_S]:
                self._pending.pop(k, None)
            if len(self._pending) > 1000:  # many unfinished sign-ins: forget the oldest
                for k in sorted(self._pending, key=lambda k: self._pending[k].created)[:500]:
                    self._pending.pop(k, None)
            self._pending[p.pid] = p
        return p

    def peek_pending(self, pid: str | None) -> Pending | None:
        with self._lock:
            p = self._pending.get(pid or "")
        if p and time.time() - p.created > PENDING_TTL_S:
            return None
        return p

    def take_pending(self, pid: str | None) -> Pending | None:
        with self._lock:
            p = self._pending.pop(pid or "", None)
        if p and time.time() - p.created > PENDING_TTL_S:
            return None
        return p


class RateLimiter:
    """At most ``limit`` events per ``window_s`` seconds per key (a client address): sign-in starts and code
    exchanges, so nobody can flood SAS Logon through the hub or push other people's sign-ins out of ``pending``."""

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, deque[float]] = {}
        self._noted: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Count one event for ``key``; False (not counted) when the key used up its limit in this window."""
        now = time.time() if now is None else now
        with self._lock:
            if len(self._hits) > 10_000:  # many addresses: forget the ones that went quiet
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window_s]:
                    self._hits.pop(k, None)
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def first_refusal(self, key: str, now: float | None = None) -> bool:
        """True once per window for a key that is being refused (to note it once, not for every request)."""
        now = time.time() if now is None else now
        with self._lock:
            last = self._noted.get(key)
            if last is not None and now - last < self.window_s:
                return False
            if len(self._noted) > 10_000:
                self._noted.clear()
            self._noted[key] = now
            return True

    def retry_after(self, key: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            q = self._hits.get(key)
            return max(1, int(self.window_s - (now - q[0])) + 1) if q else 1
