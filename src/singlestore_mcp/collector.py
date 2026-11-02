"""The Studio repository's collector: one process next to the hub that watches the cluster for every user.

    python -m singlestore_mcp.collector --stdin-config      # started by the hub (hub.collector)

With the service account (``svc_studio_collector``: PROCESS + SHOW METADATA to see every user's queries, ALL on the
repository database) it

- samples the Cluster Monitor's metrics once a minute (``metrics_store``: 30 days raw, 1 year in 10-minute rows),
- copies new query history events (``history_store.sync``) and the plan cache (``snapshot_plans``) once a minute,
- keeps all of it in SQLite files in its own folder and in the repository database (``repository``: write-through,
  with the SQLite outbox as the buffer while the cluster is down, and a restore onto an empty folder),
- prunes both sides hourly (query runs and plans 90 days, plan-cache samples 30 days),

and answers the Studios' reads on 127.0.0.1 (``POST /call``, ``remote`` on the Studio side). Each Studio has a token
the hub signed (``sign``): the SAS user, whether they are in the monitor group, and whether they are a Studio admin.
Members of the monitor group and admins see every user's queries; everyone else sees only the queries of the
SingleStore user their own connection signs in as, and no plan cache (it has no user). Metrics: everyone.

Settings come as one JSON line on stdin (never the environment or the command line)::

    {"key": "<token key>", "home": "<folder>", "database": "SSSTUDIO",
     "singlestore": {"host": "...", "port": 3306, "user": "svc_studio_collector", "password": "...",
                     "ssl_disabled": false, "ssl_ca": null},
     "retention": {"runs_days": 90, ...}, "interval_s": 60}

It prints ``COLLECTOR-READY <port> <cluster>`` and runs until its stdin closes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 64 * 1024
FLUSH_EVERY_S = 15
SYNC_EVERY_S = 60
RETAIN_EVERY_S = 3600


# ------------------------------------------------------------------ tokens (signed by the hub, checked here)

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(key: bytes, user: str, monitor: bool, admin: bool, hub: bool = False) -> str:
    """A Studio's token: who it is for and what they may see, signed with the hub's collector key (``hub``: the
    hub's own, for the status page)."""
    claims = {"u": user, "m": bool(monitor), "a": bool(admin), "i": round(time.time())}
    if hub:
        claims["hub"] = True
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(key, body.encode(), hashlib.sha256).digest())}"


def verify(key: bytes, token: str) -> dict[str, Any] | None:
    body, _, mac = (token or "").partition(".")
    if not body or not mac:
        return None
    if not hmac.compare_digest(_b64(hmac.new(key, body.encode(), hashlib.sha256).digest()), mac):
        return None
    try:
        claims = json.loads(_unb64(body))
    except ValueError:
        return None
    return claims if isinstance(claims, dict) and claims.get("u") else None


# ------------------------------------------------------------------ what a Studio may ask

def _see_all(claims: dict[str, Any]) -> bool:
    return bool(claims.get("m") or claims.get("a"))


def _calls() -> dict[str, tuple[Any, tuple[str, ...], str]]:
    """name -> (function, allowed arguments, kind): "runs" (filtered to the caller's SingleStore user unless they see
    all), "plans" (only for those who see all; others get ``empty``), "open" (metrics: everyone)."""
    from . import history_store as hs
    from . import metrics_store as ms

    return {
        "trends": (hs.trends, ("days", "now"), "runs"),
        "bucket_runs": (hs.bucket_runs, ("bucket", "failed_only", "limit"), "runs"),
        "kept_runs": (hs.kept_runs, ("before", "since", "min_ms", "limit"), "runs"),
        "kept_run": (hs.kept_run, ("event_key",), "runs"),
        "usage": (hs.usage, ("since", "limit"), "runs"),
        "origin_runs": (hs.origin_runs, ("origin_label", "since"), "runs"),
        "count_runs": (hs.count_runs, ("stmts",), "runs"),
        "stored_plans": (hs.stored_plans, ("since", "limit"), "plans"),
        "stored_plan": (hs.stored_plan, ("plan_key",), "plans"),
        "plan_executions": (hs.plan_executions, ("since",), "plans"),
        "metrics_history": (ms.history, ("range_key",), "open"),
        "metrics_trends": (ms.current_trends, ("window_s",), "open"),
        "metrics_sample": (lambda: ms.sample_once(force=False), (), "open"),
    }


_EMPTY = {"stored_plans": [], "stored_plan": None, "plan_executions": []}


def answer(fn: str, args: dict[str, Any], claims: dict[str, Any], s2_user: str | None) -> Any:
    """Run one read for a Studio, with its user's view applied."""
    calls = _calls()
    if fn not in calls:
        raise PermissionError(f"Unknown call {fn!r}.")
    func, allowed, kind = calls[fn]
    kwargs = {k: v for k, v in (args or {}).items() if k in allowed}
    if kind == "plans" and not _see_all(claims):
        return _EMPTY[fn]
    if kind == "runs" and not _see_all(claims):
        # Not in the monitor group: only the runs of the SingleStore user this Studio's connection signs in as
        # (nobody's when it couldn't tell).
        kwargs["user"] = (s2_user or "").strip() or "\x00"
    from . import remote

    with remote.serving():
        return func(**kwargs)


# ------------------------------------------------------------------ the process

class Collector:
    def __init__(self, cfg: dict[str, Any]) -> None:
        from . import connections, history_store
        from .repository import Repository

        self.key = str(cfg["key"]).encode()
        self.interval_s = int(cfg.get("interval_s") or 60)
        self.retention = cfg.get("retention") or {}
        self.source = history_store.source_id()
        p = connections.active()
        self.repo = Repository(connections.connect_kwargs(p), cfg.get("database") or "SSSTUDIO", cfg["home"], self.retention)
        self.stop = threading.Event()
        self.status: dict[str, Any] = {"started": round(time.time(), 1), "source": self.source, "last_sync": None,
                                       "last_error": None, "calls": 0, "denied": 0}
        self.httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ start-up
    def prepare(self) -> None:
        """The SQLite stores' tables, the repository's tables, a restore onto an empty folder, then the triggers."""
        from . import history_store, metrics_store

        history_store.connect().close()
        metrics_store.connect().close()
        try:
            self.repo.ensure_schema()
            if self.repo.local_empty():
                self.repo.restore()
        except Exception as exc:  # noqa: BLE001 - the cluster may be down: collect into SQLite, send later
            self.repo.status["last_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        self.repo.install_triggers()

    def _every(self, seconds: float, fn: Any, name: str, first: float = 0.0) -> None:
        def run() -> None:
            if self.stop.wait(first):
                return
            while not self.stop.is_set():
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 - keep collecting
                    self.status["last_error"] = f"{name}: {type(exc).__name__}: {str(exc)[:240]}"
                if self.stop.wait(seconds):
                    return

        threading.Thread(target=run, name=f"collector-{name}", daemon=True).start()

    def _sync(self) -> None:
        from . import history_store

        history_store.sync(force=True)
        history_store.snapshot_plans()
        self.status["last_sync"] = round(time.time(), 1)

    def _retain(self) -> None:
        from . import history_store

        r = self.repo.retention
        history_store.prune(r["runs_days"], r["plans_days"], r["samples_days"])
        try:
            self.repo.retain()
        except Exception as exc:  # noqa: BLE001 - again next hour
            self.repo.status["last_error"] = f"retain: {type(exc).__name__}: {str(exc)[:240]}"

    def _flush(self) -> None:
        try:
            self.repo.flush()
        except Exception:  # noqa: BLE001 - in repo.status; the rows stay in the outbox
            pass

    def start(self) -> int:
        from . import metrics_store

        self.prepare()
        metrics_store.start()
        self._every(SYNC_EVERY_S, self._sync, "sync", first=5)
        self._every(FLUSH_EVERY_S, self._flush, "flush", first=FLUSH_EVERY_S)
        self._every(RETAIN_EVERY_S, self._retain, "retain", first=120)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, name="collector-http", daemon=True).start()
        return self.httpd.server_address[1]

    def close(self) -> None:
        from . import metrics_store

        self.stop.set()
        metrics_store.stop(timeout=2.0)
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        try:
            self.repo.flush()
        except Exception:  # noqa: BLE001 - sent by the next collector
            pass
        self.repo.close()

    def report(self) -> dict[str, Any]:
        from . import metrics_store

        m = metrics_store.status()
        return {**self.status, "repository": dict(self.repo.status),
                "metrics": {k: m.get(k) for k in ("running", "last_sample", "last_error", "samples", "interval_s",
                                                  "retention_days", "raw_days")}}


def _handler(col: Collector) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "StudioCollector"

        def log_message(self, *args: Any) -> None:  # no access log (the hub audits)
            pass

        def _send(self, code: int, data: Any) -> None:
            body = json.dumps(data, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _claims(self) -> dict[str, Any] | None:
            auth = self.headers.get("Authorization", "")
            return verify(col.key, auth[7:]) if auth.startswith("Bearer ") else None

        def do_GET(self) -> None:  # noqa: N802
            claims = self._claims()
            if self.path != "/status" or claims is None or not claims.get("hub"):
                self._send(404, {"error": "Not found."})
                return
            self._send(200, col.report())

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/call":
                self._send(404, {"error": "Not found."})
                return
            claims = self._claims()
            if claims is None:
                col.status["denied"] += 1
                self._send(403, {"error": "This Studio isn't allowed to read the Studio repository."})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ValueError("too large")
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, {"error": "Bad request."})
                return
            if req.get("source") != col.source:
                self._send(200, {"local": True})  # another cluster: the Studio keeps its own store for it
                return
            col.status["calls"] += 1
            try:
                result = answer(str(req.get("fn") or ""), req.get("args") or {}, claims, req.get("s2_user"))
            except PermissionError as exc:
                self._send(403, {"error": str(exc)})
                return
            except (TypeError, ValueError) as exc:
                self._send(400, {"error": str(exc)[:300]})
                return
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": f"The Studio repository failed: {type(exc).__name__}: {str(exc)[:240]}"})
                return
            self._send(200, {"result": result})

    return Handler


def _setup_env(cfg: dict[str, Any]) -> None:
    """This process's state folder and its one connection: the service account, as the built-in SINGLESTORE_*
    profile. The password only goes into this process's own environment (its children: none)."""
    s2 = cfg["singlestore"]
    os.environ["SINGLESTORE_MCP_HOME"] = str(cfg["home"])
    os.environ.update({
        "SINGLESTORE_HOST": str(s2["host"]), "SINGLESTORE_PORT": str(s2.get("port") or 3306),
        "SINGLESTORE_USER": str(s2.get("user") or "svc_studio_collector"),
        "SINGLESTORE_DATABASE": str(cfg.get("database") or "SSSTUDIO"),
        "SINGLESTORE_SSL_DISABLED": "1" if s2.get("ssl_disabled") else "",
        "SINGLESTORE_SSL_CA": str(s2.get("ssl_ca") or ""), "SINGLESTORE_SSL_VERIFY": "0" if s2.get("ssl_verify") is False else "1",
        "SINGLESTORE_PASSWORD": str(s2.get("password") or ""),
        "SINGLESTORE_MCP_METRICS": "1",
        "SINGLESTORE_MCP_METRICS_INTERVAL": str(int(cfg.get("interval_s") or 60)),
        "SINGLESTORE_MCP_METRICS_DELAY": "5",
    })
    from .repository import DEFAULT_RETENTION

    r = {**DEFAULT_RETENTION, **(cfg.get("retention") or {})}
    cfg["retention"] = r
    os.environ["SINGLESTORE_MCP_METRICS_RAW_DAYS"] = str(r["metrics_raw_days"])
    os.environ["SINGLESTORE_MCP_METRICS_RETENTION_DAYS"] = str(r["metrics_days"])
    for k in ("SINGLESTORE_URL", "SINGLESTORE_MCP_SERVER_MODE"):
        os.environ.pop(k, None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m singlestore_mcp.collector")
    ap.add_argument("--stdin-config", action="store_true", required=True)
    ap.parse_args(argv)
    line = sys.stdin.readline()
    cfg = json.loads(line)
    del line
    _setup_env(cfg)
    os.makedirs(cfg["home"], exist_ok=True)
    col = Collector(cfg)
    cfg["singlestore"].pop("password", None)
    port = col.start()
    print(f"COLLECTOR-READY {port} {col.source}", flush=True)
    try:
        while sys.stdin.readline():  # the hub closes stdin to stop the collector
            pass
    except (OSError, KeyboardInterrupt):
        pass
    finally:
        col.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
