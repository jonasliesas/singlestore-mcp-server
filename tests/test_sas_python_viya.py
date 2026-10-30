"""Python in SAS cells against SAS Viya (``--sas``): two cells share Python state, WORK is the SQL Editor's SAS
session's, ODS output comes back as HTML, Restart SAS session clears the Python state. Read-only: WORK only."""

from __future__ import annotations

import time

import pytest
from support import McpSession

pytestmark = pytest.mark.sas


def _cell(mcp: McpSession, source: str, run_id: str, timeout: float = 300) -> dict:
    r = mcp.call("notebook_sas_python", {"source": source, "notebook_id": "zz-pytest-pysas", "run_id": run_id, "wait_ms": 2000},
                 timeout=timeout)
    assert not r.is_error, r.text[:800]
    d = r.data
    t0 = time.perf_counter()
    while d.get("running"):
        assert time.perf_counter() - t0 < timeout, f"{run_id} still running"
        d = mcp.ok("notebook_sas_python_status", {"run_id": run_id, "wait_ms": 10_000}, timeout=60)
    return d


def _stdout(d: dict) -> str:
    return "".join(o["text"] for o in d["outputs"] if o["output_type"] == "stream" and o["name"] == "stdout")


def test_state_persists_between_cells_and_work_is_shared(sas: McpSession) -> None:
    d = _cell(sas, 'x = 41\ndf = SAS.sd2df("sashelp.class")\nSAS.df2sd(df[df.Age > 14], "work.zz_pysas_test")\nprint("rows", len(df))',
              "pytest-a")
    assert d["status"] == "ok", d
    assert _stdout(d).endswith("rows 19\n")  # (df2sd prints a note about the index first)
    d = _cell(sas, "print('x + 1 =', x + 1)", "pytest-b")
    assert d["status"] == "ok" and _stdout(d) == "x + 1 = 42\n" and d["python"] == "resumed"
    # The table the cell wrote is in the SQL Editor's SAS session (same Compute session, same WORK).
    q = sas.ok("sas_query", {"language": "procsql", "sql": "select count(*) as n from work.zz_pysas_test"}, timeout=300)
    assert not q["error"] and q["rows"][0][q["columns"][0]] == 5


def test_errors_and_ods_output(sas: McpSession) -> None:
    d = _cell(sas, "def f(v):\n    return 1 / v\n\nf(0)", "pytest-c")
    assert d["status"] == "error"
    err = d["outputs"][-1]
    assert err["ename"] == "ZeroDivisionError" and '  File "<cell>", line 4, in <module>' in err["traceback"]
    d = _cell(sas, 'import pandas as pd\nSAS.show(pd.DataFrame({"a": [1, 2]}), title="t")', "pytest-d")
    sas_out = next(o["data"]["application/vnd.s2.sas+json"] for o in d["outputs"] if o["output_type"] == "display_data")
    assert d["status"] == "ok" and "<table" in (sas_out["listing"] or "")


def test_endsubmit_is_refused(sas: McpSession) -> None:
    r = sas.call("notebook_sas_python", {"source": "print('endsubmit')", "run_id": "pytest-e"}, timeout=60)
    assert r.is_error and "endsubmit" in r.text


def test_restart_sas_session_clears_python_state(sas: McpSession) -> None:
    assert _cell(sas, "kept = 'yes'", "pytest-f")["status"] == "ok"
    sas.ok("sas_session_reset", {}, timeout=120)
    d = _cell(sas, "print(kept)", "pytest-g")
    assert d["status"] == "error" and d.get("python") == "initialized", d
    assert d["outputs"][-1]["ename"] == "NameError"
