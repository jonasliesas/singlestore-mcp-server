"""Python in SAS notebook cells, offline: wrapping a cell in PROC PYTHON, refusing ``endsubmit``, indentation, and
turning real SAS logs into notebook outputs. The logs in ``data/proc_python_logs.json`` were recorded on Zeus
(2026-10-30) by running small read-only PROC PYTHON steps through ``sas_python.run`` (WORK tables only)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import sas_python, sas_rest, sas_runs, sas_sql, sas_viya
from singlestore_mcp.apps import notebook as nb

LOGS = json.loads((Path(__file__).parent / "data" / "proc_python_logs.json").read_text(encoding="utf-8"))


def _parse(name: str) -> dict[str, Any]:
    step = LOGS[name]
    code, offset = sas_python.prepare(step["code"])
    return sas_python.parse(step["log"], code, offset)


# ----------------------------------------------------------------------------- the cell's code


def test_wrap_puts_the_cell_into_proc_python_and_restores_options() -> None:
    code, offset = sas_python.prepare('print("hi")\n')
    assert (code, offset) == ('print("hi")', 0)
    sas = sas_python.wrap(code)
    lines = sas.split("\n")
    assert lines[0] == sas_sql.SESSION_RESET  # a failed step before doesn't make this one fail
    i = lines.index("proc python;")
    assert lines[i:i + 5] == ["proc python;", "submit;", 'print("hi")', "endsubmit;", "run;"]
    assert "options ls=max ps=max;" in lines[:i]
    assert lines[-1] == "options ls=&_s2pyls ps=&_s2pyps;"
    assert any(x.startswith("ods html5 (id=_s2py) file=") for x in lines[:i]) and "ods html5 (id=_s2py) close;" in lines


@pytest.mark.parametrize("source", [
    'print("endsubmit;")',
    "x = 1  # EndSubmit here",
    "def f():\n    return 'ENDSUBMIT'",
])
def test_endsubmit_is_refused(source: str) -> None:
    with pytest.raises(sas_python.CellRefused, match="endsubmit"):
        sas_python.prepare(source)


def test_endsubmit_refusal_names_the_cell_line() -> None:
    with pytest.raises(sas_python.CellRefused, match=r"^Line 4 contains"):
        sas_python.prepare("\n\na = 1\nprint('endsubmit')")


def test_empty_and_overlong_cells_are_refused() -> None:
    with pytest.raises(sas_python.CellRefused, match="empty"):
        sas_python.prepare("  \n\t\n")
    with pytest.raises(sas_python.CellRefused, match="characters long"):
        sas_python.prepare("x = '" + "a" * 40_000 + "'")


def test_indentation_is_removed_and_line_numbers_kept() -> None:
    code, offset = sas_python.prepare("\n\n    total = 0\n    for i in range(3):\n        total += i\n    print(total)\n\n")
    assert offset == 2
    assert code == "total = 0\nfor i in range(3):\n    total += i\nprint(total)"
    # Tabs mixed with spaces: expanded first, so dedent can line them up.
    code, _ = sas_python.prepare("\tif x:\n\t    y = 1")
    assert code == "if x:\n    y = 1"
    # Windows line ends and trailing blanks don't reach SAS.
    assert sas_python.prepare("a = 1   \r\nb = 2\r\n")[0] == "a = 1\nb = 2"


# ----------------------------------------------------------------------------- recorded logs


def test_first_cell_starts_python_and_hides_the_banner() -> None:
    r = _parse("first")
    assert r["python"] == "initialized"
    assert r["stdout"] == "python 3.12.12\n"
    assert r["error"] is None and r["sas_errors"] == [] and r["html_path"].endswith("/_s2py.html")


def test_state_resumes_and_echo_and_notes_are_stripped() -> None:
    r = _parse("resume")
    assert r["python"] == "resumed" and r["stdout"] == "counter = 2\n"
    assert ">>>" not in r["stdout"] and "NOTE" not in r["stdout"] and "proc python" not in r["stdout"].lower()


def test_traceback_is_cleaned() -> None:
    r = _parse("error")
    assert r["stdout"] == "before\n"
    err = r["error"]
    assert (err["ename"], err["evalue"]) == ("ZeroDivisionError", "division by zero")
    assert err["traceback"] == [
        "Traceback (most recent call last):",
        '  File "<cell>", line 5, in <module>',
        "    f(0)",
        '  File "<cell>", line 2, in f',
        "    return 1 / x",
        "ZeroDivisionError: division by zero",
    ]
    assert not any("<stdin>" in x or "<string>" in x for x in err["traceback"])


def test_syntax_and_indentation_errors() -> None:
    err = _parse("syntax")["error"]
    assert err["ename"] == "SyntaxError" and "never closed" in err["evalue"]
    assert '  File "<cell>", line 1' in err["traceback"] and any("^" in x for x in err["traceback"])
    assert _parse("indented")["stdout"] == "total 3\n"  # the same code ran fine once dedented


def test_long_lines_are_joined_again() -> None:
    assert _parse("long")["stdout"] == "x" * 300 + "\n"


def test_sas_object_round_trip_and_submit() -> None:
    r = _parse("data")
    assert r["stdout"] == "(19, 5)\n19\n" and r["error"] is None
    r = _parse("submit")
    assert r["stdout"] == "printed\n" and r["sas_errors"] == []
    assert LOGS["submit"]["html"] and LOGS["show"]["html"]  # ODS output came back as HTML


def test_sas_errors_from_submitted_code() -> None:
    r = _parse("sas_error")
    assert r["stdout"] == "after\n" and r["error"] is None
    assert r["sas_errors"] == ["ERROR: File WORK.NO_SUCH_TABLE.DATA does not exist."]


def test_python_warnings_point_at_the_cell() -> None:
    assert _parse("warn")["stdout"] == "<cell>:2: UserWarning: careful\n"


def test_outputs_are_nbformat() -> None:
    res = {**_parse("error"), "html": None, "listing": None, "log": "log text", "stopped": False}
    outs = sas_python.outputs(res)
    assert [o["output_type"] for o in outs] == ["stream", "display_data", "error"]
    sas = outs[1]["data"][sas_python.MIME]
    assert sas["python"] is True and sas["errors"] == 0 and sas["log"] == "log text"
    assert outs[2]["ename"] == "ZeroDivisionError"
    listing = sas_python.outputs({"stdout": "", "listing": "a < b", "log": ""})[0]["data"][sas_python.MIME]["listing"]
    assert listing == "<pre>a &lt; b</pre>"


def test_ods_html_detection_and_listing_titles() -> None:
    empty = "<html><head><style>pre { x }</style></head><body class='c'>\n</body></html>"
    assert not sas_python.has_output(empty)
    assert sas_python.has_output("<html><body><div><table><tr><td>1</td></tr></table></div></body></html>")
    assert sas_python.has_output("<html><body><svg width='1'></svg></body></html>")
    text = "\n   The SAS System      Friday, October 30, 2026 09:41:00 PM   1\n\n  Obs  Name\n   1   Alfred\n"
    assert sas_python.listing_text(text) == "  Obs  Name\n   1   Alfred"


def test_secrets_are_masked_in_the_output() -> None:
    r = sas_python.parse([{"line": "connected with password=hunter2", "type": "normal"}])
    assert "hunter2" not in r["stdout"]


# ----------------------------------------------------------------------------- the tool (fake Compute session)


class FakeCompute:
    """A Compute session that answers runs with recorded logs (no Viya)."""

    def __init__(self, name: str = "Zeus") -> None:
        self.conn, self.sid, self.lock = name, "S1", threading.RLock()
        self.current_run, self.warming = None, False
        self.steps: list[str] = []
        self.gate: threading.Event | None = None

    def run(self, code: str, *, listing: bool = False, ctl: Any = None) -> dict[str, Any]:
        if self.gate is not None:
            ctl.job = "S1/jobs/J1"
            self.gate.wait(5)
            if ctl.cancel.is_set():
                return {"state": "canceled", "log": [], "listing": "", "ms": 1}
        name = self.steps.pop(0)
        return {"state": LOGS[name]["state"], "log": LOGS[name]["log"], "listing": LOGS[name]["listing"], "ms": 1}

    def cancel_job(self, job: str) -> bool:
        if self.gate is not None:
            self.gate.set()
        return True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCompute:
    sas_runs._runs.clear()
    sas_python._started.clear()
    compute = FakeCompute()
    monkeypatch.setattr(sas_viya, "active_name", lambda: "Zeus")
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: {"name": name or "Zeus"})
    monkeypatch.setattr(sas_rest, "sessions", lambda conn=None: (compute, None))
    monkeypatch.setattr(sas_python, "_read_html", lambda c, path: "<html><body><table></table></body></html>")
    return compute


def test_tool_runs_cells_and_tracks_python_start(fake: FakeCompute) -> None:
    fake.steps = ["first", "resume"]
    assert not sas_python.python_started(fake)
    d = nb.notebook_sas_python("import sys\nprint(sys.version)", notebook_id="n-1", run_id="py-test-1").structured_content
    assert d["done"] and d["status"] == "ok" and d["connection"] == "Zeus" and d["python"] == "initialized"
    assert d["outputs"][0] == {"output_type": "stream", "name": "stdout", "text": "python 3.12.12\n"}
    assert sas_python.python_started(fake)  # the next cell doesn't say "Starting Python in SAS"
    d = nb.notebook_sas_python("counter += 1", run_id="py-test-2").structured_content
    assert d["status"] == "ok" and d["outputs"][0]["text"] == "counter = 2\n"


def test_tool_reports_python_errors(fake: FakeCompute) -> None:
    fake.steps = ["error"]
    d = nb.notebook_sas_python("def f(x):\n    return 1 / x\n\nprint('before')\nf(0)", run_id="py-test-3").structured_content
    assert d["status"] == "error" and d["outputs"][-1]["ename"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in d["summary"]


def test_tool_refuses_endsubmit_before_running(fake: FakeCompute) -> None:
    with pytest.raises(sas_python.CellRefused):
        nb.notebook_sas_python("s = 'endsubmit'", run_id="py-test-4")
    assert "py-test-4" not in sas_runs._runs


def test_stop_cancels_the_compute_job(fake: FakeCompute) -> None:
    fake.gate = threading.Event()
    d = nb.notebook_sas_python("import time\ntime.sleep(60)", run_id="py-test-5", wait_ms=200).structured_content
    assert d["running"] and d["python_started"] is False and d["state"] == "running"
    stopped = sas_runs.stop("py-test-5")
    assert stopped["stopped"] and stopped["cancel_sent"]
    d = nb.notebook_sas_python_status("py-test-5", wait_ms=5000).structured_content
    assert d["status"] == "aborted" and any("Stopped" in str(o.get("text")) for o in d["outputs"])


def test_server_mode_opens_the_notebook_without_kernels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server mode (kernels off): the Notebook still opens, for its Python in SAS cells; no kernel environment."""
    monkeypatch.setenv("SINGLESTORE_MCP_SERVER_MODE", "1")
    monkeypatch.delenv("SINGLESTORE_MCP_NOTEBOOKS", raising=False)
    monkeypatch.setattr(nb.notebook_kernel, "environment_status", lambda: pytest.fail("no kernel environment in server mode"))
    monkeypatch.setattr(nb, "with_browser_link", lambda summary, data, *a, **k: summary)
    d = nb.notebook().structured_content
    assert d["features"]["notebooks"] is False and d["environment"]["state"] == "off"
    assert "only Python in SAS and text cells run" in d["summary"]
