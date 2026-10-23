"""SAS runs from the SQL Editor: run in the background, can be stopped, queue for a busy SAS session.

A run (PROC SQL / FedSQL / FedSQL in CAS / plain SAS code) goes to a background thread right away; the tool that
started it waits a while (``wait``) and returns the result, or ``running`` with the run's id, and the editor
asks again (``sas_run_status``). Meanwhile:

- **Stop** cancels the job (Compute: the job's state set to ``canceled``; CAS: the session's running action
  cancelled), and the run ends "stopped" with the log so far. If Viya doesn't accept the cancel (or the run is
  still going 30 s later), the status says ``cancel_failed`` and the editor offers "Restart SAS session".
- **Queue**: a connection has one Compute and one CAS session, one job at a time. A run that waits for another
  one says which (``waiting_for``: its tab and how long it has run) and can be stopped while it waits, without
  touching SAS.
- **Paging**: the result table stays in the session (one per tab, ``sas_sql.result_table``), so the editor can
  page through it after the run (``page``), until the tab runs something else.

Runs are kept in memory for ``KEEP_SECONDS`` after they finished (at most ``MAX_RUNS``).
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from . import sas_rest, sas_viya
from .sas_rest import RunControl

KEEP_SECONDS = 30 * 60
MAX_RUNS = 100
STUCK_SECONDS = 30          # after Stop: still running this long -> offer to restart the session
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class Run(RunControl):
    def __init__(self, run_id: str, conn: str | None, engine: str, source: dict[str, Any], sql: str) -> None:
        super().__init__()
        self.id = run_id
        self.conn = conn                      # the SAS Viya connection (resolved name; None: none set up)
        self.engine = engine                  # "compute" | "cas"
        self.source = source                  # {tab_id, tab, file}
        self.sql = sql
        self.state = "queued"                 # queued -> running -> done | stopped | error
        self.created = time.time()
        self.started: float | None = None     # got the session
        self.ended: float | None = None
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None
        self.stop_requested: float | None = None
        self.cancel_sent: bool | None = None  # Viya accepted the cancel (None: nothing to cancel yet)
        self.done = threading.Event()
        self.last_access = time.monotonic()
        self.table: dict[str, Any] | None = None  # the result table, for paging

    @property
    def label(self) -> str:
        return str(self.source.get("tab") or "another tab")

    def info(self) -> dict[str, Any]:
        """Status for the editor (no result)."""
        now = time.time()
        out: dict[str, Any] = {"run_id": self.id, "state": self.state, "running": not self.done.is_set(),
                               "connection": self.conn, "engine": self.engine,
                               "elapsed_ms": round(((self.ended or now) - self.created) * 1000)}
        if self.state == "queued":
            holder = _holder(self)
            out["waiting_for"] = holder
        if self.stop_requested:
            out["stopping"] = not self.done.is_set()
            stuck = not self.done.is_set() and (self.cancel_sent is False or now - self.stop_requested > STUCK_SECONDS)
            out["cancel_failed"] = bool(stuck)
        return out


_runs: OrderedDict[str, Run] = OrderedDict()
_lock = threading.Lock()
# (connection, engine, table) -> id of the run whose result the table holds now
_tables: dict[tuple[str, str, str], str] = {}


def new_id() -> str:
    return "s" + secrets.token_hex(8)


def _session(run: Run) -> Any:
    c, k = sas_rest.sessions(run.conn)
    return k if run.engine == "cas" else c


def _holder(run: Run) -> dict[str, Any] | None:
    """What a queued run waits for: the editor run that holds the session (its tab), or the session busy with
    something else (the library list, a sign-on)."""
    sess = _session(run)
    if getattr(sess, "warming", False):  # the background warm-up is starting the session: the run gets it next
        return {"run_id": None, "tab": None, "elapsed_ms": None, "warming": True}
    cur = getattr(sess, "current_run", None)
    if isinstance(cur, Run) and cur is not run:
        return {"run_id": cur.id, "tab": cur.label, "tab_id": cur.source.get("tab_id"),
                "elapsed_ms": round((time.time() - (cur.started or cur.created)) * 1000)}
    return {"run_id": None, "tab": None, "elapsed_ms": None}


def start(run_id: str | None, conn: str | None, engine: str, source: dict[str, Any], sql: str,
          work: Callable[[Run], dict[str, Any]], on_finish: Callable[[Run], None] | None = None) -> Run:
    """Start ``work(run)`` in the background once the connection's session is free. ``work`` runs holding the
    session's lock; it returns the result (or raises)."""
    run_id = run_id or new_id()
    if not _ID.match(run_id):
        raise ValueError("run_id: 6-64 letters, digits, - or _")
    name = conn or sas_viya.active_name()
    run = Run(run_id, name, engine, dict(source or {}), sql)
    with _lock:
        old = _runs.pop(run_id, None)
        _runs[run_id] = run
    if old is not None and not old.done.is_set():
        stop(old.id)
    _prune()

    def main() -> None:
        sess = _session(run)
        got = False
        try:
            while not sess.lock.acquire(timeout=0.25):
                if run.cancel.is_set():
                    raise sas_rest.SasStopped("Stopped while it waited for the SAS session.")
            got = True
            sess.current_run = run
            if run.cancel.is_set():
                raise sas_rest.SasStopped("Stopped while it waited for the SAS session.")
            run.state, run.started = "running", time.time()
            run.result = work(run)
            run.state = "stopped" if run.result.get("stopped") else "done"
        except sas_rest.SasStopped as exc:
            run.state = "stopped"
            run.result = {"stopped": True, "state": "canceled", "error": None, "note": str(exc), "log": "", "log_types": [],
                          "columns": [], "rows": [], "has_result_set": False, "row_count": 0, "truncated": False,
                          "errors": 0, "warnings": 0, "affected_rows": None, "listing": None}
        except BaseException as exc:  # noqa: BLE001 - reported to the editor
            run.state, run.error = "error", exc
        finally:
            if got:
                sess.current_run = None
                sess.lock.release()
            run.ended = time.time()
            if on_finish:
                try:
                    on_finish(run)
                except Exception:  # noqa: BLE001 - history is best effort
                    pass
            run.done.set()

    threading.Thread(target=main, name=f"sas-run-{run_id}", daemon=True).start()
    return run


def get(run_id: str) -> Run:
    with _lock:
        run = _runs.get(run_id)
    if run is None:
        raise LookupError("This SAS run is no longer kept on the server: run it again.")
    run.last_access = time.monotonic()
    return run


def stop(run_id: str) -> dict[str, Any]:
    """Stop a run: a queued one just doesn't start; a running one is cancelled on Viya (the log so far stays)."""
    with _lock:
        run = _runs.get(run_id)
    if run is None or run.done.is_set():
        return {"run_id": run_id, "stopped": False, "note": "Nothing to stop."}
    run.stop_requested = run.stop_requested or time.time()
    run.cancel.set()
    if run.state == "queued":
        return {"run_id": run_id, "stopped": True, "note": "Stopped before it started."}
    sess = _session(run)
    if run.engine == "cas":
        run.cancel_sent = sess.cancel()
    elif run.job:
        run.cancel_sent = sess.cancel_job(run.job)
    # (no job yet: the run cancels it as soon as it's submitted, or doesn't submit it)
    return {"run_id": run_id, "stopped": True, "cancel_sent": run.cancel_sent, **run.info()}


def abort_connection(conn: str | None) -> list[str]:
    """Mark the runs on connection ``conn`` as stopped (its sessions are being restarted). Returns their ids."""
    name = conn or sas_viya.active_name()
    out = []
    with _lock:
        runs = [r for r in _runs.values() if r.conn == name and not r.done.is_set()]
    for r in runs:
        r.stop_requested = r.stop_requested or time.time()
        r.cancel.set()
        out.append(r.id)
    return out


def remember_table(run: Run, lib: str, table: str, meta: dict[str, Any]) -> None:
    """The run's result is in ``lib.table`` now (for paging); an earlier run's result there is gone."""
    key = (run.conn or "", run.engine, f"{lib}.{table}".upper())
    run.table = {"lib": lib, "table": table, **meta}
    with _lock:
        _tables[key] = run.id


def table_of(run: Run) -> dict[str, Any]:
    """The run's result table, if it still holds this run's result."""
    if not run.table:
        raise LookupError("This run has no result table to page through.")
    key = (run.conn or "", run.engine, f"{run.table['lib']}.{run.table['table']}".upper())
    with _lock:
        owner = _tables.get(key)
    if owner != run.id:
        raise LookupError("This result was replaced by a newer run in the same tab: run it again to page through it.")
    return run.table


def _prune() -> None:
    now = time.monotonic()
    with _lock:
        for rid, r in list(_runs.items()):
            if r.done.is_set() and now - r.last_access > KEEP_SECONDS:
                del _runs[rid]
        while len(_runs) > MAX_RUNS:
            rid = next((k for k, r in _runs.items() if r.done.is_set()), None)
            if rid is None:
                break
            del _runs[rid]


def running() -> list[dict[str, Any]]:
    with _lock:
        runs = [r for r in _runs.values() if not r.done.is_set()]
    return [{**r.info(), "tab": r.label, "tab_id": r.source.get("tab_id")} for r in runs]
