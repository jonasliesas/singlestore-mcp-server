"""The Studio repository's copy of users' files: alerts and workloads mirrored to the database, deletes, the restore onto
an empty volume, and (cluster) the same on SingleStore."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import workload
from singlestore_mcp.repository import Repository
from singlestore_mcp.repository_files import ALERT_STATE, ALERTS, FILE_TABLES, WORKLOAD_RUNS, WORKLOADS, FileMirror


class FakeRepo(Repository):
    """The database as dicts: survives a "new volume" by being passed to the next mirror."""

    def __init__(self, home: Path) -> None:
        super().__init__({}, "SSSTUDIO_T", home)
        self.tables: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {t.name: {} for t in FILE_TABLES}
        self.down = False
        self.upserts = 0

    def ensure_schema(self) -> None:
        if self.down:
            raise ConnectionError("cluster down")

    def read_table(self, t):  # noqa: ANN001
        if self.down:
            raise ConnectionError("cluster down")
        return list(self.tables[t.name].values())

    def _upsert(self, t, rows):  # noqa: ANN001
        if self.down:
            raise ConnectionError("cluster down")
        self.upserts += len(rows)
        for r in rows:
            d = dict(zip(t.names, r))
            self.tables[t.name][tuple(d[k] for k in t.pk)] = d

    def delete_rows(self, t, keys):  # noqa: ANN001
        for k in keys:
            del self.tables[t.name][k]


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    users, shared = tmp_path / "users", tmp_path / "shared"
    users.mkdir()
    shared.mkdir()
    monkeypatch.setenv("SINGLESTORE_MCP_WORKLOADS_DIR", str(shared))
    return users, shared


def _alert(i: int, last: float = 1_800_000_000.0, **more: Any) -> dict[str, Any]:
    return {"id": f"a{i}", "key": f"k{i}", "source": "s", "kind": "slow_query", "severity": "warn", "title": f"Alert {i}",
            "detail": "select 1", "count": 1, "first_seen": last - 60, "last_seen": last, "acked": None, "active": False, **more}


def _write_alerts(users: Path, user: str, alerts: list[dict[str, Any]], rules: dict[str, Any] | None = None) -> Path:
    path = users / user / "state" / "alerts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": rules or {"slow": {"enabled": True, "seconds": 9}}, "settings": {"interval_s": 60},
                                "watermarks": {"s": 5}, "alerts": alerts}), encoding="utf-8")
    return path


def _write_workloads(shared: Path, names: list[str]) -> None:
    (shared / "workloads.json").write_text(json.dumps({"version": 1, "workloads": [{"name": n, "statements": []} for n in names]}),
                                           encoding="utf-8")


def _write_run(shared: Path, run_id: str, wl: str = "W1") -> Path:
    (shared / "runs").mkdir(exist_ok=True)
    path = shared / "runs" / f"{run_id}.json"
    path.write_text(json.dumps({"id": run_id, "workload": wl, "label": "first", "state": "done", "started": 1_800_000_000.5,
                                "finished": 1_800_000_060.5, "elapsed_s": 60.0, "totals": {"n": 3}}), encoding="utf-8")
    return path


def test_alerts_are_kept_per_user_with_readable_columns(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    users, shared = dirs
    _write_alerts(users, "alice", [_alert(1, active=True), _alert(2)])
    _write_alerts(users, "bob", [_alert(1)])
    repo = FakeRepo(tmp_path)
    FileMirror(repo, users, shared).sync()
    rows = repo.tables[ALERTS.name]
    assert set(rows) == {("alice", "a1"), ("alice", "a2"), ("bob", "a1")}
    row = rows[("alice", "a1")]
    assert row["active"] == 1 and row["kind"] == "slow_query" and row["title"] == "Alert 1"
    assert row["last_seen"] == "2027-01-15 08:00:00" and row["updated"]
    assert json.loads(row["doc"])["key"] == "k1"
    assert json.loads(repo.tables[ALERT_STATE.name][("alice",)]["doc"])["rules"]["slow"]["seconds"] == 9


def test_only_changes_are_sent_and_deletes_follow(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    users, shared = dirs
    _write_alerts(users, "alice", [_alert(1), _alert(2)])
    _write_workloads(shared, ["W1", "W2"])
    _write_run(shared, "r1")
    repo = FakeRepo(tmp_path)
    m = FileMirror(repo, users, shared)
    m.sync()
    sent = repo.upserts
    m.sync()
    assert repo.upserts == sent, "nothing changed: nothing sent"

    _write_alerts(users, "alice", [_alert(2, count=7)])   # a1 cleared, a2 changed
    _write_workloads(shared, ["W2"])
    (shared / "runs" / "r1.json").unlink()
    m.sync()
    assert set(repo.tables[ALERTS.name]) == {("alice", "a2")}
    assert repo.tables[ALERTS.name][("alice", "a2")]["count"] == 7
    assert set(repo.tables[WORKLOADS.name]) == {("W2",)}
    assert not repo.tables[WORKLOAD_RUNS.name]
    assert m.status["alerts"] == 1 and m.status["workloads"] == 1 and m.status["runs"] == 0


def test_a_new_collector_sends_nothing_again_and_sees_deletes_made_while_it_was_away(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    users, shared = dirs
    _write_workloads(shared, ["W1", "W2"])
    _write_run(shared, "r1")
    repo = FakeRepo(tmp_path)
    FileMirror(repo, users, shared).sync()
    sent = repo.upserts

    FileMirror(repo, users, shared).sync()
    assert repo.upserts == sent

    _write_workloads(shared, ["W2"])
    FileMirror(repo, users, shared).sync()
    assert set(repo.tables[WORKLOADS.name]) == {("W2",)}, "the folder wasn't empty: the workload stays deleted"


def test_missing_or_unreadable_files_delete_nothing(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    users, shared = dirs
    path = _write_alerts(users, "alice", [_alert(1)])
    _write_workloads(shared, ["W1"])
    run = _write_run(shared, "r1")
    repo = FakeRepo(tmp_path)
    m = FileMirror(repo, users, shared)
    m.sync()

    path.write_text("{ half written", encoding="utf-8")
    (shared / "workloads.json").write_text("{ half written", encoding="utf-8")
    run.write_text("{ half written", encoding="utf-8")
    m.sync()
    assert ("alice", "a1") in repo.tables[ALERTS.name]
    assert ("W1",) in repo.tables[WORKLOADS.name]
    assert ("r1",) in repo.tables[WORKLOAD_RUNS.name]

    path.unlink()
    (shared / "workloads.json").unlink()
    m.sync()
    assert ("alice", "a1") in repo.tables[ALERTS.name]
    assert ("W1",) in repo.tables[WORKLOADS.name]


def test_restore_onto_an_empty_volume(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    users, shared = dirs
    _write_alerts(users, "alice", [_alert(1, last=1_800_000_200.0), _alert(2, last=1_800_000_100.0)])
    _write_workloads(shared, ["W1"])
    _write_run(shared, "r1")
    repo = FakeRepo(tmp_path)
    FileMirror(repo, users, shared).sync()
    sent = repo.upserts

    new_users, new_shared = tmp_path / "new_users", tmp_path / "new_shared"
    new_users.mkdir()
    new_shared.mkdir()
    os.environ["SINGLESTORE_MCP_WORKLOADS_DIR"] = str(new_shared)  # monkeypatch in the fixture puts it back
    m = FileMirror(repo, new_users, new_shared)
    m.sync()

    data = json.loads((new_users / "alice" / "state" / "alerts.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in data["alerts"]] == ["a2", "a1"], "oldest first, as alerts._record keeps the list"
    assert data["rules"]["slow"]["seconds"] == 9 and data["watermarks"] == {"s": 5}
    assert workload.get("W1")["name"] == "W1"
    assert json.loads((new_shared / "runs" / "r1.json").read_text(encoding="utf-8"))["totals"] == {"n": 3}
    assert m.status["restored"]["alert_files"] == 1 and m.status["restored"]["workloads"] == 1 and m.status["restored"]["runs"] == 1
    assert repo.upserts == sent, "what was restored isn't sent back"


def test_restore_keeps_what_is_there_and_ignores_strange_names(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    users, shared = dirs
    repo = FakeRepo(tmp_path)
    doc = json.dumps({"rules": {}, "settings": {}, "watermarks": {}})
    now = "2026-11-03 10:00:00"
    repo.tables[ALERT_STATE.name] = {(u,): {"user": u, "doc": doc, "updated": now} for u in ("alice", "..", "a/b")}
    repo.tables[WORKLOAD_RUNS.name] = {("../x",): {"id": "../x", "doc": "{}", "updated": now}}
    mine = _write_alerts(users, "alice", [_alert(9)])
    before = mine.read_text(encoding="utf-8")
    FileMirror(repo, users, shared).sync()
    assert mine.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in users.iterdir()) == ["alice"]
    assert not (shared.parent / "x.json").exists()


def test_a_cluster_away_at_the_start_catches_up_and_restores_later(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    users, shared = dirs
    repo = FakeRepo(tmp_path)
    repo.tables[WORKLOADS.name] = {("Old",): {"name": "Old", "doc": json.dumps({"name": "Old", "statements": []}), "updated": "x"}}
    repo.down = True
    m = FileMirror(repo, users, shared)
    with pytest.raises(ConnectionError):
        m.sync()
    assert "cluster down" in m.status["last_error"]

    _write_workloads(shared, ["New"])  # a user saves a workload meanwhile
    repo.down = False
    m.sync()
    assert m.status["last_error"] is None
    assert sorted(w["name"] for w in workload.list_workloads()) == ["New", "Old"]
    assert set(repo.tables[WORKLOADS.name]) == {("New",), ("Old",)}


def test_collector_seed_and_env_name_the_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp import collector

    # setenv first: monkeypatch then puts back (or removes) what _setup_env sets in this process.
    for k in ("SINGLESTORE_MCP_WORKLOADS_DIR", "SINGLESTORE_URL", "SINGLESTORE_MCP_SERVER_MODE"):
        monkeypatch.setenv(k, "")
    for k in ("SINGLESTORE_MCP_HOME", "SINGLESTORE_HOST", "SINGLESTORE_PORT", "SINGLESTORE_USER", "SINGLESTORE_DATABASE", "SINGLESTORE_PASSWORD",
              "SINGLESTORE_SSL_DISABLED", "SINGLESTORE_SSL_CA", "SINGLESTORE_SSL_VERIFY", "SINGLESTORE_MCP_METRICS",
              "SINGLESTORE_MCP_METRICS_INTERVAL", "SINGLESTORE_MCP_METRICS_DELAY", "SINGLESTORE_MCP_METRICS_RAW_DAYS",
              "SINGLESTORE_MCP_METRICS_RETENTION_DAYS"):
        monkeypatch.setenv(k, os.environ.get(k, ""))  # restored after the test
    collector._setup_env({"home": str(tmp_path), "singlestore": {"host": "s2.example"}, "workloads_dir": str(tmp_path / "w")})  # noqa: SLF001
    assert os.environ["SINGLESTORE_MCP_WORKLOADS_DIR"] == str(tmp_path / "w")


@pytest.mark.cluster
def test_mirror_and_restore_on_singlestore(dirs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import singlestoredb
    from support import REAL_HOME

    from singlestore_mcp import connections

    users, shared = dirs
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(REAL_HOME))  # the developer's own connection (admin)
    for k in ("SINGLESTORE_HOST", "SINGLESTORE_PORT", "SINGLESTORE_URL"):
        monkeypatch.delenv(k, raising=False)
    kwargs = connections.connect_kwargs(connections.active())
    db = f"zz_test_repofiles_{int(time.time())}"
    admin = singlestoredb.connect(**{k: v for k, v in kwargs.items() if k != "database"})
    cur = admin.cursor()
    cur.execute(f"CREATE DATABASE `{db}`")
    try:
        _write_alerts(users, "alice", [_alert(1), _alert(2, title="Ærlig talt 😀")])
        _write_workloads(shared, ["W1", "w1"])
        _write_run(shared, "r1")
        repo = Repository(kwargs, db, tmp_path)
        m = FileMirror(repo, users, shared)
        m.sync()
        assert m.status["last_error"] is None and repo.status["skipped"] == 0, repo.status
        cur.execute(f"SELECT `name` FROM `{db}`.`workloads` ORDER BY 1")
        assert sorted(r["name"] for r in cur.fetchall()) == ["W1", "w1"], "names differing by case are two workloads"
        cur.execute(f"SELECT `started`, `elapsed_s` FROM `{db}`.`workload_runs`")
        run = cur.fetchone()
        assert str(run["started"]).startswith("2027-01-15 08:00:00") and run["elapsed_s"] == 60.0

        _write_alerts(users, "alice", [_alert(2, title="Ærlig talt 😀")])
        m.sync()
        cur.execute(f"SELECT `alert_id`, `title` FROM `{db}`.`alerts`")
        assert [(r["alert_id"], r["title"]) for r in cur.fetchall()] == [("a2", "Ærlig talt 😀")]

        new_users, new_shared = tmp_path / "nu", tmp_path / "ns"
        new_users.mkdir()
        new_shared.mkdir()
        monkeypatch.setenv("SINGLESTORE_MCP_WORKLOADS_DIR", str(new_shared))
        FileMirror(repo, new_users, new_shared).sync()
        data = json.loads((new_users / "alice" / "state" / "alerts.json").read_text(encoding="utf-8"))
        assert [a["id"] for a in data["alerts"]] == ["a2"] and data["alerts"][0]["title"] == "Ærlig talt 😀"
        assert sorted(w["name"] for w in workload.list_workloads()) == ["W1", "w1"]
        assert (new_shared / "runs" / "r1.json").is_file()
        repo.close()
    finally:
        cur.execute(f"DROP DATABASE IF EXISTS `{db}`")
        admin.close()
