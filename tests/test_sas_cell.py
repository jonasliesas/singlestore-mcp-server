"""Notebook SAS cells in the Workspace's SAS session (offline): the wrapping (quiet preamble, ODS HTML5 file in
WORK, the guard against an open quote / step), reading the cell's part of the log back – from logs recorded on
Zeus 2026-10-30 (``tests/data/sas_cell_logs.json``: a DATA step, PROC PRINT, a missing table, a cell after the error,
an open quote, PROC SQL on the automatic S2 library with a page break inside the wrapped WORK path) – the notebook
outputs, and the tool against a fake Compute session (queue / Stop as Python in SAS cells)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import sas_cell, sas_python, sas_rest, sas_runs, sas_viya
from singlestore_mcp.apps import notebook as nb

LOGS = json.loads((Path(__file__).parent / "data" / "sas_cell_logs.json").read_text(encoding="utf-8"))


def test_prepare_and_wrap() -> None:
    assert sas_cell.prepare("\n\n  data a; x=1; run;  \n\n") == "  data a; x=1; run;"
    with pytest.raises(sas_cell.CellRefused):
        sas_cell.prepare(" \n\t\n")
    code = sas_cell.wrap("data a; x=1; run;")
    lines = code.split("\n")
    assert lines[0] == "options nosource nonotes;" and "options obs=max replace nosyntaxcheck; %let syscc=0;" in lines
    assert any(x.startswith("%put S2SAS_HTML=%sysfunc(pathname(work))/_s2sas.html") for x in lines)
    assert any(x.startswith('ods html5 (id=_s2sas) file="%sysfunc(pathname(work))/_s2sas.html"') for x in lines)
    # the cell runs with SOURCE on, after the marker; the guard ends an open quote / step before ODS is closed
    i = lines.index("data a; x=1; run;")
    assert lines[i - 2] == "%put S2SAS_BEGIN;" and lines[i - 1] == "options source notes;"
    assert lines[i + 1] == ";*';*\";*/;quit;run;options nosource nonotes;"
    assert lines[i + 2] == "ods html5 (id=_s2sas) close;"


def test_parse_keeps_the_cells_part_of_the_log() -> None:
    r = sas_cell.parse(LOGS["data_step"]["log"])
    text = "\n".join(x["line"] for x in r["log_lines"])
    assert text.lstrip().startswith("12   data work.zz_sascell; set sashelp.class; where age > 14; run;")
    assert "S2SAS" not in text and "options nosource" not in text and ";*';*" not in text
    assert "The data set WORK.ZZ_SASCELL has 5 observations" in text and not r["errors"]
    assert r["html_path"].endswith("/_s2sas.html") and "\n" not in r["html_path"]


def test_parse_errors_warnings_and_page_titles() -> None:
    r = sas_cell.parse(LOGS["error"]["log"])
    assert r["errors"] == ["ERROR: File WORK.DOES_NOT_EXIST_ZZ.DATA does not exist."]
    assert r["first_error"] == r["errors"][0]
    r = sas_cell.parse(LOGS["s2lib"]["log"])
    assert r["warnings"] == ["WARNING: Statement terminated early due to OUTOBS=3 option."] and not r["errors"]
    # The WORK path SAS wrapped at the line size, with a page break's title lines in between, is joined again.
    assert r["html_path"].endswith("/_s2sas.html") and " " not in r["html_path"]
    assert "The SAS System" not in "\n".join(x["line"] for x in r["log_lines"])
    # An open quote: the guard closed it and the log has the cell's line, not the guard.
    q = sas_cell.parse(LOGS["quote"]["log"])
    assert [x["line"] for x in q["log_lines"]][0].endswith("%put 'hello;") and not q["errors"]


def test_outputs_like_the_kernels_sas_cells() -> None:
    outs = sas_cell.outputs({"errors": ["ERROR: x"], "warnings": [], "first_error": "ERROR: x", "html": None,
                             "listing": "  Obs  Name", "log": "12  data a;\nERROR: x", "library": "S2 = …", "ok": False,
                             "state": "error", "stopped": False})
    d = outs[0]["data"][sas_cell.MIME]
    assert outs[0]["output_type"] == "display_data" and d["errors"] == 1 and d["first_error"] == "ERROR: x"
    assert d["listing"] == "<pre>  Obs  Name</pre>" and d["library"] == "S2 = …" and "python" not in d
    stopped = sas_cell.outputs({"errors": [], "warnings": [], "html": "<table></table>", "log": "", "stopped": True, "ok": False})
    assert stopped[0]["data"][sas_cell.MIME]["listing"] == "<table></table>" and stopped[-1]["name"] == "stderr"
    failed = sas_cell.outputs({"errors": [], "warnings": [], "log": "", "ok": False, "state": "failed", "stopped": False})
    assert failed[0]["data"][sas_cell.MIME]["errors"] == 1 and "failed" in failed[0]["data"][sas_cell.MIME]["first_error"]


def test_library_note() -> None:
    class C:
        auto_lib = {"assigned": True, "libref": "S2", "connection": "prod", "database": "SASDP"}

    assert sas_cell.library_note(C()) == "S2 = SingleStore prod / SASDP (the Workspace's automatic library, shared with the SQL Editor)"
    assert "this notebook's database MyDB isn't S2's" in sas_cell.library_note(C(), "MyDB")
    C.auto_lib = {"assigned": False, "libref": "S2", "note": "No active SingleStore connection."}
    assert sas_cell.library_note(C()) == "No S2 library: No active SingleStore connection."


def test_secrets_are_masked() -> None:
    r = sas_cell.parse([{"line": "S2SAS_BEGIN", "type": "normal"}, {"line": "ERROR: login failed password=hunter2", "type": "error"}])
    outs = sas_cell.outputs({**r, "errors": r["errors"], "log": "", "ok": False, "stopped": False})
    assert "hunter2" not in json.dumps(outs) and "hunter2" not in (r["first_error"] or "")


# ----------------------------------------------------------------------------- the tool (fake Compute session)


class FakeCompute:
    def __init__(self) -> None:
        self.conn, self.sid, self.lock = "Zeus", "S1", threading.RLock()
        self.current_run, self.warming = None, False
        self.auto_lib = {"assigned": True, "libref": "S2", "connection": "prod", "database": "SASDP"}
        self.steps: list[str] = []
        self.codes: list[str] = []
        self.gate: threading.Event | None = None

    def run(self, code: str, *, listing: bool = False, ctl: Any = None) -> dict[str, Any]:
        self.codes.append(code)
        if self.gate is not None:
            ctl.job = "S1/jobs/J1"
            self.gate.wait(5)
            if ctl.cancel.is_set():
                return {"state": "canceled", "log": [], "listing": "", "ms": 1}
        name = self.steps.pop(0)
        return {"state": LOGS[name]["state"], "log": LOGS[name]["log"], "listing": "", "ms": 1}

    def cancel_job(self, job: str) -> bool:
        if self.gate is not None:
            self.gate.set()
        return True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCompute:
    sas_runs._runs.clear()
    compute = FakeCompute()
    monkeypatch.setattr(sas_viya, "active_name", lambda: "Zeus")
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: {"name": name or "Zeus"})
    monkeypatch.setattr(sas_rest, "sessions", lambda conn=None: (compute, None))
    monkeypatch.setattr(sas_python, "_read_html", lambda c, path: "<html><body><table><tr><td>1</td></tr></table></body></html>")
    return compute


def test_tool_runs_sas_cells_in_the_workspace_session(fake: FakeCompute) -> None:
    fake.steps = ["data_step", "print"]
    d = nb.notebook_sas_cell("data work.zz_sascell; set sashelp.class; where age > 14; run;", notebook_id="n-1",
                             run_id="sc-test-1", database="MyDB").structured_content
    assert d["done"] and d["status"] == "ok" and d["connection"] == "Zeus" and d["kind"] == "sas"
    out = d["outputs"][0]["data"][sas_cell.MIME]
    assert out["errors"] == 0 and "this notebook's database MyDB" in out["library"]
    assert fake.codes[0].startswith("options nosource nonotes;")
    d = nb.notebook_sas_cell("proc print data=work.zz_sascell; run;", run_id="sc-test-2").structured_content
    assert d["outputs"][0]["data"][sas_cell.MIME]["listing"].startswith("<html>")
    run = sas_runs.get("sc-test-2")
    assert run.engine == "compute" and run.source["kind"] == "sas"  # the SQL Editor's queue / Stop


def test_tool_reports_sas_errors(fake: FakeCompute) -> None:
    fake.steps = ["error"]
    d = nb.notebook_sas_cell("data _null_; set work.does_not_exist_zz; run;", run_id="sc-test-3").structured_content
    assert d["status"] == "error" and "does not exist" in d["summary"]
    assert d["outputs"][0]["data"][sas_cell.MIME]["errors"] == 1


def test_tool_refuses_an_empty_cell(fake: FakeCompute) -> None:
    with pytest.raises(sas_cell.CellRefused):
        nb.notebook_sas_cell("  \n", run_id="sc-test-4")
    assert "sc-test-4" not in sas_runs._runs


def test_stop_cancels_a_sas_cell(fake: FakeCompute) -> None:
    fake.gate = threading.Event()
    d = nb.notebook_sas_cell("data _null_; call sleep(60, 1); run;", run_id="sc-test-5", wait_ms=200).structured_content
    assert d["running"] and d["kind"] == "sas" and d["python_started"] is True  # no "Starting Python" for SAS cells
    assert sas_runs.stop("sc-test-5")["stopped"]
    d = nb.notebook_sas_python_status("sc-test-5", wait_ms=5000).structured_content
    assert d["status"] == "aborted" and any("Stopped" in str(o.get("text")) for o in d["outputs"])
