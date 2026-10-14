"""In-app Claude assistants (SQL Editor, Query History, notebook). These spend Claude usage: they only run
with ``pytest --assistants``."""

from __future__ import annotations

import json
import time

import pytest
from support import DB, McpSession

pytestmark = [pytest.mark.cluster, pytest.mark.assistants]


def test_sql_editor_assistant(cluster: McpSession) -> None:
    eid = "e-zzpytest"
    cluster.ok("sql_editor_assistant_warm", {"editor_id": eid})
    t0 = time.perf_counter()
    cluster.ok("sql_editor_ask", {"editor_id": eid, "question": "Reply with exactly: REGRESSION-OK", "database": DB,
                                  "sql": "SELECT 1", "selected": False})
    try:
        d, secs = cluster.wait_until("sql_editor_inbox", {"editor_id": eid, "after": 0},
                                     lambda d: isinstance(d, dict) and "REGRESSION-OK" in json.dumps(d.get("messages") or []), 240, 2)
        assert secs is not None, f"no reply after {time.perf_counter() - t0:.0f} s: {json.dumps(d)[:400]}"
    finally:
        cluster.call("sql_editor_ask_cancel", {"editor_id": eid})


def test_query_history_assistant(cluster: McpSession) -> None:
    ev = (cluster.ok("query_history_data", {"min_ms": 1000, "hours": 168, "limit": 1}).get("events") or [])
    if not ev:
        pytest.skip("no traced queries in the last 7 days")
    key = ev[0]["key"]
    cluster.ok("query_history_assistant_warm")
    cluster.ok("query_history_ask", {"key": key, "question": "In one sentence: is this query slow because of the data volume?"})
    try:
        d, secs = cluster.wait_until("query_history_answer", {"key": key},
                                     lambda d: isinstance(d, dict) and (d.get("answer") or {}).get("text") and not d.get("job"), 300, 2)
        assert secs is not None, f"no answer: {json.dumps(d)[:400]}"
        assert len(d["answer"]["text"]) > 10
    finally:
        cluster.call("query_history_ask_cancel", {"key": key})


def test_workload_assistant(cluster: McpSession) -> None:
    cid = "wc-zzpytest-1"
    w = {"name": "zz pytest", "database": DB, "settings": {"pacing": "rate", "concurrency": 2},
         "statements": [{"id": "s1", "name": "Count", "sql": "SELECT COUNT(*) FROM CARS", "per_minute": 60}]}
    cluster.ok("workload_assistant_warm", {"chat_id": cid})
    cluster.ok("workload_ask", {"chat_id": cid, "workload": w,
                                "question": "Give one variant of statement 1 that filters on Cylinders with an int placeholder over the real "
                                            "range, as one ```sql block with a -- name: line. Then the word WORKLOAD-OK."})
    try:
        d, secs = cluster.wait_until("sql_editor_inbox", {"editor_id": cid, "after": 0},
                                     lambda d: isinstance(d, dict) and "WORKLOAD-OK" in json.dumps(d.get("messages") or []), 300, 2)
        assert secs is not None, f"no reply: {json.dumps(d)[:400]}"
        answer = d["messages"][-1]["text"]
        assert "```sql" in answer and "{{int:" in answer and "-- name:" in answer, answer[:800]
    finally:
        cluster.call("sql_editor_ask_cancel", {"editor_id": cid})


def test_notebook_assistant(cluster: McpSession) -> None:
    nid = "zz-pytest-nb-ask"
    cluster.ok("notebook_assistant_warm", {"notebook_id": nid})
    cluster.ok("notebook_ask", {"notebook_id": nid, "question": "Reply with exactly: NOTEBOOK-OK"})
    try:
        d, secs = cluster.wait_until("sql_editor_inbox", {"editor_id": nid, "after": 0},
                                     lambda d: isinstance(d, dict) and "NOTEBOOK-OK" in json.dumps(d.get("messages") or []), 240, 2)
        assert secs is not None, f"no reply: {json.dumps(d)[:400]}"
    finally:
        cluster.call("sql_editor_ask_cancel", {"editor_id": nid})
