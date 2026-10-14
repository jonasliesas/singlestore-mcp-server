"""Cluster Monitor Health / History / Backups tools against the cluster. Read-only on the cluster (the History
sample is written to the test server's own SQLite store)."""

from __future__ import annotations

import json

import pytest
from support import RESULT_LIMIT, McpSession

pytestmark = pytest.mark.cluster


def _ok(cluster: McpSession, name: str, args: dict | None = None, timeout: float = 60):
    r = cluster.call(name, args or {}, timeout=timeout)
    assert not r.is_error, f"{name} failed: {r.text[:500]}"
    assert max(r.text_chars, r.struct_chars) <= RESULT_LIMIT, f"{name} result too big: {r.text_chars}/{r.struct_chars}"
    return r


def test_health(cluster: McpSession) -> None:
    r = _ok(cluster, "cluster_monitor_health")
    d = r.data
    assert d["status"] in ("healthy", "warning", "critical")
    assert d["nodes"] and all(n["state"] for n in d["nodes"])
    assert any(n["type"] == "MA" for n in d["nodes"])
    assert d["databases"] and all("summary" in x for x in d["databases"])
    assert d["errors"] == {}, d["errors"]  # every documented view was readable
    assert d["versions"] and d["versions"][0].startswith("9.")
    for i in d["issues"]:
        assert i["severity"] in ("critical", "warning", "info") and i["explanation"]
        assert i["docs"].startswith("https://docs.singlestore.com/")
    # Never the license key or blob.
    assert "License_key" not in json.dumps(d) and "License_key" not in r.text
    assert r.ms < 15000
    assert "Cluster health:" in r.text


def test_backups(cluster: McpSession) -> None:
    r = _ok(cluster, "cluster_monitor_backups", {"warn_days": 1})
    d = r.data
    assert d["warn_days"] == 1
    assert d["errors"] == {}, d["errors"]
    assert d["databases"], "every database should be listed (with or without backups)"
    for x in d["databases"]:
        assert x["state"] in ("ok", "old", "failed", "never", "dropped")
    assert isinstance(d["history"], list) and isinstance(d["running"], list)


def test_history_sample_now(cluster: McpSession) -> None:
    empty = _ok(cluster, "cluster_monitor_history", {"range": "24h"}).data
    assert empty["range"] == "24h" and empty["bucket_s"] == 300
    assert empty["status"]["enabled"] is False  # the test server runs with SINGLESTORE_MCP_METRICS=0
    d = _ok(cluster, "cluster_monitor_history", {"range": "1h", "sample_now": True}).data
    assert d["points"], d
    p = d["points"][-1]
    nodes = {str(n["id"]) for n in d["nodes"]}
    assert nodes and set(p["cpu"]) <= nodes
    assert set(p["conns"]) == nodes
    assert all(0 <= v <= 100 for v in p["mem"].values())
    assert d["status"]["last_sample"]


def test_history_bad_range_falls_back(cluster: McpSession) -> None:
    assert _ok(cluster, "cluster_monitor_history", {"range": "1y"}).data["range"] == "1h"
