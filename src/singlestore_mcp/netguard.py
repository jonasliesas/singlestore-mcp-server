"""Recovering from network breaks: a VPN that drops or reconnects (also on another gateway, with a new local IP),
a laptop that slept, a Wi-Fi change.

After such a break, TCP connections opened before it are often dead without anyone saying so (a *half-open*
connection): a query sent on one waits for an answer that never comes – Windows' default TCP keepalive notices
after two hours. This module gives the SingleStore pool (``db``), the SAS Viya client (``sas_rest``) and the
Workspace what they need to notice in seconds instead:

- ``socket_options()`` / ``tune(sock)``: TCP keepalive after ``SINGLESTORE_MCP_TCP_KEEPALIVE`` = "idle,interval,count"
  seconds (default 10,5,3: a dead peer is noticed ~25 s after it went silent, also during a long query) and, on Linux,
  TCP_USER_TIMEOUT (unacknowledged data); only options this OS accepts are used.
- A watcher thread (every ``TICK_S``) that notices a **sleep / resume** (the clock jumped) and a **changed route**
  (the local address a tracked connection's peer is reached from is no longer the connection's own: the VPN moved
  to another address). A route change closes the tracked sockets bound to the old address at once – a read waiting
  on one returns an error instead of hanging – and both events raise ``epoch()``, so pools validate their idle
  connections (a ping with a short timeout) before using them again.
- ``ping(raw, timeout)``: COM_PING on a driver connection, bounded.
- Health per service ("singlestore", "sas"): ``failure`` / ``success`` record what callers saw; while a service
  is down, ``probe`` runs its registered probe in the background (at most every ``PROBE_EVERY_S``), so it's found
  back within seconds of the network returning even when nothing else asks. ``snapshot()`` is what the
  Workspace's "connection lost – retrying" line reads (``connection_health``), ``notice`` adds a one-time message
  (e.g. "the SAS session was restarted after a connection loss").
"""

from __future__ import annotations

import collections
import itertools
import os
import socket
import sys
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, default)))
    except ValueError:
        return default


CONNECT_TIMEOUT_S = _env_float("SINGLESTORE_MCP_CONNECT_TIMEOUT", 10)   # TCP connect to the cluster / Viya
CONNECT_DEADLINE_S = _env_float("SINGLESTORE_MCP_CONNECT_DEADLINE", 20)  # a whole DB login (TCP, TLS, auth, SETs)
PING_TIMEOUT_S = _env_float("SINGLESTORE_MCP_PING_TIMEOUT", 3)           # validating an idle pooled connection
TICK_S = _env_float("SINGLESTORE_MCP_NET_TICK", 2)
SLEEP_GAP_S = 10.0          # the watcher's clock jumped this much more than TICK_S: the computer slept
PROBE_EVERY_S = _env_float("SINGLESTORE_MCP_PROBE_EVERY", 3)


class Unreachable(ConnectionError, RuntimeError):
    """A service couldn't be reached over the network (VPN off, Wi-Fi gone); retried by itself once it's back."""


# ------------------------------------------------------------------ socket options

def _keepalive() -> tuple[int, int, int] | None:
    raw = os.environ.get("SINGLESTORE_MCP_TCP_KEEPALIVE", "10,5,3").strip().lower()
    if raw in ("0", "off", "no", "false"):
        return None
    try:
        idle, interval, count = (max(1, int(float(x))) for x in raw.split(","))
        return idle, interval, count
    except ValueError:
        return 10, 5, 3


_options: dict[int, dict[int, int]] | None = None


def socket_options() -> dict[int, dict[int, int]]:
    """Keepalive (and Linux's TCP_USER_TIMEOUT) socket options this OS accepts, as ``singlestoredb``'s
    ``socket_options`` ({level: {option: value}}). Probed once on a throwaway socket: an option the OS refuses would
    otherwise fail every connect."""
    global _options
    if _options is not None:
        return _options
    ka = _keepalive()
    wanted: list[tuple[int, int, int]] = []
    if ka:
        idle, interval, count = ka
        wanted.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
        for name, value in (("TCP_KEEPIDLE", idle), ("TCP_KEEPALIVE", idle), ("TCP_KEEPINTVL", interval),
                            ("TCP_KEEPCNT", count)):
            opt = getattr(socket, name, None)
            if opt is not None and not (name == "TCP_KEEPALIVE" and hasattr(socket, "TCP_KEEPIDLE")):
                wanted.append((socket.IPPROTO_TCP, opt, value))
        if sys.platform.startswith("linux") and hasattr(socket, "TCP_USER_TIMEOUT"):
            wanted.append((socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, (idle + interval * count) * 1000))
    out: dict[int, dict[int, int]] = {}
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        probe = None
    for level, opt, value in wanted:
        try:
            if probe is not None:
                probe.setsockopt(level, opt, value)
            out.setdefault(level, {})[opt] = value
        except OSError:
            pass
    if probe is not None:
        probe.close()
    _options = out
    return out


def tune(sock: Any) -> None:
    """Apply ``socket_options()`` to an open socket (HTTP connections); never raises."""
    for level, opts in socket_options().items():
        for opt, value in opts.items():
            try:
                sock.setsockopt(level, opt, value)
            except (OSError, AttributeError, ValueError):
                pass


# ------------------------------------------------------------------ routes

def sock_info(sock: Any) -> tuple[str, tuple[Any, ...], int] | None:
    """(local address, peer address, family) of a connected socket, or None."""
    try:
        return sock.getsockname()[0], tuple(sock.getpeername()[:2]), sock.family
    except (OSError, AttributeError, ValueError):
        return None


def route_source(peer: tuple[Any, ...], family: int = socket.AF_INET) -> str | None:
    """The local address the OS would send to ``peer`` from now (a UDP "connect" only looks up the route: no packet
    is sent). None when there's no route (network down)."""
    try:
        s = socket.socket(family, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        s.connect((peer[0], peer[1] or 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def moved(info: tuple[str, tuple[Any, ...], int] | None) -> bool:
    """Was the connection described by ``info`` opened from an address the route to its peer no longer uses?
    (No route at all isn't "moved": a short Wi-Fi blip may leave connections alive.)"""
    if not info:
        return False
    src = route_source(info[1], info[2])
    return src is not None and src != info[0]


# ------------------------------------------------------------------ epoch, watcher

_lock = threading.RLock()
_epoch = 0
_events: collections.deque[dict[str, Any]] = collections.deque(maxlen=20)
_tracked: weakref.WeakKeyDictionary[Any, tuple[str, tuple[str, tuple[Any, ...], int] | None]] = weakref.WeakKeyDictionary()
_listeners: list[Callable[[str], None]] = []
_watcher: threading.Thread | None = None


def epoch() -> int:
    """Goes up on every network event (resume from sleep, route change, a failure seen): connections idle since an
    older epoch are validated before they're used."""
    return _epoch


def bump(reason: str) -> int:
    global _epoch
    with _lock:
        _epoch += 1
        _events.append({"at": time.time(), "event": reason, "epoch": _epoch})
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(reason)
        except Exception:  # noqa: BLE001 - a listener never stops the others
            pass
    return _epoch


def on_event(fn: Callable[[str], None]) -> None:
    """Call ``fn(reason)`` on every network event (from the watcher's or the reporting thread)."""
    with _lock:
        _listeners.append(fn)


def track(sock: Any, service: str) -> None:
    """Watch this connected socket: closed at once when the route to its peer moves to another local address."""
    info = sock_info(sock)
    if info is None:
        return
    with _lock:
        try:
            _tracked[sock] = (service, info)
        except TypeError:
            return
        if len(_peers) < 64:
            _peers.setdefault((info[1], info[2]), info[0])
    ensure_watching()


def ensure_watching() -> None:
    global _watcher
    with _lock:
        if _watcher is not None and _watcher.is_alive():
            return
        _watcher = threading.Thread(target=_watch, name="s2-netguard", daemon=True)
        _watcher.start()


def _shutdown(sock: Any) -> None:
    """End a dead connection so a thread blocked reading it gets an error now."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, AttributeError, ValueError):
        pass
    if sys.platform == "win32":
        # On Windows a shutdown doesn't wake a recv that is already waiting; closing the handle does (the driver's
        # makefile() holds a reference, so socket.close() alone would wait for it).
        import _socket

        try:
            _socket.socket.close(sock)
        except (OSError, AttributeError, TypeError, ValueError):
            pass


_peers: dict[tuple[Any, ...], str | None] = {}  # (peer, family) -> the local address its route used last


def check_routes() -> int:
    """Close tracked sockets whose route moved to another local address; returns how many (one ``bump`` if any).
    A route to a known peer that disappears (VPN off) or comes back is an event too: the services are checked again
    at once, so the Workspace's "connection lost" line shows and goes within seconds."""
    with _lock:
        items = list(_tracked.items())
        peers = dict(_peers)
    sources: dict[tuple[Any, ...], str | None] = {key: route_source(key[0], key[1]) for key in peers}
    changed = [key for key, src in sources.items() if src != peers[key]]
    if changed:
        with _lock:
            for key in changed:
                _peers[key] = sources[key]
    stale = []
    for sock, (_service, info) in items:
        if getattr(sock, "_closed", False) or sock_info(sock) is None:
            with _lock:
                _tracked.pop(sock, None)
            continue
        key = (info[1], info[2])
        if key not in sources:
            sources[key] = route_source(info[1], info[2])
        src = sources[key]
        if src is not None and src != info[0]:
            stale.append(sock)
    for sock in stale:
        with _lock:
            _tracked.pop(sock, None)
        _shutdown(sock)
    if stale or changed:
        bump("route")
        probe_all(force=True)
    return len(stale)


def _clock() -> tuple[float, float]:
    return time.monotonic(), time.time()


_sleep = time.sleep


def _watch() -> None:
    last_mono, last_wall = _clock()
    while True:
        _sleep(TICK_S)
        try:
            now_mono, now_wall = _clock()
            gap = max(now_mono - last_mono, now_wall - last_wall)
            last_mono, last_wall = now_mono, now_wall
            if gap > TICK_S + SLEEP_GAP_S:
                bump("resume")
                probe_all()
            check_routes()
            for svc in down_services():
                probe(svc)
        except Exception:  # noqa: BLE001 - the watcher never dies
            pass


# ------------------------------------------------------------------ health

_health: dict[str, dict[str, Any]] = {}
_probes: dict[str, Callable[[], None]] = {}
_probing: dict[str, float] = {}
_notices: collections.deque[dict[str, Any]] = collections.deque(maxlen=20)
_notice_ids = itertools.count(1)


def failure(service: str, error: BaseException | str) -> None:
    """A call to ``service`` failed because of the network."""
    text = str(error)[:300] or type(error).__name__
    now = time.time()
    first = False
    with _lock:
        h = _health.setdefault(service, {"state": "ok"})
        if h.get("state") != "down":
            first = True
            h.update(state="down", since=now, failures=0)
        h.update(error=text, last_failure=now, failures=int(h.get("failures") or 0) + 1)
    if first:
        bump(f"{service}-down")
    ensure_watching()


def success(service: str) -> None:
    """A call to ``service`` worked (cheap when nothing changed)."""
    h = _health.get(service)
    if h is None or h.get("state") == "ok":
        if h is None:
            with _lock:
                _health.setdefault(service, {"state": "ok"})
        return
    with _lock:
        h = _health[service]
        if h.get("state") == "ok":
            return
        down_s = round(time.time() - float(h.get("since") or time.time()), 1)
        h.update(state="ok", recovered=time.time(), down_s=down_s, error=None, failures=0)


def is_down(service: str) -> bool:
    return (_health.get(service) or {}).get("state") == "down"


def down_services() -> list[str]:
    with _lock:
        return [s for s, h in _health.items() if h.get("state") == "down"]


def set_probe(service: str, fn: Callable[[], None]) -> None:
    """``fn()`` checks ``service`` (and records ``success`` / ``failure``); run in the background by ``probe``."""
    with _lock:
        _probes[service] = fn


def probe(service: str, force: bool = False) -> bool:
    """Run the service's probe in the background unless one ran within ``PROBE_EVERY_S`` (``force``: unless one
    runs now). True when one was started."""
    with _lock:
        fn = _probes.get(service)
        if fn is None:
            return False
        last = _probing.get(service)
        now = time.monotonic()
        # Every few seconds for the first two minutes of an outage, then every 30 s (a forced probe – the page came
        # back online or was shown again – goes at once).
        h = _health.get(service) or {}
        long_down = h.get("state") == "down" and time.time() - float(h.get("since") or time.time()) > 120
        every = max(PROBE_EVERY_S, 30.0) if long_down else PROBE_EVERY_S
        if last is not None and (last < 0 or (not force and now - last < every)):
            return False
        _probing[service] = -1.0  # running

    def run() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - the probe records what it found
            pass
        finally:
            with _lock:
                _probing[service] = time.monotonic()

    threading.Thread(target=run, name=f"s2-probe-{service}", daemon=True).start()
    return True


def probe_all(force: bool = False) -> None:
    """After a network event: check every service that was used (has a health entry)."""
    with _lock:
        services = [s for s in _health if s in _probes]
    for s in services:
        probe(s, force)


def notice(service: str, text: str) -> None:
    with _lock:
        _notices.append({"id": next(_notice_ids), "at": time.time(), "service": service, "text": text})


def snapshot() -> dict[str, Any]:
    """Health per service, the latest one-time notices and network events (no secrets: errors are cut short)."""
    with _lock:
        return {"services": {s: dict(h) for s, h in _health.items()}, "notices": list(_notices),
                "events": list(_events)[-5:], "epoch": _epoch}


def reset_for_tests() -> None:
    with _lock:
        _health.clear()
        _probing.clear()
        _peers.clear()
        _notices.clear()
        _events.clear()


# ------------------------------------------------------------------ driver helpers

def ping(raw: Any, timeout: float | None = None) -> None:
    """COM_PING on a ``singlestoredb`` (pymysql-style) connection, failing after ``timeout`` seconds instead of
    hanging on a half-open socket. Raises the driver's error (the connection is then closed)."""
    timeout = PING_TIMEOUT_S if timeout is None else timeout
    old_r, old_w = getattr(raw, "_read_timeout", None), getattr(raw, "_write_timeout", None)
    raw._read_timeout = raw._write_timeout = timeout
    try:
        raw.ping(reconnect=False)
    finally:
        raw._read_timeout, raw._write_timeout = old_r, old_w
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            try:
                sock.settimeout(old_r)  # None: blocking again, as the driver leaves it
            except OSError:
                pass
