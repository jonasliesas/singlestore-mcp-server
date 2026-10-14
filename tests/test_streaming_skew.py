"""On the cluster (read-only): the SQL Editor's streamed results (first page early, the rest in pages, Stop) and
the Schema Explorer's skew view."""

from __future__ import annotations

import time

import pytest
from support import DB, TABLE, McpSession

pytestmark = pytest.mark.cluster

BIG = "SELECT a.Make, a.Model, a.MSRP FROM CARS a, CARS b LIMIT 100000"  # 428 x 428 rows: plenty


def test_editor_streams_a_big_limit(cluster: McpSession) -> None:
    first = cluster.ok("sql_editor_query", {"sql": BIG, "database": DB, "stream_id": "r_ztest_big1"})
    assert first["stream_id"] == "r_ztest_big1" and first["columns"] == ["Make", "Model", "MSRP"]
    assert 0 < len(first["rows"]) <= 1000, "the first call should only carry the first page"
    deadline = time.monotonic() + 120
    st = first
    while not st["done"] and time.monotonic() < deadline:
        st = cluster.ok("sql_editor_rows", {"stream_id": "r_ztest_big1", "offset": 10**9, "limit": 0, "wait_ms": 2000})
    assert st["done"] and st["loaded"] == 100_000 and not st["capped"] and st["error"] is None, st
    page = cluster.ok("sql_editor_rows", {"stream_id": "r_ztest_big1", "offset": 99_000, "limit": 5000})
    assert len(page["rows"]) == 1000 and page["offset"] == 99_000
    # The run is in the editor's history with the full row count.
    hist = cluster.ok("sql_editor_history", {"search": "CARS a, CARS b LIMIT 100000", "limit": 1})["entries"]
    assert hist and hist[0]["rows"] == 100_000, hist


def test_editor_stop(cluster: McpSession) -> None:
    first = cluster.ok("sql_editor_query", {"sql": BIG.replace("100000", "99999"), "database": DB, "stream_id": "r_ztest_stop"})
    stop = cluster.ok("sql_editor_stop", {"stream_id": "r_ztest_stop"})
    st = cluster.ok("sql_editor_rows", {"stream_id": "r_ztest_stop", "offset": 10**9, "limit": 0, "wait_ms": 5000})
    if not stop["stopped"]:
        assert first["done"], "Stop found nothing to stop while the result was still loading"
        return
    assert st["done"] and st["stopped"] and st["loaded"] < 99_999 and st["error"] is None, st
    assert cluster.ok("sql_editor_stop", {"stream_id": "r_ztest_stop"})["stopped"] is False


def test_editor_stream_errors(cluster: McpSession) -> None:
    assert "doesn't exist" in cluster.err("sql_editor_query", {"sql": "SELECT * FROM zz_no_such_table", "database": DB})
    assert "no longer kept" in cluster.err("sql_editor_rows", {"stream_id": "r_zz_never_started"})


def test_query_grid_stops_reading_at_max_rows(cluster: McpSession) -> None:
    started = time.monotonic()
    d = cluster.ok("query_grid", {"sql": BIG, "database": DB, "max_rows": 1000})
    assert d["row_count"] == 1000 and d["truncated"], d
    assert time.monotonic() - started < 30


def test_skew_of_a_table_and_a_database(cluster: McpSession) -> None:
    t = cluster.ok("schema_explorer_skew", {"database": DB, "table": TABLE})
    assert t["kind"] in ("distributed", "reference"), t
    if t["kind"] == "distributed":
        assert t["partition_count"] > 0 and sum(p["rows"] for p in t["partitions"]) == t["rows_total"]
        assert t["level"] in ("ok", "moderate", "high", "small") and t["advice"]["docs"].startswith("https://docs.singlestore.com")
        assert t["leaves"] and t["rows"]["rsd_pct"] is not None
    d = cluster.ok("schema_explorer_skew", {"database": DB})
    assert d["tables"] and all(x["level"] in ("ok", "moderate", "high", "small") for x in d["tables"])
    order = {"high": 0, "moderate": 1, "ok": 2, "small": 3}
    assert [order[x["level"]] for x in d["tables"]] == sorted(order[x["level"]] for x in d["tables"])
    assert "not found" in cluster.err("schema_explorer_skew", {"database": DB, "table": "zz_no_such_table"})
