"""The Studio repository (server version): the collector's tokens and filtering, the Studios' remote reads, the
SQLite outbox / restore, the monitor group, and (cluster) the write-through to a SingleStore database."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import collector, history_store, metrics_store, remote
from singlestore_mcp.hub import access
from singlestore_mcp.repository import TABLES, Repository

KEY = b"k" * 32


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A collector-like home: the built-in connection is the cluster the rows come from (never connected)."""
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("SINGLESTORE_HOST", "s2.example")
    monkeypatch.setenv("SINGLESTORE_PORT", "3306")
    for k in ("SINGLESTORE_URL", "SINGLESTORE_MCP_SERVER_MODE"):
        monkeypatch.delenv(k, raising=False)
    history_store._schema_ready.clear()
    metrics_store._schema_ready.clear()
    remote.configure(None, None, None)
    yield tmp_path
    remote.configure(None, None, None)
    history_store._schema_ready.clear()
    metrics_store._schema_ready.clear()


def _run(conn: sqlite3.Connection, key: str, user: str, finished: str, ms: int = 2000, sql: str = "select 1 from t") -> None:
    cols = history_store._RUN_COLUMNS
    conn.execute(f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 ("s2.example:3306", key, finished, ms, user, "db", 1, None, "SELECT", 1, history_store.shape_of(sql),
                  sql, None, '["db.t"]', None, history_store.statement_key(sql, "db")))


def _plan(conn: sqlite3.Connection, key: str, last: str) -> None:
    conn.execute("INSERT INTO plans (source, plan_key, node_id, plan_id, database, first_load, last_executed, runs, text, tables)"
                 " VALUES ('s2.example:3306', ?, 1, 1, 'db', ?, ?, 3, 'select * from t', '[\"db.t\"]')", (key, last, last))


def _fill(now: str = "2026-11-02 10:00:00") -> None:
    conn = history_store.connect()
    _run(conn, "1:1:1", "alice", now)
    _run(conn, "1:1:2", "Bob", now)
    _run(conn, "1:1:3", "alice", now, sql="select 2 from t")
    _plan(conn, "1:7:x", now)
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ tokens

def test_tokens_are_signed() -> None:
    t = collector.sign(KEY, "norjni", monitor=True, admin=False)
    c = collector.verify(KEY, t)
    assert c and c["u"] == "norjni" and c["m"] is True and c["a"] is False and "hub" not in c
    assert collector.verify(b"x" * 32, t) is None, "another key"
    body, mac = t.split(".")
    forged = collector._b64(json.dumps({"u": "norjni", "m": True, "a": True}).encode())
    assert collector.verify(KEY, f"{forged}.{mac}") is None, "changed claims"
    assert collector.verify(KEY, "") is None and collector.verify(KEY, "abc") is None
    assert collector.verify(KEY, collector.sign(KEY, "hub", False, True, hub=True))["hub"] is True


# ------------------------------------------------------------------ who sees what

def test_users_outside_the_monitor_group_see_only_their_own_queries(home: Path) -> None:
    _fill()
    me = {"u": "alice-sas", "m": False, "a": False}
    runs = collector.answer("kept_runs", {"before": None}, me, "ALICE")
    assert {r["user"] for r in runs} == {"alice"} and len(runs) == 2
    t = collector.answer("trends", {"days": 30, "now": "2026-11-02 12:00:00"}, me, "alice")
    assert t["stored"] == 2
    u = collector.answer("usage", {}, me, "alice")
    assert [x["user"] for x in u["users"]] == ["alice"] and u["plans_hidden"] and u["plans"] == 0
    # The plan cache has no user: only for the monitor group / admins.
    assert collector.answer("stored_plans", {}, me, "alice") == []
    assert collector.answer("stored_plan", {"plan_key": "1:7:x"}, me, "alice") is None
    assert collector.answer("kept_run", {"event_key": "1:1:2"}, me, "alice") is None, "Bob's run"
    # Couldn't tell who the connection signs in as: nobody's runs.
    assert collector.answer("kept_runs", {"before": None}, me, None) == []


def test_monitor_group_and_admins_see_everyone(home: Path) -> None:
    _fill()
    for claims in ({"u": "mon", "m": True, "a": False}, {"u": "adm", "m": False, "a": True}):
        assert len(collector.answer("kept_runs", {"before": None}, claims, "someone")) == 3
        assert [p["plan_key"] for p in collector.answer("stored_plans", {}, claims, None)] == ["1:7:x"]
        assert collector.answer("kept_run", {"event_key": "1:1:2"}, claims, None)["user"] == "Bob"


def test_a_studio_cant_widen_its_view(home: Path) -> None:
    _fill()
    me = {"u": "alice-sas", "m": False, "a": False}
    # "user" isn't an argument a Studio may pass; the collector sets it.
    runs = collector.answer("kept_runs", {"before": None, "user": None}, me, "alice")
    assert {r["user"] for r in runs} == {"alice"}
    with pytest.raises(PermissionError):
        collector.answer("prune", {}, me, "alice")


# ------------------------------------------------------------------ the Studio side, over HTTP

@pytest.fixture
def served(home: Path, monkeypatch: pytest.MonkeyPatch):
    """The collector's HTTP handler on a free port (no cluster: the Repository is never used)."""
    col = collector.Collector.__new__(collector.Collector)
    col.key, col.source = KEY, history_store.source_id()
    col.status = {"calls": 0, "denied": 0}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), collector._handler(col))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(remote, "_signed_in_as", lambda: "alice")
    yield f"http://127.0.0.1:{httpd.server_address[1]}", col
    httpd.shutdown()
    httpd.server_close()


def test_studio_reads_through_the_collector(served: tuple[str, Any]) -> None:
    url, col = served
    _fill()
    remote.configure(url, collector.sign(KEY, "alice-sas", False, False), col.source)
    assert remote.active()
    assert {r["user"] for r in history_store.kept_runs(None)} == {"alice"}
    assert history_store.stored_plans() == []
    # select 1 / select 2 are one statement (literals replaced): alice's two runs, not Bob's.
    assert history_store.count_runs([history_store.statement_key("select 1 from t", "db")]) == 2
    # The Studio doesn't sync or save the plan cache itself.
    assert history_store.sync(force=True)["collector"] is True
    assert history_store.save_plans([], force=True)["collector"] is True
    assert col.status["calls"] >= 3


def test_another_cluster_uses_the_local_store(served: tuple[str, Any]) -> None:
    url, _col = served
    _fill()
    remote.configure(url, collector.sign(KEY, "alice-sas", False, False), "other.example:3306")
    assert not remote.active()
    assert len(history_store.kept_runs(None)) == 3, "local SQLite, unfiltered (the desktop behaviour)"


def test_a_bad_token_is_refused(served: tuple[str, Any]) -> None:
    url, col = served
    remote.configure(url, collector.sign(b"z" * 32, "alice-sas", True, True), col.source)
    with pytest.raises(remote.CollectorError, match="isn't allowed"):
        history_store.kept_runs(None)
    assert col.status["denied"] == 1


def test_collector_down_says_so(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "_signed_in_as", lambda: "alice")
    remote.configure("http://127.0.0.1:9", collector.sign(KEY, "a", False, False), history_store.source_id())
    with pytest.raises(remote.CollectorError, match="isn't answering"):
        history_store.trends()


# ------------------------------------------------------------------ outbox, flush, restore (SQLite side)

class FakeRepo(Repository):
    """A Repository whose SingleStore side is a dict of tables (rows keyed by primary key)."""

    def __init__(self, home: Path) -> None:
        super().__init__({}, "SSSTUDIO_T", home)
        self.remote: dict[str, dict[tuple, dict[str, Any]]] = {t.name: {} for t in TABLES}
        self.down = False

    def _upsert(self, t, rows):  # noqa: ANN001
        if self.down:
            raise ConnectionError("cluster down")
        for r in rows:
            d = dict(zip(t.names, r))
            self.remote[t.name][tuple(d[k] for k in t.pk)] = d

    def _run(self, fn):  # noqa: ANN001 - restore's read: rows of the table it asks for
        rows: list[dict[str, Any]] = []

        class Cur:
            def execute(self_, sql, params=()):  # noqa: ANN001, N805
                name = sql.split(" FROM `", 1)[1].split("`", 1)[0]
                rows[:] = list(self.remote[name].values())

            def fetchall(self_):  # noqa: N805
                return rows

        return fn(Cur())


def test_outbox_sends_new_and_changed_rows_and_buffers_while_down(home: Path) -> None:
    history_store.connect().close()
    metrics_store.connect().close()
    repo = FakeRepo(home)
    repo.install_triggers()
    _fill()
    repo.down = True
    with pytest.raises(ConnectionError):
        repo.flush()
    assert repo.pending() == 4 and repo.status["last_error"].startswith("ConnectionError")
    repo.down = False
    assert repo.flush() == {"runs": 3, "plans": 1}
    assert repo.pending() == 0 and len(repo.remote["runs"]) == 3
    # An update (the plan cache's counters) is sent again; a deleted row isn't.
    conn = history_store.connect()
    conn.execute("UPDATE plans SET runs = 9 WHERE plan_key = '1:7:x'")
    _run(conn, "1:1:9", "carl", "2026-11-02 11:00:00")
    conn.execute("DELETE FROM runs WHERE event_key = '1:1:9'")
    conn.commit()
    conn.close()
    assert repo.flush() == {"plans": 1, "runs": 0}
    assert repo.remote["plans"][("s2.example:3306", "1:7:x")]["runs"] == 9
    # Metrics too (INSERT OR REPLACE is an insert for the trigger).
    mconn = metrics_store.connect()
    metrics_store.write(mconn, "s2.example:3306", 600, 60, [{"node_id": 1, "host": "h", "type": "MA", "cpu": 5.0}])
    metrics_store.write(mconn, "s2.example:3306", 600, 60, [{"node_id": 1, "host": "h", "type": "MA", "cpu": 7.0}])
    mconn.close()
    assert repo.flush() == {"metrics": 1}
    assert repo.remote["metrics"][("s2.example:3306", 1, 60, 600)]["cpu"] == 7.0


def test_restore_fills_an_empty_folder_without_sending_it_back(home: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    history_store.connect().close()
    metrics_store.connect().close()
    repo = FakeRepo(home)
    repo.install_triggers()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _fill(now)
    repo.flush()
    # A new pod volume: empty files, the same repository.
    fresh = tmp_path_factory.mktemp("fresh")
    import os

    os.environ["SINGLESTORE_MCP_HOME"] = str(fresh)
    history_store._schema_ready.clear()
    metrics_store._schema_ready.clear()
    history_store.connect().close()
    metrics_store.connect().close()
    repo2 = FakeRepo(fresh)
    repo2.remote = repo.remote
    assert repo2.local_empty()
    got = repo2.restore()
    assert got["runs"] == 3 and got["plans"] == 1
    repo2.install_triggers()
    assert repo2.pending() == 0, "restored rows aren't sent back"
    assert {r["user"] for r in history_store.kept_runs(None)} == {"alice", "Bob"}


def test_prune_keeps_the_retention(home: Path) -> None:
    conn = history_store.connect()
    _run(conn, "old", "a", "2020-01-01 00:00:00")
    _run(conn, "new", "a", time.strftime("%Y-%m-%d %H:%M:%S"))
    conn.commit()
    conn.close()
    assert history_store.prune(90, 90, 30)["runs"] == 1
    assert [r["key"] for r in history_store.kept_runs(None)] == ["new"]


# ------------------------------------------------------------------ the monitor group

def test_monitor_group_from_the_sas_groups() -> None:
    rules = access.Rules(users_group="Users", monitor_group="SpeedyStoreStudioMonitor")
    groups = [{"id": "Users", "name": "Users"}, {"id": "ssmon", "name": "SpeedyStoreStudioMonitor"}]
    a = access.decide("u", rules, groups)
    assert a.app and a.monitor and a.monitor_via == "group SpeedyStoreStudioMonitor"
    b = access.decide("u", rules, groups[:1])
    assert b.app and not b.monitor
    # Only a monitor group (no access groups): groups are read for it, access stays "everyone".
    only = access.Rules(monitor_group="SpeedyStoreStudioMonitor")
    assert only.wants_groups and not only.needs_groups
    assert access.decide("u", only, groups[1:]).monitor
    assert not access.decide("u", only, None).monitor, "group check failed: no monitor rights"
    assert access.Rules.from_json(rules.to_json()).monitor_group == "SpeedyStoreStudioMonitor"


# ------------------------------------------------------------------ the real thing (a zz_test_ database)

@pytest.mark.cluster
def test_write_through_and_restore_on_singlestore(home: Path, tmp_path_factory: pytest.TempPathFactory,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    import singlestoredb
    from support import REAL_HOME

    from singlestore_mcp import connections

    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(REAL_HOME))  # the developer's own connection (admin)
    for k in ("SINGLESTORE_HOST", "SINGLESTORE_PORT"):
        monkeypatch.delenv(k)
    kwargs = connections.connect_kwargs(connections.active())
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(home))
    monkeypatch.setenv("SINGLESTORE_HOST", "s2.example")
    db = f"zz_test_repo_{int(time.time())}"
    admin = singlestoredb.connect(**kwargs)
    cur = admin.cursor()
    cur.execute(f"CREATE DATABASE `{db}`")
    try:
        history_store.connect().close()
        metrics_store.connect().close()
        repo = Repository(kwargs, db, home)
        repo.ensure_schema()
        repo.ensure_schema()  # idempotent
        repo.install_triggers()
        _fill(time.strftime("%Y-%m-%d %H:%M:%S"))
        mconn = metrics_store.connect()
        metrics_store.write(mconn, "s2.example:3306", int(time.time()) // 60 * 60, 60,
                            [{"node_id": 1, "host": "h", "type": "MA", "cpu": 5.0, "mem_b": 1.0, "mem_max_b": 2.0}])
        mconn.close()
        assert repo.flush() == {"runs": 3, "plans": 1, "metrics": 1}
        conn = history_store.connect()
        conn.execute("UPDATE plans SET runs = 9 WHERE plan_key = '1:7:x'")
        conn.commit()
        conn.close()
        repo.flush()
        cur.execute(f"SELECT `user`, `rows`, `finished` FROM `{db}`.`runs` ORDER BY `event_key`")
        assert [r["user"] for r in cur.fetchall()] == ["alice", "Bob", "alice"]
        cur.execute(f"SELECT `runs` FROM `{db}`.`plans`")
        assert cur.fetchone()["runs"] == 9
        # A new, empty folder gets everything back.
        fresh = tmp_path_factory.mktemp("fresh_s2")
        monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(fresh))
        history_store._schema_ready.clear()
        metrics_store._schema_ready.clear()
        history_store.connect().close()
        metrics_store.connect().close()
        repo2 = Repository(kwargs, db, fresh)
        assert repo2.local_empty()
        assert repo2.restore() == {"runs": 3, "plans": 1, "plan_samples": 0, "metrics": 1}
        before = {r["key"]: r for r in history_store.kept_runs(None)}
        assert {r["user"] for r in before.values()} == {"alice", "Bob"}
        assert history_store.stored_plan("1:7:x")["runs"] == 9
        assert repo2.retain()["runs"] == 0
        repo.close()
        repo2.close()
    finally:
        cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
        admin.close()


# ------------------------------------------------------------------ the hub's side

def test_hub_settings_for_the_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from singlestore_mcp.hub import config

    for k, v in {"STUDIO_VIYA_URL": "https://viya.example", "STUDIO_MASTER_KEY": "m" * 40, "STUDIO_DATA_DIR": str(tmp_path),
                 "STUDIO_DEFAULT_S2_HOST": "10.1.2.3", "STUDIO_MONITOR_GROUP": "SpeedyStoreStudioMonitor"}.items():
        monkeypatch.setenv(k, v)
    for k in ("STUDIO_COLLECTOR_PASSWORD", "STUDIO_COLLECTOR_HOST", "STUDIO_REPOSITORY_DB"):
        monkeypatch.delenv(k, raising=False)
    cfg = config.load()
    assert not cfg.collector, "no password: no repository"
    assert cfg.rules.monitor_group == "SpeedyStoreStudioMonitor" and cfg.rules.wants_groups
    monkeypatch.setenv("STUDIO_COLLECTOR_PASSWORD", "not-a-real-one")
    cfg = config.load()
    assert cfg.collector and cfg.repository_db == "SSSTUDIO" and cfg.collector_s2 == {"host": "10.1.2.3", "port": "3306"}
    assert "not-a-real-one" not in repr(cfg)
    monkeypatch.setenv("STUDIO_REPOSITORY_DB", "bad name;")
    with pytest.raises(SystemExit):
        config.load()


def test_studios_get_a_token_with_what_they_may_see(tmp_path: Path) -> None:
    from singlestore_mcp.hub.audit import Audit
    from singlestore_mcp.hub.collector import CollectorProcess
    from singlestore_mcp.hub.config import HubConfig
    from singlestore_mcp.hub.spawner import Spawner

    cfg = HubConfig(viya_url="https://viya.example", master_key=b"m" * 32, data_dir=tmp_path, admins=("boss",),
                    collector_password="x", collector_s2={"host": "h"})
    sp = Spawner(cfg, Audit(tmp_path / "a.jsonl", echo=False))
    cp = CollectorProcess(cfg, sp.audit)
    sp.collector = cp
    assert sp.seed("u", None)["collector"] is None, "collector not running yet"
    cp.port, cp.source = 4711, "h:3306"
    sp.access["u"] = access.Access(app=True, app_via="", notebooks=False, notebooks_via="", monitor=True)
    seed = sp.seed("u", None)["collector"]
    assert seed["url"] == "http://127.0.0.1:4711" and seed["source"] == "h:3306"
    assert collector.verify(cp.key, seed["token"]) | {"i": 0} == {"u": "u", "m": True, "a": False, "i": 0}
    assert collector.verify(cp.key, sp.seed("boss", None)["collector"]["token"])["a"] is True
    assert collector.verify(cp.key, sp.seed("other", None)["collector"]["token"])["m"] is False
    env = sp._env("u")
    assert env["SINGLESTORE_MCP_WORKLOADS_DIR"] == str(tmp_path / "shared" / "workloads")
    assert "x" not in {env.get("STUDIO_COLLECTOR_PASSWORD"), env.get("SINGLESTORE_PASSWORD")}


def test_workloads_are_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp import workload

    shared = tmp_path / "shared"
    monkeypatch.setenv("SINGLESTORE_MCP_WORKLOADS_DIR", str(shared))
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path / "alice"))
    workload.save({"name": "zz shared", "statements": [{"sql": "SELECT 1"}]})
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path / "bob"))
    assert [w["name"] for w in workload.list_workloads()] == ["zz shared"]
    workload.delete("zz shared")
    assert workload.list_workloads() == [] and not (shared / ".workloads.lock").exists()
    assert workload._restore_file().parent == tmp_path / "bob" / "workloads", "trace settings stay per Studio"


def test_admin_sees_every_users_open_alerts(tmp_path: Path) -> None:
    from singlestore_mcp.hub.app import all_alerts
    from singlestore_mcp.hub.config import HubConfig

    cfg = HubConfig(viya_url="https://viya.example", master_key=b"m" * 32, data_dir=tmp_path)
    now = time.time()
    for user, alerts in {"alice": [{"kind": "disk", "title": "Disk 95%", "last_seen": now, "acked": None},
                                   {"kind": "disk", "title": "old", "last_seen": now - 30 * 86400}],
                         "bob": [{"kind": "failed_query", "title": "Failed", "last_seen": now - 60, "acked": now}]}.items():
        d = tmp_path / "users" / user / "state"
        d.mkdir(parents=True)
        (d / "alerts.json").write_text(json.dumps({"alerts": alerts}), encoding="utf-8")
    got = all_alerts(cfg)
    assert [(a["user"], a["title"]) for a in got] == [("alice", "Disk 95%")]


def test_admins_see_the_server_in_the_alerts_view(served: tuple[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp.apps import alerts as alerts_app

    url, col = served
    col.overview = lambda: {"repository": {"source": col.source, "repository": {"database": "SSSTUDIO"}},
                            "alerts": [{"user": "bob", "title": "Disk 95%"}]}
    remote.configure(url, collector.sign(KEY, "boss", False, True), col.source)
    assert remote.admin_overview()["alerts"][0]["user"] == "bob"
    data = alerts_app.alerts_data().structured_content
    assert data["server"]["repository"]["repository"]["database"] == "SSSTUDIO"
    remote.configure(url, collector.sign(KEY, "alice", True, False), col.source)  # monitor, not admin
    assert remote.admin_overview() is None
    # The collector refuses it too, whatever the Studio believes.
    monkeypatch.setattr(remote, "_claims", lambda: {"a": True})
    assert "Only Studio admins" in remote.admin_overview()["error"]
