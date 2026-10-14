"""Test workloads: placeholder filling, validation and run comparison (offline, against a temp home), and a
real run through the MCP tools (cluster)."""

from __future__ import annotations

import json
import random
import re
import time
from datetime import date
from pathlib import Path

import pytest
from support import DB, McpSession

# ============================================================================= offline unit tests


@pytest.fixture
def wl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    from singlestore_mcp import workload

    return workload


class TestPlaceholders:
    def test_int(self, wl) -> None:
        rnd = random.Random(1)
        for _ in range(200):
            v = int(wl.render("{{int:3:7}}", rnd))
            assert 3 <= v <= 7

    def test_int_with_spaces_inside_braces(self, wl) -> None:
        assert wl.render("{{ int:5:5 }}", random.Random(0)) == "5"

    def test_float(self, wl) -> None:
        rnd = random.Random(2)
        for _ in range(200):
            assert 0.5 <= float(wl.render("{{float:0.5:1.5}}", rnd)) <= 1.5

    def test_pick(self, wl) -> None:
        rnd = random.Random(3)
        seen = {wl.render("'{{pick:US|EU|Asia}}'", rnd) for _ in range(200)}
        assert seen == {"'US'", "'EU'", "'Asia'"}

    def test_date(self, wl) -> None:
        rnd = random.Random(4)
        for _ in range(100):
            d = date.fromisoformat(wl.render("{{date:2024-01-01:2024-01-31}}", rnd))
            assert date(2024, 1, 1) <= d <= date(2024, 1, 31)

    def test_several_in_one_statement(self, wl) -> None:
        sql = wl.render("SELECT * FROM t WHERE a = {{int:1:1}} AND b = '{{pick:x}}' AND c < {{float:2:2}}", random.Random(5))
        assert sql == "SELECT * FROM t WHERE a = 1 AND b = 'x' AND c < 2.0"

    def test_plain_sql_unchanged(self, wl) -> None:
        sql = "SELECT '{{not a placeholder}}', '{x}' FROM t"
        assert wl.render(sql, random.Random(6)) == sql

    @pytest.mark.parametrize("bad", ["{{int:a:b}}", "{{int:1}}", "{{float:x:1}}", "{{date:2024-13-01:2024-12-31}}", "{{date:yesterday}}"])
    def test_bad_placeholder_raises(self, wl, bad: str) -> None:
        with pytest.raises(ValueError, match="Bad placeholder"):
            wl.render(f"SELECT {bad}", random.Random(7))


class TestValidation:
    def _w(self, **kw):
        base = {"name": "zz test unit", "database": DB, "statements": [{"sql": "SELECT 1", "per_minute": 6}], "settings": {}}
        base.update(kw)
        return base

    @pytest.mark.parametrize("sql,ro", [
        ("SELECT 1", True), ("  with x as (select 1) select * from x", True), ("/* c */ SELECT 1", True),
        ("SELECT 'DELETE FROM t'", True), ("DELETE FROM CARS", False), ("UPDATE t SET a = 1", False),
        ("INSERT INTO t VALUES (1)", False), ("DROP TABLE t", False), ("SELECT 1; DROP TABLE t", False),
    ])
    def test_read_only_detection(self, wl, sql: str, ro: bool) -> None:
        from singlestore_mcp import compare

        assert compare.read_only(sql) is ro

    def test_write_refused_without_allow_writes(self, wl) -> None:
        with pytest.raises(ValueError, match="read-only"):
            wl.save(self._w(statements=[{"sql": "DELETE FROM CARS WHERE 1=0", "per_minute": 1}]))

    def test_write_allowed_with_allow_writes(self, wl) -> None:
        w = wl.save(self._w(statements=[{"sql": "DELETE FROM zz_t WHERE 1=0", "per_minute": 1}], settings={"allow_writes": True}))
        assert w["settings"]["allow_writes"] is True

    @pytest.mark.parametrize("kw,match", [
        ({"name": "bad/name"}, "Name"),
        ({"settings": {"pacing": "fast"}}, "pacing"),
        ({"statements": []}, "at least one"),
        ({"statements": [{"sql": "SELECT 1", "id": "a"}, {"sql": "SELECT 2", "id": "a"}]}, "unique"),
        ({"statements": [{"sql": "SELECT 1", "per_minute": -1}]}, "between"),
        ({"statements": [{"sql": "  ;"}]}, "no SQL"),
    ], ids=["name", "pacing", "no statements", "duplicate ids", "negative rate", "empty sql"])
    def test_save_validation(self, wl, kw: dict, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            wl.save(self._w(**kw))

    def test_save_list_rename_delete(self, wl, tmp_path: Path) -> None:
        wl.save(self._w(settings={"concurrency": 999, "duration_s": 1}))
        [w] = wl.list_workloads()
        assert w["settings"]["concurrency"] == wl.MAX_CONCURRENCY and w["settings"]["duration_s"] == 5  # clamped
        assert (tmp_path / "workloads" / "workloads.json").is_file()
        wl.save(self._w(name="zz test unit 2"), original_name="zz test unit")
        assert [x["name"] for x in wl.list_workloads()] == ["zz test unit 2"]
        wl.delete("zz test unit 2")
        assert wl.list_workloads() == []
        with pytest.raises(LookupError):
            wl.get("zz test unit 2")


def _stmt(sid: str, sql: str, count: int, rows_avg: float | None, p50: float = 10.0) -> dict:
    return {"id": sid, "name": f"Statement {sid}", "sql": sql, "count": count, "errors": 0, "qps": count / 20,
            "p50_ms": p50, "p95_ms": p50 * 2, "rows_avg": rows_avg}


def _save_run(wl, run_id: str, state: str, statements: list[dict], label: str = "", **settings) -> None:
    run = {"id": run_id, "workload": "zz test unit", "label": label, "state": state, "elapsed_s": 20.0, "source": "test",
           "started": time.time(), "finished": time.time(),
           "settings": {"concurrency": 4, "pacing": "rate", "duration_s": 20, **settings},
           "totals": {"count": sum(s["count"] for s in statements), "errors": 0, "qps": 1.0, "p50_ms": 10.0, "p95_ms": 20.0},
           "statements": statements}
    (wl._runs_dir() / f"{run_id}.json").write_text(json.dumps(run), encoding="utf-8")


class TestCompareRuns:
    def test_rows_differ_and_placeholder(self, wl) -> None:
        many = wl.MIN_COMPARE_COUNT * 5
        _save_run(wl, "zz-base", "done", [_stmt("s1", "SELECT * FROM t", many, 10.0),
                                          _stmt("s2", "SELECT * FROM t WHERE a = {{int:1:9}}", many, 10.0),
                                          _stmt("s3", "SELECT 1", many, 1.0)], label="baseline")
        _save_run(wl, "zz-new", "done", [_stmt("s1", "SELECT * FROM t", many, 12.0, p50=5.0),
                                         _stmt("s2", "SELECT * FROM t WHERE a = {{int:1:9}}", many, 99.0),
                                         _stmt("s3", "SELECT 1", many, 1.0)], label="after")
        c = wl.compare_runs("zz-base", "zz-new")
        by = {s["id"]: s for s in c["statements"]}
        assert by["s1"]["rows_differ"] is True, "plain SQL with a different rows_avg should be flagged"
        assert by["s2"]["rows_differ"] is False, "statements with placeholders read different rows every time"
        assert by["s3"]["rows_differ"] is False
        assert by["s1"]["p50_change_pct"] == -50.0 and by["s1"]["speedup"] == 2.0
        assert c["notes"] == [], c["notes"]
        assert "row counts differ" in wl.compare_summary(c)

    def test_notes_for_stopped_run_and_low_counts(self, wl) -> None:
        few = wl.MIN_COMPARE_COUNT - 1
        _save_run(wl, "zz-done", "done", [_stmt("s1", "SELECT 1", 100, 1.0)])
        _save_run(wl, "zz-stopped", "stopped", [_stmt("s1", "SELECT 1", few, 1.0)], label="stopped early")
        c = wl.compare_runs("zz-done", "zz-stopped")
        notes = " ".join(c["notes"])
        assert "zz-stopped" in notes and "stopped" in notes and "didn't finish" in notes, c["notes"]
        assert f"Fewer than {wl.MIN_COMPARE_COUNT} executions" in notes and "Statement s1" in notes, c["notes"]

    def test_notes_for_different_settings_and_missing_statements(self, wl) -> None:
        _save_run(wl, "zz-a", "done", [_stmt("s1", "SELECT 1", 50, 1.0), _stmt("s2", "SELECT 2", 50, 1.0)])
        _save_run(wl, "zz-b", "done", [_stmt("s1", "SELECT 1", 50, 1.0)], concurrency=8)
        c = wl.compare_runs("zz-a", "zz-b")
        notes = " ".join(c["notes"])
        assert "Different concurrency: 4 vs 8" in notes and "Only in the first run: Statement s2" in notes, c["notes"]

    def test_unknown_or_bad_run_id(self, wl) -> None:
        with pytest.raises(LookupError):
            wl.compare_runs("zz-nope", "zz-nope2")
        with pytest.raises(ValueError):
            wl.load_run("../etc/passwd")


    def test_old_run_rates_go_over_the_set_duration(self, wl) -> None:
        """Runs saved before runs lasted their full duration ended after the last release (51 of 60 s), so
        their rates were too high; compare puts them over the set duration, new runs are left alone."""
        old = [{**_stmt("s1", "SELECT 1", 60, 1.0), "qps": round(60 / 51, 3)}]
        _save_run(wl, "zz-old", "done", old, duration_s=60)
        path = wl._runs_dir() / "zz-old.json"
        d = json.loads(path.read_text(encoding="utf-8"))
        d.update(elapsed_s=51.0, totals={**d["totals"], "qps": round(60 / 51, 2)})
        path.write_text(json.dumps(d), encoding="utf-8")
        _save_run(wl, "zz-new2", "done", [{**_stmt("s1", "SELECT 1", 60, 1.0), "qps": 1.0}], duration_s=60)
        d = json.loads((wl._runs_dir() / "zz-new2.json").read_text(encoding="utf-8"))
        d.update(elapsed_s=60.0, timing=2)
        (wl._runs_dir() / "zz-new2.json").write_text(json.dumps(d), encoding="utf-8")
        c = wl.compare_runs("zz-old", "zz-new2")
        [s] = c["statements"]
        assert s["base_qps"] == 1.0 and s["new_qps"] == 1.0, s
        assert c["totals"]["base_qps"] == 1.0
        assert wl.load_run("zz-old")["elapsed_s"] == 51.0, "the saved file itself isn't changed"


# ----------------------------------------------------------------------------- run timing (fake clock and connection)

_SCALE = 50.0  # the fake clock runs 50x real time: a 10 s run takes 0.2 s


class _FakeCursor:
    description = None
    rowcount = 0

    def execute(self, sql: str) -> None:
        time.sleep(0.002)  # 0.1 s on the fake clock

    def close(self) -> None:
        pass


class _FakeConn:
    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def close(self) -> None:
        pass


@pytest.fixture
def fake_run(wl, monkeypatch: pytest.MonkeyPatch):
    """A _Run factory whose connections are fakes and whose clock runs _SCALE times faster."""
    base = time.perf_counter()
    monkeypatch.setattr(wl._Run, "_now", lambda self: base + (time.perf_counter() - base) * _SCALE)
    monkeypatch.setattr(wl._Run, "_pause", lambda self, s: self.stop_flag.wait(max(0.0, s) / _SCALE))
    monkeypatch.setattr(wl._Run, "_connect", lambda self, database: (_FakeConn(), 1))
    monkeypatch.setattr(wl._Run, "_rtt", lambda self, conn: None)
    monkeypatch.setattr("singlestore_mcp.history_store.source_id", lambda: "test")

    def make(pacing: str = "rate", per_minute: float = 30, duration_s: int = 10, think_ms: int = 0):
        wl.save({"name": "zz timing", "statements": [{"id": "a", "sql": "SELECT 1", "per_minute": per_minute}],
                 "settings": {"pacing": pacing, "concurrency": 2, "duration_s": duration_s, "think_ms": think_ms}})
        return wl._Run(wl.get("zz timing"), "", {})

    return make


class TestRunTiming:
    def test_rate_run_lasts_the_full_duration(self, fake_run) -> None:
        """Regression: with 30/min (every 2 s) the last release is at ~8-9 s and the run ended there."""
        run = fake_run("rate", 30, 10)
        run.run()
        r = run.summary()
        assert r["state"] == "done" and r["error"] is None, r
        assert r["elapsed_s"] == 10.0, r["elapsed_s"]
        assert r["finished"] - r["started"] == pytest.approx(10.0)
        [s] = r["statements"]
        assert s["count"] == 5, s  # released at ~0.x, 2.x, 4.x, 6.x, 8.x
        assert s["qps"] == 0.5 and s["per_minute"] == 30.0 and r["totals"]["qps"] == 0.5
        assert r["timing"] == 2

    def test_max_run_lasts_the_full_duration(self, fake_run) -> None:
        run = fake_run("max", 1, 10, think_ms=700)
        run.run()
        r = run.summary()
        assert r["state"] == "done" and r["elapsed_s"] == 10.0, r
        assert r["statements"][0]["count"] > 10

    def test_stop_is_immediate_and_ends_the_run_there(self, fake_run, wl) -> None:
        import threading

        run = fake_run("rate", 60, 600)
        t = threading.Thread(target=run.run)
        t.start()
        deadline = time.monotonic() + 5
        while run.state != "running" and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(3 / _SCALE)  # ~3 s on the fake clock
        stopped_at = time.monotonic()
        run.stop()
        t.join(5)
        assert not t.is_alive() and time.monotonic() - stopped_at < 1.0, "Stop should end the run at once"
        r = run.summary()
        assert r["state"] == "stopped", r
        assert 2.0 <= r["elapsed_s"] <= 8.0, r["elapsed_s"]
        assert r["finished"] - r["started"] == pytest.approx(r["elapsed_s"], abs=0.1)
        assert wl.load_run(run.id)["state"] == "stopped"


class TestHints:
    def test_name_and_rate_lines_are_taken_out(self, wl) -> None:
        h = wl.parse_hints("-- name: Top makes\n-- per_minute: 30\nSELECT Make FROM CARS;\n")
        assert h == {"sql": "SELECT Make FROM CARS", "name": "Top makes", "per_minute": 30.0}

    @pytest.mark.parametrize("line,rate", [("-- per minute: 12.5", 12.5), ("--per-minute:7", 7.0), ("  -- PER_MINUTE : 0", 0.0),
                                           ("-- per_minute: 2,5", 2.5), ("-- per_minute: lots", None), ("-- per_minute: -1", None),
                                           ("-- per_minute: 70000", None)])
    def test_rate_spellings_and_bad_values(self, wl, line: str, rate: float | None) -> None:
        h = wl.parse_hints(f"{line}\nSELECT 1")
        assert h["per_minute"] == rate and h["sql"] == "SELECT 1", h

    def test_other_comments_stay_and_hints_only_at_the_top(self, wl) -> None:
        h = wl.parse_hints("-- cars by origin\n-- name: By origin\nSELECT *\n-- name: not a hint here\nFROM CARS")
        assert h["name"] == "By origin"
        assert h["sql"] == "-- cars by origin\nSELECT *\n-- name: not a hint here\nFROM CARS"

    def test_no_hints(self, wl) -> None:
        assert wl.parse_hints("  SELECT 1 ;  ") == {"sql": "SELECT 1", "name": None, "per_minute": None}

    def test_name_is_cleaned(self, wl) -> None:
        assert wl.parse_hints("-- name:   a  */ b   \nSELECT 1")["name"] == "a b"
        assert len(wl.parse_hints(f"-- name: {'x' * 200}\nSELECT 1")["name"]) == 80

    def test_statements_from_sql(self, wl) -> None:
        stmts = wl.statements_from_sql(["-- per_minute: 5\nSELECT 1", "/* c */ SELECT   2\nFROM t", "  ", "-- name: only\n"], per_minute=42)
        assert [s["sql"] for s in stmts] == ["SELECT 1", "/* c */ SELECT   2\nFROM t"]
        assert stmts[0]["per_minute"] == 5 and stmts[0]["name"] == "SELECT 1" and stmts[0]["hints"]["per_minute"] == 5
        assert stmts[1]["per_minute"] == 42 and stmts[1]["name"] == "SELECT 2 FROM t" and stmts[1]["hints"] == {"name": None, "per_minute": None}
        # what save() keeps of them
        w = wl.save({"name": "zz test hints", "statements": stmts, "settings": {}})
        assert [s["id"] for s in w["statements"]] == ["s1", "s2"] and "hints" not in w["statements"][0]


class TestUpdateStatement:
    def _save(self, wl):
        return wl.save({"name": "zz test upd", "database": DB, "settings": {},
                        "statements": [{"id": "a", "name": "A", "sql": "SELECT 1", "per_minute": 6},
                                       {"id": "b", "name": "B", "sql": "SELECT 2", "per_minute": 6, "database": "other"}]})

    def test_updates_and_saves(self, wl) -> None:
        self._save(wl)
        r = wl.update_statement("zz test upd", "a", "-- per_minute: 9\nSELECT 11;", DB)
        assert r["statement"] == {**r["statement"], "sql": "SELECT 11", "name": "A", "per_minute": 9.0, "database": None}
        saved = wl.get("zz test upd")
        assert saved["statements"][0]["sql"] == "SELECT 11" and saved["statements"][1]["sql"] == "SELECT 2"

    def test_database_change_becomes_the_statements_own(self, wl) -> None:
        self._save(wl)
        assert wl.update_statement("zz test upd", "a", "SELECT 1", "information_schema")["statement"]["database"] == "information_schema"
        assert wl.update_statement("zz test upd", "b", "-- name: Bee\nSELECT 3", "other")["statement"]["name"] == "Bee"

    def test_missing_workload_or_statement(self, wl) -> None:
        with pytest.raises(LookupError, match="doesn't exist any more"):
            wl.update_statement("zz nope", "a", "SELECT 1")
        self._save(wl)
        with pytest.raises(LookupError, match="no statement 'zz'"):
            wl.update_statement("zz test upd", "zz", "SELECT 1")

    def test_write_refused_and_empty(self, wl) -> None:
        self._save(wl)
        with pytest.raises(ValueError, match="read-only"):
            wl.update_statement("zz test upd", "a", "DELETE FROM CARS")
        with pytest.raises(ValueError, match="no SQL"):
            wl.update_statement("zz test upd", "a", "-- name: x\n ; ")
        assert wl.get("zz test upd")["statements"][0]["sql"] == "SELECT 1", "a refused update must not change the workload"


class TestChatContext:
    def _big(self) -> tuple[dict, dict, dict]:
        long_sql = "SELECT " + ", ".join(f"col_{i}" for i in range(4000)) + " FROM SASDP.CARS WHERE Make = '{{pick:A|B}}'"
        w = {"name": "zz big", "database": DB, "description": "d" * 5000, "settings": {"pacing": "rate", "concurrency": 8},
             "statements": [{"id": f"s{i}", "name": f"Statement {i}", "per_minute": 6, "sql": long_sql, "database": None, "enabled": i % 2 == 0}
                            for i in range(50)]}
        run = {"id": "r1", "label": "base", "state": "done", "elapsed_s": 60, "totals": {"count": 500, "qps": 8.3, "p50_ms": 12, "p95_ms": 40, "errors": 1},
               "statements": [{"id": f"s{i}", "name": f"Statement {i}", "count": 10, "p50_ms": 12.5, "p95_ms": 41.0, "errors": 1, "rows_avg": 3.0,
                               "error_samples": [{"error": "boom " * 200, "count": 1}]} for i in range(50)]}
        tables = {f"SASDP.T{i}": {"rows": 1_000_000, "cardinality": {f"c{k}": k for k in range(100)}, "ddl": "CREATE TABLE x (" + "a INT, " * 3000 + ")"}
                  for i in range(25)}
        return w, run, tables

    def test_compact_for_a_huge_workload(self, wl) -> None:
        w, run, tables = self._big()
        text = wl.chat_context(w, run, tables)
        assert len(text) < 50_000, len(text)
        assert "Statement 49" in text and "[s49]" in text, "every statement is listed even when their SQL is cut"
        assert "Last run r1" in text and "p95 41.0 ms" in text

    def test_small_workload_in_full(self, wl) -> None:
        w = {"name": "zz small", "database": DB, "settings": {"pacing": "max", "concurrency": 2},
             "statements": [{"id": "s1", "name": "Count", "per_minute": 30, "sql": "SELECT COUNT(*) FROM CARS", "database": None, "enabled": True}]}
        tables = {"SASDP.CARS": {"rows": 428, "cardinality": {"make": 38}, "ddl": "CREATE TABLE `CARS` (`Make` varchar(13))"}}
        text = wl.chat_context(w, None, tables)
        for part in ("Workload 'zz small'", "default database SASDP", "pacing max", "relative weight", "2 connections",
                     "[s1] Count · per_minute 30", "SELECT COUNT(*) FROM CARS", "No run results yet", "SASDP.CARS (~428 rows)",
                     "make 38", "CREATE TABLE `CARS`"):
            assert part in text, (part, text)
        assert len(text) < 2000

    def test_new_workload_and_missing_table(self, wl) -> None:
        text = wl.chat_context({"name": "", "statements": []}, None, {"SASDP.GONE": {"missing": True, "error": "doesn't exist"}})
        assert "new, not saved yet" in text and "SASDP.GONE: not found" in text

    def test_tables_read(self, wl) -> None:
        w = {"database": DB, "statements": [
            {"sql": "WITH x AS (SELECT * FROM CARS) SELECT * FROM x JOIN other_db.T2 t ON 1 = 1 WHERE a = {{int:1:5}}"},
            {"sql": "SELECT * FROM information_schema.TABLES"}, {"sql": "SELECT * FROM CARS", "database": "X"},
            {"sql": "SELECT {{int:oops}} FROM CARS"}]}
        assert wl.tables_read(w) == ["SASDP.CARS", "other_db.T2", "X.CARS"]


# ============================================================================= app tools through the MCP server (no cluster)


class TestEditorHandoffTools:
    def test_parse_statements(self, mcp: McpSession) -> None:
        d = mcp.ok("workload_parse_statements", {"sql": ["-- name: N\n-- per_minute: 3\nSELECT 1", "SELECT 2"], "per_minute": 15})
        assert [(s["name"], s["per_minute"], s["sql"]) for s in d["statements"]] == [("N", 3.0, "SELECT 1"), ("SELECT 2", 15.0, "SELECT 2")]

    def test_handoff_put_take_once(self, mcp: McpSession) -> None:
        stmts = [{"name": "A", "sql": "SELECT 1", "per_minute": 60}]
        hid = mcp.ok("workload_handoff_put", {"statements": stmts, "database": DB, "new_name": "zz test new"})["id"]
        d = mcp.ok("workload_handoff_take", {"handoff_id": hid})
        assert d["statements"] == stmts and d["database"] == DB and d["new_name"] == "zz test new" and d["workload"] is None
        assert "expired" in mcp.err("workload_handoff_take", {"handoff_id": hid})
        mcp.err("workload_handoff_put", {"statements": []})

    def test_workload_link_with_handoff_opens(self, mcp: McpSession) -> None:
        """The browser link carries ``handoff`` for the page; the workload tool itself ignores it."""
        r = mcp.call("workload", {"handoff": "abc123"})
        assert not r.is_error, r.text[:400]

    def test_update_statement(self, mcp: McpSession) -> None:
        name = "zz test upd tool"
        mcp.ok("workload_save", {"workload": {"name": name, "database": DB, "settings": {},
                                              "statements": [{"id": "s1", "name": "One", "sql": "SELECT 1", "per_minute": 6}]}})
        try:
            d = mcp.ok("workload_update_statement", {"name": name, "statement_id": "s1", "sql": "-- name: Uno\nSELECT 1 AS uno", "database": DB})
            assert d["statement"]["sql"] == "SELECT 1 AS uno" and d["statement"]["name"] == "Uno"
            listed = next(w for w in mcp.ok("workload_overview")["workloads"] if w["name"] == name)
            assert listed["statements"][0]["sql"] == "SELECT 1 AS uno"
            assert "no statement" in mcp.err("workload_update_statement", {"name": name, "statement_id": "s9", "sql": "SELECT 2"})
        finally:
            mcp.call("workload_delete", {"name": name, "runs_too": True})
        assert "doesn't exist any more" in mcp.err("workload_update_statement", {"name": name, "statement_id": "s1", "sql": "SELECT 2"})

    def test_editor_tab_keeps_its_origin(self, mcp: McpSession) -> None:
        tab = mcp.ok("sql_editor_tab_new", {})["tab"]["id"]
        try:
            origin = {"workload": "zz w", "statement_id": "s2", "statement_name": "Two", "junk": "x"}
            mcp.ok("sql_editor_save_draft", {"tab_id": tab, "sql": "SELECT 2", "origin": origin})
            assert mcp.ok("sql_editor_state", {"tab_id": tab})["draft"]["origin"] == {"workload": "zz w", "statement_id": "s2", "statement_name": "Two"}
            mcp.ok("sql_editor_save_draft", {"tab_id": tab, "sql": "SELECT 2", "origin": {"workload": ""}})
            assert mcp.ok("sql_editor_state", {"tab_id": tab})["draft"]["origin"] is None
        finally:
            mcp.call("sql_editor_tab_close", {"tab_id": tab})


# ============================================================================= through the MCP tools (cluster)

NAME = "zz test workload"
STATEMENTS = [
    {"id": "count", "name": "Count", "sql": "SELECT COUNT(*) FROM CARS", "per_minute": 60},
    {"id": "makes", "name": "Makes", "sql": "SELECT Make, COUNT(*) AS n FROM CARS GROUP BY Make ORDER BY n DESC", "per_minute": 120},
    {"id": "cyl", "name": "By cylinders", "sql": "SELECT * FROM CARS WHERE Cylinders = {{int:4:8}}", "per_minute": 30},
]
DURATION = 20


def _wait_done(mcp: McpSession, run_id: str, timeout: float) -> dict:
    d, secs = mcp.wait_until("workload_status", {"run_id": run_id},
                             lambda d: isinstance(d, dict) and d.get("state") not in ("starting", "running"), timeout, 1.0)
    assert secs is not None, f"run {run_id} still {d.get('state') if isinstance(d, dict) else d} after {timeout} s"
    return d


@pytest.fixture(scope="module")
def defined(cluster: McpSession):
    d = cluster.ok("workload_define", {"name": NAME, "statements": STATEMENTS, "database": DB, "concurrency": 4,
                                        "duration_s": DURATION, "description": "pytest: read-only statements on SASDP.CARS"})
    yield d
    cluster.call("workload_delete", {"name": NAME, "runs_too": True})


@pytest.fixture(scope="module")
def finished_run(cluster: McpSession, defined) -> dict:
    started = cluster.ok("workload_run", {"name": NAME, "label": "pytest baseline"})
    run_id = started["id"]
    second = cluster.call("workload_run", {"name": NAME, "label": "pytest second"})
    live = cluster.ok("workload_status", {"run_id": run_id})
    final = _wait_done(cluster, run_id, DURATION + 60)
    return {"id": run_id, "second": second, "live": live, "final": final}


@pytest.fixture(scope="module")
def stopped_run(cluster: McpSession, finished_run) -> dict:
    started = cluster.ok("workload_run", {"name": NAME, "label": "pytest stopped", "duration_s": 60})
    time.sleep(3)
    stop = cluster.ok("workload_stop", {"run_id": started["id"]})
    final = _wait_done(cluster, started["id"], 30)
    return {"id": started["id"], "stop": stop, "final": final}


@pytest.mark.cluster
class TestWorkloadRun:
    def test_define(self, defined) -> None:
        assert defined["saved"] and len(defined["workload"]["statements"]) == 3

    def test_define_write_refused(self, cluster: McpSession) -> None:
        text = cluster.err("workload_define", {"name": "zz test writes", "database": DB,
                                                "statements": [{"sql": "DELETE FROM CARS WHERE 1=0", "per_minute": 1}]})
        assert "read-only" in text, text

    def test_only_one_run_at_a_time(self, finished_run) -> None:
        r = finished_run["second"]
        assert r.is_error and "one at a time" in r.text, r.text[:400]

    def test_live_status_while_running(self, finished_run) -> None:
        live = finished_run["live"]
        assert live["state"] in ("starting", "running", "done"), live
        assert "summary" in live and live["id"] == finished_run["id"]

    def test_finishes_without_errors(self, finished_run) -> None:
        f = finished_run["final"]
        assert f["state"] == "done", f"state {f['state']}, error {f.get('error')}"
        assert f["totals"]["errors"] == 0, [s.get("error_samples") for s in f["statements"]]
        assert f["elapsed_s"] == DURATION, f"the run should last its full duration, not {f['elapsed_s']} s"
        assert f["finished"] - f["started"] == pytest.approx(DURATION, abs=0.2)

    @pytest.mark.parametrize("sid", [s["id"] for s in STATEMENTS])
    def test_rate_within_15_percent(self, finished_run, sid: str) -> None:
        s = next(x for x in finished_run["final"]["statements"] if x["id"] == sid)
        target = s["target_per_minute"]
        assert abs(s["per_minute"] - target) <= 0.15 * target, f"{sid}: {s['per_minute']}/min vs target {target}/min ({s['count']} runs)"
        assert s["errors"] == 0 and s["count"] > 0

    def test_stop(self, stopped_run) -> None:
        stop = stopped_run["stop"]
        assert stop["state"] == "stopped", f"workload_stop returned state {stop['state']!r}"
        assert stop["message"].startswith("Stopped"), stop["message"]
        assert stopped_run["final"]["state"] == "stopped"

    def test_compare(self, cluster: McpSession, finished_run, stopped_run) -> None:
        r = cluster.call("workload_compare", {"base_run_id": finished_run["id"], "new_run_id": stopped_run["id"]})
        assert not r.is_error, r.text[:500]
        c = r.data
        assert {s["id"] for s in c["statements"]} == {s["id"] for s in STATEMENTS}
        notes = " ".join(c["notes"])
        assert stopped_run["id"] in notes and "stopped" in notes, c["notes"]
        # The short stopped run usually has statements with 1–9 executions; how many depends on cluster load,
        # so check the note against the data rather than assume it.
        few = [s for r in (finished_run, stopped_run) for s in r["final"]["statements"] if 0 < (s.get("count") or 0) < 10]
        assert bool(re.search(r"Fewer than \d+ executions", notes)) == bool(few), (c["notes"], few)
        cyl = next(s for s in c["statements"] if s["id"] == "cyl")
        assert cyl["rows_differ"] is False, "placeholder statements must not be flagged for differing rows"

    def test_list_shows_runs(self, cluster: McpSession, finished_run, stopped_run) -> None:
        d = cluster.ok("workload_list")
        assert NAME in [w["name"] for w in d["workloads"]]
        ids = {r["id"] for r in d["runs"]}
        assert {finished_run["id"], stopped_run["id"]} <= ids, ids
        assert d["running"] is None
