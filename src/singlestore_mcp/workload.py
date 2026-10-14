"""Test workloads: saved sets of SQL statements, each with how often it runs, replayed against the cluster.

A *workload* is a name, a default database, settings and a list of statements. Each statement runs N
times a minute ("fixed rate") or, with pacing "max", as often as the workers can, mixed by the same
numbers used as weights. Workloads are saved in ``~/.singlestore-mcp/workloads/workloads.json``; every
run is saved as ``runs/<run id>.json`` with per-statement latency percentiles, throughput, errors and a
per-second timeline, so two runs (before and after a sort key, an index, a new shard key ...) can be
compared statement by statement.

Running:

* ``concurrency`` workers, each with its own connection (opened once per run, so connecting isn't
  measured). The network round trip is measured at the start and reported (``rtt_ms``).
* Fixed rate: a scheduler releases each statement on time ("even" spacing or "random" Poisson
  arrivals). If every worker is busy the statement waits; the wait is reported as queue time, and
  releases more than ``concurrency * 20`` behind are skipped and counted (the workload couldn't keep up).
* Each execution's time is the wall time of execute + reading all rows on its worker's connection.
* Every statement carries ``/* workload:<name> stmt:<id> */``: the same text every run, so plans are
  reused across runs, and Query History shows these queries with the source "Workload: <name>".
* Statements may use placeholders, filled in per execution: ``{{int:1:1000}}``, ``{{float:0:1}}``,
  ``{{pick:a|b|c}}``, ``{{date:2024-01-01:2024-12-31}}`` (values are put in as written, so quote
  strings yourself: ``'{{pick:US|EU}}'``).
* Read-only (SELECT / WITH) unless the workload has ``allow_writes``. A per-statement timeout cancels a
  statement with ``KILL QUERY`` (only after checking that connection still runs this workload's
  statement). Stop cancels the statements that are running.
* A run lasts its full duration: statements are released (or, with pacing "max", started) until
  ``duration_s`` after the start, and the run's elapsed time, end time and rates use that window
  (or the time until Stop). Runs saved before this (no ``"timing": 2``) ended after their last
  release; ``compare_runs`` puts their rates over the set duration too.
"""

from __future__ import annotations

import heapq
import json
import math
import queue
import random
import re
import statistics
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import compare, fileio
from .paths import data_dir

MAX_STATEMENTS = 50
MAX_CONCURRENCY = 64
MAX_DURATION_S = 4 * 3600
MAX_RUNS_KEPT = 30            # per workload; older run files are removed
MIN_COMPARE_COUNT = 10        # fewer executions than this per statement: compare warns the numbers are noise
_LAT_KEEP = 50_000            # latencies kept per statement for the percentiles (reservoir sample)
_NAME_RE = re.compile(r"^[\w][\w .\-]{0,59}$")
_PLACEHOLDER = re.compile(r"\{\{\s*(int|float|pick|date)\s*:([^}]*)\}\}")

_lock = threading.RLock()
_runs: dict[str, "_Run"] = {}  # live and recently finished runs in this process


# ------------------------------------------------------------------ storage

def _dir() -> Path:
    return data_dir("workloads")


def _runs_dir() -> Path:
    return data_dir("workloads", "runs")


def _load_all() -> dict[str, dict[str, Any]]:
    # A file that exists but can't be read raises (fileio.read_json) instead of reading as empty, so a later
    # save can't overwrite every other workload.
    data = fileio.read_json(_dir() / "workloads.json", {})
    if not isinstance(data, dict):
        return {}
    return {w["name"]: w for w in data.get("workloads", []) if isinstance(w, dict) and w.get("name")}


def _save_all(workloads: dict[str, dict[str, Any]]) -> None:
    fileio.write_json(_dir() / "workloads.json",
                      {"version": 1, "workloads": sorted(workloads.values(), key=lambda w: w["name"].lower())}, indent=1)


def list_workloads() -> list[dict[str, Any]]:
    runs = list_runs()
    out = []
    for w in sorted(_load_all().values(), key=lambda w: w["name"].lower()):
        mine = [r for r in runs if r["workload"] == w["name"]]
        out.append({**w, "runs": len(mine), "last_run": mine[0] if mine else None})
    return out


def get(name: str) -> dict[str, Any]:
    w = _load_all().get(name)
    if not w:
        raise LookupError(f"No workload named {name!r}.")
    return w


def _clean_statement(s: dict[str, Any], i: int, pacing: str) -> dict[str, Any]:
    sql = str(s.get("sql") or "").strip().rstrip(";").strip()
    if not sql:
        raise ValueError(f"Statement {i + 1} has no SQL.")
    if "*/" in str(s.get("name") or ""):
        raise ValueError("A statement name can't contain */.")
    try:
        per_minute = float(s.get("per_minute", s.get("weight", 6)) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Statement {i + 1}: runs per minute must be a number.") from exc
    if per_minute < 0 or per_minute > 60_000:
        raise ValueError(f"Statement {i + 1}: runs per minute must be between 0 and 60000.")
    sid = str(s.get("id") or "").strip() or f"s{i + 1}"
    if not re.match(r"^[\w\-]{1,20}$", sid):
        raise ValueError(f"Statement id {sid!r}: letters, digits, - and _ only.")
    return {
        "id": sid,
        "name": str(s.get("name") or "").strip()[:80] or f"Statement {i + 1}",
        "sql": sql,
        "database": (str(s.get("database")).strip() or None) if s.get("database") else None,
        "per_minute": per_minute,
        "enabled": bool(s.get("enabled", True)),
    }


def save(workload: dict[str, Any], original_name: str | None = None) -> dict[str, Any]:
    """Create or replace a workload (renaming it when ``original_name`` differs)."""
    name = str(workload.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("Name: 1-60 letters, digits, spaces, '.', '-' or '_'.")
    settings = dict(workload.get("settings") or {})
    pacing = settings.get("pacing", "rate")
    if pacing not in ("rate", "max"):
        raise ValueError('pacing must be "rate" (runs per minute) or "max" (as fast as possible).')
    stmts = workload.get("statements") or []
    if not isinstance(stmts, list) or not stmts:
        raise ValueError("Add at least one statement.")
    if len(stmts) > MAX_STATEMENTS:
        raise ValueError(f"At most {MAX_STATEMENTS} statements.")
    clean = [_clean_statement(s, i, pacing) for i, s in enumerate(stmts)]
    ids = [s["id"] for s in clean]
    if len(set(ids)) != len(ids):
        raise ValueError("Statement ids must be unique.")
    out = {
        "name": name,
        "description": str(workload.get("description") or "").strip()[:2000],
        "database": (str(workload.get("database")).strip() or None) if workload.get("database") else None,
        "settings": {
            "pacing": pacing,
            "arrivals": settings.get("arrivals", "even") if settings.get("arrivals", "even") in ("even", "random") else "even",
            "concurrency": max(1, min(MAX_CONCURRENCY, int(settings.get("concurrency", 4)))),
            "duration_s": max(5, min(MAX_DURATION_S, int(settings.get("duration_s", 60)))),
            "timeout_s": max(1.0, min(3600.0, float(settings.get("timeout_s", 60)))),
            "think_ms": max(0, min(60_000, int(settings.get("think_ms", 0)))),
            "fetch_rows": bool(settings.get("fetch_rows", True)),
            "allow_writes": bool(settings.get("allow_writes", False)),
            "trace_all": bool(settings.get("trace_all", False)),
        },
        "statements": clean,
    }
    if not out["settings"]["allow_writes"]:
        for s in clean:
            if not compare.read_only(s["sql"]):
                raise ValueError(f"{s['name']} isn't a read-only SELECT / WITH statement. "
                                 "Turn on \"Allow writes\" for workloads that change data.")
    with _lock:
        all_ = _load_all()
        prev = all_.get(original_name or name) or all_.get(name)
        if original_name and original_name != name and name in all_:
            raise ValueError(f"A workload named {name!r} already exists.")
        out["created"] = (prev or {}).get("created") or time.time()
        out["updated"] = time.time()
        if original_name and original_name != name:
            all_.pop(original_name, None)
        all_[name] = out
        _save_all(all_)
    return out


def delete(name: str, runs_too: bool = False) -> dict[str, Any]:
    with _lock:
        all_ = _load_all()
        if name not in all_:
            raise LookupError(f"No workload named {name!r}.")
        del all_[name]
        _save_all(all_)
    removed = 0
    if runs_too:
        for r in list_runs():
            if r["workload"] == name:
                removed += delete_run(r["id"])
    return {"deleted": name, "runs_deleted": removed}


# ------------------------------------------------------------------ placeholders

def render(sql: str, rnd: random.Random) -> str:
    def one(m: re.Match[str]) -> str:
        kind, arg = m.group(1), m.group(2)
        parts = arg.split("|") if kind == "pick" else arg.split(":")
        try:
            if kind == "int":
                return str(rnd.randint(int(parts[0]), int(parts[1])))
            if kind == "float":
                return repr(round(rnd.uniform(float(parts[0]), float(parts[1])), 6))
            if kind == "pick":
                return rnd.choice(parts)
            lo, hi = date.fromisoformat(parts[0].strip()), date.fromisoformat(parts[1].strip())
            return (lo + timedelta(days=rnd.randint(0, max(0, (hi - lo).days)))).isoformat()
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Bad placeholder {m.group(0)}: use {{{{int:1:100}}}}, {{{{float:0:1}}}}, "
                             "{{pick:a|b}} or {{date:2024-01-01:2024-12-31}}") from exc

    return _PLACEHOLDER.sub(one, sql)


def tag(workload: str, sid: str) -> str:
    return f"/* workload:{workload.replace('*/', '')} stmt:{sid} */ "


# ------------------------------------------------------------------ optional: trace every query during a run
# Needs the ALTER EVENT TRACE permission (reading MV_TRACE_EVENTS_STATUS needs it too, which makes it the check).
# The Query_completion trace's configuration is saved to a file before it changes, so it is put back after
# the run, or by the next run / server start if the server stopped in between.

def _restore_file() -> Path:
    return _dir() / "trace-restore.json"


def _trace_status() -> dict[str, Any] | None:
    from .db import db

    rows = db.execute("/* s2-workload */ SELECT * FROM information_schema.MV_TRACE_EVENTS_STATUS")[1]
    for r in rows:
        low = {k.lower(): v for k, v in r.items()}
        if str(low.get("event_type") or low.get("event") or low.get("name") or "").lower() == "query_completion":
            return low
    return None


def _with_clause(config: Any) -> str:
    """CONFIGURATION (JSON object of setting -> value) as the WITH (...) list of CREATE EVENT TRACE."""
    cfg = json.loads(config) if isinstance(config, str) else dict(config or {})
    parts = []
    for k, v in cfg.items():
        if not re.match(r"^\w+$", str(k)):
            raise ValueError(f"unexpected setting {k!r}")
        val = str(v).strip().lower()
        if val in ("on", "off", "true", "false", "1", "0") and not k.lower().endswith("_ms"):
            val = "on" if val in ("on", "true", "1") else "off"
        elif not re.match(r"^-?\d+(\.\d+)?$", val):
            raise ValueError(f"unexpected value {v!r} for {k}")
        parts.append(f"{k} = {val}")
    return ", ".join(parts)


def trace_begin() -> str:
    """Trace every query (Duration_threshold_ms = 0). Returns a note for the run."""
    from .db import db

    recover_trace()
    try:
        prev = _trace_status()
    except Exception as exc:  # noqa: BLE001 - typically no ALTER EVENT TRACE permission
        return f"Not tracing every query: the trace settings can't be read ({str(exc)[:160]}). Needs the ALTER EVENT TRACE permission."
    prev_cfg = prev.get("configuration") if prev else None
    if prev_cfg:
        try:
            _with_clause(prev_cfg)  # must be restorable before anything changes
        except (ValueError, TypeError) as exc:
            return f"Not tracing every query: the current trace configuration couldn't be read back safely ({exc})."
    _restore_file().write_text(json.dumps({"existed": bool(prev), "configuration": prev_cfg, "saved": time.time()}), encoding="utf-8")
    try:
        if prev:
            db.execute("DROP EVENT TRACE Query_completion", fetch=False)
        db.execute("CREATE EVENT TRACE Query_completion WITH (Query_text = on, Duration_threshold_ms = 0, "
                   "Trace_all_failed_executions = on)", fetch=False)
    except Exception as exc:  # noqa: BLE001
        recover_trace()
        return f"Not tracing every query: {str(exc)[:200]}"
    return ("Every query was traced during this run (Query_completion, Duration_threshold_ms = 0), so all executions are in "
            "Query History; tracing adds some overhead, so compare traced runs only with traced runs.")


def recover_trace() -> str | None:
    """Put back the trace configuration saved by trace_begin (no-op when nothing is saved)."""
    from .db import db

    path = _restore_file()
    if not path.is_file():
        return None
    saved = json.loads(path.read_text(encoding="utf-8"))
    try:
        try:
            db.execute("DROP EVENT TRACE Query_completion", fetch=False)
        except Exception:  # noqa: BLE001 - there may be none
            pass
        if saved.get("existed") and saved.get("configuration"):
            db.execute(f"CREATE EVENT TRACE Query_completion WITH ({_with_clause(saved['configuration'])})", fetch=False)
        elif saved.get("existed"):
            db.execute("CREATE EVENT TRACE Query_completion", fetch=False)
    except Exception as exc:  # noqa: BLE001 - keep the file so the next attempt can retry
        return (f"Couldn't restore the Query_completion trace: {str(exc)[:200]}. Previous configuration: "
                f"{saved.get('configuration')}")
    path.unlink(missing_ok=True)
    return None


# ------------------------------------------------------------------ statistics

def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


class _Stat:
    def __init__(self) -> None:
        self.count = 0
        self.errors = 0
        self.timeouts = 0
        self.rows = 0
        self.lat: list[float] = []
        self.wait: list[float] = []
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.skipped = 0
        self.error_samples: dict[str, int] = {}
        self.rnd = random.Random(7)

    def _keep(self, arr: list[float], v: float, n: int) -> None:
        if len(arr) < _LAT_KEEP:
            arr.append(v)
        else:  # reservoir sample: every execution has the same chance to be kept
            j = self.rnd.randint(0, n - 1)
            if j < _LAT_KEEP:
                arr[j] = v

    def add(self, ms: float, wait_ms: float, rows: int | None, error: str | None, timeout: bool) -> None:
        self.count += 1
        if error:
            self.errors += 1
            self.timeouts += int(timeout)
            key = error[:200]
            if key in self.error_samples or len(self.error_samples) < 5:
                self.error_samples[key] = self.error_samples.get(key, 0) + 1
            return
        self.rows += rows or 0
        self.total_ms += ms
        self.max_ms = max(self.max_ms, ms)
        ok = self.count - self.errors
        self._keep(self.lat, ms, ok)
        self._keep(self.wait, wait_ms, ok)

    def summary(self, elapsed_s: float) -> dict[str, Any]:
        lat = sorted(self.lat)
        wait = sorted(self.wait)
        ok = self.count - self.errors
        r = lambda v: None if v is None else round(v, 1)  # noqa: E731
        return {
            "count": self.count, "ok": ok, "errors": self.errors, "timeouts": self.timeouts, "skipped": self.skipped,
            "error_rate": round(self.errors / self.count, 4) if self.count else 0,
            "qps": round(self.count / elapsed_s, 3) if elapsed_s > 0 else 0,
            "per_minute": round(self.count / elapsed_s * 60, 1) if elapsed_s > 0 else 0,
            "min_ms": r(lat[0] if lat else None), "p50_ms": r(_pct(lat, 50)), "p90_ms": r(_pct(lat, 90)),
            "p95_ms": r(_pct(lat, 95)), "p99_ms": r(_pct(lat, 99)), "max_ms": r(self.max_ms if ok else None),
            "mean_ms": r(self.total_ms / ok if ok else None),
            "wait_p95_ms": r(_pct(wait, 95)), "rows_avg": round(self.rows / ok, 1) if ok else None,
            "error_samples": [{"error": k, "count": v} for k, v in self.error_samples.items()],
        }


class _Second:
    __slots__ = ("n", "errors", "lat")

    def __init__(self) -> None:
        self.n, self.errors, self.lat = 0, 0, []


# ------------------------------------------------------------------ running

class _Run:
    def __init__(self, workload: dict[str, Any], label: str, overrides: dict[str, Any]) -> None:
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.workload = workload
        self.settings = {**workload["settings"], **{k: v for k, v in overrides.items() if v is not None}}
        self.settings["concurrency"] = max(1, min(MAX_CONCURRENCY, int(self.settings["concurrency"])))
        self.settings["duration_s"] = max(5, min(MAX_DURATION_S, int(self.settings["duration_s"])))
        self.label = label.strip()[:80]
        self.statements = [s for s in workload["statements"] if s["enabled"] and s["per_minute"] > 0]
        if not self.statements:
            raise ValueError("No enabled statement with runs per minute above 0.")
        self.stats = {s["id"]: _Stat() for s in self.statements}
        self.seconds: dict[int, _Second] = {}
        self.state = "starting"
        self.error: str | None = None
        self.stop_flag = threading.Event()
        self.started = time.time()
        self.t0 = self._now()
        self.finished: float | None = None
        self.finished_perf: float | None = None
        self.stopped_perf: float | None = None
        self.rtt_ms: float | None = None
        self.lock = threading.Lock()
        self.busy: dict[int, tuple[int, float]] = {}  # worker -> (connection id, started)
        self.conn_ids: dict[int, int] = {}
        self.source = None
        self.notes: list[str] = []
        self.traced = False

    # -- recording
    # -- clock (tests swap these for a faster one)
    def _now(self) -> float:
        return time.perf_counter()

    def _pause(self, seconds: float) -> bool:
        """Wait ``seconds`` or until Stop; True when stopped."""
        return self.stop_flag.wait(max(0.0, seconds))

    def record(self, sid: str, ms: float, wait_ms: float, rows: int | None, error: str | None, timeout: bool = False) -> None:
        sec = int(self._now() - self.t0)
        with self.lock:
            self.stats[sid].add(ms, wait_ms, rows, error, timeout)
            b = self.seconds.get(sec)
            if b is None:
                b = self.seconds[sec] = _Second()
            b.n += 1
            if error:
                b.errors += 1
            else:
                b.lat.append(ms)

    def elapsed(self) -> float:
        """Seconds the run has been running: until Stop, else at most the set duration (statements still
        finishing after the end don't make the run longer)."""
        end = self.stopped_perf or self.finished_perf or self._now()
        return max(0.001, min(end - self.t0, float(self.settings["duration_s"])))

    def timeline(self, since: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            items = sorted((s, b) for s, b in self.seconds.items() if s >= since)
            out = []
            for s, b in items:
                lat = sorted(b.lat)
                out.append({"t": s, "n": b.n, "errors": b.errors, "p50_ms": round(_pct(lat, 50) or 0, 1) if lat else None,
                            "p95_ms": round(_pct(lat, 95) or 0, 1) if lat else None})
            return out

    def summary(self, timeline_since: int | None = 0) -> dict[str, Any]:
        el = self.elapsed()
        with self.lock:
            per = [{"id": s["id"], "name": s["name"], "sql": s["sql"], "database": s["database"] or self.workload.get("database"),
                    "target_per_minute": s["per_minute"], **self.stats[s["id"]].summary(el)} for s in self.statements]
        total = sum(p["count"] for p in per)
        errors = sum(p["errors"] for p in per)
        all_lat = sorted(v for s in self.stats.values() for v in s.lat)
        out = {
            "id": self.id, "workload": self.workload["name"], "label": self.label, "state": self.state, "error": self.error,
            "started": self.started, "finished": self.finished, "elapsed_s": round(el, 1),
            "duration_s": self.settings["duration_s"], "settings": self.settings, "database": self.workload.get("database"),
            "source": self.source, "rtt_ms": self.rtt_ms, "notes": self.notes, "timing": 2,
            "totals": {"count": total, "errors": errors, "qps": round(total / el, 2),
                       "p50_ms": _round(_pct(all_lat, 50)), "p95_ms": _round(_pct(all_lat, 95)), "p99_ms": _round(_pct(all_lat, 99)),
                       "skipped": sum(p["skipped"] for p in per)},
            "statements": per,
        }
        if timeline_since is not None:
            out["timeline"] = self.timeline(timeline_since)
        return out

    # -- connections
    def _connect(self, database: str | None) -> tuple[Any, int]:
        conn = compare._connect(database)
        cur = conn.cursor()
        cur.execute("SELECT CONNECTION_ID()")
        cid = int(compare._first(cur))
        cur.close()
        return conn, cid

    def _kill(self, cid: int) -> bool:
        import singlestoredb as s2

        from . import connections

        killer = s2.connect(**connections.connect_kwargs(connections.active()), autocommit=True)
        try:
            cur = killer.cursor()
            cur.execute("/* s2-workload */ SELECT ID FROM information_schema.PROCESSLIST WHERE ID = %s AND INFO LIKE %s",
                        (int(cid), f"%workload:{self.workload['name']}%"))
            if not cur.fetchall():
                return False
            cur.execute(f"KILL QUERY {int(cid)}")
            return True
        finally:
            killer.close()

    def _execute(self, worker: int, conn: Any, cid: int, current_db: list[str | None], stmt: dict[str, Any],
                 rnd: random.Random, wait_ms: float) -> None:
        database = stmt["database"] or self.workload.get("database")
        try:
            sql = tag(self.workload["name"], stmt["id"]) + render(stmt["sql"], rnd)
        except ValueError as exc:
            self.record(stmt["id"], 0, wait_ms, None, str(exc))
            return
        cur = conn.cursor()
        killed = {"done": False}

        def on_timeout() -> None:
            try:
                killed["done"] = self._kill(cid)
            except Exception:  # noqa: BLE001 - reported as a timeout either way
                killed["done"] = True

        timer = threading.Timer(self.settings["timeout_s"], on_timeout)
        try:
            if database and current_db[0] != database:
                from .db import quote_identifier

                cur.execute(f"USE {quote_identifier(database)}")
                current_db[0] = database
            self.busy[worker] = (cid, self._now())
            timer.start()
            t = self._now()
            cur.execute(sql)
            rows = None
            if cur.description is not None:
                rows = 0
                if self.settings.get("fetch_rows", True):
                    while True:
                        chunk = cur.fetchmany(5000)
                        if not chunk:
                            break
                        rows += len(chunk)
            else:
                rows = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            ms = (self._now() - t) * 1000
            self.record(stmt["id"], ms, wait_ms, rows, None)
        except Exception as exc:  # noqa: BLE001 - counted per statement
            now = self._now()
            timeout = killed["done"] or (now - self.busy.get(worker, (0, now))[1] >= self.settings["timeout_s"])
            stopped = self.stop_flag.is_set()
            msg = (f"timed out after {self.settings['timeout_s']:g} s (cancelled)" if timeout and not stopped
                   else "cancelled (run stopped)" if stopped else str(exc)[:300])
            if not stopped:
                self.record(stmt["id"], 0, wait_ms, None, msg, timeout=timeout)
        finally:
            timer.cancel()
            self.busy.pop(worker, None)
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    def _rtt(self, conn: Any) -> None:
        try:
            self.rtt_ms = round(compare._rtt(conn) * 1000, 1)
        except Exception:  # noqa: BLE001
            self.rtt_ms = None

    def run(self) -> None:
        from .history_store import source_id

        conns: list[tuple[Any, int]] = []
        try:
            self.source = source_id()
            n = self.settings["concurrency"]
            db0 = self.workload.get("database")
            # Open the connections in parallel (each is a few round trips).
            opened: list[Any] = [None] * n
            errors: list[str] = []

            def open_one(i: int) -> None:
                try:
                    opened[i] = self._connect(db0)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc)[:300])

            threads = [threading.Thread(target=open_one, args=(i,), daemon=True) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            conns = [c for c in opened if c]
            if not conns:
                raise RuntimeError("Couldn't connect: " + (errors[0] if errors else "unknown error"))
            if len(conns) < n:
                self.notes.append(f"Only {len(conns)} of {n} connections could be opened: {errors[0] if errors else ''}")
            self._rtt(conns[0][0])
            if self.settings.get("trace_all"):
                note = trace_begin()
                self.traced = note.startswith("Every query")
                self.notes.append(note)
            self.state = "running"
            self.started = time.time()  # the measured window starts now (connecting isn't part of it)
            self.t0 = self._now()
            deadline = self.t0 + self.settings["duration_s"]
            if self.settings["pacing"] == "max":
                self._run_max(conns, deadline)
            else:
                self._run_rate(conns, deadline)
        except Exception as exc:  # noqa: BLE001 - reported in the run
            self.error = str(exc)[:500]
        finally:
            self.finished_perf = self._now()
            self.finished = self.started + self.elapsed()  # start + duration, or the time of Stop
            if self.traced:
                problem = recover_trace()
                if problem:
                    self.notes.append(problem)
            for conn, _ in conns:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self.state = "error" if self.error else "stopped" if self.stop_flag.is_set() else "done"
            try:
                _persist(self)
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"Couldn't save the run: {exc}")

    def _run_max(self, conns: list[tuple[Any, int]], deadline: float) -> None:
        weights = [s["per_minute"] for s in self.statements]
        think = self.settings["think_ms"] / 1000

        def worker(i: int, conn: Any, cid: int) -> None:
            rnd = random.Random(hash((self.id, i)))
            current_db: list[str | None] = [self.workload.get("database")]
            while not self.stop_flag.is_set() and self._now() < deadline:
                stmt = rnd.choices(self.statements, weights)[0]
                self._execute(i, conn, cid, current_db, stmt, rnd, 0.0)
                if think:
                    self._pause(min(think, deadline - self._now()))

        threads = [threading.Thread(target=worker, args=(i, c, cid), daemon=True, name=f"s2-workload-{i}")
                   for i, (c, cid) in enumerate(conns)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _run_rate(self, conns: list[tuple[Any, int]], deadline: float) -> None:
        q: queue.Queue[tuple[dict[str, Any], float] | None] = queue.Queue()
        max_backlog = len(conns) * 20
        random_arrivals = self.settings.get("arrivals") == "random"
        rnd = random.Random(self.id)

        def worker(i: int, conn: Any, cid: int) -> None:
            wr = random.Random(hash((self.id, i)))
            current_db: list[str | None] = [self.workload.get("database")]
            while True:
                item = q.get()
                if item is None:
                    return
                stmt, due = item
                if self.stop_flag.is_set():
                    continue
                wait_ms = max(0.0, (self._now() - due) * 1000)
                self._execute(i, conn, cid, current_db, stmt, wr, wait_ms)

        threads = [threading.Thread(target=worker, args=(i, c, cid), daemon=True, name=f"s2-workload-{i}")
                   for i, (c, cid) in enumerate(conns)]
        for t in threads:
            t.start()
        # Scheduler: each statement is released every 60/per_minute s (staggered start), or with
        # exponentially distributed gaps for random arrivals.
        heap: list[tuple[float, int]] = []
        for k, s in enumerate(self.statements):
            gap = 60.0 / s["per_minute"]
            heapq.heappush(heap, (self.t0 + rnd.uniform(0, min(gap, 1.0)), k))
        while heap and not self.stop_flag.is_set():
            due, k = heap[0]
            now = self._now()
            if due >= deadline:
                break
            if due > now:
                self._pause(min(due - now, 0.25))
                continue
            heapq.heappop(heap)
            s = self.statements[k]
            if q.qsize() >= max_backlog:
                with self.lock:
                    self.stats[s["id"]].skipped += 1
            else:
                q.put((s, due))
            gap = 60.0 / s["per_minute"]
            heapq.heappush(heap, (due + (rnd.expovariate(1 / gap) if random_arrivals else gap), k))
        # The next release falls after the end: the run still lasts until the deadline (it used to end
        # here, after the last release, a little before the set duration).
        while not self.stop_flag.is_set() and self._now() < deadline:
            self._pause(min(deadline - self._now(), 0.25))
        # Let what's still queued finish (up to one statement timeout), then stop the workers.
        while not q.empty() and not self.stop_flag.is_set() and self._now() < deadline + self.settings["timeout_s"]:
            self._pause(0.05)
        if not q.empty():
            dropped = 0
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item:
                    dropped += 1
            if dropped:
                self.notes.append(f"{dropped} queued executions were still waiting at the end and didn't run.")
        for _ in threads:
            q.put(None)
        for t in threads:
            t.join()

    def stop(self) -> None:
        if self.stop_flag.is_set() or self.state not in ("starting", "running"):
            return
        if self.state == "running":
            self.stopped_perf = self._now()
        self.stop_flag.set()
        for cid, _ in list(self.busy.values()):
            threading.Thread(target=lambda c=cid: _quiet(self._kill, c), daemon=True).start()


def _quiet(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        pass


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


def start(name: str, label: str = "", duration_s: int | None = None, concurrency: int | None = None) -> dict[str, Any]:
    w = get(name)
    with _lock:
        running = [r for r in _runs.values() if r.state in ("starting", "running")]
        if running:
            raise ValueError(f"Workload {running[0].workload['name']!r} is running; stop it first (one at a time).")
        if not w["settings"]["allow_writes"]:
            for s in w["statements"]:
                if s["enabled"] and not compare.read_only(s["sql"]):
                    raise ValueError(f"{s['name']} isn't read-only and the workload doesn't allow writes.")
        run = _Run(w, label, {"duration_s": duration_s, "concurrency": concurrency})
        _runs[run.id] = run
        for old in [k for k, r in _runs.items() if r.state not in ("starting", "running")][:-5]:
            _runs.pop(old, None)
    threading.Thread(target=run.run, daemon=True, name=f"s2-workload-{run.id}").start()
    return run.summary(timeline_since=None)


def stop(run_id: str) -> dict[str, Any]:
    run = _runs.get(run_id)
    if not run:
        raise LookupError("That run isn't running in this server.")
    run.stop()
    # Give the run a moment to wind down, so the result shows "stopped" rather than "running".
    deadline = time.monotonic() + 5
    while run.state in ("starting", "running") and time.monotonic() < deadline:
        time.sleep(0.1)
    return run.summary(timeline_since=None)


def stop_all() -> None:
    for r in list(_runs.values()):
        r.stop()


def status(run_id: str, since: int = 0) -> dict[str, Any]:
    run = _runs.get(run_id)
    if run:
        return run.summary(timeline_since=since)
    return load_run(run_id)


def running() -> dict[str, Any] | None:
    for r in _runs.values():
        if r.state in ("starting", "running"):
            return r.summary(timeline_since=None)
    return None


# ------------------------------------------------------------------ saved runs

def _persist(run: _Run) -> None:
    data = run.summary(timeline_since=0)
    path = _runs_dir() / f"{run.id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    # Keep the newest MAX_RUNS_KEPT runs of this workload.
    mine = [r for r in list_runs() if r["workload"] == run.workload["name"]]
    for old in mine[MAX_RUNS_KEPT:]:
        delete_run(old["id"])


def load_run(run_id: str) -> dict[str, Any]:
    if not re.match(r"^[\w\-]{1,40}$", run_id or ""):
        raise ValueError("Not a run id.")
    path = _runs_dir() / f"{run_id}.json"
    if not path.is_file():
        raise LookupError(f"No saved run {run_id!r}.")
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs(workload: str | None = None) -> list[dict[str, Any]]:
    out = []
    for p in _runs_dir().glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if workload and d.get("workload") != workload:
            continue
        out.append({k: d.get(k) for k in ("id", "workload", "label", "state", "started", "finished", "elapsed_s", "source", "totals", "settings")})
    for r in _runs.values():  # the running one isn't saved yet
        if r.state in ("starting", "running") and (not workload or r.workload["name"] == workload):
            s = r.summary(timeline_since=None)
            out.append({k: s.get(k) for k in ("id", "workload", "label", "state", "started", "finished", "elapsed_s", "source", "totals", "settings")})
    out.sort(key=lambda r: r.get("started") or 0, reverse=True)
    return out


def delete_run(run_id: str) -> int:
    if not re.match(r"^[\w\-]{1,40}$", run_id or ""):
        raise ValueError("Not a run id.")
    path = _runs_dir() / f"{run_id}.json"
    if path.is_file():
        path.unlink()
        return 1
    return 0


# ------------------------------------------------------------------ comparing runs

def _full_window(r: dict[str, Any]) -> dict[str, Any]:
    """A run saved before runs lasted their full duration (no ``"timing": 2``) ended after its last release,
    so its rates were over a shorter time than the window its statements were released in; put them over
    the set duration, like newer runs."""
    dur = (r.get("settings") or {}).get("duration_s") or r.get("duration_s")
    el = r.get("elapsed_s")
    if r.get("timing") or r.get("state") != "done" or not dur or not el or el >= dur - 0.5:
        return r
    out = {**r, "elapsed_s": float(dur), "statements": []}
    for s in r.get("statements") or []:
        s = dict(s)
        if s.get("count") is not None:
            s["qps"] = round(s["count"] / dur, 3)
            s["per_minute"] = round(s["count"] / dur * 60, 1)
        out["statements"].append(s)
    t = dict(r.get("totals") or {})
    if t.get("count") is not None:
        t["qps"] = round(t["count"] / dur, 2)
    out["totals"] = t
    return out


def compare_runs(base_id: str, new_id: str) -> dict[str, Any]:
    a, b = _full_window(status(base_id)), _full_window(status(new_id))
    notes = []
    if a.get("source") and b.get("source") and a["source"] != b["source"]:
        notes.append(f"The runs used different clusters ({a['source']} vs {b['source']}).")
    for k in ("concurrency", "pacing", "duration_s"):
        if a["settings"].get(k) != b["settings"].get(k):
            notes.append(f"Different {k}: {a['settings'].get(k)} vs {b['settings'].get(k)}.")
    for r in (a, b):
        if r.get("state") not in (None, "done"):
            notes.append(f"Run {r['id']} ({r.get('label') or 'no label'}) didn't finish ({r['state']} after {r.get('elapsed_s')} s).")
    bs = {s["id"]: s for s in b["statements"]}
    few = [s["name"] for s in a["statements"] + b["statements"] if 0 < (s.get("count") or 0) < MIN_COMPARE_COUNT]
    if few:
        notes.append(f"Fewer than {MIN_COMPARE_COUNT} executions for {', '.join(dict.fromkeys(few))}: "
                     "p95 and the percent changes are mostly noise; run longer for a fair comparison.")
    rows = []
    for s in a["statements"]:
        t = bs.get(s["id"])
        if not t:
            continue
        changed_sql = " ".join(s["sql"].split()) != " ".join(t["sql"].split())

        def delta(x: float | None, y: float | None) -> float | None:
            return round((y - x) / x * 100, 1) if x and y is not None else None

        rows.append({
            "id": s["id"], "name": s["name"], "sql": t["sql"], "sql_changed": changed_sql,
            "base_p50_ms": s["p50_ms"], "new_p50_ms": t["p50_ms"], "p50_change_pct": delta(s["p50_ms"], t["p50_ms"]),
            "base_p95_ms": s["p95_ms"], "new_p95_ms": t["p95_ms"], "p95_change_pct": delta(s["p95_ms"], t["p95_ms"]),
            "base_qps": s["qps"], "new_qps": t["qps"], "base_count": s["count"], "new_count": t["count"],
            "base_errors": s["errors"], "new_errors": t["errors"],
            "speedup": round(s["p50_ms"] / t["p50_ms"], 2) if s["p50_ms"] and t["p50_ms"] else None,
            "base_rows_avg": s.get("rows_avg"), "new_rows_avg": t.get("rows_avg"),
            # With placeholders every execution reads different rows, so differing averages are expected.
            "rows_differ": (not _PLACEHOLDER.search(t["sql"]) and s.get("rows_avg") is not None
                            and t.get("rows_avg") is not None and s["rows_avg"] != t["rows_avg"]),
        })
    only_a = [s["name"] for s in a["statements"] if s["id"] not in bs]
    if only_a:
        notes.append("Only in the first run: " + ", ".join(only_a))
    ta, tb = a["totals"], b["totals"]
    return {
        "base": {k: a.get(k) for k in ("id", "label", "started", "workload", "totals", "settings")},
        "new": {k: b.get(k) for k in ("id", "label", "started", "workload", "totals", "settings")},
        "totals": {"base_p50_ms": ta.get("p50_ms"), "new_p50_ms": tb.get("p50_ms"), "base_p95_ms": ta.get("p95_ms"),
                   "new_p95_ms": tb.get("p95_ms"), "base_qps": ta.get("qps"), "new_qps": tb.get("qps"),
                   "base_errors": ta.get("errors"), "new_errors": tb.get("errors")},
        "statements": rows, "notes": notes,
    }


def compare_summary(c: dict[str, Any]) -> str:
    t = c["totals"]
    lines = [f"Run {c['base']['id']} ({c['base']['label'] or 'no label'}) vs {c['new']['id']} ({c['new']['label'] or 'no label'}): "
             f"all statements p50 {t['base_p50_ms']} -> {t['new_p50_ms']} ms, p95 {t['base_p95_ms']} -> {t['new_p95_ms']} ms, "
             f"{t['base_qps']} -> {t['new_qps']} per s, errors {t['base_errors']} -> {t['new_errors']}."]
    for s in sorted(c["statements"], key=lambda s: s["p50_change_pct"] if s["p50_change_pct"] is not None else 0):
        lines.append(f"- {s['name']}: p50 {s['base_p50_ms']} -> {s['new_p50_ms']} ms ({_signed(s['p50_change_pct'])}), "
                     f"p95 {s['base_p95_ms']} -> {s['new_p95_ms']} ms ({_signed(s['p95_change_pct'])}), "
                     f"errors {s['base_errors']} -> {s['new_errors']}"
                     + (", SQL changed" if s["sql_changed"] else "") + (", row counts differ" if s["rows_differ"] else ""))
    lines += [f"Note: {n}" for n in c["notes"]]
    return "\n".join(lines)


def _signed(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def run_summary_text(r: dict[str, Any], max_statements: int = 15) -> str:
    t = r["totals"]
    lines = [f"Workload {r['workload']!r} run {r['id']}{' (' + r['label'] + ')' if r.get('label') else ''}: {r['state']}, "
             f"{r['elapsed_s']} of {r['duration_s']} s, {t['count']} executions ({t['qps']} per s), {t['errors']} errors, "
             f"p50 {t['p50_ms']} ms, p95 {t['p95_ms']} ms, network round trip {r.get('rtt_ms')} ms, "
             f"concurrency {r['settings']['concurrency']}, pacing {r['settings']['pacing']}."]
    if r.get("error"):
        lines.append(f"Error: {r['error']}")
    for s in sorted(r["statements"], key=lambda s: -(s["p95_ms"] or 0))[:max_statements]:
        lines.append(f"- {s['name']} [{s['id']}]: {s['count']} runs ({s['per_minute']}/min, target {s['target_per_minute']}), "
                     f"p50 {s['p50_ms']} / p95 {s['p95_ms']} / max {s['max_ms']} ms, errors {s['errors']}"
                     + (f", queue wait p95 {s['wait_p95_ms']} ms" if s.get("wait_p95_ms") else "")
                     + (f", {s['skipped']} skipped (couldn't keep up)" if s.get("skipped") else "")
                     + (f": {s['error_samples'][0]['error'][:150]}" if s.get("error_samples") else ""))
    lines += [f"Note: {n}" for n in r.get("notes") or []]
    return "\n".join(lines)


# ------------------------------------------------------------------ statements from SQL text (editor, Claude)
# SQL sent to a workload from the SQL Editor or proposed by the Workload chat may start with hint comments:
#   -- name: Top makes by price
#   -- per_minute: 30
# They name the statement and set its rate; the hint lines are taken out of the SQL, other comments stay.

_HINT = re.compile(r"^\s*--\s*(name|per[ _-]?minute)\s*:\s*(.*?)\s*$", re.I)
_LEADING_COMMENTS = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|#[^\n]*(?:\n|$)|/\*.*?\*/)*", re.S)


def parse_hints(sql: str) -> dict[str, Any]:
    """``{"sql", "name", "per_minute"}``: the statement without its leading ``-- name:`` / ``-- per_minute:``
    hint lines (and without a trailing ``;``), with the hints' values (None when absent or not usable)."""
    lines = (sql or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    name: str | None = None
    per_minute: float | None = None
    keep: list[str] = []
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("--")):
        m = _HINT.match(lines[i])
        if not m:
            keep.append(lines[i])
        elif m.group(1).lower() == "name":
            name = " ".join(m.group(2).replace("*/", "").split())[:80] or None
        else:
            try:
                v = float(m.group(2).replace(",", "."))
                per_minute = v if 0 <= v <= 60_000 else None
            except ValueError:
                per_minute = None
        i += 1
    body = "\n".join(keep + lines[i:]).strip().rstrip(";").rstrip()
    return {"sql": body, "name": name, "per_minute": per_minute}


def default_name(sql: str) -> str:
    """A statement name from its SQL: the first 60 characters after any leading comments."""
    return " ".join(_LEADING_COMMENTS.sub("", sql, count=1).split())[:60] or "Statement"


def statements_from_sql(sqls: list[str], per_minute: float = 60) -> list[dict[str, Any]]:
    """Workload statements (without ids: the workload gives them theirs) from SQL texts, using their hints."""
    out = []
    for text in sqls[:MAX_STATEMENTS]:
        h = parse_hints(str(text or ""))
        if not h["sql"]:
            continue
        out.append({"name": h["name"] or default_name(h["sql"]), "sql": h["sql"],
                    "per_minute": h["per_minute"] if h["per_minute"] is not None else float(per_minute),
                    "database": None, "enabled": True,
                    "hints": {"name": h["name"], "per_minute": h["per_minute"]}})  # what the SQL itself set
    return out


def update_statement(name: str, statement_id: str, sql: str, database: str | None = None) -> dict[str, Any]:
    """Put new SQL into one statement of a saved workload and save it (the SQL Editor's "Update in workload").
    Hints in the SQL also set the name / rate; a ``database`` other than the statement's (or the workload's)
    becomes the statement's own. Returns {"workload", "statement"}."""
    h = parse_hints(sql)
    if not h["sql"]:
        raise ValueError("There's no SQL to put in the workload.")
    with _lock:
        w = _load_all().get(name)
        if not w:
            raise LookupError(f"The workload {name!r} doesn't exist any more (deleted or renamed).")
        s = next((x for x in w["statements"] if x["id"] == statement_id), None)
        if s is None:
            raise LookupError(f"The workload {name!r} has no statement {statement_id!r} any more "
                              "(removed, or added but never saved).")
        s["sql"] = h["sql"]
        if h["name"]:
            s["name"] = h["name"]
        if h["per_minute"] is not None:
            s["per_minute"] = h["per_minute"]
        if database and database != (s.get("database") or w.get("database")):
            s["database"] = database
        saved = save(w, name)
    return {"workload": saved, "statement": next(x for x in saved["statements"] if x["id"] == statement_id)}


# ------------------------------------------------------------------ context for the Workload chat (Claude)

CHAT_CONTEXT_LIMIT = 40_000   # characters; the question and the system prompt come on top
_SQL_CAPS = (4000, 1500, 500, 200)
_DDL_CAPS = (3000, 1200, 400, 0)


def tables_read(workload: dict[str, Any]) -> list[str]:
    """"db.table" of the tables the workload's statements name (placeholders filled in first), at most 20."""
    from . import history_store

    names: list[str] = []
    for s in workload.get("statements") or []:
        sql = str(s.get("sql") or "")
        try:
            sql = render(sql, random.Random(0))
        except ValueError:
            pass
        db0 = s.get("database") or workload.get("database")
        try:
            found = history_store.tables_in(sql, db0)
        except Exception:  # noqa: BLE001 - best effort: odd SQL just adds no tables
            continue
        for t in found:
            if t.get("database") and str(t["database"]).lower() not in ("information_schema", "cluster", "memsql"):
                key = f"{t['database']}.{t['table']}"
                if key not in names:
                    names.append(key)
    return names[:20]


def table_definitions(names: list[str]) -> dict[str, dict[str, Any]]:
    """SHOW CREATE TABLE, row counts and column cardinalities (optimizer statistics) of these tables."""
    if not names:
        return {}
    from . import query_advisor

    return query_advisor._table_info(names)


def chat_context(workload: dict[str, Any], run: dict[str, Any] | None = None,
                 tables: dict[str, dict[str, Any]] | None = None, limit: int = CHAT_CONTEXT_LIMIT) -> str:
    """What the Workload chat sends with each question: the workload (settings, every statement), the last
    run's per-statement results and the definitions of the tables read, kept under ``limit`` characters
    (long SQL and DDL are cut shorter until it fits)."""
    for sql_cap, ddl_cap in zip(_SQL_CAPS, _DDL_CAPS):
        text = _chat_context(workload, run, tables or {}, sql_cap, ddl_cap)
        if len(text) <= limit:
            return text
    return text[: limit - 60] + "\n… (context cut: the workload is very large)"


def _cut(text: str, cap: int) -> str:
    text = str(text or "")
    return text if len(text) <= cap else text[:cap] + f" … ({len(text) - cap} more characters)"


def _chat_context(w: dict[str, Any], run: dict[str, Any] | None, tables: dict[str, dict[str, Any]],
                  sql_cap: int, ddl_cap: int) -> str:
    st = w.get("settings") or {}
    pacing = st.get("pacing", "rate")
    lines = [f"Workload {w.get('name') or '(new, not saved yet)'!r}; default database {w.get('database') or '(connection default)'}."]
    if w.get("description"):
        lines.append(f"Description: {_cut(w['description'], 300)}")
    lines.append(
        f"Settings: pacing {pacing} ({'per_minute = runs per minute' if pacing == 'rate' else 'per_minute = relative weight'}), "
        f"arrivals {st.get('arrivals', 'even')}, {st.get('concurrency', 4)} connections, {st.get('duration_s', 60)} s, "
        f"timeout {st.get('timeout_s', 60)} s, think {st.get('think_ms', 0)} ms, read all rows {bool(st.get('fetch_rows', True))}, "
        f"allow writes {bool(st.get('allow_writes', False))}.")
    stmts = (w.get("statements") or [])[:MAX_STATEMENTS]
    lines.append(f"Statements ({len(stmts)}):")
    for i, s in enumerate(stmts, 1):
        flags = "" if s.get("enabled", True) else ", disabled"
        lines.append(f"{i}. [{s.get('id')}] {s.get('name') or ''} · per_minute {s.get('per_minute')} · database "
                     f"{s.get('database') or '(workload default)'}{flags}\n```sql\n{_cut(s.get('sql') or '', sql_cap)}\n```")
    if run and run.get("statements"):
        t = run.get("totals") or {}
        lines.append(f"Last run {run.get('id') or ''} ({run.get('label') or 'no label'}, {run.get('state') or '?'}, "
                     f"{run.get('elapsed_s')} s): {t.get('count')} executions, {t.get('qps')} per s, p50 {t.get('p50_ms')} ms, "
                     f"p95 {t.get('p95_ms')} ms, errors {t.get('errors')}.")
        for s in run["statements"][:MAX_STATEMENTS]:
            err = (s.get("error_samples") or [{}])[0].get("error") if s.get("error_samples") else None
            lines.append(f"- [{s.get('id')}] {s.get('name')}: {s.get('count')} runs, p50 {s.get('p50_ms')} ms, "
                         f"p95 {s.get('p95_ms')} ms, errors {s.get('errors')}, rows avg {s.get('rows_avg')}"
                         + (f"; error: {_cut(err, 160)}" if err else ""))
    else:
        lines.append("No run results yet.")
    if tables:
        lines.append("Tables the statements read:")
        for name, info in list(tables.items())[:20]:
            if info.get("missing"):
                lines.append(f"- {name}: not found ({_cut(info.get('error') or '', 120)})")
                continue
            head = f"- {name}" + (f" (~{info['rows']:,} rows)" if isinstance(info.get("rows"), int) else "")
            card = info.get("cardinality") or {}
            if card:
                head += "; distinct values: " + ", ".join(f"{k} {v:,}" for k, v in list(card.items())[:40])
            lines.append(head)
            if ddl_cap and info.get("ddl"):
                lines.append(f"```sql\n{_cut(info['ddl'], ddl_cap)}\n```")
    return "\n".join(lines)


# ------------------------------------------------------------------ statements from the query history

def from_history(hours: float = 24, top: int = 10, min_ms: int = 0) -> list[dict[str, Any]]:
    """The heaviest read-only query shapes of the last ``hours`` (from MV_TRACE_EVENTS) as workload statements,
    with each shape's observed rate as runs per minute."""
    from . import history_store, query_advisor
    from .db import db

    rows = db.execute(
        "/* s2-workload */ SELECT DETAILS::$query_text AS q, DETAILS::$context_database AS db, DETAILS::%%duration_ms AS ms"
        " FROM information_schema.MV_TRACE_EVENTS WHERE EVENT_TYPE = 'Query_completion' AND DETAILS::%%success = 1"
        " AND TIME > NOW() - INTERVAL %s SECOND AND DETAILS::%%duration_ms >= %s"
        " AND DETAILS::$query_text NOT LIKE %s AND DETAILS::$query_text NOT LIKE %s ORDER BY TIME DESC LIMIT 20000",
        (int(hours * 3600), int(min_ms), f"%{history_store.MARKER_PREFIX}%", "%/* workload:%"),
    )[1]
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        sql = query_advisor.strip(r["q"] or "").rstrip().rstrip(";").rstrip()
        if not sql or len(sql) > 50_000 or not compare.read_only(sql):
            continue
        key = history_store.shape_of(sql) + "@" + str(r["db"] or "")
        g = groups.setdefault(key, {"sql": sql, "database": r["db"] or None, "runs": 0, "total_ms": 0, "ms": []})
        g["runs"] += 1
        g["total_ms"] += int(r["ms"] or 0)
        g["ms"].append(int(r["ms"] or 0))
    ranked = sorted(groups.values(), key=lambda g: -g["total_ms"])[: max(1, min(MAX_STATEMENTS, int(top)))]
    out = []
    for i, g in enumerate(ranked):
        per_minute = round(g["runs"] / (hours * 60), 2)
        out.append({"id": f"h{i + 1}", "name": " ".join(g["sql"].split())[:60], "sql": g["sql"], "database": g["database"],
                    "per_minute": max(0.1, per_minute), "enabled": True,
                    "history": {"runs": g["runs"], "median_ms": int(statistics.median(g["ms"])), "total_ms": g["total_ms"]}})
    return out
