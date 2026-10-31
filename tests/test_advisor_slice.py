"""query_advisor_report keeps its tool result under Claude's size limit: the most urgent tables, no examples."""

from __future__ import annotations

import json

from singlestore_mcp.apps import query_history as qh


def _table(i: int) -> dict:
    return {"table": f"db.t{i}", "priority": "high" if i < 3 else "low", "recommendations": [{"title": "x" * 500}],
            "examples": ["SELECT " + "c, " * 300 + "1"] * 5}


def test_large_result_is_sliced_and_marked_partial() -> None:
    result = {"tables": [_table(i) for i in range(70)], "workload": {"n": 1}}
    sliced = qh._advisor_slice(result)
    assert sliced["partial"] is True and sliced["workload"] == {"n": 1}
    assert [t["table"] for t in sliced["tables"]] == [f"db.t{i}" for i in range(qh._ADVISOR_SLICE)]  # most urgent first
    assert all(t["examples"] == [] for t in sliced["tables"])
    assert len(json.dumps(sliced)) < 50_000 < len(json.dumps(result))
    assert len(result["tables"]) == 70 and "examples" in result["tables"][0]  # the cached result is untouched


def test_small_result_without_examples_is_unchanged() -> None:
    result = {"tables": [{"table": "db.t", "priority": "ok", "recommendations": []}]}
    assert qh._advisor_slice(result) is result
