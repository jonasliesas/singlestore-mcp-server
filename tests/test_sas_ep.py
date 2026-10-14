"""SAS Embedded Process analysis (singlestore_mcp.sas_ep): parsing, the fixed-wait plateau, shared waits, rows vs
time, the time breakdown, the tuning tips and the Usage tab's EP overview. Offline: no server, no cluster; the
overview runs against its own empty SINGLESTORE_MCP_HOME. The numbers mirror what the cluster showed in Oct 2026."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from singlestore_mcp import history_store, sas_ep

JOB = ("SELECT `status`, `message` FROM `MyDB`.`sasepFunction`('RUNDATASTEP  epdatabase=\\\"MyDB\\\" databaseUser=\\\"u\\\" "
       "databasePassword=\\\"{SAS005}NOTAREALSECRET\\\" inputDatabase=\\\"MyDB\\\" inputTable=\\\"_hello_world\\\" "
       "outputDatabase=\\\"MyDB\\\" outputTable=\\\"_hello_world_1\\\" externalFunction=\\\"hA54A002DF1EDB24C9B8A81F6B04F034E_MyDB\\\"')")
FORMAT = ("SELECT COUNT(*) !:> bigint as __sas_countstar, SUM(t.Age) !:> double FROM ( SELECT  *\n FROM FANS.WORKFORCE_DATA) "
          "as t  JOIN EP.h0123456789ABCDEF0123456789ABCDEF_EP(Attrition_Type) as ep")
LOAD = ("SELECT * FROM ( SELECT ep.va__d_X !:> double AS va__d_X FROM ( SELECT  *\n FROM FANS.WORKFORCE_DATA) as t  "
        "JOIN EP.h0123456789ABCDEF0123456789ABCDEF_EP(Age) as ep) WHERE partition_id() = 3")


def call(start: float, ms: float, rows: int | None = 1, kind: str = "format", ok: bool = True) -> dict:
    return {"start": start, "end": start + ms / 1000, "ms": ms, "rows": rows, "ok": ok, "kind": kind, "db": "FANS", "user": "admin"}


# ------------------------------------------------------------------ parsing

def test_parse_and_kind() -> None:
    job = sas_ep.parse(JOB)
    assert job["kind"] == "job" and job["command"] == "RUNDATASTEP"
    assert job["params"]["inputTable"] == "_hello_world" and "databasePassword" not in job["params"]
    fmt = sas_ep.parse(FORMAT, "FANS")
    assert fmt["kind"] == "format" and fmt["inputs"] == ["FANS.WORKFORCE_DATA"] and fmt["functions"][0]["args"] == ["Attrition_Type"]
    assert sas_ep.parse(LOAD, "FANS")["partition"] == 3
    assert sas_ep.parse("INSERT INTO EP.sasepv5 VALUES (?, ?, now())") == {"bookkeeping": True}
    assert sas_ep.parse("SELECT 1") is None
    assert [sas_ep.kind_of(x) for x in (JOB, FORMAT, "DELETE FROM EP.sasepv5 WHERE hash = 'x'", "SELECT 1")] == \
        ["job", "format", "bookkeeping", None]
    assert "NOTAREALSECRET" not in sas_ep.masked(JOB)


def test_epoch_reads_trace_and_store_times() -> None:
    a = sas_ep.epoch("2026-10-14T12:13:29.476228")
    b = sas_ep.epoch("2026-10-14 12:13:24.230128")
    assert a - b == pytest.approx(5.2461, abs=1e-4)
    assert sas_ep.epoch(None) == 0.0 and sas_ep.epoch("not a time") == 0.0


# ------------------------------------------------------------------ plateau, bursts, rows vs time

# 14 traced calls in the 5.19–5.37 s band, a few faster ones (warm calls / later starters in a burst).
PLATEAU = [5299, 5257, 5259, 5190, 5204, 5224, 5223, 5224, 5236, 5230, 5345, 5366, 5244, 5257]
FAST = [1096, 1118, 1119, 1378, 1500, 1831, 2774, 2774, 2775]


def test_plateau_found_with_warm_baseline() -> None:
    p = sas_ep.plateau(PLATEAU + FAST)
    assert p and p["count"] == len(PLATEAU) and p["total"] == len(PLATEAU) + len(FAST)
    assert 5190 <= p["low_ms"] <= p["center_ms"] <= p["high_ms"] <= 5366
    assert p["warm_ms"] == 1500 and p["fixed_ms"] == p["center_ms"] - 1500
    assert sas_ep.on_plateau(5246, p) and not sas_ep.on_plateau(2774, p) and not sas_ep.on_plateau(0, p)


def test_no_plateau_when_times_spread_or_too_few() -> None:
    assert sas_ep.plateau([1000 * i for i in range(1, 20)]) is None
    assert sas_ep.plateau([5200, 5210]) is None
    assert sas_ep.plateau([]) is None
    assert sas_ep.plateau([300, 310, 305, 320]) is None  # sub-second: not a "fixed wait"
    p = sas_ep.plateau([5200] * 5)
    assert p and p["warm_ms"] is None and p["fixed_ms"] == 5200


def test_bursts_detect_a_shared_wait() -> None:
    # 2026-10-12 06:46: three calls start together, three start 3.6–4.1 s later; all end within ~0.2 s.
    t0 = 1_000_000.0
    calls = [call(t0, 5230), call(t0, 5345), call(t0, 5366),
             call(t0 + 4.063, 1378), call(t0 + 3.943, 1500), call(t0 + 3.613, 1831),
             call(t0 + 600, 5200)]   # alone, ten minutes later
    bs = sas_ep.bursts(calls)
    assert len(bs) == 1
    b = bs[0]
    assert b["calls"] == 6 and b["shared"] and b["start_spread_s"] >= 3.6 and b["end_spread_s"] <= 0.25


def test_warm_baseline_ignores_calls_that_joined_a_shared_wait() -> None:
    t0 = 1_000_000.0
    burst = [call(t0, 5230), call(t0, 5345), call(t0, 5366), call(t0 + 3.6, 1831), call(t0 + 3.9, 1500)]
    alone = [call(t0 + 500, 1096), call(t0 + 900, 1118), call(t0 + 1300, 1119)]
    others = [call(t0 + 2000 + i * 100, ms) for i, ms in enumerate([5244, 5257, 5259, 5299])]
    pl = sas_ep.calls_plateau(burst + alone + others)
    assert pl["warm_ms"] == 1118 and pl["warm_calls"] == 3 and pl["fixed_ms"] == pl["center_ms"] - 1118
    assert sas_ep.plateau([c["ms"] for c in burst + alone + others])["warm_ms"] != 1118  # the naive median mixes them in
    assert sas_ep.calls_plateau([call(0, 100)]) is None


def test_bursts_not_shared_when_they_finish_apart() -> None:
    t0 = 2_000_000.0
    bs = sas_ep.bursts([call(t0, 5000), call(t0 + 2, 5000)])
    assert len(bs) == 1 and not bs[0]["shared"]


def test_rows_fit_shows_time_independent_of_rows() -> None:
    calls = [call(i * 100.0, ms, rows) for i, (ms, rows) in
             enumerate([(5224, 104), (5223, 52), (5241, 803), (5243, 889), (5246, 1343), (5236, 1), (5230, 1)])]
    f = sas_ep.rows_fit(calls)
    assert f and 5150 <= f["intercept_ms"] <= 5300 and abs(f["per_1000_rows_ms"]) < 50 and f["max_rows"] == 1343
    assert sas_ep.rows_fit(calls[:3]) is None
    assert sas_ep.rows_fit([call(i, 5000, 1) for i in range(6)]) is None  # rows don't vary


def test_histogram_folds_the_tail() -> None:
    h = sas_ep.histogram([100, 120, 5200, 5210, 5300], width_ms=250)
    assert h == [{"lo_ms": 0, "n": 2}, {"lo_ms": 5000, "n": 2}, {"lo_ms": 5250, "n": 1}]
    many = sas_ep.histogram([i * 250 for i in range(30)], max_buckets=5)
    assert len(many) == 5 and many[-1]["more"] and sum(b["n"] for b in many) == 30


# ------------------------------------------------------------------ card points

def test_breakdown_measures_the_wait_before_the_eps_sql() -> None:
    start = 1_000_000.0
    gen = {"start": start + 5.212, "end": start + 5.212, "ms": 0.0, "ok": False, "error": "ER_TABLE_EXISTS_ERROR"}
    plan = {"runs": 1, "CPU_TIME": 3, "QUEUED_TIME": 0}
    p = sas_ep.breakdown(5257, plan, None, gen, start)
    assert [s["label"] for s in p["segments"]][0] == "before the EP's SQL" and p["segments"][0]["ms"] == 5212
    assert p["verdict"]["level"] == "warn" and "waiting" in p["verdict"]["text"]
    assert "CPU in SingleStore 3 ms" in p["verdict"]["text"]


def test_breakdown_estimates_the_fixed_wait_from_the_plateau() -> None:
    pl = sas_ep.plateau(PLATEAU + FAST)
    p = sas_ep.breakdown(5246, None, pl, None)
    wait = next(s for s in p["segments"] if s["kind"] == "wait")
    assert wait["how"] == "estimate" and wait["ms"] == 5246 - pl["warm_ms"]
    assert p["verdict"]["level"] == "warn"
    off = sas_ep.breakdown(1500, {"runs": 1, "CPU_TIME": 900, "QUEUED_TIME": 0}, pl, None)
    assert all(s["kind"] != "wait" for s in off["segments"]) and off["verdict"]["level"] == "ok"


def test_peers_point_flags_the_plateau() -> None:
    calls = [call(i * 100.0, ms, rows) for i, (ms, rows) in enumerate(zip(PLATEAU + FAST, [1, 52, 104, 803, 889, 1343] * 4))]
    pl = sas_ep.plateau([c["ms"] for c in calls])
    on = sas_ep._peers_point(calls, 5246, pl)
    assert on["verdict"]["level"] == "warn" and "plateau" in on["verdict"]["text"] and on["plateau"] == pl
    assert on["histogram"] and on["this_ms"] == 5246
    off = sas_ep._peers_point(calls, 1500, pl)
    assert off["verdict"]["level"] == "ok"
    few = sas_ep._peers_point(calls[:3], 5246, None)
    assert few["verdict"]["level"] == "info"


def test_burst_point_for_many_small_calls() -> None:
    t0 = 3_000_000.0
    calls = [call(t0, 5200), call(t0 + 0.06, 5238), call(t0 + 0.08, 5272), call(t0 + 0.08, 5353, 2)]
    p = sas_ep._burst_point(calls, dict(calls[0]))
    assert p["verdict"]["level"] == "warn" and "4 calls" in p["verdict"]["text"] and p["verdict"]["step"]
    assert sum(1 for m in p["members"] if m["this"]) == 1
    alone = sas_ep._burst_point([call(t0 + 999, 5000)], call(t0, 5000))
    assert alone["verdict"]["level"] == "ok"


def test_generated_statement_failure_is_critical() -> None:
    p = sas_ep._generated_failed({"error": "ER_TABLE_EXISTS_ERROR", "ms": 0}, {"outputDatabase": "MyDB", "outputTable": "_hello_world_1"})
    assert p["verdict"]["level"] == "crit" and "MyDB._hello_world_1 already exists" in p["verdict"]["text"]


def test_tips_are_marked_and_sourced() -> None:
    pl = sas_ep.plateau(PLATEAU + FAST)
    card = {"kind": "format", "duration_ms": 5246, "points": [sas_ep.breakdown(5246, None, pl, None)]}
    tips = sas_ep.tips(card, pl)
    kinds = {t["kind"] for t in tips}
    assert kinds == {"measured", "hypothesis", "documented"}
    for t in tips:
        assert t["text"] and all(s["url"].startswith("https://") for s in t["sources"])
    assert any("resolv.conf" in t["text"] and t["kind"] == "hypothesis" for t in tips)
    assert any("Duration_threshold_ms = 100" in t["text"] for t in tips)
    # Off the plateau: no fixed-wait tips, still the general ones.
    off = sas_ep.tips({"kind": "job", "duration_ms": 1500, "points": []}, pl)
    assert not any("resolv.conf" in t["text"] for t in off) and any("KEEP=" in t["text"] for t in off)
    json.dumps(tips)  # goes to the app as JSON


# ------------------------------------------------------------------ EP overview from the local store

@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(history_store, "source_id", lambda: "test:3306")
    return "test:3306"


def _add_run(conn, src: str, i: int, finished: str, ms: int, sql: str, db: str, rows: int = 1, ok: int = 1) -> None:
    conn.execute("INSERT INTO runs (source, event_key, finished, ms, user, database, ok, error_code, category, rows, shape,"
                 " sample, origin, tables, plan_id, stmt) VALUES (?, ?, ?, ?, 'admin', ?, ?, NULL, 'DML', ?, ?, ?, ?, '[]', NULL, ?)",
                 (src, f"4:1:{i}", finished, ms, db, ok, rows, history_store.shape_of(sql), sql[:300], history_store.origin(sql),
                  history_store.statement_key(sql, db)))


def test_overview_by_source(store: str) -> None:
    conn = history_store.connect()
    try:
        for i, day in enumerate(["2026-10-12", "2026-10-13", "2026-10-14"]):
            _add_run(conn, store, i, f"{day} 12:35:21.664179", 5257, JOB, "MyDB")
        # A VA burst: three at 5.2 s, three later starters ending together.
        for j, ms in enumerate([5230, 5345, 5366]):
            _add_run(conn, store, 10 + j, f"2026-10-12 06:46:18.{263 + j * 60:03d}000", ms, FORMAT, "FANS")
        for j, ms in enumerate([1378, 1500, 1831]):
            _add_run(conn, store, 20 + j, f"2026-10-12 06:46:18.{474 + j:03d}000", ms, FORMAT, "FANS")
        _add_run(conn, store, 30, "2026-10-14 07:40:16.791362", 5200, FORMAT, "FANS")
        _add_run(conn, store, 40, "2026-10-14 08:00:00", 900, "SELECT 1", "FANS")  # not EP
        conn.execute("INSERT INTO plans (source, plan_key, node_id, plan_id, database, first_load, last_executed, runs, exec_ms,"
                     " text, origin, seen) VALUES (?, 'k1', 1, 1, 'FANS', '2026-10-12 06:00:00', '2026-10-14 12:15:28', 61, 480,"
                     " 'INSERT INTO EP.sasepv5 VALUES (?, ?, now())', 'SAS EP', '2026-10-12 06:00:00'),"
                     " (?, 'k2', 4, 2, 'FANS', '2026-10-12 06:00:00', '2026-10-14 12:15:28', 61, 560,"
                     " 'INSERT INTO EP.sasepv5 VALUES (?, ?, now())', 'SAS EP', '2026-10-12 06:00:00')", (store, store))
        conn.commit()
    finally:
        conn.close()
    o = sas_ep.overview()
    assert o["calls"] == 10 and o["failed"] == 0
    by = {(r["kind"], r["database"]): r for r in o["rows"]}
    assert by[("job", "MyDB")]["calls"] == 3 and by[("job", "MyDB")]["plateau"] == 3
    assert by[("format", "FANS")]["calls"] == 7 and by[("format", "FANS")]["plateau"] == 4
    assert o["plateau"] and o["plateau_calls"] == 7 and 0.8 < o["plateau_time_share"] < 1
    assert o["shared_bursts"] == 1
    # The registration plan is seen on two aggregators: counted once (61), not 122.
    assert o["all_format_functions"] == 61 and o["traced_format_functions"] >= 1
    lines = sas_ep.overview_lines(o)
    assert lines[0].startswith("SAS EP: 10 traced calls") and any("61 VA / CAS EP functions" in x for x in lines)
    since = sas_ep.overview("2026-10-14 00:00:00")
    assert since["calls"] == 2


def test_overview_empty_store(store: str) -> None:
    o = sas_ep.overview()
    assert o["calls"] == 0 and o["plateau"] is None and sas_ep.overview_lines(o) == []


def test_overview_adds_live_trace_calls_not_yet_stored(store: str) -> None:
    t = sas_ep.epoch("2026-10-14 07:40:11")
    live = [{**call(t + i * 0.05, ms), "stmt": "s1"} for i, ms in enumerate([5200, 5238, 5272, 5353])]
    old = {**call(sas_ep.epoch("2026-10-01 00:00:00"), 5200), "stmt": "s0"}
    o = sas_ep.overview("2026-10-14 00:00:00", live + [old])
    assert o["calls"] == 4 and o["bursts"] == 1 and o["rows"][0]["source"] == "SAS VA / CAS (format function)"
