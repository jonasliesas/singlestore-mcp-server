"""Offline: the Schema Explorer's reshard script ("Change shard key…") from recorded
SHOW CREATE TABLE text (9.0.40) - columnstore and rowstore tables, keys from the PRIMARY KEY, AUTO_INCREMENT, computed
columns, names that need backquotes - and the warnings filled in from the cluster facts. Nothing runs."""

from __future__ import annotations

import datetime as dt

import pytest

from singlestore_mcp import reshard as rs

NOW = dt.datetime(2026, 10, 30, 21, 15, tzinfo=dt.timezone.utc)

# SHOW CREATE TABLE SASDP.CARS (columnstore, keyless; the COMMENTs hold escaped JSON), as 9.0.40 returns it.
CARS = (
    "CREATE TABLE `CARS` (\n"
    "  `Make` char(13) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '{ \\\"viya\\\":\\\"4.0\\\",\\\"bytes\\\":13}' ,\n"
    "  `Model` char(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT '{ \\\"viya\\\":\\\"4.0\\\",\\\"bytes\\\":40}' ,\n"
    "  `MSRP` double DEFAULT NULL COMMENT '{ \\\"viya\\\":\\\"4.0\\\",\\\"format\\\":\\\"DOLLAR8.\\\"}' ,\n"
    "  `Note` text CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT 'not a SHARD KEY (`Make`), it''s a comment',\n"
    # 9.0.40 writes a quote in a COMMENT as \' (seen in SHOW CREATE TABLE of a zz_test_ table)
    r"  `Other` int(11) DEFAULT NULL COMMENT 'it\'s a SHARD KEY (`Model`) () note' ," "\n"
    "  SORT KEY `__UNORDERED` ()\n"
    "  , SHARD KEY () \n"
    ") AUTOSTATS_CARDINALITY_MODE=INCREMENTAL AUTOSTATS_HISTOGRAM_MODE=CREATE AUTOSTATS_SAMPLING=ON "
    "SQL_MODE='STRICT_ALL_TABLES,NO_AUTO_CREATE_USER' CHARACTER SET=`utf8mb4` COLLATE=`utf8mb4_bin`"
)

# SASModel.rag_doc_events: columnstore, AUTO_INCREMENT column and table option, a hash key, an explicit shard key.
EVENTS = (
    "CREATE TABLE `rag_doc_events` (\n"
    "  `id` bigint(20) NOT NULL AUTO_INCREMENT,\n"
    "  `run_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,\n"
    "  `doc_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,\n"
    "  `recorded_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),\n"
    "  KEY `id` (`id`) USING HASH,\n"
    "  SHARD KEY `__SHARDKEY` (`run_id`),\n"
    "  SORT KEY `__UNORDERED` ()\n"
    ") AUTO_INCREMENT=2000000 AUTOSTATS_CARDINALITY_MODE=INCREMENTAL AUTOSTATS_HISTOGRAM_MODE=CREATE AUTOSTATS_SAMPLING=ON "
    "SQL_MODE='STRICT_ALL_TABLES,NO_AUTO_CREATE_USER' CHARACTER SET=`utf8mb4` COLLATE=`utf8mb4_bin`"
)

# A rowstore table with an explicit shard key inside its PRIMARY KEY, and secondary keys.
EPISODE = (
    "CREATE ROWSTORE TABLE `episode` (\n"
    "  `episode_id` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,\n"
    "  `hp_id` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,\n"
    "  `valid_from` datetime(6) NOT NULL,\n"
    "  `current` varchar(1) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL DEFAULT 'Y',\n"
    "  SHARD KEY `sk_e` (`hp_id`),\n"
    "  PRIMARY KEY (`hp_id`,`episode_id`,`valid_from`),\n"
    "  KEY `e_curr` (`episode_id`,`current`)\n"
    ") AUTOSTATS_CARDINALITY_MODE=PERIODIC AUTOSTATS_HISTOGRAM_MODE=CREATE "
    "SQL_MODE='STRICT_ALL_TABLES,NO_AUTO_CREATE_USER' CHARACTER SET=`utf8mb4` COLLATE=`utf8mb4_bin`"
)

# A rowstore table sharded by its PRIMARY KEY (no SHARD KEY clause) with names that need backquotes, and a
# computed column.
ODD = (
    "CREATE ROWSTORE TABLE `my ``odd`` table` (\n"
    "  `id` bigint(20) NOT NULL,\n"
    "  `we``ird col` int(11) NOT NULL,\n"
    "  `total` as (`id` + `we``ird col`) PERSISTED bigint(21),\n"
    "  PRIMARY KEY (`id`,`we``ird col`)\n"
    ") AUTOSTATS_CARDINALITY_MODE=PERIODIC SQL_MODE='STRICT_ALL_TABLES'"
)


def sql_lines(script: str) -> list[str]:
    return [line for line in script.splitlines() if line.strip() and not line.startswith("--")]


def test_columnstore_keyless_to_a_key() -> None:
    out = rs.build_script("SASDP", "CARS", CARS, ["Make", "Model"], {"rows": 428, "data_bytes": 2_000_000}, now=NOW)
    create = out["create"]
    # only the SHARD KEY changed; the comment that mentions a SHARD KEY stays as it is
    assert create == CARS.replace("CREATE TABLE `CARS`", "CREATE TABLE `SASDP`.`CARS__reshard`").replace(
        "  , SHARD KEY () \n", "  , SHARD KEY (`Make`, `Model`) \n")
    assert "COMMENT 'not a SHARD KEY (`Make`), it''s a comment'" in create and r"COMMENT 'it\'s a SHARD KEY (`Model`) () note'" in create and "SORT KEY `__UNORDERED` ()" in create
    lines = sql_lines(out["script"])
    assert lines[-3:] == [
        "WHERE DATABASE_NAME = 'SASDP' AND TABLE_NAME = 'CARS__reshard' AND PARTITION_TYPE = 'Master';",
        "ALTER TABLE `SASDP`.`CARS` RENAME TO `SASDP`.`CARS__old`;",
        "ALTER TABLE `SASDP`.`CARS__reshard` RENAME TO `SASDP`.`CARS`;"]
    assert "INSERT INTO `SASDP`.`CARS__reshard` SELECT * FROM `SASDP`.`CARS`;" in lines
    assert ("SELECT (SELECT COUNT(*) FROM `SASDP`.`CARS`) AS rows_old, (SELECT COUNT(*) FROM `SASDP`.`CARS__reshard`) "
            "AS rows_new;") in lines
    assert "-- DROP TABLE `SASDP`.`CARS__old`;" in out["script"].splitlines()
    assert not any(line.startswith("DROP") for line in lines), "the DROP stays commented out"
    assert "AGGREGATOR SYNC" not in out["script"] and "STOP PIPELINE" not in out["script"]
    head = out["script"].split("\n\n")[0]
    assert "Nothing has been run" in head and "-- Shard key: (keyless) now -> (Make, Model)" in head
    assert "about 3.8 MB in all" in head and "No pipeline loads into SASDP.CARS" in head
    assert "quiet window" in head and "no atomic swap" in head
    assert out["file_name"] == "reshard-20261030-2115-cars-make-model.sql"


def test_auto_increment_pipelines_views_grants() -> None:
    facts = {"rows": 5_000_000, "data_bytes": 3 * 1024 ** 3, "current_shard": ["run_id"],
             "pipelines": [{"name": "events_pipe", "state": "Running"}, {"name": "old pipe", "state": "Stopped"}],
             "views": ["`SASModel`.`events_v`"], "grants": ["SELECT to 'bob'@'%'", "SELECT to ROLE 'reader'"],
             "existing": ["rag_doc_events__old"], "connection": "Zeus (zeus.example:3306)", "prepared_by": "admin@%",
             "simulation": {"rsd_pct": 2.5, "current_rsd_pct": 48.0, "elapsed_ms": 30_000, "partition_count": 32}}
    out = rs.build_script("SASModel", "rag_doc_events", EVENTS, ["doc_id"], facts, now=NOW)
    assert "  SHARD KEY (`doc_id`),\n  SORT KEY `__UNORDERED` ()\n) AUTO_INCREMENT=2000000 " in out["create"]
    assert "`id` bigint(20) NOT NULL AUTO_INCREMENT," in out["create"] and "KEY `id` (`id`) USING HASH," in out["create"]
    lines = sql_lines(out["script"])
    assert lines[0] == "STOP PIPELINE `SASModel`.`events_pipe`;"  # only the running one
    assert "AGGREGATOR SYNC AUTO_INCREMENT ON `SASModel`.`rag_doc_events__reshard` ALL;" in lines
    assert lines.index("INSERT INTO `SASModel`.`rag_doc_events__reshard` SELECT * FROM `SASModel`.`rag_doc_events`;") \
        < lines.index("AGGREGATOR SYNC AUTO_INCREMENT ON `SASModel`.`rag_doc_events__reshard` ALL;")
    assert lines[-2:] == ["ALTER TABLE `SASModel`.`rag_doc_events__reshard` RENAME TO `SASModel`.`rag_doc_events`;",
                          "START PIPELINE `SASModel`.`events_pipe`;"]
    text = out["script"]
    assert "-- Connection: Zeus (zeus.example:3306)" in text and "-- Prepared by: admin@% on 2026-10-30 21:15" in text
    assert "-- Shard key: (run_id) now -> (doc_id)" in text and "2.5 % row variation predicted, 48.0 % now over 32 partitions" in text
    assert "3.0 GB" in text and "about 6.0 GB in all" in text
    assert "`events_pipe` (running), `old pipe` (stopped)" in text
    assert "Views that refer to SASModel.rag_doc_events: `SASModel`.`events_v`" in text
    assert "SELECT to 'bob'@'%'; SELECT to ROLE 'reader'" in text
    assert "SASModel.rag_doc_events__old already exists" in text
    assert "in 30 s" in text and "60 s–3 min" in text  # from the simulation's elapsed time (x2 to x6)


def test_rowstore_with_primary_key() -> None:
    out = rs.build_script("sl", "episode", EPISODE, ["episode_id"], now=NOW)
    assert out["create"].startswith("CREATE ROWSTORE TABLE `sl`.`episode__reshard` (\n")
    assert "  SHARD KEY (`episode_id`),\n  PRIMARY KEY (`hp_id`,`episode_id`,`valid_from`),\n" in out["create"]
    assert "KEY `e_curr` (`episode_id`,`current`)\n) AUTOSTATS_CARDINALITY_MODE=PERIODIC" in out["create"]
    # unique keys must contain every shard key column (9.0.40: error 1744)
    with pytest.raises(rs.ReshardError, match="error 1744"):
        rs.build_script("sl", "episode", EPISODE, ["current"])
    with pytest.raises(rs.ReshardError, match="already sharded on"):
        rs.build_script("sl", "episode", EPISODE, ["hp_id"])


def test_names_that_need_backquotes_and_computed_columns() -> None:
    out = rs.build_script("my db", "my `odd` table", ODD, ["we`ird col"], now=NOW)
    create = out["create"]
    assert create.startswith("CREATE ROWSTORE TABLE `my db`.`my ``odd`` table__reshard` (\n")
    # sharded by the PRIMARY KEY before (no SHARD KEY clause): one is added after the last item
    assert create.endswith("  PRIMARY KEY (`id`,`we``ird col`),\n  SHARD KEY (`we``ird col`)\n) AUTOSTATS_CARDINALITY_MODE=PERIODIC "
                           "SQL_MODE='STRICT_ALL_TABLES'")
    # the computed column isn't inserted into: an explicit column list
    assert out["insert"] == ("INSERT INTO `my db`.`my ``odd`` table__reshard` (`id`, `we``ird col`)\n"
                             "SELECT `id`, `we``ird col` FROM `my db`.`my ``odd`` table`;")
    lines = sql_lines(out["script"])
    assert "ALTER TABLE `my db`.`my ``odd`` table` RENAME TO `my db`.`my ``odd`` table__old`;" in lines
    assert "WHERE DATABASE_NAME = 'my db' AND TABLE_NAME = 'my `odd` table__reshard' AND PARTITION_TYPE = 'Master';" in lines
    assert out["new_table"] == "my `odd` table__reshard" and out["current_shard"] == ["id", "we`ird col"]


def test_refusals() -> None:
    with pytest.raises(rs.ReshardError, match="case-sensitive"):
        rs.build_script("SASDP", "CARS", CARS, ["make"])
    with pytest.raises(rs.ReshardError, match="Each column once"):
        rs.build_script("SASDP", "CARS", CARS, ["Make", "Make"])
    with pytest.raises(rs.ReshardError, match="Pick the columns"):
        rs.build_script("SASDP", "CARS", CARS, [])
    with pytest.raises(rs.ReshardError, match="reference table"):
        rs.build_script("d", "r", "CREATE REFERENCE TABLE `r` (\n  `a` int(11) DEFAULT NULL,\n  PRIMARY KEY (`a`)\n)", ["a"])
    with pytest.raises(rs.ReshardError, match="too long"):
        rs.build_script("d", "t" * 60, CARS.replace("`CARS`", f"`{'t' * 60}`"), ["Make"])
    with pytest.raises(rs.ReshardError, match="Not a CREATE TABLE"):
        rs.build_script("d", "v", "CREATE VIEW `v` AS SELECT 1", ["a"])


def test_view_references_and_simulation_numbers() -> None:
    assert rs.refers_to("SELECT `CARS`.`Make` AS `Make` FROM  `SASDP`.`CARS` as `CARS` ", "FANS", "SASDP", "CARS")
    assert rs.refers_to("SELECT `t`.`a` AS `a` FROM `t` as `t` ", "test", "test", "t")
    assert not rs.refers_to("SELECT `t`.`a` AS `a` FROM `t` as `t` ", "other", "test", "t")
    assert not rs.refers_to("SELECT `x`.`CARS` AS `CARS2` FROM `SASDP`.`x` as `x`", "SASDP", "SASDP", "CARS")
    assert rs.clean_simulation({"rsd_pct": "3.14159", "elapsed_ms": 1234.56, "fraction": 0.25, "evil": "x; DROP", "partition_count": -1}) == \
        {"rsd_pct": 3.1, "elapsed_ms": 1234.6, "fraction": 0.25}
    assert rs.clean_simulation("nope") == {}


def test_auto_increment_sync_with_a_backquote_in_the_name() -> None:
    # 9.0.40 refuses a table name with a backquote in AGGREGATOR SYNC AUTO_INCREMENT (error 1103): the whole database
    out = rs.build_script("SASModel", "ev`x", EVENTS.replace("`rag_doc_events`", "`ev``x`"), ["doc_id"], now=NOW)
    assert "AGGREGATOR SYNC AUTO_INCREMENT ON `SASModel` ALL;" in sql_lines(out["script"])
    out = rs.build_script("d`b", "ev", EVENTS.replace("`rag_doc_events`", "`ev`"), ["doc_id"], now=NOW)
    assert not any(line.startswith("AGGREGATOR") for line in sql_lines(out["script"]))
    assert "-- AGGREGATOR SYNC AUTO_INCREMENT ON `d``b`.`ev__reshard` ALL;" in out["script"]
