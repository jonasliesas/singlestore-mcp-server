"""SAS Viya REST client for the Workspace: one SAS Compute session and one CAS session, kept alive and reused.

Signs in with the Viya sign-in from ``sas_viya`` (OAuth token, ``sas.json``, compute context). Used by the SQL
Editor's "Run as" PROC SQL / FedSQL / FedSQL in CAS and by the Schema Explorer's SAS sources.

Every call to Viya is a round trip (and the token, TLS and proxy add to the first), so:

- HTTP connections are kept open (``http.client`` keep-alive, one per thread and host);
- the Compute session is created once and reused (one job at a time, under a lock) until it expires, when
  it's recreated and the startup code runs again;
- job state is long-polled (``?wait=``), result tables are read through the Compute data API in parallel.

Startup code (``sas.json`` → ``startup_code``, set in Connections) runs once per session with
``options nosource nosource2`` and its log is never kept or shown (it typically ``%include``s libname files
that hold passwords): only the number of errors and the first error line (passwords masked) are reported.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import sas_sql, sas_viya
from .sas_sql import SasError

JOB_TIMEOUT = 900          # seconds a single job may run
_WAIT = 20                 # long-poll seconds per state request
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
    if conn is None:
        conn = pool[key] = _open(scheme, host, port, proxy, verify, timeout)
    conn.timeout = timeout
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    if proxy and scheme == "http":
        target = url
    return conn, target


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
    for _ in range(4):
        conn, target = _conn(url, verify, timeout, direct)
        reused = conn.sock is not None
        sent = False
        try:
            if conn.sock is None:
                conn.connect()
            sent = True
            conn.request(method, target, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.will_close:
                _drop(url)
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw
        except TimeoutError:
            _drop(url)
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
            auth: bool = True, raw: bool = False) -> tuple[int, Any, dict[str, str]]:
    """One HTTP request to Viya on a kept-alive connection: (status, parsed JSON or text, headers).
    ``raw``: a successful (2xx) answer's body as bytes, as it came (file contents)."""
    s = sas_viya.settings()
    data = None
    hdrs = {"Accept": accept, **(headers or {})}
    if body is not None:
        text = body if isinstance(body, (bytes, str)) else json.dumps(body)
        data = text.encode() if isinstance(text, str) else text
        hdrs["Content-Type"] = content_type
    if auth:
        hdrs["Authorization"] = f"Bearer {sas_viya.token(120)}"
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
        raise SasError(f"Couldn't reach SAS Viya at {s['url']}: {last}") from last
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


def _base() -> str:
    url = sas_viya.settings()["url"]
    if not url:
        raise sas_viya.NeedsSignIn("SAS Viya isn't set up: enter its address in the Connections window (SAS Viya).")
    return url


# ------------------------------------------------------------------ Compute


class Compute:
    """One SAS Compute session on the configured compute context, reused for every run."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sid: str | None = None
        self.key: tuple[str, str] | None = None
        self.started: float | None = None
        self.startup: dict[str, Any] | None = None
        self.libraries: list[dict[str, Any]] | None = None
        self.last_used: float | None = None

    # -- session

    def _context_id(self, name: str) -> str:
        q = urllib.parse.quote(f'eq(name,"{name}")')
        status, body, _ = request("GET", f"{_base()}/compute/contexts?filter={q}&limit=5")
        if status != 200:
            raise _viya_error(status, body, "Couldn't list the compute contexts")
        items = body.get("items", []) if isinstance(body, dict) else []
        if not items:
            raise LookupError(f"No compute context named {name!r}: pick one in the Connections window (SAS Viya → Test).")
        return items[0]["id"]

    def session(self) -> str:
        s = sas_viya.settings()
        key = (s["url"], s["compute_context"])
        with self.lock:
            if self.sid and self.key == key:
                return self.sid
            if self.sid:
                self.close()
            ctx = self._context_id(s["compute_context"])
            status, body, _ = request("POST", f"{_base()}/compute/contexts/{ctx}/sessions",
                                      body={"name": "SingleStore Workspace", "description": "SQL Editor / Schema Explorer"},
                                      accept="application/vnd.sas.compute.session+json, application/json", timeout=180)
            if status not in (200, 201) or not isinstance(body, dict) or not body.get("id"):
                raise _viya_error(status, body, f"Couldn't start a SAS session on {s['compute_context']!r}")
            self.sid, self.key, self.started = body["id"], key, time.time()
            self.libraries = None
            self.startup = self._run_startup(s.get("startup_code") or "")
            return self.sid

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

    def close(self) -> None:
        with self.lock:
            sid, self.sid, self.key, self.libraries = self.sid, None, None, None
            if sid:
                try:
                    request("DELETE", f"{_base()}/compute/sessions/{sid}", timeout=15)
                except Exception:  # noqa: BLE001 - it expires anyway
                    pass

    # -- jobs

    def _submit(self, code: str, sid: str) -> str:
        status, body, _ = request("POST", f"{_base()}/compute/sessions/{sid}/jobs",
                                  body={"code": code.splitlines() or [""]},
                                  accept="application/vnd.sas.compute.job+json, application/json", timeout=60)
        if status in (404, 410):
            raise _SessionGone()
        if status not in (200, 201) or not isinstance(body, dict) or not body.get("id"):
            raise _viya_error(status, body, "SAS didn't accept the code")
        return f"{sid}/jobs/{body['id']}"

    def _wait(self, job: str) -> str:
        deadline = time.time() + JOB_TIMEOUT
        state = "running"
        while True:
            status, body, _ = request("GET", f"{_base()}/compute/sessions/{job}/state?wait={_WAIT}",
                                      accept="text/plain", timeout=_WAIT + 30)
            if status in (404, 410):
                raise _SessionGone()
            if status != 200:
                raise _viya_error(status, body, "Couldn't read the job state")
            state = str(body).strip().lower()
            if state not in _RUNNING:
                return state
            if time.time() > deadline:
                request("PUT", f"{_base()}/compute/sessions/{job}/state?value=canceled", body="", accept="text/plain",
                        content_type="text/plain")
                raise SasError(f"The SAS job ran longer than {JOB_TIMEOUT // 60} minutes and was cancelled.")

    def _log(self, job: str, kind: str = "log") -> list[dict[str, str]]:
        status, body, _ = request("GET", f"{_base()}/compute/sessions/{job}/{kind}?limit=200000", timeout=120)
        if status != 200 or not isinstance(body, dict):
            return []
        return [{"line": str(i.get("line", "")), "type": str(i.get("type", "normal")).lower()} for i in body.get("items", [])]

    def run(self, code: str, *, listing: bool = False) -> dict[str, Any]:
        """Run SAS code; returns state, log lines ({line, type}) and, with ``listing``, the listing text."""
        with self.lock:
            for attempt in range(2):
                sid = self.session()
                started = time.perf_counter()
                try:
                    job = self._submit(code, sid)
                    state = self._wait(job)
                except _SessionGone:
                    self.sid = None  # expired: start a new one (startup code runs again)
                    if attempt:
                        raise SasError("The SAS session ended and couldn't be restarted.") from None
                    continue
                with ThreadPoolExecutor(2) as pool:
                    log_f = pool.submit(self._log, job)
                    lst_f = pool.submit(self._log, job, "listing") if listing else None
                    log = log_f.result()
                    lst = lst_f.result() if lst_f else []
                self.last_used = time.time()
                return {"state": state, "log": log, "listing": "\n".join(x["line"] for x in lst),
                        "ms": round((time.perf_counter() - started) * 1000, 1)}
        raise SasError("SAS session unavailable")

    # -- data

    def _data(self, path: str, timeout: float = 120) -> Any:
        def get() -> tuple[int, Any]:
            st, b, _ = request("GET", f"{_base()}/compute/sessions/{self.session()}/data{path}", timeout=timeout,
                               accept=_COLLECTION)
            return st, b

        with self.lock:
            status, body = get()
            if session_gone(status, body):
                self.sid = None
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
            base = f"{_base()}/compute/sessions/{sid}/data"

            def get(pair: tuple[str, str], base: str = base) -> tuple[int, Any]:
                st, b, _ = request("GET", f"{base}/{_q(pair[0])}/{_q(pair[1])}/columns?limit=10000", timeout=120,
                                   accept=_COLLECTION)
                return st, b

            with ThreadPoolExecutor(max(1, min(4, len(pairs)))) as pool:
                answers = list(pool.map(get, pairs))
            if any(session_gone(st, b) for st, b in answers) and not attempt:
                with self.lock:
                    if self.sid == sid:
                        self.sid = None
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

    def rows(self, libref: str, table: str, limit: int) -> dict[str, Any]:
        """Columns (with SAS type, length, format, label) and the first ``limit`` rows, in parallel."""
        with self.lock:
            sid = self.session()
            base = f"{_base()}/compute/sessions/{sid}/data/{_q(libref)}/{_q(table)}"

            def get(path: str) -> Any:
                status, body, _ = request("GET", base + path, timeout=180, accept=_COLLECTION)
                if status != 200:
                    raise _viya_error(status, body, f"Couldn't read {libref}.{table}")
                return body

            with ThreadPoolExecutor(3) as pool:
                cols_f = pool.submit(get, "/columns?limit=10000")
                # Unformatted: numbers stay numbers (formatted values come as padded text); date formats are
                # applied here from the column metadata.
                rows_f = pool.submit(get, f"/rows?start=0&limit={max(1, int(limit))}&format=false")
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
    """One CAS session (REST, on the configured CAS server), reused for every action."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sid: str | None = None
        self.key: tuple[str, str] | None = None
        self.caslib: str | None = None
        self.loaded: set[str] = set()

    def _root(self) -> str:
        server = sas_viya.settings().get("cas_server") or "cas-shared-default"
        return f"{_base()}/{server}-http"

    def session(self) -> str:
        s = sas_viya.settings()
        key = (s["url"], s.get("cas_server") or "cas-shared-default")
        with self.lock:
            if self.sid and self.key == key:
                return self.sid
            status, body, _ = request("POST", f"{self._root()}/cas/sessions", body={}, timeout=60)
            if status not in (200, 201) or not isinstance(body, dict) or not body.get("session"):
                raise _viya_error(status, body, f"Couldn't start a CAS session on {key[1]}")
            self.sid, self.key, self.caslib, self.loaded = body["session"], key, None, set()
            return self.sid

    def close(self) -> None:
        with self.lock:
            sid, self.sid = self.sid, None
            if sid:
                try:
                    request("DELETE", f"{self._root()}/cas/sessions/{sid}", timeout=15)
                except Exception:  # noqa: BLE001
                    pass

    def action(self, name: str, params: dict[str, Any] | None = None, *, check: bool = True,
               timeout: float = JOB_TIMEOUT) -> dict[str, Any]:
        """Run one CAS action: {results, log, severity, status, reason}. Raises SasError on errors when ``check``."""
        with self.lock:
            for attempt in range(2):
                sid = self.session()
                status, body, _ = request("POST", f"{self._root()}/cas/sessions/{sid}/actions/{name}",
                                          body=params or {}, timeout=timeout)
                if status in (404, 410) and not attempt:
                    self.sid = None
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


compute = Compute()
cas = Cas()


def reset() -> None:
    """End both sessions (the next run starts fresh ones; the startup code runs again)."""
    compute.close()
    cas.close()


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
                                     "server": None, "schema": None})
        sysname = str(r.get("sysname") or "").strip().lower()
        value = str(r.get("sysvalue") or "").strip()
        if sysname in ("database", "data base"):
            lib["database"] = value or None
        elif sysname == "schema":
            lib["schema"] = value or None
    for lib in libs.values():
        lib["singlestore"] = lib["engine"].upper() in sas_sql.SAS_ENGINES_SINGLESTORE
        if lib["singlestore"]:
            lib["server"] = lib["path"]
    return sorted(libs.values(), key=lambda x: (x["name"] != "WORK", x["name"]))


_LIBS_CODE = """proc sql noprint;
  create table WORK._S2LIBS as select libname, engine, path, sysname, sysvalue
  from dictionary.libnames where level <= 1;
quit;"""


def libraries(force: bool = False) -> list[dict[str, Any]]:
    """The librefs assigned in the Compute session (compute context + startup code), engine and database."""
    with compute.lock:
        if compute.libraries is not None and not force:
            return compute.libraries
        res = compute.run(_LIBS_CODE)
        if res["state"] in ("error", "failed"):
            counts = sas_sql.log_counts(res["log"])
            raise SasError(counts["first_error"] or "Couldn't list the SAS libraries")
        data = compute.rows("WORK", "_S2LIBS", 5000)
        compute.libraries = library_rows([{k.lower(): v for k, v in r.items()} for r in data["rows"]])
        return compute.libraries


def is_name(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][\w]{0,31}", s or ""))
