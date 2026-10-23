"""Pre-warm the Workspace's SAS session: start the active SAS Viya connection's Compute session (with the automatic
SingleStore library and the startup code) and its CAS session in a background thread when the Workspace opens, so
the first PROC SQL / SAS library list doesn't wait ~15–35 s for a cold session.

- ``prewarm(conn)`` returns at once (it never touches Viya on the caller's thread) and starts at most one warm-up
  per connection. The warm-up holds the session's own lock (``Compute.lock``) while it starts the session, so a
  run or a library list that asks meanwhile waits for the same warm-up instead of starting a second session
  (``sas_runs`` reports it as ``waiting_for: {warming: true}``).
- It never asks anyone to sign in: a connection that isn't signed in (``NeedsSignIn``) or a Viya that can't be
  reached (VPN off) is skipped and the reason recorded (``status``).
- Keep-alive: the Workspace asks again every few minutes while it's open. When the session was checked more than
  ``KEEPALIVE_S`` ago, the warm-up reads the session's state (``GET /compute/sessions/{id}/state``, no SAS code)
  and the CAS session (``GET /cas/sessions/{id}``); a session that ended (Viya's idle timeout) is started again in
  the background. Nothing polls when no window asks, so it stops by itself.
- The SAS 9 sign-on (SAS/CONNECT) stays lazy: it runs when the Schema Explorer's SAS 9 source is opened.
- Off per connection (Connections → SAS Viya → Edit → "Start the SAS session when the Workspace opens") or for
  the whole server with ``SINGLESTORE_MCP_SAS_PREWARM=0`` (the test suite does that unless a test turns it on).

Status (no secrets, ever: errors are masked and cut short) per connection: ``state`` is one of ``off``,
``idle``, ``starting``, ``ready``, ``not_signed_in``, ``unreachable``, ``error``.
"""

from __future__ import annotations

import http.client
import os
import threading
import time
from typing import Any

from . import sas_rest, sas_sql, sas_viya
from .sas_sql import SasError

ENV = "SINGLESTORE_MCP_SAS_PREWARM"
KEEPALIVE_S = 240.0      # a session is checked at most this often (the Workspace asks every ~4 minutes)
RETRY_S = 60.0           # after "unreachable" / "error", another warm-up only after this long

_lock = threading.RLock()
_state: dict[str, dict[str, Any]] = {}       # connection -> what the last warm-up found
_threads: dict[str, threading.Thread] = {}   # connection -> the warm-up running now


def env_enabled() -> bool:
    return os.environ.get(ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def enabled(conn: str | None = None) -> tuple[bool, str | None]:
    """(on, why not) for connection ``conn`` (default: the active one)."""
    if not env_enabled():
        return False, f"Pre-warming is turned off ({ENV}=0)."
    s = sas_viya.settings(conn)
    if not s.get("name") or not s.get("url"):
        return False, "No SAS Viya connection."
    if not s.get("prewarm", True):
        return False, "Turned off for this connection (Connections → SAS Viya → Edit)."
    return True, None


def _name(conn: str | None) -> str:
    return conn or sas_viya.active_name() or ""


def _set(name: str, **values: Any) -> None:
    with _lock:
        _state[name] = {**_state.get(name, {}), **values, "at": time.time()}


def _warming(name: str) -> bool:
    th = _threads.get(name)
    return bool(th and th.is_alive())


def status(conn: str | None = None) -> dict[str, Any]:
    """The session state of connection ``conn`` for the views' status line (no network)."""
    name = _name(conn)
    if not name:
        return {"connection": None, "state": "off", "note": "No SAS Viya connection."}
    compute, cas = sas_rest.sessions(name)
    with _lock:
        st = dict(_state.get(name, {}))
        warming = _warming(name)
    on, why = enabled(name) if name else (False, None)
    out: dict[str, Any] = {"connection": name, "enabled": on, "compute_session": bool(compute.sid),
                           "cas_session": bool(cas.sid)}
    for k in ("ms", "compute_ms", "cas_ms", "finished", "checked", "note", "cas_note"):
        if st.get(k) is not None:
            out[k] = st[k]
    if warming and st.get("state") == "starting":
        out["state"] = "starting"  # (the session id comes before the automatic library and the startup code)
    elif (compute.sid and st.get("state") == "unreachable" and not warming
          and float(st.get("at") or 0) > float(compute.last_used or 0)):
        out["state"] = "unreachable"  # the keep-alive check couldn't reach Viya (VPN dropped?)
    elif compute.sid:
        out["state"] = "ready"
        out.pop("note", None)
        if compute.startup and compute.startup.get("errors"):
            out["startup_errors"] = compute.startup["errors"]
    elif warming:
        out["state"] = "starting"
    elif st.get("state") in ("not_signed_in", "unreachable", "error"):
        out["state"] = st["state"]
    elif not on:
        out["state"], out["note"] = "off", why
    else:
        out["state"] = "idle"  # no session yet (it ended, or it was never started): the next run starts one
    return out


def prewarm(conn: str | None = None, *, cas: bool = True) -> dict[str, Any]:
    """Start the connection's sessions in the background (default: the active connection) unless they're running
    and were checked recently, or a warm-up is going on. Returns the status at once."""
    name = _name(conn)
    if not name:
        return status(conn)
    on, why = enabled(name)
    if not on:
        return {**status(name), "skipped": why}
    compute, _ = sas_rest.sessions(name)
    with _lock:
        if _warming(name):
            return status(name)
        st = _state.get(name, {})
        now = time.time()
        if compute.sid and now - float(st.get("checked") or 0) < KEEPALIVE_S:
            return status(name)
        if not compute.sid and st.get("state") in ("unreachable", "error") and now - float(st.get("at") or 0) < RETRY_S:
            return status(name)
        if not compute.sid:
            # Signed in? From the stored token only (no network); the warm-up renews it if needed.
            try:
                info = sas_viya.token_info(name)
            except LookupError as exc:
                return {**status(name), "skipped": str(exc)}
            if not info.get("signed_in") and not info.get("foreign_available"):
                _state[name] = {"state": "not_signed_in", "at": now,
                                "note": f"Not signed in to SAS Viya ({name}): sign in under Connections → SAS Viya."}
                return status(name)
        th = threading.Thread(target=_warm, args=(name, cas), name=f"sas-prewarm-{name}", daemon=True)
        _threads[name] = th
        if not compute.sid:
            _state[name] = {**st, "state": "starting", "at": now, "started": now, "note": None}
        th.start()
    return status(name)


def _problem(exc: BaseException) -> tuple[str, str]:
    """(state, note) for a failed warm-up; the note has no secrets."""
    if isinstance(exc, sas_viya.NeedsSignIn):
        return "not_signed_in", str(exc)[:300]
    text = sas_sql.mask_secrets(str(exc) or type(exc).__name__)[:300]
    if isinstance(exc, (OSError, http.client.HTTPException, TimeoutError)) or "Couldn't reach SAS Viya" in text:
        return "unreachable", text
    return "error", text


def _alive(compute: sas_rest.Compute) -> bool:
    """Is the Compute session still there? (one GET of its state; no SAS code)"""
    sid = compute.sid
    if not sid:
        return False
    status, body, _ = sas_rest.request("GET", f"{sas_rest._base(compute.conn)}/compute/sessions/{sid}/state",
                                       accept="text/plain", timeout=30, conn=compute.conn)
    if sas_rest.session_gone(status, body) or status in (404, 410):
        return False
    if status != 200:
        raise sas_rest._viya_error(status, body, "Couldn't read the SAS session's state")
    return True


def _cas_alive(cas: sas_rest.Cas) -> bool:
    sid = cas.sid
    if not sid:
        return False
    status, _, _ = sas_rest.request("GET", f"{cas._root()}/cas/sessions/{sid}", timeout=30, conn=cas.conn)
    return status not in (404, 410)


def _warm(name: str, with_cas: bool) -> None:
    compute, cas = sas_rest.sessions(name)
    started = time.perf_counter()
    result: dict[str, Any] = {}
    try:
        # The lock a run takes: a run (or a library list) that comes meanwhile waits for this warm-up. A run that
        # holds it already uses the session: nothing to do.
        if not compute.lock.acquire(timeout=1):
            _set(name, state="ready" if compute.sid else "idle", checked=time.time())
            return
        try:
            compute.warming = True
            try:
                if compute.sid and not _alive(compute):
                    compute.sid = None  # Viya ended it (idle timeout): start a new one now, not at the next run
                    _set(name, state="starting", started=time.time(), note=None)
                fresh = not compute.sid
                t = time.perf_counter()
                compute.session()  # session + automatic SingleStore library + startup code
                if not fresh:
                    compute.ensure_auto()  # another SingleStore connection / a renewed token: re-assign it now
                if fresh:
                    result["compute_ms"] = round((time.perf_counter() - t) * 1000)
            finally:
                compute.warming = False
        finally:
            compute.lock.release()
        if with_cas and cas.lock.acquire(timeout=1):
            cas.warming = True
            try:
                t = time.perf_counter()
                try:
                    if cas.sid and not _cas_alive(cas):
                        cas.sid = None
                    if not cas.sid:
                        cas.session()
                        result["cas_ms"] = round((time.perf_counter() - t) * 1000)
                    result["cas_note"] = None
                except Exception as exc:  # noqa: BLE001 - CAS is optional: Compute is ready all the same
                    result["cas_note"] = _problem(exc)[1]
            finally:
                cas.warming = False
                cas.lock.release()
        if "compute_ms" in result:
            result["ms"] = round((time.perf_counter() - started) * 1000)
        _set(name, state="ready", note=None, checked=time.time(), finished=time.time(), **result)
    except BaseException as exc:  # noqa: BLE001 - recorded for the status line; never raised
        state, note = _problem(exc)
        _set(name, state=state, note=note, checked=time.time())
    finally:
        with _lock:
            if _threads.get(name) is threading.current_thread():
                _threads.pop(name, None)


def wait(conn: str | None = None, timeout: float = 120) -> dict[str, Any]:
    """Wait for a running warm-up to end (tests, the perf script). Returns the status."""
    name = _name(conn)
    with _lock:
        th = _threads.get(name)
    if th:
        th.join(timeout)
    return status(name)


def forget(conn: str | None = None) -> None:
    """Forget what the last warm-up found (the connection was reset, signed out or deleted)."""
    with _lock:
        _state.pop(_name(conn), None)


__all__ = ["ENV", "KEEPALIVE_S", "SasError", "enabled", "env_enabled", "forget", "prewarm", "status", "wait"]
