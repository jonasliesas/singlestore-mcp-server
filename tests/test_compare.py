"""Before/after comparison of a table's queries against a new table (runs SELECTs only)."""

from __future__ import annotations

import json

import pytest
from support import DB, NEW_TABLE, TABLE, McpSession

pytestmark = pytest.mark.cluster

OLD = f"{DB}.{TABLE}"


def test_compare_candidates_and_run(cluster: McpSession) -> None:
    d = cluster.ok("query_history_compare_candidates", {"table": OLD, "new_table": NEW_TABLE})
    qs = d.get("queries", []) if isinstance(d, dict) else []
    if not qs:
        pytest.skip(f"no traced queries on {OLD} to compare: {str(d)[:200]}")
    s = cluster.ok("query_history_compare_start", {"table": OLD, "new_table": NEW_TABLE, "shapes": [qs[0]["shape"]],
                                                    "runs": 1, "timeout_s": 60})
    jid = s.get("job_id") or s.get("id")
    assert jid, f"no job id: {str(s)[:300]}"
    try:
        st, secs = cluster.wait_until("query_history_compare_status", {"job_id": jid},
                                      lambda d: isinstance(d, dict) and d.get("state") not in ("running", "queued", "starting"), 180, 1)
        assert secs is not None, f"comparison still running after 180 s: {json.dumps(st)[:300]}"
        assert st.get("state") == "done", json.dumps(st)[:500]
        assert st.get("table") == OLD and st.get("new_table") == NEW_TABLE
    finally:
        cluster.call("query_history_compare_cancel", {"job_id": jid})


def test_compare_refuses_bad_names(cluster: McpSession) -> None:
    cluster.err("query_history_compare_candidates", {"table": "nodot", "new_table": "x"})


def test_compare_unknown_job(cluster: McpSession) -> None:
    r = cluster.call("query_history_compare_status", {"job_id": "zz-no-such-job"})
    assert r.is_error or (isinstance(r.data, dict) and r.data.get("state") in (None, "unknown", "missing")), r.text[:300]
