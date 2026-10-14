"""Schema Explorer skew (data distribution) maths and advice, offline: no cluster, the partition rows are made up."""

from __future__ import annotations

from singlestore_mcp.apps import schema_explorer as se


def _parts(rows: list[int], leaves: int = 2, disk_per_row: int = 10) -> list[dict]:
    return [{"partition": i, "host": f"leaf-{i % leaves}:3306", "rows": r, "memory_bytes": 0, "disk_bytes": r * disk_per_row}
            for i, r in enumerate(rows)]


def test_even_table() -> None:
    s = se.skew_stats(_parts([25_000, 25_100, 24_900, 25_000]))
    assert s["level"] == "ok" and s["rows"]["rsd_pct"] < 1 and s["partition_count"] == 4
    assert s["rows_total"] == 100_000 and s["data_bytes_total"] == 1_000_000
    assert [x["rows"] for x in s["leaves"]] == [49_900, 50_100]


def test_small_tables_dont_count() -> None:
    s = se.skew_stats(_parts([52, 104, 104, 64, 52, 52]))  # SASDP.CARS on the test cluster
    assert s["level"] == "small" and s["rows"]["rsd_pct"] > 30


def test_skew_measures() -> None:
    s = se.skew_stats(_parts([90_000, 10_000, 10_000, 10_000]))
    assert s["rows"]["avg"] == 30_000 and s["rows"]["max_over_avg_pct"] == 200.0
    assert s["rows"]["rsd_pct"] == 115.5  # population stddev / avg, like the docs' STDDEV(ROWS)/AVG(ROWS)
    assert s["level"] == "high" and s["largest"][0]["partition"] == 0 and s["largest"][0]["rows_share_pct"] == 75.0
    assert se.skew_level(100_000, 15.0, 30.0) == "moderate"
    assert se.skew_level(100_000, 5.0, 5.0) == "ok"
    assert se.skew_level(100_000, 5.0, 150.0) == "high"


def test_advice_for_a_shard_key() -> None:
    s = se.skew_stats(_parts([90_000, 10_000, 10_000, 10_000]))
    a = se.skew_advice(s, {"shard": {"columns": ["Origin"]}}, "SASDP", "Big`T")
    text = " ".join(a["notes"])
    assert a["level"] == "high" and not a["keyless"] and "low-cardinality" in text and "ALTER TABLE" in text
    assert a["check_sql"]["top_values"] == "SELECT `Origin`, COUNT(*) AS n FROM `SASDP`.`Big``T` GROUP BY `Origin` ORDER BY n DESC LIMIT 20;"
    assert a["docs"].startswith("https://docs.singlestore.com/db/v9.0/")


def test_advice_for_keyless_and_even() -> None:
    skewed = se.skew_stats(_parts([90_000, 10_000, 10_000, 10_000]))
    a = se.skew_advice(skewed, {"shard": {"columns": []}}, "d", "t")
    assert a["keyless"] and "force_random_reshuffle" in " ".join(a["notes"]) and a["check_sql"] is None
    even = se.skew_stats(_parts([25_000] * 4))
    assert se.skew_advice(even, None, "d", "t")["check_sql"] is None


def test_uneven_leaves_with_even_partitions() -> None:
    s = se.skew_stats([{"partition": i, "host": "a" if i < 6 else "b", "rows": 10_000, "memory_bytes": 0, "disk_bytes": 0}
                       for i in range(8)])
    assert s["level"] == "ok"
    assert "REBALANCE" in " ".join(se.skew_advice(s, {"shard": {"columns": ["id"]}}, "d", "t")["notes"])


def test_partitions_from_the_views() -> None:
    """Master partitions only; disk per partition = the largest node's segments (a replica holds the same)."""
    part_rows = [
        {"TABLE_NAME": "t", "ORDINAL": 0, "HOST": "leaf-0", "PORT": 3306, "PARTITION_TYPE": "Master", "ROWS": 10, "MEMORY_USE": 5},
        {"TABLE_NAME": "t", "ORDINAL": 1, "HOST": "leaf-1", "PORT": 3306, "PARTITION_TYPE": "Master", "ROWS": 30, "MEMORY_USE": 0},
        {"TABLE_NAME": "r", "ORDINAL": 0, "HOST": "leaf-0", "PORT": 3306, "PARTITION_TYPE": "Reference", "ROWS": 7, "MEMORY_USE": 0},
    ]
    disk_rows = [
        {"TABLE_NAME": "t", "part": 0, "NODE_ID": 2, "disk": "100"}, {"TABLE_NAME": "t", "part": 0, "NODE_ID": 3, "disk": "100"},
        {"TABLE_NAME": "t", "part": 1, "NODE_ID": 3, "disk": "300"},
    ]
    per = se._skew_partitions(part_rows, disk_rows)
    assert per["r"]["reference"] and not per["r"]["partitions"]
    p0, p1 = per["t"]["partitions"]
    assert (p0["host"], p0["rows"], p0["memory_bytes"], p0["disk_bytes"]) == ("leaf-0:3306", 10, 5, 100)
    assert p1["disk_bytes"] == 300
