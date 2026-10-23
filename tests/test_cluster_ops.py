"""Offline tests for the Cluster Monitor's Health / History / Backups tabs: the metrics store (sampling, retention,
downsampling), health classification and the backup overview. No cluster, no credentials."""

from __future__ import annotations

import json
import threading

import pytest

from singlestore_mcp import metrics_store as ms
from singlestore_mcp.apps import cluster_ops as co

NOW = 1_800_000_000  # a fixed "now", aligned to 10 minutes


# ----------------------------------------------------------------------------- metrics store

@pytest.fixture
def conn(tmp_path):
    c = ms.connect(str(tmp_path / "metrics.db"))
    yield c
    c.close()


def _row(node_id: int, **v) -> dict:
    base = {"node_id": node_id, "host": f"h{node_id}", "type": "LEAF", "cpu": 10.0, "cpu_max": 10.0, "host_cpu": 5.0,
            "mem_b": 50.0, "mem_max_b": 100.0, "disk_used_b": 20.0, "disk_total_b": 100.0, "queued": 0, "queued_max": 0,
            "running": 1, "conns": 10}
    base.update(v)
    base["cpu_max"] = v.get("cpu_max", base["cpu"])
    base["queued_max"] = v.get("queued_max", base["queued"])
    return base


def test_write_and_read_one_hour(conn) -> None:
    for i in range(5):
        ms.write(conn, "c1", NOW - 300 + i * 60, 60, [_row(1, cpu=10 + i), _row(2, cpu=50)])
    ms.write(conn, "other", NOW - 60, 60, [_row(1, cpu=99)])  # another cluster never mixes in
    d = ms.read(conn, "c1", 3600, NOW)
    assert d["bucket_s"] == 60
    assert [p["t"] for p in d["points"]] == [NOW - 300 + i * 60 for i in range(5)]
    assert d["points"][-1]["cpu"] == {"1": 14.0, "2": 50.0}
    assert d["points"][0]["mem"]["1"] == 50.0 and d["points"][0]["disk"]["1"] == 20.0
    assert [n["id"] for n in d["nodes"]] == [1, 2]
    assert d["stored"]["rows"] == 10


def test_write_same_bucket_is_idempotent(conn) -> None:
    ms.write(conn, "c1", NOW, 60, [_row(1, cpu=10)])
    ms.write(conn, "c1", NOW, 60, [_row(1, cpu=20)])  # e.g. two server processes in the same minute
    assert tuple(conn.execute("SELECT COUNT(*), MAX(cpu) FROM metrics").fetchone()) == (1, 20.0)


def test_read_buckets_average_and_peak(conn) -> None:
    for i, (cpu, q) in enumerate([(10, 0), (20, 3), (30, 1), (40, 0), (50, 0)]):
        ms.write(conn, "c1", NOW - 3000 + i * 60, 60, [_row(1, cpu=cpu, queued=q)])
    d = ms.read(conn, "c1", 86400, NOW, bucket_s=300)
    assert len(d["points"]) == 1
    p = d["points"][0]
    assert p["cpu"]["1"] == 30.0          # average
    assert p["cpu_max"]["1"] == 50.0      # peak
    assert p["queued"]["1"] == 3          # queued shows the peak


def test_bucket_sizes() -> None:
    assert ms.bucket_for(3600) == 60 and ms.bucket_for(86400) == 300 and ms.bucket_for(7 * 86400) == 1800


def test_compact_folds_old_raw_rows_and_keeps_recent(conn) -> None:
    old = NOW - 2 * 86400
    # One complete 10-minute bucket of raw rows two days ago: 10 samples, CPU 0..90, one queue spike.
    for i in range(10):
        ms.write(conn, "c1", old + i * 60, 60, [_row(1, cpu=i * 10, queued=5 if i == 3 else 0)])
    ms.write(conn, "c1", NOW - 120, 60, [_row(1, cpu=7)])  # recent: stays raw
    r = ms.compact(conn, NOW, retention_days=7)
    assert r["folded"] == 1 and r["raw_removed"] == 10
    rows = conn.execute("SELECT t, res, n, cpu, cpu_max, queued, queued_max FROM metrics ORDER BY t").fetchall()
    assert [tuple(x) for x in rows] == [(old, 600, 10, 45.0, 90.0, 0.5, 5.0), (NOW - 120, 60, 1, 7.0, 7.0, 0.0, 0.0)]
    # Reading a week mixes coarse and raw rows, weighted by their sample counts.
    d = ms.read(conn, "c1", 7 * 86400, NOW)
    assert d["bucket_s"] == 1800
    assert [p["cpu"]["1"] for p in d["points"]] == [45.0, 7.0]
    # Compacting again changes nothing.
    assert ms.compact(conn, NOW, retention_days=7) == {"folded": 0, "raw_removed": 0, "expired": 0}


def test_compact_cutoff_is_bucket_aligned(conn) -> None:
    """A 10-minute bucket that straddles the 24 h line stays raw until it is complete."""
    now = NOW + 300  # half-way through a bucket
    cut = NOW - ms.RAW_KEEP_S  # the aligned cut-off for this "now"
    for i in range(10):
        ms.write(conn, "c1", cut + i * 60, 60, [_row(1)])
    assert ms.compact(conn, now, retention_days=7)["folded"] == 0
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE res = 60").fetchone()[0] == 10


def test_weighted_average_ignores_missing_values(conn) -> None:
    ms.write(conn, "c1", NOW - 2 * 86400, 60, [_row(1, cpu=None)])
    ms.write(conn, "c1", NOW - 2 * 86400 + 60, 60, [_row(1, cpu=40)])
    ms.compact(conn, NOW, retention_days=7)
    assert tuple(conn.execute("SELECT cpu, n FROM metrics").fetchone()) == (40.0, 2)


def test_retention_drops_old_rows(conn) -> None:
    ms.write(conn, "c1", NOW - 10 * 86400, 600, [_row(1)])
    ms.write(conn, "c1", NOW - 3 * 86400, 600, [_row(1)])
    r = ms.compact(conn, NOW, retention_days=7)
    assert r["expired"] == 1
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1


def test_settings_and_off_switch(monkeypatch) -> None:
    for k in ("SINGLESTORE_MCP_METRICS", "SINGLESTORE_MCP_METRICS_INTERVAL", "SINGLESTORE_MCP_METRICS_RETENTION_DAYS"):
        monkeypatch.delenv(k, raising=False)
    s = ms.settings()
    assert s["enabled"] and s["interval_s"] == 60 and s["retention_days"] == 7
    monkeypatch.setenv("SINGLESTORE_MCP_METRICS_INTERVAL", "1")
    monkeypatch.setenv("SINGLESTORE_MCP_METRICS_RETENTION_DAYS", "500")
    assert ms.settings()["interval_s"] == 15 and ms.settings()["retention_days"] == 90
    monkeypatch.setenv("SINGLESTORE_MCP_METRICS_INTERVAL", "junk")
    assert ms.settings()["interval_s"] == 60
    for off in ("0", "off", "False", "no"):
        monkeypatch.setenv("SINGLESTORE_MCP_METRICS", off)
        assert ms.settings()["enabled"] is False
        assert ms.start() is False


def test_sample_once_skips_when_another_process_just_sampled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ms, "_path", lambda: str(tmp_path / "m.db"))
    monkeypatch.setattr(ms, "_source", lambda: "c1")
    calls = []
    monkeypatch.setattr(ms, "_collect", lambda: calls.append(1) or [_row(1), _row(2)])
    monkeypatch.delenv("SINGLESTORE_MCP_METRICS_INTERVAL", raising=False)
    monkeypatch.setattr(ms.time, "time", lambda: NOW + 5.0)  # all calls in the same minute
    assert ms.sample_once()["nodes"] == 2
    assert ms.sample_once()["skipped"] is True      # the same minute: someone already sampled
    assert ms.sample_once(force=True)["skipped"] is False
    assert len(calls) == 2
    h = ms.history("1h")
    assert h["range"] == "1h" and h["source"] == "c1" and len(h["points"]) == 1
    assert ms.history("bogus")["range"] == "1h"


def test_sampler_thread_starts_late_and_stops(tmp_path, monkeypatch) -> None:
    """The thread waits SINGLESTORE_MCP_METRICS_DELAY before the first sample (start-up stays untouched)."""
    monkeypatch.setenv("SINGLESTORE_MCP_METRICS", "1")
    monkeypatch.setenv("SINGLESTORE_MCP_METRICS_DELAY", "3600")
    sampled = threading.Event()
    monkeypatch.setattr(ms, "sample_once", lambda force=False: sampled.set())
    assert ms.start() is True
    assert ms.status()["running"] is True
    ms.stop(timeout=5)
    assert not sampled.is_set()
    assert ms.status()["running"] is False


def test_build_rows() -> None:
    nodes = [{"ID": 1, "IP_ADDR": "node-x-master-0.svc-x", "TYPE": "MA", "MAX_MEMORY_MB": 1024, "MEMSQL_B": 512 * 1024 * 1024},
             {"ID": 2, "IP_ADDR": "node-x-leaf-0.svc-x", "TYPE": "LEAF", "MAX_MEMORY_MB": None, "MEMSQL_B": None}]
    rates = {1: {"cpu": {"singlestore_pct": 12.5, "host_pct": 30.0}, "mounts": [{"used_b": 40, "total_b": 100}]}}
    procs = [{"NODE_ID": 1, "conns": 7, "running": "2", "queued": "1"}]
    rows = co.metrics_store.build_rows(nodes, rates, procs)
    assert rows[0] == {"node_id": 1, "host": "node-x-master-0", "type": "MA", "cpu": 12.5, "cpu_max": 12.5, "host_cpu": 30.0,
                       "mem_b": 512 * 1024 * 1024, "mem_max_b": 1024 * 1024 * 1024, "disk_used_b": 40, "disk_total_b": 100,
                       "queued": 1, "queued_max": 1, "running": 2, "conns": 7}
    assert rows[1]["cpu"] is None and rows[1]["mem_max_b"] is None and rows[1]["conns"] == 0


# ----------------------------------------------------------------------------- health classification

def _node(i: int, typ: str = "LEAF", state: str = "online", ag: int | None = 1, pair: str | None = None, role: str | None = None) -> dict:
    return {"ID": i, "IP_ADDR": f"node-{i}.svc-x", "PORT": 3306, "TYPE": typ, "STATE": state, "AVAILABILITY_GROUP": ag,
            "VERSION": "9.0.40", "UPTIME": 100, "RUNTIME_STATE": state, "PAIR_HOST": pair, "PAIR_PORT": 3306 if pair else None,
            "ROLE": role, "LATENCY": 0.0001}


def _db(name: str, summary: str = "healthy", **counts) -> dict:
    base = {"database_name": name, "num_partitions": 4, "num_sub_partitions": 16, "summary": summary, "online": 4,
            "replicating": 0, "recovering": 0, "pending": 0, "transition": 0, "unrecoverable": 0, "offline": 0, "sync_mismatch": 0}
    base.update(counts)
    return base


def _healthy_raw(**over) -> dict:
    raw = {
        "nodes": [_node(1, "MA", ag=None, role="Leader"), _node(2), _node(3)],
        "databases": [_db("d1")],
        "partitions": [{"DATABASE_NAME": "d1", "ROLE": "Master", "STATE": "", "IS_OFFLINE": 0, "IS_READ_REPLICA": 0, "N": 4}],
        "orphans": [], "replication": [], "events": [],
        "settings": [{"VARIABLE_NAME": "License_type", "VARIABLE_VALUE": "Enterprise"},
                     {"VARIABLE_NAME": "License_expiration", "VARIABLE_VALUE": "0"},
                     {"VARIABLE_NAME": "License_capacity", "VARIABLE_VALUE": "18446744073709551615 units"},
                     {"VARIABLE_NAME": "Used_cluster_capacity", "VARIABLE_VALUE": "2 units"},
                     {"VARIABLE_NAME": "Maximum_cluster_capacity", "VARIABLE_VALUE": "18446744073709551615 units"},
                     {"VARIABLE_NAME": "redundancy_level", "VARIABLE_VALUE": "2"}],
    }
    raw.update(over)
    return raw


def _titles(d: dict, sev: str | None = None) -> list[str]:
    return [i["title"] for i in d["issues"] if sev is None or i["severity"] == sev]


def _r2_pairs() -> list[dict]:
    return [_node(1, "MA", ag=None, role="Leader"), _node(2, ag=1, pair="node-3.svc-x"), _node(3, ag=2, pair="node-2.svc-x")]


def test_healthy_cluster() -> None:
    raw = _healthy_raw(nodes=_r2_pairs(), partitions=[
        {"DATABASE_NAME": "d1", "ROLE": "Master", "STATE": "", "IS_OFFLINE": 0, "IS_READ_REPLICA": 0, "N": 4},
        {"DATABASE_NAME": "d1", "ROLE": "Replica", "STATE": "SYNC", "IS_OFFLINE": 0, "IS_READ_REPLICA": 0, "N": 4}])
    d = co.classify_health(raw, now=NOW)
    assert d["status"] == "healthy", d["issues"]
    assert d["issues"] == []
    assert d["databases"][0]["masters"] == 4 and d["databases"][0]["replicas"] == 4
    assert d["license"]["unlimited"] and d["license"]["expires_at"] is None and d["license"]["used_units"] == 2
    assert d["versions"] == ["9.0.40"]
    assert d["nodes"][1]["pair"] == "node-3:3306"


def test_node_offline_is_critical() -> None:
    d = co.classify_health(_healthy_raw(nodes=[_node(1, "MA", ag=None), _node(2, state="offline"), _node(3)]), now=NOW)
    assert d["status"] == "critical"
    assert any("node-2" in t and "offline" in t for t in _titles(d, "critical"))
    assert d["issues"][0]["docs"].startswith("https://docs.singlestore.com/db/v9.0/")


def test_partition_problems() -> None:
    raw = _healthy_raw(
        databases=[_db("ok"), _db("deg", "degraded", recovering=1), _db("off", "offline", offline=2), _db("lost", "degraded", unrecoverable=1),
                   _db("mix", sync_mismatch=1)],
        orphans=[{"DATABASE_NAME": "gone", "N": 3}])
    d = co.classify_health(raw, now=NOW)
    crit, warn = _titles(d, "critical"), _titles(d, "warning")
    assert "off: 2 partition(s) offline" in crit
    assert "lost: unrecoverable partitions" in crit
    assert "deg: degraded" in warn
    assert "mix: replication mode mismatch" in warn
    assert "3 orphan partition(s)" in warn
    # Problem databases first.
    assert d["databases"][-1]["database"] in ("ok", "mix")
    assert d["status"] == "critical"
    # Worst first.
    sev = [i["severity"] for i in d["issues"]]
    assert sev == sorted(sev, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s])


def test_offline_partition_instance_from_distributed_partitions() -> None:
    raw = _healthy_raw(partitions=[{"DATABASE_NAME": "d1", "ROLE": "Master", "STATE": "UNRECOVERABLE", "IS_OFFLINE": 1,
                                    "IS_READ_REPLICA": 0, "N": 1}])
    d = co.classify_health(raw, now=NOW)
    assert "d1: unrecoverable partitions" in _titles(d, "critical")


def test_high_availability_checks() -> None:
    r1 = co.classify_health(_healthy_raw(settings=[{"VARIABLE_NAME": "redundancy_level", "VARIABLE_VALUE": "1"}]), now=NOW)
    assert r1["status"] == "healthy"  # info only
    assert "No high availability (redundancy_level 1)" in _titles(r1, "info")
    # redundancy 2 with every leaf in one group and no pairs
    r2 = co.classify_health(_healthy_raw(), now=NOW)
    assert "redundancy_level 2 but only one availability group" in _titles(r2, "warning")
    assert any("without a pair" in t for t in _titles(r2, "warning"))
    assert "d1: 4 partition(s) without a replica" in _titles(r2, "warning")
    # unequal groups
    r3 = co.classify_health(_healthy_raw(nodes=[*_r2_pairs(), _node(4, ag=1, pair="node-3.svc-x")]), now=NOW)
    assert "Availability groups have different numbers of leaves" in _titles(r3, "warning")


def _event(etype: str, sev: str, ago: int, **details) -> dict:
    return {"ORIGIN_NODE_ID": 1, "EVENT_TIME": "2026-10-14T10:00:00", "SEVERITY": sev, "EVENT_TYPE": etype,
            "DETAILS": json.dumps(details), "AGO_S": ago}


def test_recent_events_become_issues_and_old_ones_dont() -> None:
    events = [_event("NODE_OFFLINE", "WARNING", 600, node="n2"), _event("NODE_OFFLINE", "WARNING", 1200, node="n2"),
              _event("PARTITION_UNRECOVERABLE", "ERROR", 300), _event("PIPELINE_STOPPED", "WARNING", 3 * 86400),
              _event("DATABASE_CREATE_FINISH", "NOTICE", 60, database_name="x"), _event("MAX_MEMORY", "NOTICE", 100)]
    d = co.classify_health(_healthy_raw(nodes=_r2_pairs(), events=events,
                                        partitions=[{"DATABASE_NAME": "d1", "ROLE": r, "STATE": "", "IS_OFFLINE": 0, "IS_READ_REPLICA": 0, "N": 4}
                                                    for r in ("Master", "Replica")]), now=NOW)
    titles = _titles(d)
    assert "2 NODE_OFFLINE events in the last 24 h" in _titles(d, "warning")
    assert "1 PARTITION_UNRECOVERABLE event in the last 24 h" in _titles(d, "critical")
    assert "1 MAX_MEMORY event in the last 24 h" in titles      # a known problem type even at NOTICE
    assert not any("PIPELINE_STOPPED" in t for t in titles)      # older than the window
    assert not any("DATABASE_CREATE" in t for t in titles)       # routine
    off = next(i for i in d["issues"] if i.get("event_type") == "NODE_OFFLINE")
    assert off["last_ago_s"] == 600 and "offline" in off["explanation"]
    assert d["events"][0]["details"] == {"node": "n2"} and d["events"][0]["help"]


def test_rebalance_running() -> None:
    running = co.classify_health(_healthy_raw(nodes=_r2_pairs(), events=[_event("REBALANCE_STARTED", "NOTICE", 30)],
                                              partitions=[]), now=NOW)
    assert "A partition rebalance is running" in _titles(running, "info")
    done = co.classify_health(_healthy_raw(nodes=_r2_pairs(), partitions=[], events=[
        _event("REBALANCE_FINISHED", "NOTICE", 10), _event("REBALANCE_STARTED", "NOTICE", 30)]), now=NOW)
    assert "A partition rebalance is running" not in _titles(done)


def test_license_checks_and_no_license_key() -> None:
    def lic(exp: int, used: str = "2 units", max_: str = "18446744073709551615 units") -> dict:
        return co.classify_health(_healthy_raw(nodes=_r2_pairs(), partitions=[], settings=[
            {"VARIABLE_NAME": "License_type", "VARIABLE_VALUE": "Premium"},
            {"VARIABLE_NAME": "License_expiration", "VARIABLE_VALUE": str(exp)},
            {"VARIABLE_NAME": "Used_cluster_capacity", "VARIABLE_VALUE": used},
            {"VARIABLE_NAME": "Maximum_cluster_capacity", "VARIABLE_VALUE": max_},
            {"VARIABLE_NAME": "redundancy_level", "VARIABLE_VALUE": "2"}]), now=NOW)
    expired = lic(NOW - 86400)
    assert expired["license"]["expired"] and any(t.startswith("License expired") for t in _titles(expired, "warning"))
    soon = lic(NOW + 5 * 86400)
    assert any(t.startswith("License expires") for t in _titles(soon, "warning"))
    assert lic(NOW + 365 * 86400)["issues"] == []
    over = lic(0, used="10 units", max_="8 units")
    assert "License capacity exceeded" in _titles(over, "critical")
    # The classification only reads the named variables: a License / License_key row never reaches the output.
    raw = _healthy_raw(settings=[{"VARIABLE_NAME": "License_key", "VARIABLE_VALUE": "secret-key-123"},
                                 {"VARIABLE_NAME": "License", "VARIABLE_VALUE": "secret-blob"}])
    assert "secret" not in json.dumps(co.classify_health(raw, now=NOW))
    assert "License_key" not in co._HEALTH_SQL["settings"] and "'License'" not in co._HEALTH_SQL["settings"]


def test_replication_lag() -> None:
    d = co.classify_health(_healthy_raw(nodes=_r2_pairs(), partitions=[], replication=[
        {"IS_DR": 1, "IS_SYNC": 0, "DATABASE_NAME": "d1", "PRIMARY_URI": "a/d1", "SECONDARY_URI": "b/d1", "PRIMARY_STATE": "online",
         "SECONDARY_STATE": "replicating", "LSN_LAG": "1200", "VOLUME_LAG_MB": 30, "REPLICATION_THROUGHPUT_MBPS": 1,
         "ESTIMATED_CATCHUP_TIME_S": 400}]), now=NOW)
    assert "Replication of d1 is 400 s behind" in _titles(d, "warning")
    assert d["replication"][0]["lsn_lag"] == 1200


def test_empty_views_dont_break_classification() -> None:
    d = co.classify_health({}, now=NOW)
    assert d["status"] == "healthy" and d["nodes"] == [] and d["license"]["type"] is None


# ----------------------------------------------------------------------------- backups

def _backup(i: int, db: str, start: str, end: str, status: str = "Success", size: float = 100.0, **kw) -> dict:
    return {"BACKUP_ID": i, "DATABASE_NAME": db, "START_TIMESTAMP": start, "END_TIMESTAMP": end, "STATUS": status, "SIZE": size,
            "TYPE": "FULL", "IMPLEMENTATION": "Lockfree", "NUM_PARTITIONS": 8, "BACKUP_PATH": f"/b/{db}/{i}",
            "INCR_BACKUP_ID": None, "ERROR_CODE": kw.get("code"), "ERROR_MESSAGE": kw.get("msg"), "SERVER_VERSION": "9.0.40"}


def test_backup_overview_states() -> None:
    now = co._ts("2026-10-14T12:00:00")
    hist = [
        _backup(1, "fresh", "2026-10-14T01:00:00", "2026-10-14T01:05:30"),
        _backup(2, "stale", "2026-09-01T01:00:00", "2026-09-01T01:10:00", size=2048),
        _backup(3, "broken", "2026-10-10T01:00:00", "2026-10-10T01:01:00"),
        _backup(4, "broken", "2026-10-13T01:00:00", "2026-10-13T01:00:20", status="Failure", code=1777, msg="disk full"),
        _backup(5, "fixed", "2026-10-12T01:00:00", "2026-10-12T01:00:20", status="Failure", code=1, msg="x"),
        _backup(6, "fixed", "2026-10-13T01:00:00", "2026-10-13T01:02:00"),
        _backup(7, "olddb", "2026-10-13T01:00:00", "2026-10-13T01:02:00"),
    ]
    d = co.backup_overview(hist, [], ["fresh", "stale", "broken", "fixed", "nobackup"], now, warn_days=7)
    by = {x["database"]: x for x in d["databases"]}
    assert by["fresh"]["state"] == "ok" and by["fresh"]["last_success"]["duration_s"] == 330.0
    assert by["fresh"]["age_s"] == 11 * 3600 - 330
    assert by["stale"]["state"] == "old"
    assert by["broken"]["state"] == "failed" and by["broken"]["failures"] == 1
    assert by["broken"]["last_failure"]["error"] == "disk full"
    assert by["fixed"]["state"] == "ok"
    assert by["nobackup"]["state"] == "never"
    assert by["olddb"]["state"] == "dropped"
    titles = [i["title"] for i in d["issues"]]
    assert "broken: the last backup failed" in titles
    assert any(t.startswith("stale: last good backup is 43 days old") for t in titles)
    assert "1 of 5 database(s) have no backup in the history" in titles
    assert d["total"] == 7 and d["failed_total"] == 2
    assert d["history"][0]["id"] == 1  # newest first
    # A wider limit makes the stale one fine.
    assert {x["database"]: x["state"] for x in co.backup_overview(hist, [], ["stale"], now, warn_days=60)["databases"]}["stale"] == "ok"


def test_backup_overview_empty_history_and_running() -> None:
    running = [{"ORIGIN_NODE_ID": 1, "DATABASE_NAME": "d1", "TARGET": "S3", "ROWSTORE_PROGRESS": 100, "COLUMNSTORE_PROGRESS": 40,
                "STATUS": "Columnstore", "STEP_EST_SEC_REMAIN": 30, "IMPLEMENTATION": "Lockfree"}]
    d = co.backup_overview([], running, ["d1", "d2"], NOW)
    assert [x["state"] for x in d["databases"]] == ["never", "never"]
    assert d["issues"][0]["severity"] == "info"  # nothing recorded at all: probably backed up some other way
    assert d["running"][0]["columnstore_pct"] == 40 and d["running"][0]["database"] == "d1"


# ----------------------------------------------------------------------------- CPU baseline (first open of the Cluster view)

def _fake_counters(monkeypatch, cm) -> list:
    calls = []

    def counters():
        calls.append(1)
        ts = int(len(calls) * 2e9)  # each read 2 s after the previous one (cluster clock)
        return {1: {"ts": ts, "cores": 4, "host_cpus": 4, "memsql_ns": len(calls) * 10**9, "used_ns": len(calls) * 10**9,
                    "idle_ns": len(calls) * 10**9, "mounts": {}}}

    monkeypatch.setattr(cm, "_counters", counters)
    monkeypatch.setattr(cm, "_samples", type(cm._samples)(maxlen=8))
    return calls


def test_primed_baseline_means_no_wait(monkeypatch) -> None:
    from singlestore_mcp.apps import cluster_monitor as cm

    calls = _fake_counters(monkeypatch, cm)
    slept = []
    monkeypatch.setattr(cm.time, "sleep", lambda s: slept.append(s))
    assert cm.prime_baseline() is True and len(calls) == 1
    assert cm.prime_baseline() is False and len(calls) == 1  # fresh: no second read
    rates, gap = cm._rates()
    assert slept == [] and len(calls) == 2 and gap == 2.0  # measured against the primed sample, no 1 s wait
    assert rates[1]["cpu"]["singlestore_pct"] == 12.5


def test_without_baseline_rates_wait_a_second(monkeypatch) -> None:
    from singlestore_mcp.apps import cluster_monitor as cm

    _fake_counters(monkeypatch, cm)
    slept = []
    monkeypatch.setattr(cm.time, "sleep", lambda s: slept.append(s))
    cm._rates()
    assert slept == [1.0]


def test_sampler_primes_the_baseline_early(monkeypatch) -> None:
    from singlestore_mcp.apps import cluster_monitor as cm

    monkeypatch.setenv("SINGLESTORE_MCP_METRICS", "1")
    monkeypatch.setenv("SINGLESTORE_MCP_METRICS_DELAY", "3600")
    monkeypatch.setattr(ms, "BASELINE_DELAY_S", 0)
    primed = threading.Event()
    monkeypatch.setattr(cm, "prime_baseline", lambda *a, **k: primed.set())
    monkeypatch.setattr(ms, "sample_once", lambda force=False: pytest.fail("sampled before the delay"))
    assert ms.start() is True
    assert primed.wait(5), "no baseline right after start"
    ms.stop(timeout=5)


# ----------------------------------------------------------------------------- trends and trend alerts

GB = 1024 ** 3


def test_trends_disk_full_in_days(conn) -> None:
    # 24 h of samples: disk grows 10 GB/day steadily (100 of 200 GB used now); memory flat with noise.
    for i in range(0, 24 * 60, 10):
        t = NOW - 86400 + i * 60
        used = 90 * GB + (i * 60) / 86400 * 10 * GB
        ms.write(conn, "c1", t, 60, [_row(1, disk_used_b=used, disk_total_b=200 * GB, mem_b=(50 + (i % 3)) * GB, mem_max_b=100 * GB)])
    tr = ms.trends(conn, "c1", NOW)
    disk, mem = tr[0]["disk"], tr[0]["memory"]
    assert disk["r2"] > 0.99 and abs(disk["per_day_b"] - 10 * GB) < 0.1 * GB
    assert 9.5 < disk["days_left"] < 10.5
    assert mem["days_left"] is None  # no steady climb
    # Too little history: nothing.
    assert ms.trends(conn, "c1", NOW, window_s=3600) == []


def test_check_trends_alerts_within_the_rule() -> None:
    from singlestore_mcp import alerts

    tr = [{"node_id": 2, "host": "leaf-0", "disk": {"used_b": 100 * GB, "cap_b": 200 * GB, "span_s": 86400, "points": 144,
                                                      "per_day_b": 10 * GB, "r2": 0.99, "days_left": 10.0},
           "memory": {"used_b": 90 * GB, "cap_b": 100 * GB, "span_s": 86400, "points": 144, "per_day_b": 10 * GB, "r2": 0.9,
                      "days_left": 1.0}}]
    found = alerts.check_trends({"days": 14}, {"days": 3}, tr)
    by = {f["kind"]: f for f in found}
    assert by["disk_trend"]["severity"] == "warning" and "full in ~10 days" in by["disk_trend"]["title"]
    assert by["memory_trend"]["severity"] == "critical" and "~24 h" in by["memory_trend"]["title"]
    assert all(f["continuing"] for f in found)
    assert alerts.check_trends({"days": 7}, None, tr) == []  # 10 days is beyond a 7-day rule; memory rule off


def test_trend_rules_have_days(tmp_path, monkeypatch) -> None:
    from singlestore_mcp import alerts

    monkeypatch.setattr(alerts, "_file", lambda: tmp_path / "alerts.json")
    monkeypatch.setattr(alerts, "_state", {})
    st = alerts.update_rules({"disk_trend": {"days": 30}, "memory_trend": {"days": 0, "enabled": False}})
    assert st["rules"]["disk_trend"]["days"] == 30 and st["rules"]["memory_trend"] == {**alerts.DEFAULT_RULES["memory_trend"], "days": 1, "enabled": False}
    monkeypatch.setattr(alerts, "_state", {})  # read back from the file
    assert alerts.state()["rules"]["disk_trend"]["days"] == 30
