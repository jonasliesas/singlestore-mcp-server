"""On the cluster: Security "Drop view…" for a column view - listed by security_views, previewed (DROP VIEW, no grants),
refused without the typed name, run after it, and gone. Writes only a zz_test_ table and view in the `test` database
(dropped in teardown, checked to be gone); no users or roles are created."""

from __future__ import annotations

import secrets

import pytest
from support import McpSession

pytestmark = pytest.mark.cluster

T = "test"


def _gone(cluster: McpSession, tag: str) -> bool:
    r = cluster.ok("run_sql", {"sql": f"SELECT TABLE_NAME AS t FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{T}'"
                                      f" AND TABLE_NAME LIKE '%{tag}'"})
    return not r.get("rows")


def test_drop_view_round_trip(cluster: McpSession) -> None:
    cap = cluster.ok("security_admin_capabilities")
    tag = secrets.token_hex(3)
    table, view = f"zz_test_dvt_{tag}", f"zz_test_dvv_{tag}"
    try:
        cluster.ok("run_sql", {"sql": f"CREATE TABLE `{T}`.`{table}` (a INT, b INT, secret INT, SHARD KEY (a))"})
        cluster.ok("run_sql", {"sql": f"CREATE VIEW `{T}`.`{view}` AS SELECT a, b FROM `{T}`.`{table}`"})
        listed = cluster.ok("security_views", {"database": T, "table": table})["views"]
        assert [(v["view"], v["columns"], v["grants"]) for v in listed] == [(view, ["a", "b"], [])], listed
        assert cluster.ok("security_views", {"database": T, "table": "zz_test_no_such"})["views"] == []

        action = {"action": "drop_view", "database": T, "view": view}
        p = cluster.ok("security_change_plan", {"action": action})
        assert p["statements"] == [f"DROP VIEW `{T}`.`{view}`;"]
        assert any(w.startswith(f"No grants on {T}.{view}") for w in p["warnings"]), p["warnings"]
        assert any(f"Reads {T}.{table}" in w for w in p["warnings"]), p["warnings"]
        assert p["confirm"]["typed"] and p["confirm"]["phrase"] == view
        if not p["allowed"]["ok"]:
            pytest.skip(f"{cap['me']['user']} can't drop the view: {p['allowed']['reason']}")
        assert "No view" in cluster.err("security_change_plan", {"action": {**action, "view": table}})  # a table, not a view
        assert "type" in cluster.err("security_change_apply", {"action": action, "confirm": True, "typed_confirmation": "nope"})
        out = cluster.ok("security_change_apply", {"action": action, "confirm": True, "typed_confirmation": view})
        assert out["ok"] and [r["status"] for r in out["results"]] == ["ok"], out
        assert cluster.ok("security_views", {"database": T, "table": table})["views"] == []
        n = cluster.ok("run_sql", {"sql": f"SELECT COUNT(*) AS n FROM `{T}`.`{table}`"})["rows"][0]["n"]
        assert n == 0, "the table the view read stays"
    finally:
        cluster.call("run_sql", {"sql": f"DROP VIEW IF EXISTS `{T}`.`{view}`"})
        cluster.call("run_sql", {"sql": f"DROP TABLE IF EXISTS `{T}`.`{table}`"})
        assert _gone(cluster, tag), "zz_test_ objects left behind"
