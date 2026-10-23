"""SAS runs from the SQL Editor, offline: background runs, Stop (Compute job cancel / CAS action cancel), the
queue for a busy session, paging of a run's result table, sessions per SAS Viya connection, the job time limit
and the FedSQL driver trace. Viya is replaced by fakes."""

from __future__ import annotations

import threading
import time
import urllib.parse
from typing import Any

import pytest

from singlestore_mcp import sas_rest, sas_runs, sas_sql, sas_viya
from singlestore_mcp.apps import sas_workspace as ws


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    sas_runs._runs.clear()
    sas_runs._tables.clear()
    monkeypatch.setattr(sas_viya, "active_name", lambda: "Zeus")
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: {"name": name or "Zeus", "url": f"https://{name or 'zeus'}.example",
                                                                  "compute_context": "ctx", "cas_server": "cas-shared-default",
                                                                  "libref": "S2", "auto_libname": False, "startup_code": ""})
    monkeypatch.setattr(sas_rest, "_sessions", {})
    yield
    for r in list(sas_runs._runs.values()):
        r.cancel.set()


def _wait(run: sas_runs.Run, timeout: float = 5) -> None:
    assert run.done.wait(timeout), f"run {run.id} didn't finish ({run.state})"


# ----------------------------------------------------------------------------- sessions per connection


def test_one_session_pair_per_connection() -> None:
    a = sas_rest.sessions()
    assert a is sas_rest.sessions("Zeus") and a[0].conn == "Zeus" and a[1].conn == "Zeus"
    b = sas_rest.sessions("Other")
    assert b is not a and b[0].conn == "Other"
    # compute / cas follow the active connection.
    assert sas_rest.compute.lock is a[0].lock
    sas_rest.compute.libraries = ["x"]
    assert a[0].libraries == ["x"]


def test_requests_use_the_connections_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(sas_rest, "request", lambda method, url=None, **kw: seen.append((method, url, kw.get("conn"))) or (404, {}, {}))
    c = sas_rest.sessions("Other")[0]
    c.sid, c.key = "S9", ("https://Other.example", "ctx")
    with pytest.raises(LookupError):
        c.columns("WORK", "X")
    assert seen and seen[0][1].startswith("https://Other.example/compute/sessions/S9/") and seen[0][2] == "Other"


def test_job_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SINGLESTORE_MCP_SAS_TIMEOUT_MIN", raising=False)
    assert sas_rest.job_timeout() == sas_rest.DEFAULT_TIMEOUT_MIN * 60 >= 4 * 3600
    monkeypatch.setenv("SINGLESTORE_MCP_SAS_TIMEOUT_MIN", "30")
    assert sas_rest.job_timeout() == 1800
    monkeypatch.setenv("SINGLESTORE_MCP_SAS_TIMEOUT_MIN", "0")
    assert sas_rest.job_timeout() is None


# ----------------------------------------------------------------------------- cancel on Viya


class FakeViya:
    """The Compute job / session state endpoints and the CAS action cancel."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.job_put_status = 200

    def request(self, method: str, url: str | None = None, **kw: Any) -> tuple[int, Any, dict[str, str]]:
        path = urllib.parse.urlsplit(url).path
        self.calls.append((method, path, kw.get("headers") or {}))
        if method == "GET" and "/jobs/" in path:
            return 200, {"id": "J1", "state": "running"}, {"etag": '"job-etag"'}
        if method == "GET":
            return 200, {"id": "S1"}, {"etag": '"session-etag"'}
        if method == "PUT" and "/jobs/" in path:
            return self.job_put_status, "canceled", {}
        if method == "PUT":
            return 200, "canceled", {}
        if method == "DELETE" and path.endswith("/actions"):
            return 200, {}, {}
        return 500, {}, {}


def test_compute_cancel_uses_the_jobs_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeViya()
    monkeypatch.setattr(sas_rest, "request", fake.request)
    c = sas_rest.Compute("Zeus")
    assert c.cancel_job("S1/jobs/J1")
    put = [x for x in fake.calls if x[0] == "PUT"]
    assert put == [("PUT", "/compute/sessions/S1/jobs/J1/state", {"If-Match": '"job-etag"'})]
    # Viya refuses the job cancel: the session's running code is cancelled instead.
    fake.calls.clear()
    fake.job_put_status = 412
    assert c.cancel_job("S1/jobs/J1")
    assert [x[1] for x in fake.calls if x[0] == "PUT"] == ["/compute/sessions/S1/jobs/J1/state", "/compute/sessions/S1/state"]
    assert fake.calls[-1][2] == {"If-Match": '"session-etag"'}


def test_cas_cancel_deletes_the_running_action(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeViya()
    monkeypatch.setattr(sas_rest, "request", fake.request)
    k = sas_rest.Cas("Zeus")
    assert not k.cancel()  # no session: nothing to cancel
    k.sid = "C1"
    assert k.cancel()
    assert fake.calls[-1][:2] == ("DELETE", "/cas-shared-default-http/cas/sessions/C1/actions")


# ----------------------------------------------------------------------------- background runs, Stop, queue


class BlockingCompute(sas_rest.Compute):
    """A Compute session whose job runs until it's cancelled (or ``release`` is set)."""

    def __init__(self) -> None:
        super().__init__("Zeus")
        self.release = threading.Event()
        self.cancelled: list[str] = []
        self.submitted: list[str] = []

    def run(self, code: str, *, listing: bool = False, ctl: Any = None) -> dict[str, Any]:
        with self.lock:
            self.submitted.append(code)
            if ctl is not None:
                ctl.job = f"S1/jobs/J{len(self.submitted)}"
            while not self.release.is_set():
                if self.cancelled:
                    return {"state": "canceled", "log": [{"line": "NOTE: partial log line", "type": "note"}], "listing": "", "ms": 1}
                time.sleep(0.02)
            return {"state": "completed", "log": [{"line": "NOTE: done", "type": "note"}], "listing": "", "ms": 1}

    def cancel_job(self, job: str) -> bool:
        self.cancelled.append(job)
        return True

    def rows(self, libref: str, table: str, limit: int, start: int = 0) -> dict[str, Any]:
        data = [{"N": i} for i in range(250)]
        return {"columns": ["N"], "column_meta": [{"name": "N", "type": "num"}], "rows": data[start:start + limit],
                "total_rows": 250}


@pytest.fixture
def blocking(monkeypatch: pytest.MonkeyPatch) -> BlockingCompute:
    c = BlockingCompute()
    pair = (c, sas_rest.Cas("Zeus"))
    monkeypatch.setattr(sas_rest, "sessions", lambda conn=None: pair)
    monkeypatch.setattr(ws.editor_state, "add_history", lambda *a, **k: None)
    return c


def test_stop_cancels_the_job_and_keeps_the_partial_log(blocking: BlockingCompute) -> None:
    r = ws.sas_query("procsql", "select * from sashelp.class", run_id="s_stop_1", wait_ms=200, tab_id="T1", tab="untitled 1")
    d = r.structured_content
    assert d["running"] and d["run_id"] == "s_stop_1" and d["state"] in ("queued", "running")
    time.sleep(0.1)
    st = ws.sas_stop("s_stop_1").structured_content
    assert st["stopped"] and st["cancel_sent"] is True and blocking.cancelled == ["S1/jobs/J1"]
    d = ws.sas_run_status("s_stop_1", wait_ms=5000).structured_content
    assert not d["running"] and d["stopped"] and d["run_state"] == "stopped" and not d["error"]
    assert "partial log line" in d["log"] and not d["has_result_set"]
    assert ws.sas_stop("s_stop_1").structured_content["stopped"] is False  # already done


def test_second_run_waits_for_the_first_and_says_for_which_tab(blocking: BlockingCompute) -> None:
    ws.sas_query("procsql", "select 1 as a from sashelp.class", run_id="s_first_1", wait_ms=100, tab_id="T1", tab="Report A")
    d = ws.sas_query("procsql", "select 2 as b from sashelp.class", run_id="s_second", wait_ms=300, tab_id="T2", tab="untitled 2").structured_content
    assert d["running"] and d["state"] == "queued"
    assert d["waiting_for"]["tab"] == "Report A" and d["waiting_for"]["run_id"] == "s_first_1"
    # A waiting run can be stopped without touching SAS.
    assert ws.sas_stop("s_second").structured_content["stopped"]
    d = ws.sas_run_status("s_second", wait_ms=3000).structured_content
    assert d["stopped"] and len(blocking.submitted) == 1
    blocking.release.set()
    d = ws.sas_run_status("s_first_1", wait_ms=5000).structured_content
    assert not d["running"] and d["has_result_set"] and d["row_count"] == 250 and d["total_rows"] == 250


def test_paging_a_runs_result_table(blocking: BlockingCompute) -> None:
    blocking.release.set()
    d = ws.sas_query("procsql", "select n from work.t", run_id="s_page_1", max_rows=100, table_rows=100_000, tab_id="T1").structured_content
    assert d["row_count"] == 100 and d["truncated"] and d["total_rows"] == 250 and "outobs=100000" in d["code"]
    assert sas_sql.result_table("T1") in d["code"]
    p = ws.sas_rows("s_page_1", offset=100, limit=100).structured_content
    assert [r["N"] for r in p["rows"]] == list(range(100, 200)) and p["more"]
    p = ws.sas_rows("s_page_1", offset=200, limit=100).structured_content
    assert len(p["rows"]) == 50 and not p["more"]
    # The tab's next run replaces its result table: the old result can't be paged any more.
    ws.sas_query("procsql", "select n from work.u", run_id="s_page_2", tab_id="T1")
    with pytest.raises(LookupError, match="replaced"):
        ws.sas_rows("s_page_1", offset=0, limit=10)
    # Another tab has its own table.
    assert sas_sql.result_table("T2") != sas_sql.result_table("T1")


def test_a_stop_that_doesnt_end_the_run_offers_a_restart(blocking: BlockingCompute, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(blocking, "cancel_job", lambda job: False)
    ws.sas_query("procsql", "select 1 as a from sashelp.class", run_id="s_stuck_1", wait_ms=200)
    st = ws.sas_stop("s_stuck_1").structured_content
    assert st["cancel_sent"] is False and st["cancel_failed"]
    blocking.release.set()
    ws.sas_run_status("s_stuck_1", wait_ms=5000)


def test_reset_stops_the_runs_on_that_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    resets = []
    monkeypatch.setattr(sas_rest, "reset", lambda conn=None: resets.append(conn))
    monkeypatch.setattr(ws, "_status", lambda connection=None: {"connection": connection})
    gate = threading.Event()
    run = sas_runs.start("s_reset_1", "Other", "compute", {}, "x", lambda r: (gate.wait(5), {"ok": True})[1])
    time.sleep(0.1)
    d = ws.sas_session_reset("Other").structured_content
    assert d["stopped_runs"] == ["s_reset_1"] and resets == ["Other"] and run.cancel.is_set()
    gate.set()
    _wait(run)


def test_unknown_run_and_bad_ids() -> None:
    with pytest.raises(LookupError):
        ws.sas_run_status("s_nosuchrun")
    with pytest.raises(ValueError):
        sas_runs.start("../x", None, "compute", {}, "x", lambda r: {})


# ----------------------------------------------------------------------------- FedSQL driver trace


def test_auto_library_has_the_driver_trace() -> None:
    code = sas_sql.auto_libname_code("S2", "h", 3306, "u", "pw", "DB", trace=True)
    assert "driver_trace=SQL" in code and "driver_traceoptions=APPEND" in code and "_s2trace.log" in code
    assert code.index("nosource") < code.index("driver_trace")
    assert "driver_trace" not in sas_sql.auto_libname_code("S2", "h", 3306, "u", "pw", "DB")


def test_driver_trace_statements_and_fedsql_verdict() -> None:
    lines = ["[DIAG] SQLState:HY092", "DEFAULT: select * from `MyDB`.`CARS` where 1=0",
             'DRIVER SQL: "SELECT * FROM `MYDB`.`CARS` WHERE 1=0 " on connection 0x1',
             "DEFAULT: select COUNT ( * )  as `N` from `MyDB`.`CARS` where  ( (`Make` = 'Audi') )",
             "DEFAULT: select COUNT ( * )  as `N` from `MyDB`.`CARS` where  ( (`Make` = 'Audi') )"]
    traced = sas_sql.driver_trace_statements(lines)
    assert [t["sql"] for t in traced] == ["select COUNT ( * )  as `N` from `MyDB`.`CARS` where  ( (`Make` = 'Audi') )"]
    assert traced[0]["engine"] == "SSTORE"  # the editor offers "Find in Query History" for it
    plan = "SeqScan from S2.CARS {Push Down}\n"
    v = sas_sql.pushdown_verdict("fedsql", plan, traced=traced)
    assert v["verdict"] == "full" and v["sent"] == traced and "DRIVER_TRACE" in v["notes"][0]
    v = sas_sql.pushdown_verdict("fedsql", plan, trace_note="not traced here")
    assert v["sent"][0]["engine"] == "FEDSQL" and v["notes"] == ["not traced here"]
    # A plain table read (no plan lines kept): the whole table went to SAS.
    pulled = sas_sql.driver_trace_statements(["DEFAULT: select `MyDB`.`CARS`.`Make` from `MyDB`.`CARS`"])
    assert sas_sql.pushdown_verdict("fedsql", "", traced=pulled)["verdict"] == "pulled"


# ----------------------------------------------------------------------------- Find in Query History (plan cache)


def test_history_lookup_returns_a_query_history_plan_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short pushed-down statement is only in the plan cache: the lookup gives a Query History plan-cache row
    (summed over the aggregators) so the editor can open Query History on it."""
    from singlestore_mcp import history_store

    sql = "select COUNT ( * ) as `N` from `MyDB`.`CARS`"
    stmt = history_store.statement_key(sql, None)
    base = {"stmt": stmt, "text": sql, "text_len": len(sql), "database": "MyDB", "origin": None, "tables": ["CARS"],
            "failed": 0, "rows": 1, "exec_ms": 30.0, "avg_ms": 3.0, "cpu_ms": 2.0, "blob_wait_ms": 0, "queued_ms": 0,
            "avg_max_mem": 10, "avg_spill": 0, "avg_blob_miss": 0, "first_load": "2026-10-16 10:00:00", "warnings": None}
    plans = [{**base, "plan_key": "1:5:x", "node_id": 1, "runs": 10, "last_executed": "2026-10-16 10:05:00"},
             {**base, "plan_key": "2:7:y", "node_id": 2, "runs": 5, "exec_ms": 15.0, "last_executed": "2026-10-16 10:06:00"}]
    monkeypatch.setattr(history_store, "read_plancache", lambda: plans)

    class NoHistory:
        def execute(self, *a):
            return self

        def fetchone(self):
            return {"n": 0}

        def close(self):
            pass

    monkeypatch.setattr(history_store, "connect", lambda: NoHistory())
    d = ws.sas_history_lookup(sql, "MyDB").structured_content
    assert d["found"] and d["history"] == 0 and d["plan_cache"]["runs"] == 15
    p = d["plan"]
    assert p["key"] == f"plan:{stmt}" and p["runs"] == 15 and p["plan_keys"] == ["1:5:x", "2:7:y"] and p["nodes"] == [1, 2]
    assert p["avg_ms"] == 3.0 and p["last_executed"] == "2026-10-16 10:06:00" and p["pinned"]
