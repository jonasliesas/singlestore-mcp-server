"""On the cluster (read-only): the Schema Explorer's reshard simulation on SASDP.CARS - the predicted rows per
partition add up to the table, a sample is scaled up, and bad columns are refused before anything runs."""

from __future__ import annotations

import pytest
from support import DB, TABLE, McpSession

pytestmark = pytest.mark.cluster


def test_reshard_simulation(cluster: McpSession) -> None:
    now = cluster.ok("schema_explorer_skew", {"database": DB, "table": TABLE})
    if now["kind"] != "distributed":
        pytest.skip(f"{DB}.{TABLE} is a {now['kind']} table")
    r = cluster.ok("schema_explorer_reshard", {"database": DB, "table": TABLE, "columns": ["Make"]})
    assert r["sql"].startswith("SELECT WITH(leaf_pushdown=true)") and "GROUP BY `Make`" in r["sql"]
    assert r["partition_count"] == now["partition_count"] and r["rows_total"] == now["rows_total"]
    assert sum(p["rows"] for p in r["partitions"]) == now["rows_total"]
    assert [p["current_rows"] for p in r["partitions"]] == [p["rows"] for p in now["partitions"]]
    assert 0 < r["distinct_keys"] <= now["rows_total"] and r["current"]["rows"]["rsd_pct"] == now["rows"]["rsd_pct"]
    assert r["fraction"] is None and r["notes"]

    many = cluster.ok("schema_explorer_reshard", {"database": DB, "table": TABLE, "columns": ["Make", "Model", "DriveTrain"]})
    assert many["distinct_keys"] > r["distinct_keys"]

    sample = cluster.ok("schema_explorer_reshard", {"database": DB, "table": TABLE, "columns": ["Model"], "sample_rows": 200})
    assert 0 < sample["fraction"] < 1 and "RAND() <" in sample["sql"] and 0 < sample["sampled_rows"] < now["rows_total"]

    assert "No column" in cluster.err("schema_explorer_reshard", {"database": DB, "table": TABLE, "columns": ["make"]})
    assert "not found" in cluster.err("schema_explorer_reshard", {"database": DB, "table": "zz_no_such_table", "columns": ["x"]})
