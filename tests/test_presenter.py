"""Presenter mode's labels (presenter.py) and the Studio's own-query classifier (history_store.is_studio_query).
Offline: the seeding from saved connections and the cluster is replaced by fixed values."""

from __future__ import annotations

import pytest

from singlestore_mcp import history_store, presenter


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch):
    def seed() -> None:
        presenter._add("posten prod", "connection")
        presenter._add("zeus", "connection")
        presenter._add("s2.customer.example.com", "host")
        presenter._add("jonas.lie@example.com", "user")
        presenter._add("root", "user")  # generic: never a label

    monkeypatch.setattr(presenter, "_seed", seed)
    monkeypatch.setattr(presenter, "_seed_cluster_users", lambda: True)
    presenter.reset()
    yield presenter.labels
    presenter.reset()


def test_labels_are_neutral_and_stable(labels) -> None:
    got = {x["value"]: x["label"] for x in labels()}
    assert got == {"posten prod": "Connection A", "zeus": "Connection B", "s2.customer.example.com": "host-1",
                   "jonas.lie@example.com": "user1"}
    # Values a page finds later get the next label; known ones keep theirs (also in another case).
    got = {x["value"]: x["label"] for x in labels([{"value": "10.1.2.3", "kind": "host"}, {"value": "POSTEN PROD", "kind": "connection"},
                                                  {"value": "viya.example.com", "kind": "viya"}, {"value": "%", "kind": "host"}])}
    assert got["10.1.2.3"] == "host-2" and got["viya.example.com"] == "viya-1" and got["posten prod"] == "Connection A"
    assert "%" not in got and "POSTEN PROD" not in got and "root" not in got


@pytest.mark.parametrize(("sql", "own"), [
    ("SELECT @@datadir AS d", True),
    ("SELECT NOW() AS t", True),
    ("select @", True),
    ("SELECT CURRENT_USER()", True),
    ("/* s2-cluster-monitor */ SELECT ID FROM information_schema.MV_PROCESSLIST", True),
    ("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @", True),
    ("SHOW DATABASES", True),
    ("SELECT Make, Model FROM CARS ORDER BY MSRP DESC LIMIT @", False),
    ("SELECT 1 AS x FROM CARS", False),
    ("SELECT * FROM information_schema.TABLES t JOIN SASDP.CARS c ON 1 = 1", False),
    ("/* workload:w1 stmt:s1 */ SELECT COUNT(*) FROM CARS", False),
    (None, False),
])
def test_is_studio_query(sql: str | None, own: bool) -> None:
    assert history_store.is_studio_query(sql) is own
