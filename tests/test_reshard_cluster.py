"""On the cluster: the Schema Explorer's reshard simulation on SASDP.CARS (read-only) - the predicted rows per
partition add up to the table, a sample is scaled up, and bad columns are refused before anything runs - and the
reshard script, generated for zz_test_ tables in the `test` database and run there (the only writes)."""

from __future__ import annotations

import re

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


# ----------------------------------------------------------------------------- reshard script, run on a zz_test_ copy
# Writes only zz_test_ tables and views in the `test` database (made here, dropped in teardown, checked to be gone).

T = "test"


def _statements(script: str) -> list[str]:
    body = "\n".join(line for line in script.splitlines() if not line.startswith("--"))
    return [s.strip() for s in body.split(";\n") if s.strip().rstrip(";").strip()]


def _qi(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _leftovers(cluster: McpSession, tag: str) -> list[str]:
    r = cluster.ok("run_sql", {"sql": f"SELECT TABLE_NAME AS t FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{T}'"
                                      " AND TABLE_NAME LIKE 'zz\\_test\\_%'"})
    return [x["t"] for x in r.get("rows", []) if tag in x["t"]]


@pytest.fixture
def zz_tag(cluster: McpSession):
    import secrets

    tag = secrets.token_hex(3)
    yield tag
    for _ in range(2):  # views first, then the tables they read
        for name in _leftovers(cluster, tag):
            cluster.call("run_sql", {"sql": f"DROP VIEW IF EXISTS {_qi(T)}.{_qi(name)}"})
            cluster.call("run_sql", {"sql": f"DROP TABLE IF EXISTS {_qi(T)}.{_qi(name)}"})
    assert not _leftovers(cluster, tag), "zz_test_ objects left behind"


@pytest.mark.parametrize("kind", ["columnstore", "rowstore"])
def test_reshard_script_runs_on_a_zz_test_copy(cluster: McpSession, zz_tag: str, kind: str) -> None:
    """Generate the script for a zz_test_ table in `test` and run every statement of it there: the swapped-in table
    has the new SHARD KEY and keeps the rest of its DDL (SORT KEY / keys / AUTO_INCREMENT / comment), the rows,
    a view that reads it by name, and new AUTO_INCREMENT values above the copied ones (AGGREGATOR SYNC)."""
    if kind == "columnstore":
        table = f"zz_test_rs_{zz_tag}"
        ddl = (f"CREATE TABLE {_qi(T)}.{_qi(table)} (id BIGINT NOT NULL AUTO_INCREMENT, grp INT NOT NULL,"
               " name VARCHAR(20) COMMENT 'it''s a SHARD KEY (grp) note', SORT KEY (grp), KEY (id) USING HASH, SHARD KEY (grp))")
    else:  # a rowstore table, sharded by its PRIMARY KEY, with a name that needs backquotes
        table = f"zz_test_rs `odd` {zz_tag}"
        ddl = (f"CREATE ROWSTORE TABLE {_qi(T)}.{_qi(table)} (id BIGINT NOT NULL AUTO_INCREMENT, grp INT NOT NULL,"
               " name VARCHAR(20), PRIMARY KEY (id, grp), KEY (name))")
    fq = f"{_qi(T)}.{_qi(table)}"
    view = f"zz_test_rsv_{zz_tag}"
    cluster.ok("run_sql", {"sql": ddl})
    cluster.ok("run_sql", {"sql": f"INSERT INTO {fq} (grp, name) VALUES " + ", ".join(f"({i % 3}, 'n{i}')" for i in range(300))})
    cluster.ok("run_sql", {"sql": f"CREATE VIEW {_qi(T)}.{_qi(view)} AS SELECT grp, COUNT(*) AS n FROM {fq} GROUP BY grp"})

    d = cluster.ok("schema_explorer_reshard_script", {"database": T, "table": table, "columns": ["id"],
                                                     "simulation": {"rsd_pct": 1.0, "current_rsd_pct": 90.0, "elapsed_ms": 400}})
    script = d["script"]
    assert "Nothing has been run" in script and f"`{T}`.`{view}`" in script, script[:2000]
    assert d["new_table"] == f"{table}__reshard" and d["rows"] == 300
    assert cluster.ok("run_sql", {"sql": f"SELECT COUNT(*) AS n FROM {fq}"})["rows"][0]["n"] == 300, "making the script ran something"
    assert not [t for t in _leftovers(cluster, zz_tag) if t.endswith(("__reshard", "__old"))]

    results = []
    for stmt in _statements(script):
        results.append(cluster.ok("run_sql", {"sql": stmt}))
    counts = next(r["rows"][0] for r in results if r.get("rows") and "rows_old" in r["rows"][0])
    assert counts["rows_old"] == counts["rows_new"] == 300
    skew = next(r["rows"][0] for r in results if r.get("rows") and "row_variation_pct" in r["rows"][0])
    assert int(skew["rows"]) == 300 and int(skew["partitions"]) > 1

    now = cluster.ok("run_sql", {"sql": f"SHOW CREATE TABLE {fq}"})["rows"][0]["Create Table"]
    assert re.search(r"SHARD KEY (`[^`]*` )?\(`id`\)", now), now
    assert "AUTO_INCREMENT" in now
    if kind == "columnstore":
        assert "SORT KEY" in now and "(`grp`)" in now and "USING HASH" in now and r"it\'s a SHARD KEY (grp) note" in now, now
    else:
        assert now.startswith("CREATE ROWSTORE TABLE") and "PRIMARY KEY (`id`,`grp`)" in now, now
    old = cluster.ok("run_sql", {"sql": f"SHOW CREATE TABLE {_qi(T)}.{_qi(table + '__old')}"})["rows"][0]["Create Table"]
    shard_line = next((line for line in old.splitlines() if line.strip().lstrip(", ").startswith("SHARD KEY")), "")
    assert ("(`grp`)" in shard_line) if kind == "columnstore" else ("(`id`" not in shard_line), old
    v = cluster.ok("run_sql", {"sql": f"SELECT SUM(n) AS n FROM {_qi(T)}.{_qi(view)}"})["rows"][0]["n"]
    assert int(v) == 300, "the view follows the name to the new table"
    top = cluster.ok("run_sql", {"sql": f"SELECT MAX(id) AS m FROM {fq}"})["rows"][0]["m"]
    cluster.ok("run_sql", {"sql": f"INSERT INTO {fq} (grp, name) VALUES (9, 'after')"})
    new_id = cluster.ok("run_sql", {"sql": f"SELECT id FROM {fq} WHERE name = 'after'"})["rows"][0]["id"]
    assert int(new_id) > int(top), "AGGREGATOR SYNC AUTO_INCREMENT: new values start above the copied ones"


def test_reshard_script_refusals(cluster: McpSession, zz_tag: str) -> None:
    table = f"zz_test_rsk_{zz_tag}"
    cluster.ok("run_sql", {"sql": f"CREATE TABLE {_qi(T)}.{_qi(table)} (a INT, b INT, PRIMARY KEY (a), SHARD KEY (a))"})
    assert "error 1744" in cluster.err("schema_explorer_reshard_script", {"database": T, "table": table, "columns": ["b"]})
    assert "already sharded" in cluster.err("schema_explorer_reshard_script", {"database": T, "table": table, "columns": ["a"]})
    assert "not found" in cluster.err("schema_explorer_reshard_script", {"database": T, "table": "zz_test_no_such", "columns": ["a"]})
