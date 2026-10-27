"""SAS Viya REST client for the Workspace: one SAS Compute session and one CAS session per SAS Viya connection,
kept alive and reused.

Signs in with the Viya sign-in from ``sas_viya`` (OAuth token, ``sas.json``, compute context). Used by the SQL
Editor's "Run as" PROC SQL / FedSQL / FedSQL in CAS and by the Schema Explorer's SAS sources. ``sessions(name)``
gives a connection's pair (``Compute``, ``Cas``); ``compute`` / ``cas`` are the active connection's.

Every call to Viya is a round trip (and the token, TLS and proxy add to the first), so:

- HTTP connections are kept open (``http.client`` keep-alive, one per thread and host); after a network event
  (sleep / resume, another route: ``netguard``) or ``HTTP_IDLE_S`` of idleness a kept connection isn't reused but
  replaced, its socket has a short TCP keepalive, and connecting gives up after ``netguard.CONNECT_TIMEOUT_S``;
- the Compute session is created once and reused (one job at a time, under a lock) until it expires, when
  it's recreated and the startup code runs again;
- job state is long-polled (``?wait=``), result tables are read through the Compute data API in parallel.

A run can be stopped (``Compute.cancel_job`` / ``Cas.cancel``, the documented job / action cancel); a job runs at
most ``job_timeout()`` seconds (SINGLESTORE_MCP_SAS_TIMEOUT_MIN, default 240 minutes, 0 = no limit).

Startup code (``sas.json`` → ``startup_code``, set in Connections) runs once per session with
``options nosource nosource2`` and its log is never kept or shown (it typically ``%include``s libname files
that hold passwords): only the number of errors and the first error line (passwords masked) are reported.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import connections, netguard, sas_sql, sas_viya
from .sas_sql import SasError

DEFAULT_TIMEOUT_MIN = 240  # a single job may run this long (SINGLESTORE_MCP_SAS_TIMEOUT_MIN; 0 = no limit)
_WAIT = 20                 # long-poll seconds per state request
HTTP_IDLE_S = 120.0        # a kept-alive connection idle longer than this is replaced, not reused
RESTARTED = ("The SAS session had ended (connection loss or Viya's idle timeout) and was started again: the automatic "
             "SingleStore library and the startup code ran again; WORK tables and macro variables from before are gone.")


def job_timeout() -> float | None:
    """Seconds a single SAS job / CAS action may run before it's cancelled (None: no limit)."""
    raw = os.environ.get("SINGLESTORE_MCP_SAS_TIMEOUT_MIN", "").strip()
    try:
        minutes = float(raw) if raw else DEFAULT_TIMEOUT_MIN
    except ValueError:
        minutes = DEFAULT_TIMEOUT_MIN
    return None if minutes <= 0 else minutes * 60


class SasStopped(SasError):
    """The run was stopped by the user (its session may have been ended meanwhile)."""


class RunControl:
    """Lets another thread stop a running job: ``cancel`` is set by Stop, ``job`` is the job running now."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.job: str | None = None

_RUNNING = {"pending", "queued", "running", "idle"}
_local = threading.local()
_COLLECTION = "application/vnd.sas.collection+json, application/json"


# ------------------------------------------------------------------ HTTP


def _ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _proxy_for(url: str) -> str | None:
    host = urllib.parse.urlsplit(url).hostname or ""
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") if url.startswith("https") else proxies.get("http")
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    return proxy


def _open(scheme: str, host: str, port: int, proxy: str | None, verify: bool, timeout: float) -> http.client.HTTPConnection:
    if proxy:
        p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=timeout,
                                                                          context=_ssl_context(verify))
            conn.set_tunnel(host, port)
            return conn
        return http.client.HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=_ssl_context(verify))
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _conn(url: str, verify: bool, timeout: float, direct: bool) -> tuple[http.client.HTTPConnection, str]:
    parts = urllib.parse.urlsplit(url)
    scheme, host = parts.scheme, parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    proxy = None if direct else _proxy_for(url)
    key = (scheme, host, port, proxy, verify)
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    conn = pool.get(key)
    if conn is not None and conn.sock is not None and _stale(conn):
        conn.close()  # possibly dead after a network break: connect afresh instead of waiting on it
    if conn is None:
        conn = pool[key] = _open(scheme, host, port, proxy, verify, timeout)
    conn.timeout = timeout
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    if proxy and scheme == "http":
        target = url
    return conn, target


def _stale(conn: http.client.HTTPConnection) -> bool:
    """A kept-alive connection not worth reusing: opened before a network event, idle long, or from an address the
    route no longer uses (half-open after a VPN switch / sleep: a request on it would wait for its whole timeout)."""
    return (getattr(conn, "_s2_epoch", None) != netguard.epoch()
            or time.monotonic() - float(getattr(conn, "_s2_used", 0.0)) > HTTP_IDLE_S
            or netguard.moved(getattr(conn, "_s2_info", None)))


def _connect(conn: http.client.HTTPConnection, timeout: float) -> None:
    """Connect (a proxy tunnel and TLS included) within ``netguard.CONNECT_TIMEOUT_S``, then wait up to ``timeout``
    for answers; short TCP keepalive; the socket is watched (closed at once when the route moves)."""
    conn.timeout = min(timeout, netguard.CONNECT_TIMEOUT_S)
    try:
        conn.connect()
    finally:
        conn.timeout = timeout
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
        netguard.tune(conn.sock)
        netguard.track(conn.sock, "sas")
        conn._s2_info = netguard.sock_info(conn.sock)  # type: ignore[attr-defined]
        conn._s2_epoch = netguard.epoch()  # type: ignore[attr-defined]
        conn._s2_used = time.monotonic()  # type: ignore[attr-defined]


def _drop(url: str) -> None:
    pool = getattr(_local, "pool", {})
    host = urllib.parse.urlsplit(url).hostname
    for key in [k for k in pool if k[1] == host]:
        try:
            pool.pop(key).close()
        except Exception:  # noqa: BLE001
            pass


_IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
_RETRY_WAITS = (0.5, 1.5)  # seconds before the 2nd and 3rd try on a fresh connection


def _transient(exc: BaseException) -> bool:
    """A dropped connection (the VPN, a proxy or Viya's ingress cut it, often seen as an SSL EOF) – worth another
    try. Not: refused connections or unknown hosts (Viya down / no VPN), which fail the same way again."""
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)):
        return False
    return isinstance(exc, (ssl.SSLError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                            http.client.RemoteDisconnected, http.client.IncompleteRead, EOFError))


def _send(url: str, method: str, data: bytes | None, hdrs: dict[str, str], timeout: float, direct: bool,
          verify: bool) -> tuple[int, dict[str, str], bytes]:
    """One request on a kept-alive connection. A reused connection the server (or a proxy) closed meanwhile is
    reopened once. On a fresh connection a dropped connection is tried again twice (after 0.5 s and 1.5 s) when
    nothing was sent yet (the TLS handshake) or the request is idempotent – never a POST that may have arrived."""
    fresh_retries = 0
    timed_out = False
    for _ in range(4):
        conn, target = _conn(url, verify, timeout, direct)
        reused = conn.sock is not None
        sent = False
        try:
            if conn.sock is None:
                _connect(conn, timeout)
            sent = True
            conn.request(method, target, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.will_close:
                _drop(url)
            else:
                conn._s2_used, conn._s2_epoch = time.monotonic(), netguard.epoch()  # type: ignore[attr-defined]
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw
        except TimeoutError:
            _drop(url)
            # A kept-alive connection that went silent (half-open): an idempotent request gets one fresh try.
            if reused and not timed_out and method.upper() in _IDEMPOTENT:
                timed_out = True
                continue
            raise
        except (OSError, http.client.HTTPException, EOFError) as exc:
            _drop(url)
            if reused:
                continue  # the server closed the kept-alive connection: reconnect
            if fresh_retries < len(_RETRY_WAITS) and _transient(exc) and (not sent or method.upper() in _IDEMPOTENT):
                time.sleep(_RETRY_WAITS[fresh_retries])
                fresh_retries += 1
                continue
            raise
    raise SasError(f"SAS Viya kept dropping the connection ({method} {urllib.parse.urlsplit(url).path})")


def request(method: str, url: str, *, body: Any = None, accept: str = "application/json",
            content_type: str = "application/json", timeout: float = 60, headers: dict[str, str] | None = None,
            auth: bool = True, raw: bool = False, conn: str | None = None) -> tuple[int, Any, dict[str, str]]:
    """One HTTP request to Viya on a kept-alive connection: (status, parsed JSON or text, headers).
    ``raw``: a successful (2xx) answer's body as bytes, as it came (file contents). ``conn``: the SAS Viya
    connection whose sign-in (and certificate check) is used (default: the active one)."""
    s = sas_viya.settings(conn)
    data = None
    hdrs = {"Accept": accept, **(headers or {})}
    if body is not None:
        text = body if isinstance(body, (bytes, str)) else json.dumps(body)
        data = text.encode() if isinstance(text, str) else text
        hdrs["Content-Type"] = content_type
    if auth:
        hdrs["Authorization"] = f"Bearer {sas_viya.token(120, conn) if conn else sas_viya.token(120)}"
    proxied = bool(_proxy_for(url))
    # The configured proxy first (unless Viya answered only directly before), then directly.
    routes = [sas_viya._route["direct"], not sas_viya._route["direct"]] if proxied else [True]
    last: Exception | None = None
    for direct in routes:
        try:
            status, rh, body_bytes = _send(url, method, data, hdrs, timeout, direct, s["ssl_verify"])
        except (OSError, http.client.HTTPException, EOFError) as exc:
            _drop(url)
            last = exc
            continue
        if proxied:
            sas_viya._route["direct"] = direct
        break
    else:
        netguard.failure("sas", last or "unreachable")
        raise SasError(f"Couldn't reach SAS Viya at {s['url']}: {last}. It's retried by itself once the network (VPN) "
                       "is back – no restart needed.") from last
    netguard.success("sas")
    if status == 401 and auth:
        raise sas_viya.NeedsSignIn("SAS Viya refused the sign-in (401): sign in again in the Connections window.")
    if raw and 200 <= status < 300:
        return status, body_bytes, rh
    if "json" in rh.get("content-type", ""):
        try:
            return status, json.loads(body_bytes or b"null"), rh
        except ValueError:
            pass
    return status, body_bytes.decode("utf-8", "replace"), rh


def _viya_error(status: int, body: Any, what: str) -> SasError:
    if isinstance(body, dict):
        msg = body.get("message") or body.get("errorMessage") or ""
        details = body.get("details") or []
        if isinstance(details, list) and details:
            msg = f"{msg} ({'; '.join(str(d) for d in details[:3])})"
        if msg:
            return SasError(f"{what}: {msg} (HTTP {status})")
    return SasError(f"{what}: HTTP {status} {str(body)[:300]}")


def _base(conn: str | None = None) -> str:
    url = sas_viya.settings(conn)["url"]
    if not url:
        raise sas_viya.NeedsSignIn("SAS Viya isn't set up: enter its address in the Connections window (SAS Viya).")
    return url


# ------------------------------------------------------------------ Compute


class Compute:
    """One SAS Compute session on the configured compute context, reused for every run. ``conn``: the SAS Viya
    connection it belongs to (None: whichever is active)."""

    def __init__(self, conn: str | None = None) -> None:
        self.conn = conn
        self.current_run: Any = None                  # the editor run holding the session now (sas_runs.Run)
        self.lock = threading.RLock()
        self.sid: str | None = None
        self.key: tuple[str, str] | None = None
        self.started: float | None = None
        self.startup: dict[str, Any] | None = None
        self.libraries: list[dict[str, Any]] | None = None
        self.last_used: float | None = None
        self.auto_lib: dict[str, Any] | None = None   # the automatic SingleStore library (status, no secrets)
        self._auto_key: tuple[Any, ...] | None = None  # what it was assigned with (password only as a hash)
        self.sas9: dict[str, Any] | None = None       # SAS/CONNECT sign-on to SAS 9 and its libraries
        self.warming = False                          # a background warm-up (sas_prewarm) holds the lock now
        self.ended = False                            # Viya ended the session (found gone): say so once restarted
        self.notice: str | None = None                # RESTARTED, until the next run's log shows it

    def gone(self) -> None:
        """Viya says the session doesn't exist any more (it ended while the network was away, or idled out): the
        next call starts a new one (startup code included) and the user is told once."""
        if self.sid:
            self.ended = True
        self.sid = None

    # -- session

    def _context_id(self, name: str) -> str:
        q = urllib.parse.quote(f'eq(name,"{name}")')
        status, body, _ = request("GET", conn=self.conn, url=f"{_base(self.conn)}/compute/contexts?filter={q}&limit=5")
        if status != 200:
            raise _viya_error(status, body, "Couldn't list the compute contexts")
        items = body.get("items", []) if isinstance(body, dict) else []
        if not items:
            raise LookupError(f"No compute context named {name!r}: pick one in the Connections window (SAS Viya → Test).")
        return items[0]["id"]

    def session(self) -> str:
        s = sas_viya.settings(self.conn)
        key = (s["url"], s["compute_context"])
        with self.lock:
            if self.sid and self.key == key:
                return self.sid
            if self.sid:
                self.close()
            ctx = self._context_id(s["compute_context"])
            status, body, _ = request("POST", conn=self.conn, url=f"{_base(self.conn)}/compute/contexts/{ctx}/sessions",
                                      body={"name": "SingleStore Workspace", "description": "SQL Editor / Schema Explorer"},
                                      accept="application/vnd.sas.compute.session+json, application/json", timeout=180)
            if status not in (200, 201) or not isinstance(body, dict) or not body.get("id"):
                raise _viya_error(status, body, f"Couldn't start a SAS session on {s['compute_context']!r}")
            self.sid, self.key, self.started = body["id"], key, time.time()
            self.libraries = None
            self.auto_lib, self._auto_key, self.sas9 = None, None, None
            # The automatic SingleStore library first, so the startup code can override it.
            self._ensure_auto()
            self.startup = self._run_startup(s.get("startup_code") or "")
            if self.ended:
                self.ended = False
                if self.last_used:  # only when it had been used: WORK tables etc. from before are gone
                    self.notice = RESTARTED
                    netguard.notice("sas", f"SAS session ({s.get('name') or 'SAS Viya'}) restarted: it had ended "
                                           "(connection loss or idle timeout). WORK tables from before are gone.")
            return self.sid

    # -- the automatic SingleStore library (LIBNAME <libref> SSTORE … from the active SingleStore connection)

    def _ensure_auto(self) -> None:
        """Assign (or re-assign) the automatic SingleStore library when it's missing or what it should point at
        changed: another active connection, its settings, its password, or a renewed token (Entra renews a few
        minutes before expiry, so a token library is re-assigned before the old token runs out). Never raises
        for SAS or credential problems (they're reported in ``auto_lib``); a session that ended does."""
        with self.lock:
            if not self.sid:
                return
            try:
                spec = auto_spec(self.conn)
            except Exception as exc:  # noqa: BLE001 - credential store / settings: report, don't break the session
                spec = {"skip": f"Couldn't read the SingleStore connection: {str(exc)[:200]}"}
            if "skip" in spec:
                if self.auto_lib is None or not self.auto_lib.get("assigned"):
                    self.auto_lib = {"assigned": False, "note": spec["skip"], "libref": spec.get("libref"),
                                     "enabled": spec.get("enabled", True)}
                elif not spec.get("enabled", True):
                    self.auto_lib = {**self.auto_lib, "note": spec["skip"]}  # turned off: the library stays until the session ends
                return
            if self._auto_key == spec["key"]:
                return
            try:
                self._assign_auto(spec)
            except _SessionGone:
                raise
            except Exception as exc:  # noqa: BLE001 - Viya refused the job etc.: report it
                self._auto_key = None
                self.auto_lib = {"assigned": False, "enabled": True, "libref": spec["libref"],
                                 "connection": spec["profile"].name,
                                 "error": sas_sql.mask_value(str(exc), spec["password"])[:400]}

    def _assign_auto(self, spec: dict[str, Any]) -> None:
        p, pw = spec["profile"], spec["password"]
        code = sas_sql.auto_libname_code(spec["libref"], p.host, p.port, p.user, pw, p.database, trace=True)
        job = self._submit(code, self.sid or "")
        try:
            state = self._wait(job)
            lines = self._log(job)
        finally:
            self._forget_job(job)
        ok, errors = sas_sql.auto_libname_result([x["line"] for x in lines])
        errors = [sas_sql.mask_value(x, pw) for x in errors]
        ok = ok and state not in ("failed", "canceled")
        self.libraries = None
        self._auto_key = spec["key"] if ok else None
        self.auto_lib = {"assigned": ok, "enabled": True, "libref": spec["libref"], "connection": p.name, "host": p.host,
                         "port": p.port, "user": p.user, "database": p.database, "auth": p.auth,
                         "token_expires": spec.get("expires"), "assigned_at": time.time(),
                         "error": " ".join(errors)[:400] or (None if ok else f"SAS job {state}")}

    def _forget_job(self, job: str) -> None:
        """Delete a finished job from the session (its code held a password; the log never did: NOSOURCE)."""
        try:
            request("DELETE", conn=self.conn, url=f"{_base(self.conn)}/compute/sessions/{job}", timeout=15)
        except Exception:  # noqa: BLE001 - the session's end removes it anyway
            pass

    def ensure_auto(self) -> None:
        """``_ensure_auto`` for callers outside a run (the library list): a session that ended is forgotten."""
        with self.lock:
            try:
                self._ensure_auto()
            except _SessionGone:
                self.gone()

    def _run_startup(self, code: str) -> dict[str, Any]:
        if not code.strip():
            return {"ran": False}
        wrapped = f"options nosource nosource2;\n{code}\n;options source nosource2;"
        job = self._submit(wrapped, self.sid)
        state = self._wait(job)
        lines = self._log(job)
        counts = sas_sql.log_counts(lines)
        # Nothing of this log is kept: it can hold what the included files print.
        first = sas_sql.mask_secrets(counts["first_error"])[:300] if counts["first_error"] else None
        return {"ran": True, "state": state, "errors": counts["errors"], "warnings": counts["warnings"], "first_error": first}

    def close(self, force: bool = False) -> None:
        """End the session. ``force``: without waiting for a run that holds the session (Restart SAS session after
        a Stop that didn't work); that run then fails with "session ended"."""
        if force:
            self._close()
            return
        with self.lock:
            self._close()

    def _close(self) -> None:
        sid, self.sid, self.key, self.libraries = self.sid, None, None, None
        self.auto_lib, self._auto_key, self.sas9 = None, None, None
        if sid:
            try:
                request("DELETE", conn=self.conn, url=f"{_base(self.conn)}/compute/sessions/{sid}", timeout=15)
            except Exception:  # noqa: BLE001 - it expires anyway
                pass

    def cancel_job(self, job: str) -> bool:
        """Cancel a running job: the documented ``PUT /compute/sessions/{id}/jobs/{id}/state?value=canceled`` with
        the job's ETag (If-Match); if Viya refuses that, the session's running code (``PUT .../sessions/{id}/state
        ?value=canceled``). True when Viya accepted one of them (the job then ends as "canceled")."""
        sid = job.split("/jobs/")[0]
        for path, accept in ((job, "application/vnd.sas.compute.job+json, application/json"),
                             (sid, "application/vnd.sas.compute.session+json, application/json")):
            try:
                status, _, headers = request("GET", conn=self.conn, url=f"{_base(self.conn)}/compute/sessions/{path}",
                                             accept=accept, timeout=30)
                if status != 200:
                    continue
                etag = headers.get("etag")
                status, _, _ = request("PUT", conn=self.conn, url=f"{_base(self.conn)}/compute/sessions/{path}/state?value=canceled",
                                       body="", accept="text/plain", content_type="text/plain",
                                       headers={"If-Match": etag} if etag else {}, timeout=30)
                if 200 <= status < 300 or status == 409:  # 409: it isn't running any more
                    return True
            except (SasError, OSError, http.client.HTTPException):
                continue
        return False

    # -- jobs

    def _submit(self, code: str, sid: str) -> str:
        status, body, _ = request("POST", conn=self.conn, url=f"{_base(self.conn)}/compute/sessions/{sid}/jobs",
                                  body={"code": code.splitlines() or [""]},
                                  accept="application/vnd.sas.compute.job+json, application/json", timeout=60)
        if status in (404, 410):
            raise _SessionGone()
        if status not in (200, 201) or not isinstance(body, dict) or not body.get("id"):
            raise _viya_error(status, body, "SAS didn't accept the code")
        return f"{sid}/jobs/{body['id']}"

    def _wait(self, job: str, ctl: RunControl | None = None) -> str:
        limit = job_timeout()
        deadline = time.time() + limit if limit else None
        state = "running"
        while True:
            status, body, _ = request("GET", conn=self.conn, url=f"{_base(self.conn)}/compute/sessions/{job}/state?wait={_WAIT}",
                                      accept="text/plain", timeout=_WAIT + 30)
            if status in (404, 410):
                if ctl is not None and ctl.cancel.is_set():
                    raise SasStopped("Stopped: the SAS session was ended.")
                raise _SessionGone()
            if status != 200:
                raise _viya_error(status, body, "Couldn't read the job state")
            state = str(body).strip().lower()
            if state not in _RUNNING:
                return state
            if deadline and time.time() > deadline:
                self.cancel_job(job)
                minutes = round((limit or 0) / 60)
                raise SasError(f"The SAS job ran longer than {minutes} minutes and was cancelled "
                               "(SINGLESTORE_MCP_SAS_TIMEOUT_MIN sets the limit).")

    def _log(self, job: str, kind: str = "log") -> list[dict[str, str]]:
        status, body, _ = request("GET", conn=self.conn, url=f"{_base(self.conn)}/compute/sessions/{job}/{kind}?limit=200000", timeout=120)
        if status != 200 or not isinstance(body, dict):
            return []
        return [{"line": str(i.get("line", "")), "type": str(i.get("type", "normal")).lower()} for i in body.get("items", [])]

    def run(self, code: str, *, listing: bool = False, ctl: RunControl | None = None) -> dict[str, Any]:
        """Run SAS code; returns state, log lines ({line, type}) and, with ``listing``, the listing text.
        ``ctl``: Stop sets ``ctl.cancel`` and cancels ``ctl.job`` (the log so far comes back, state "canceled")."""
        with self.lock:
            for attempt in range(2):
                if ctl is not None and ctl.cancel.is_set():
                    raise SasStopped("Stopped before it started.")
                sid = self.session()
                started = time.perf_counter()
                try:
                    self._ensure_auto()
                    if ctl is not None and ctl.cancel.is_set():
                        raise SasStopped("Stopped before it started.")
                    job = self._submit(code, sid)
                    if ctl is not None:
                        ctl.job = job
                        if ctl.cancel.is_set():  # Stop came while the code was being submitted
                            self.cancel_job(job)
                    state = self._wait(job, ctl)
                except _SessionGone:
                    self.gone()  # expired: start a new one (startup code runs again)
                    if ctl is not None and ctl.cancel.is_set():
                        raise SasStopped("Stopped: the SAS session was ended.") from None
                    if attempt:
                        raise SasError("The SAS session ended and couldn't be restarted.") from None
                    continue
                finally:
                    if ctl is not None:
                        ctl.job = None
                with ThreadPoolExecutor(2) as pool:
                    log_f = pool.submit(self._log, job)
                    lst_f = pool.submit(self._log, job, "listing") if listing else None
                    log = log_f.result()
                    lst = lst_f.result() if lst_f else []
                self.last_used = time.time()
                if self.notice:
                    log = [{"line": f"NOTE: {self.notice}", "type": "note"}, *log]
                    self.notice = None
                return {"state": state, "log": log, "listing": "\n".join(x["line"] for x in lst),
                        "ms": round((time.perf_counter() - started) * 1000, 1)}
        raise SasError("SAS session unavailable")

    # -- data

    def _data(self, path: str, timeout: float = 120) -> Any:
        def get() -> tuple[int, Any]:
            st, b, _ = request("GET", conn=self.conn, url=f"{_base(self.conn)}/compute/sessions/{self.session()}/data{path}", timeout=timeout,
                               accept=_COLLECTION)
            return st, b

        with self.lock:
            status, body = get()
            if session_gone(status, body):
                self.gone()
                status, body = get()
        if status == 404:
            raise LookupError(f"Not found in the SAS session: {urllib.parse.unquote(path)}")
        if status != 200:
            raise _viya_error(status, body, "SAS data request failed")
        return body

    def columns(self, libref: str, table: str) -> list[dict[str, Any]]:
        body = self._data(f"/{_q(libref)}/{_q(table)}/columns?limit=10000")
        return [_column(c) for c in (body.get("items", []) if isinstance(body, dict) else [])]

    def columns_many(self, pairs: list[tuple[str, str]]) -> tuple[str, dict[tuple[str, str], list[dict[str, Any]] | None]]:
        """Columns of several tables at once (the SQL Editor's autocomplete): one Compute data request per table,
        in parallel. Returns (session id, {(libref, table): columns, or None when there's no such table}).
        Other failures raise. A session that ended meanwhile is restarted once."""
        for attempt in range(2):
            with self.lock:
                sid = self.session()
            base = f"{_base(self.conn)}/compute/sessions/{sid}/data"

            def get(pair: tuple[str, str], base: str = base) -> tuple[int, Any]:
                st, b, _ = request("GET", conn=self.conn, url=f"{base}/{_q(pair[0])}/{_q(pair[1])}/columns?limit=10000", timeout=120,
                                   accept=_COLLECTION)
                return st, b

            with ThreadPoolExecutor(max(1, min(4, len(pairs)))) as pool:
                answers = list(pool.map(get, pairs))
            if any(session_gone(st, b) for st, b in answers) and not attempt:
                with self.lock:
                    if self.sid == sid:
                        self.gone()
                continue
            out: dict[tuple[str, str], list[dict[str, Any]] | None] = {}
            for pair, (st, b) in zip(pairs, answers, strict=True):
                if st == 200:
                    out[pair] = [_column(c) for c in (b.get("items", []) if isinstance(b, dict) else [])]
                elif st == 404:
                    out[pair] = None
                else:
                    raise _viya_error(st, b, f"Couldn't describe {pair[0]}.{pair[1]}")
            return sid, out
        raise SasError("SAS session unavailable")

    def tables(self, libref: str) -> list[dict[str, Any]]:
        body = self._data(f"/{_q(libref)}?limit=10000")
        out = []
        for t in body.get("items", []) if isinstance(body, dict) else []:
            out.append({"name": t.get("name"), "type": t.get("type") or "DATA", "label": t.get("label") or None})
        return [t for t in out if t["name"]]

    def table_info(self, libref: str, table: str) -> dict[str, Any]:
        body = self._data(f"/{_q(libref)}/{_q(table)}")
        return body if isinstance(body, dict) else {}

    def rows(self, libref: str, table: str, limit: int, start: int = 0) -> dict[str, Any]:
        """Columns (with SAS type, length, format, label) and ``limit`` rows from row ``start`` (0-based), in
        parallel."""
        with self.lock:
            sid = self.session()
            base = f"{_base(self.conn)}/compute/sessions/{sid}/data/{_q(libref)}/{_q(table)}"

            def get(path: str) -> Any:
                status, body, _ = request("GET", conn=self.conn, url=base + path, timeout=180, accept=_COLLECTION)
                if status != 200:
                    raise _viya_error(status, body, f"Couldn't read {libref}.{table}")
                return body

            with ThreadPoolExecutor(3) as pool:
                cols_f = pool.submit(get, "/columns?limit=10000")
                # Unformatted: numbers stay numbers (formatted values come as padded text); date formats are
                # applied here from the column metadata.
                rows_f = pool.submit(get, f"/rows?start={max(0, int(start))}&limit={max(1, int(limit))}&format=false")
                info_f = pool.submit(get, "")
                cols = [_column(c) for c in cols_f.result().get("items", [])]
                rows_body = rows_f.result()
                try:
                    info = info_f.result()
                except SasError:
                    info = {}
        names = [c["name"] for c in cols]
        fmts = [c.get("format") for c in cols]
        out = []
        for item in rows_body.get("items", []) if isinstance(rows_body, dict) else []:
            cells = item.get("cells", item) if isinstance(item, dict) else item
            out.append({n: (v.rstrip() if isinstance(v, str) else sas_sql.sas_value(v, f))
                        for n, v, f in zip(names, cells, fmts, strict=False)})
        total = info.get("rowCount") if isinstance(info, dict) else None
        return {"columns": names, "column_meta": cols, "rows": out, "total_rows": total}


class _SessionGone(Exception):
    pass


def session_gone(status: int, body: Any) -> bool:
    """Is this Compute answer "the session has ended"? Every 404 names the session in its ``path:`` detail, so a
    missing table (errorCode 5314) or libref (5301) must not count; an ended session is 410, or 404 with
    errorCode 5837 / "A session with the ID … could not be found"."""
    if status == 410:
        return True
    if status != 404:
        return False
    if isinstance(body, dict):
        if body.get("errorCode") == 5837:
            return True
        text = " ".join([str(body.get("message") or ""), *[str(d) for d in body.get("details") or [] if not str(d).startswith("path:")]])
    else:
        text = str(body)
    return bool(re.search(r"session with the id .* could not be found|session .*(not found|has ended|expired)", text, re.I))


def _q(name: str) -> str:
    return urllib.parse.quote(str(name), safe="")


def _column(c: dict[str, Any]) -> dict[str, Any]:
    fmt = c.get("format")
    if isinstance(fmt, dict):
        fmt = fmt.get("name")
    t = str(c.get("type") or "").lower()
    return {"name": c.get("name"), "type": "char" if t.startswith("char") or t in ("string", "varchar") else "num",
            "length": c.get("length"), "format": fmt or None, "label": c.get("label") or None,
            "position": (c["index"] + 1) if isinstance(c.get("index"), int) else None}


# ------------------------------------------------------------------ CAS


class Cas:
    """One CAS session (REST, on the configured CAS server), reused for every action. ``conn``: the SAS Viya
    connection it belongs to (None: whichever is active)."""

    def __init__(self, conn: str | None = None) -> None:
        self.conn = conn
        self.current_run: Any = None
        self.lock = threading.RLock()
        self.sid: str | None = None
        self.key: tuple[str, str] | None = None
        self.caslib: str | None = None
        self.loaded: set[str] = set()
        self.warming = False                          # a background warm-up (sas_prewarm) holds the lock now
        self.ended = False                            # CAS ended the session (found gone): say so once restarted

    def gone(self) -> None:
        if self.sid:
            self.ended = True
        self.sid = None

    def _root(self) -> str:
        server = sas_viya.settings(self.conn).get("cas_server") or "cas-shared-default"
        return f"{_base(self.conn)}/{server}-http"

    def session(self) -> str:
        s = sas_viya.settings(self.conn)
        key = (s["url"], s.get("cas_server") or "cas-shared-default")
        with self.lock:
            if self.sid and self.key == key:
                return self.sid
            status, body, _ = request("POST", f"{self._root()}/cas/sessions", body={}, timeout=60, conn=self.conn)
            if status not in (200, 201) or not isinstance(body, dict) or not body.get("session"):
                raise _viya_error(status, body, f"Couldn't start a CAS session on {key[1]}")
            self.sid, self.key, self.caslib, self.loaded = body["session"], key, None, set()
            if self.ended:
                self.ended = False
                netguard.notice("sas", f"CAS session ({s.get('name') or 'SAS Viya'}) restarted: it had ended "
                                       "(connection loss or idle timeout). Session tables from before are gone.")
            return self.sid

    def close(self, force: bool = False) -> None:
        """End the session (``force``: without waiting for an action that is running)."""
        if force:
            self._close()
            return
        with self.lock:
            self._close()

    def _close(self) -> None:
        sid, self.sid = self.sid, None
        if sid:
            try:
                request("DELETE", f"{self._root()}/cas/sessions/{sid}", timeout=15, conn=self.conn)
            except Exception:  # noqa: BLE001
                pass

    def cancel(self) -> bool:
        """Cancel the action running in the session: the documented ``DELETE /cas/sessions/{id}/actions`` (CAS
        sends itself a cancelAction). True when CAS accepted it."""
        sid = self.sid
        if not sid:
            return False
        try:
            status, _, _ = request("DELETE", f"{self._root()}/cas/sessions/{sid}/actions", timeout=30, conn=self.conn)
        except (SasError, OSError, http.client.HTTPException):
            return False
        return 200 <= status < 300

    def action(self, name: str, params: dict[str, Any] | None = None, *, check: bool = True,
               timeout: float | None = None) -> dict[str, Any]:
        """Run one CAS action: {results, log, severity, status, reason}. Raises SasError on errors when ``check``.
        ``timeout``: seconds (default ``job_timeout()``)."""
        if timeout is None:
            timeout = job_timeout() or 7 * 24 * 3600.0
        with self.lock:
            for attempt in range(2):
                sid = self.session()
                status, body, _ = request("POST", f"{self._root()}/cas/sessions/{sid}/actions/{name}",
                                          body=params or {}, timeout=timeout, conn=self.conn)
                if status in (404, 410) and not attempt:
                    self.gone()
                    continue
                break
        if status not in (200, 201) or not isinstance(body, dict):
            raise _viya_error(status, body, f"CAS action {name} failed")
        disp = body.get("disposition") or {}
        log = body.get("log")
        if log is None and isinstance(body.get("logEntries"), list):
            log = "\n".join(str(e.get("message", "")) for e in body["logEntries"])
        out = {"results": body.get("results") or {}, "log": log or "", "severity": disp.get("severity") or "Normal",
               "status": disp.get("formattedStatus") or disp.get("reason") or "", "status_code": disp.get("statusCode", 0)}
        if check and str(out["severity"]).lower() == "error":
            errors = [x for x in out["log"].splitlines() if x.startswith("ERROR")]
            raise SasError(" ".join(errors[:3]) or out["status"] or f"CAS action {name} failed")
        return out

    def ensure_actionset(self, name: str) -> None:
        if name in self.loaded:
            return
        self.action("builtins.loadActionSet", {"actionSet": name})
        self.loaded.add(name)

    def set_caslib(self, caslib: str | None) -> None:
        if caslib and caslib != self.caslib:
            self.action("sessionProp.setSessOpt", {"caslib": caslib})
            self.caslib = caslib


def cas_table(obj: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A CAS result table in the REST shape ({schema: [...], rows: [[...]]}) as (columns, row dicts)."""
    if not isinstance(obj, dict):
        return [], []
    schema = obj.get("schema") or obj.get("columns") or []
    cols = []
    for c in schema:
        if isinstance(c, dict):
            cols.append({"name": c.get("name"), "label": c.get("label") or None, "type": c.get("type"),
                         "format": c.get("format") or None, "length": c.get("width") or c.get("length")})
        else:
            cols.append({"name": str(c), "label": None, "type": None, "format": None, "length": None})
    names = [c["name"] for c in cols]
    rows = []
    for r in obj.get("rows") or []:
        if isinstance(r, dict):
            rows.append(r)
        else:
            rows.append({n: v for n, v in zip(names, r, strict=False)})
    return cols, rows


def first_table(results: dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in results:
            return results[n]
    for v in results.values():
        if isinstance(v, dict) and ("rows" in v or "schema" in v):
            return v
    return None


# ------------------------------------------------------------------ sessions per SAS Viya connection

_sessions: dict[str, tuple[Compute, Cas]] = {}
_sessions_lock = threading.Lock()


def sessions(conn: str | None = None) -> tuple[Compute, Cas]:
    """The Compute and CAS session objects of SAS Viya connection ``conn`` (default: the active one): one pair per
    connection, so SQL Editor tabs pinned to the same connection share its sessions."""
    name = conn or sas_viya.active_name() or ""
    with _sessions_lock:
        pair = _sessions.get(name)
        if pair is None:
            pair = _sessions[name] = (Compute(name or None), Cas(name or None))
        return pair


def all_sessions() -> list[tuple[str, Compute, Cas]]:
    with _sessions_lock:
        return [(name, c, k) for name, (c, k) in _sessions.items()]


class _Active:
    """``compute`` / ``cas``: the active connection's session object (attributes are read and set on it)."""

    def __init__(self, index: int) -> None:
        object.__setattr__(self, "_index", index)

    def _target(self) -> Any:
        return sessions()[object.__getattribute__(self, "_index")]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target(), name, value)


compute: Compute = _Active(0)  # type: ignore[assignment]
cas: Cas = _Active(1)  # type: ignore[assignment]


def auto_spec(conn: str | None = None) -> dict[str, Any]:
    """What the automatic SingleStore library should be: ``{libref, profile, password, key, expires}``, or
    ``{skip: why}`` when there's none (turned off, no active connection, a URL connection, no token).
    ``conn``: the SAS Viya connection whose settings (libref, on/off) apply."""
    s = sas_viya.settings(conn)
    libref = (s.get("libref") or "S2").upper()
    if not s.get("auto_libname", True):
        return {"skip": f"Automatic {libref} library turned off (Connections → SAS Viya → Edit).", "libref": libref,
                "enabled": False}
    p = connections.active()
    if p is None:
        return {"skip": "No active SingleStore connection.", "libref": libref}
    if p.url or not p.host:
        return {"skip": f"No {libref} library: the active SingleStore connection {p.name} uses a connection URL.", "libref": libref}
    try:
        pw = connections.password_for(p)
    except connections.NeedsSignIn as exc:
        return {"skip": f"No {libref} library: {exc}", "libref": libref}
    exp = connections._token_expiry(pw) if p.auth != "password" else None
    digest = hashlib.sha256(pw.encode()).hexdigest()
    key = (libref, p.name, p.host, int(p.port), p.user, p.database, p.auth, digest)
    return {"libref": libref, "profile": p, "password": pw, "key": key, "expires": exp}


def _on_singlestore_change() -> None:
    # The active SingleStore connection changed: the library is re-assigned before the next SAS run (and the
    # library list re-read). Nothing runs here: the listener is called on the thread that switched.
    for _, c, _k in all_sessions():
        c.libraries = None


connections.on_change(_on_singlestore_change)


# ------------------------------------------------------------------ SAS 9 through SAS/CONNECT


SAS9_DOWN_HINT = ("A SAS 9 server that is shut down (CAUKI, for example, every evening at 22:00) doesn't answer: "
                  "that's expected then.")


def sas9_connect(force: bool = False, conn: str | None = None) -> dict[str, Any]:
    """Sign on to the SAS 9 server from the Workspace's Compute session (the connection's "SAS 9 sign-on code",
    run with NOSOURCE like the startup code; its log is never kept) and assign its libraries through remote
    library services (``libname X server=<server> slibref=X``). Done once per SAS session (``force``: again).
    Returns ``{ok, server, assigned, skipped, ms}``; raises SasError when the sign-on fails."""
    s = sas_viya.settings(conn)
    compute = sessions(conn)[0]
    code, server = (s.get("sas9_signon") or "").strip(), (s.get("sas9_server") or "").strip().upper()
    if not code or not server:
        raise SasError("SAS 9 isn't set up: add the SAS 9 sign-on code and server in Connections → SAS Viya → Edit.")
    wanted = sas_sql.parse_sas9_libraries(s.get("sas9_libraries"))
    with compute.lock:
        compute.session()
        if compute.sas9 and compute.sas9.get("ok") and compute.sas9.get("server") == server and not force:
            return compute.sas9
        started = time.perf_counter()
        for attempt in range(2):
            try:
                res = _sas9_sign_on(compute, code, server, wanted)
                break
            except _SessionGone:
                compute.gone()
                if attempt:
                    raise SasError("The SAS session ended and couldn't be restarted.") from None
                compute.session()
        res["ms"] = round((time.perf_counter() - started) * 1000)
        compute.sas9 = res
        compute.libraries = None
    if not res["ok"]:
        raise SasError(res["error"])
    return res


def _sas9_sign_on(c: Compute, code: str, server: str, wanted: list[tuple[str, str]]) -> dict[str, Any]:
    # The marker: a new session's first job log also holds the compute context's autoexec (errors included).
    wrapped = f"options nosource nosource2 nonotes nomprint nosymbolgen;\n%put S2SAS9_BEGIN;\n{code}\n;options source notes;"
    job = c._submit(wrapped, c.sid or "")
    state = c._wait(job)
    lines = [x for x in c._log(job)]
    start = next((i for i, x in enumerate(lines) if x["line"].startswith("S2SAS9_BEGIN")), 0)
    counts = sas_sql.log_counts(lines[start:])  # only counted: the included file's log is never kept
    c._forget_job(job)
    if state in ("failed", "canceled") or counts["errors"]:
        first = sas_sql.mask_secrets(counts["first_error"] or f"SAS job {state}")[:300]
        return {"ok": False, "server": server, "error": f"Couldn't sign on to SAS 9 ({server}): {first} {SAS9_DOWN_HINT}"}
    # The remote libraries: dictionary.libnames on the SAS 9 side, copied back through its WORK.
    listing = (f"rsubmit {server} wait=yes;\n"
               "  proc sql noprint; create table work._s2r9 as select distinct libname, engine from dictionary.libnames; quit;\n"
               "endrsubmit;\n"
               f"libname _s2r9w server={server} slibref=work;\n"
               "data work._s2r9; set _s2r9w._s2r9; run;\n"
               "libname _s2r9w clear;")
    res = c.run(listing)
    counts = sas_sql.log_counts(res["log"])
    if counts["errors"]:
        return {"ok": False, "server": server,
                "error": f"Signed on to {server}, but couldn't list its libraries: {sas_sql.mask_secrets(counts['first_error'])[:300]}"}
    remote = [str(r.get("libname") or r.get("LIBNAME") or "").strip().upper()
              for r in c.rows("WORK", "_S2R9", 5000)["rows"]]
    remote = sorted({x for x in remote if x})
    if wanted:
        pairs = [(loc, rem) for loc, rem in wanted if rem in remote]
        missing = [rem for _, rem in wanted if rem not in remote]
    else:
        pairs = [(x, x) for x in remote if x not in sas_sql.SAS9_SKIP and not x.startswith("_")]
        missing = []
    explicit = {loc for loc, _ in wanted}
    lines = ["%macro _s2r9a(local, remote, always);",
             "  %if &always or %sysfunc(libref(&local)) ne 0 %then %do;",
             f"    libname &local server={server} slibref=&remote;",
             "    %if %sysfunc(libref(&local)) ne 0 %then %put S2R9FAIL &local;",
             "  %end;",
             "  %else %put S2R9SKIP &local;",
             "%mend;"]
    lines += [f"%_s2r9a({loc}, {rem}, {1 if loc in explicit else 0})" for loc, rem in pairs]
    lines.append("%sysmacdelete _s2r9a;")
    res = c.run("\n".join(lines))
    log = [x["line"] for x in res["log"]]
    skipped = sorted({ln.split()[1] for ln in log if ln.startswith("S2R9SKIP ")})
    failed = sorted({ln.split()[1] for ln in log if ln.startswith("S2R9FAIL ")})
    assigned = [{"local": loc, "remote": rem} for loc, rem in pairs if loc not in skipped and loc not in failed]
    return {"ok": True, "server": server, "assigned": assigned, "skipped": skipped, "failed": failed,
            "missing": missing, "remote": remote}


def _probe() -> None:
    """Health probe while SAS Viya is unreachable: one unauthenticated request (any HTTP answer means it's back),
    then the background warm-up / keep-alive check right away (``sas_prewarm``)."""
    url = sas_viya.settings().get("url")
    if not url:
        return
    request("GET", f"{url}/SASLogon/", accept="text/html, */*", timeout=8, auth=False)
    from . import sas_prewarm

    sas_prewarm.prewarm(retry=True)


netguard.set_probe("sas", _probe)


def reset(conn: str | None = None) -> None:
    """End connection ``conn``'s two sessions (default: the active one's); the next run starts fresh ones (the
    startup code runs again). Doesn't wait for a run that holds them (a Stop that didn't work)."""
    c, k = sessions(conn)
    c.close(force=True)
    k.close(force=True)


def sas_code_log(lines: list[dict[str, str]]) -> str:
    """The job log as text, passwords masked."""
    return sas_sql.mask_secrets("\n".join(x["line"] for x in lines))


def strip_echo(lines: list[dict[str, str]], marker: str) -> list[dict[str, str]]:
    """Drop the log lines before ``marker`` (e.g. housekeeping code before the user's statement)."""
    for i, x in enumerate(lines):
        if marker in x["line"]:
            return lines[i:]
    return lines


def library_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """dictionary.libnames rows (one per libref and info line) → one entry per libref with engine and database."""
    libs: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = str(r.get("libname") or "").strip()
        if not name:
            continue
        lib = libs.setdefault(name, {"name": name, "engine": str(r.get("engine") or "").strip(),
                                     "path": str(r.get("path") or "").strip() or None, "database": None,
                                     "server": None, "schema": None, "user": None, "remote_server": None})
        sysname = str(r.get("sysname") or "").strip().lower()
        value = str(r.get("sysvalue") or "").strip()
        if sysname in ("database", "data base"):
            lib["database"] = value or None
        elif sysname == "schema":
            lib["schema"] = value or None
        elif sysname == "user":
            lib["user"] = value or None
        elif sysname == "accessed through server":
            lib["remote_server"] = value or None
    for lib in libs.values():
        engine = lib["engine"].upper()
        lib["singlestore"] = engine in sas_sql.SAS_ENGINES_SINGLESTORE
        lib["singlestore_kind"] = "access" if lib["singlestore"] else None
        lib["sas9"] = engine in sas_sql.SAS_ENGINES_REMOTE
        if lib["singlestore"]:
            lib["server"] = lib["path"]
        lib.update(sas_sql.singlestore_badge(lib["singlestore_kind"], lib["database"], lib["server"]))
    return sorted(libs.values(), key=lambda x: (x["name"] != "WORK", x["name"]))


def _mark(libs: list[dict[str, Any]], compute: Compute) -> list[dict[str, Any]]:
    """The automatic SingleStore library and the SAS 9 libraries this Workspace assigned, marked as such."""
    auto = compute.auto_lib or {}
    sas9 = {a["local"]: a["remote"] for a in (compute.sas9 or {}).get("assigned") or []}
    for lib in libs:
        # (the startup code may have assigned the same libref elsewhere since: then it isn't the automatic one)
        if (auto.get("assigned") and lib["name"] == auto.get("libref") and lib["singlestore"]
                and str(lib.get("server") or "").lower() == str(auto.get("host") or "").lower()
                and str(lib.get("database") or "").lower() == str(auto.get("database") or "").lower()):
            lib.update(auto=True, connection=auto.get("connection"), port=auto.get("port"))
            lib.update(sas_sql.singlestore_badge("access", lib["database"], lib["server"], auto=auto.get("connection")))
        if lib["sas9"]:
            lib["remote_libref"] = sas9.get(lib["name"])
            lib["badge"], lib["badge_style"] = "9", "sas9"
            lib["badge_title"] = (f"SAS 9 library on {lib.get('remote_server') or '?'} (SAS/CONNECT remote library services)"
                                  + (f", remote libref {lib['remote_libref']}" if lib.get("remote_libref") else ""))
    return libs


_LIBS_CODE = """proc sql noprint;
  create table WORK._S2LIBS as select libname, engine, path, sysname, sysvalue
  from dictionary.libnames where level <= 1;
quit;"""


def libraries(force: bool = False, conn: str | None = None) -> list[dict[str, Any]]:
    """The librefs assigned in the Compute session (compute context + startup code), engine and database."""
    compute = sessions(conn)[0]
    with compute.lock:
        if compute.sid:
            compute.ensure_auto()  # may re-assign it (another SingleStore connection) and forget the list
        if compute.libraries is not None and not force:
            return compute.libraries
        res = compute.run(_LIBS_CODE)
        if res["state"] in ("error", "failed"):
            counts = sas_sql.log_counts(res["log"])
            raise SasError(counts["first_error"] or "Couldn't list the SAS libraries")
        data = compute.rows("WORK", "_S2LIBS", 5000)
        compute.libraries = _mark(library_rows([{k.lower(): v for k, v in r.items()} for r in data["rows"]]), compute)
        return compute.libraries


def is_name(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][\w]{0,31}", s or ""))
