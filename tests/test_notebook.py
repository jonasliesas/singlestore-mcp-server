"""Notebook kernel: Python, SQL and SAS cells, streaming, interrupt, restart and shutdown (slow)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import pytest
from support import DB, McpSession

pytestmark = [pytest.mark.cluster, pytest.mark.notebook]

NID = "zz-pytest-nb"


@dataclass
class Cell:
    outputs: list = field(default_factory=list)
    done: bool = False
    secs: float = 0.0
    partial_before_done: bool = False  # some output arrived in a poll before the one that said done
    start: dict | None = None

    @property
    def blob(self) -> str:
        return json.dumps(self.outputs, ensure_ascii=False)

    @property
    def errors(self) -> list:
        return [o for o in self.outputs if o.get("output_type") == "error" or o.get("type") == "error"]


def _wait_state(mcp: McpSession, nid: str, states: tuple[str, ...], timeout: float = 120) -> dict:
    d, secs = mcp.wait_until("notebook_kernel_state", {"notebook_id": nid},
                             lambda d: isinstance(d, dict) and d.get("state") in states, timeout, 0.5)
    return d


def _poll(mcp: McpSession, run_id: str, cell: Cell, timeout: float, t0: float) -> Cell:
    after = 0
    while time.perf_counter() - t0 < timeout:
        p = mcp.ok("notebook_poll", {"run_id": run_id, "after": after})
        new = p.get("outputs", [])
        cell.outputs += new
        after = p.get("next", after + len(new))
        if p.get("done"):
            cell.done = True
            break
        if new:
            cell.partial_before_done = True
        time.sleep(0.3)
    cell.secs = time.perf_counter() - t0
    return cell


@pytest.fixture(scope="module")
def kernel(cluster: McpSession):
    env = cluster.ok("notebook_environment")
    if env.get("state") != "ready":
        pytest.skip(f"notebook Python environment not ready: {env.get('state')} (missing {env.get('missing')})")
    cluster.ok("notebook_kernel_start", {"notebook_id": NID})
    st = _wait_state(cluster, NID, ("idle", "error", "dead"))
    assert st.get("state") == "idle", f"kernel didn't start: {st}"

    def run(cell_type: str, source: str, timeout: float = 240, **kw) -> Cell:
        t0 = time.perf_counter()
        d = cluster.ok("notebook_run", {"notebook_id": NID, "cell_type": cell_type, "source": source, **kw})
        assert d.get("run_id"), f"no run id: {d}"
        return _poll(cluster, d["run_id"], Cell(start=d), timeout, t0)

    yield run
    cluster.call("notebook_kernel_control", {"notebook_id": NID, "action": "shutdown"})


def _ok(cell: Cell, expect: str | None = None) -> None:
    assert cell.done, f"cell didn't finish in {cell.secs:.0f} s: {cell.blob[:600]}"
    assert not cell.errors, cell.blob[:1500]
    if expect is not None:
        assert expect in cell.blob, f"{expect!r} not in output: {cell.blob[:1000]}"


def test_python_basic(kernel) -> None:
    _ok(kernel("python", "import sys, pandas as pd; print('py', sys.version_info[:2], pd.__version__)"), "py")


def test_sql_to_dataframe(kernel) -> None:
    _ok(kernel("sql", "SELECT Make, COUNT(*) AS n FROM CARS GROUP BY Make ORDER BY n DESC LIMIT 5", database=DB, name="makes"), "Toyota")
    _ok(kernel("python", "print(type(makes).__name__, len(makes), list(makes.columns))"), "DataFrame 5 ['Make', 'n']")
    _ok(kernel("python", "print(type(df).__name__, len(df))"), "DataFrame 5")


def test_matplotlib_png(kernel) -> None:
    _ok(kernel("sql", "SELECT Make, COUNT(*) AS n FROM CARS GROUP BY Make ORDER BY n DESC LIMIT 5", database=DB, name="makes"))
    _ok(kernel("python", "import matplotlib.pyplot as plt; makes.plot.bar(x='Make', y='n'); plt.show()"), "image/png")


def test_sql_literal_percent(kernel) -> None:
    _ok(kernel("sql", "SELECT '100%' AS pct, 'a%%b' AS s", database=DB), "100%")


def test_sql_error_is_shown(kernel) -> None:
    cell = kernel("sql", "SELECT * FROM zz_no_table", database=DB)
    assert cell.done
    assert cell.errors, f"no error output: {cell.blob[:500]}"
    assert "1146" in cell.blob or "doesn't exist" in cell.blob, cell.blob[:800]


def test_streamed_output(kernel) -> None:
    cell = kernel("python", "import time\nfor i in range(4):\n    print('tick', i, flush=True); time.sleep(0.6)")
    _ok(cell, "tick 3")
    assert cell.partial_before_done, "output only arrived when the cell had finished (not streamed)"


def test_sas_packages_import(kernel) -> None:
    _ok(kernel("python", "import swat, sasctl, saspy; print('sas pkgs ok')"), "sas pkgs ok")


@pytest.mark.sas
def test_sas_cell_reads_singlestore(kernel) -> None:
    _ok(kernel("sas", "proc sql outobs=3; select Make, Model, MSRP from S2.CARS; quit;", database=DB, timeout=300), "MSRP")


@pytest.mark.sas
def test_sas_data_step(kernel) -> None:
    _ok(kernel("sas", "data work.zz; set S2.CARS(obs=5); ratio=MSRP/Invoice; run; proc print data=work.zz; run;", database=DB, timeout=300), "ratio")


def _interrupt(cluster: McpSession, run_id: str, timeout: float = 30) -> tuple[dict, float | None]:
    t0 = time.perf_counter()
    cluster.ok("notebook_kernel_control", {"notebook_id": NID, "action": "interrupt"})
    d, _ = cluster.wait_until("notebook_poll", {"run_id": run_id, "after": 0}, lambda x: isinstance(x, dict) and x.get("done"), timeout, 0.3)
    return d, (time.perf_counter() - t0) if isinstance(d, dict) and d.get("done") else None


def test_interrupt_python_cell(cluster: McpSession, kernel) -> None:
    d = cluster.ok("notebook_run", {"notebook_id": NID, "cell_type": "python", "source": "x = 0\nwhile True: x += 1"})
    time.sleep(1.5)
    p, secs = _interrupt(cluster, d["run_id"])
    assert secs is not None and secs < 10, f"busy Python cell not stopped ({secs})"
    assert "KeyboardInterrupt" in json.dumps(p), json.dumps(p)[:400]


def test_interrupt_sleeping_python_cell(cluster: McpSession, kernel) -> None:
    """A single long call: on Windows, time.sleep only noticed the interrupt when it returned (the kernel's interrupt
    didn't set CPython's SIGINT event); the notebook's kernel start-up code now does."""
    d = cluster.ok("notebook_run", {"notebook_id": NID, "cell_type": "python", "source": "import time\ntime.sleep(60)"})
    time.sleep(1.5)
    p, secs = _interrupt(cluster, d["run_id"])
    assert secs is not None and secs < 5, f"time.sleep cell took {secs} s to stop after interrupt (limit 5 s)"
    assert "KeyboardInterrupt" in json.dumps(p), json.dumps(p)[:400]


def test_restart_stops_a_cell_interrupt_cannot(cluster: McpSession, kernel) -> None:
    """A wait the interrupt can't reach on Windows (a lock / an Event): Restart kernel ends the cell instead of queueing
    behind it (the bridge gives the cell up a moment after the restart's interrupt)."""
    _ok(kernel("python", "zz_before = 1; print('set')"), "set")
    d = cluster.ok("notebook_run", {"notebook_id": NID, "cell_type": "python", "source": "import threading\nthreading.Event().wait(120)"})
    time.sleep(1.5)
    t0 = time.perf_counter()
    cluster.ok("notebook_kernel_control", {"notebook_id": NID, "action": "restart"})
    p, _ = cluster.wait_until("notebook_poll", {"run_id": d["run_id"], "after": 0}, lambda x: isinstance(x, dict) and x.get("done"), 30, 0.3)
    assert isinstance(p, dict) and p.get("done"), f"the cell didn't end after Restart: {p}"
    st = _wait_state(cluster, NID, ("idle", "error", "dead"))
    assert st.get("state") == "idle", st
    assert time.perf_counter() - t0 < 30
    _ok(kernel("python", "print('zz_before' in globals())"), "False")


def test_interrupt_sql_cell(cluster: McpSession, kernel) -> None:
    _ok(kernel("sql", "SELECT 1 AS warm", database=DB))  # connection open: the next query blocks in a socket read
    d = cluster.ok("notebook_run", {"notebook_id": NID, "cell_type": "sql", "source": "SELECT SLEEP(20) AS s", "database": DB})
    time.sleep(2)
    p, secs = _interrupt(cluster, d["run_id"])
    assert secs is not None and secs < 5, f"SQL cell took {secs} s to stop after interrupt (limit 5 s)"
    _ok(kernel("sql", "SELECT 42 AS answer", database=DB), "42")  # connection still usable


def test_restart_clears_state(cluster: McpSession, kernel) -> None:
    _ok(kernel("python", "zz_marker = 1; print('set')"), "set")
    cluster.ok("notebook_kernel_control", {"notebook_id": NID, "action": "restart"})
    st = _wait_state(cluster, NID, ("idle", "error", "dead"))
    assert st.get("state") == "idle", st
    _ok(kernel("python", "print('zz_marker' in globals())"), "False")


def test_shutdown(cluster: McpSession) -> None:
    nid = "zz-pytest-nb-shutdown"
    cluster.ok("notebook_kernel_start", {"notebook_id": nid})
    assert _wait_state(cluster, nid, ("idle", "error", "dead")).get("state") == "idle"
    assert cluster.ok("notebook_kernel_control", {"notebook_id": nid, "action": "shutdown"}).get("stopped")
    assert cluster.ok("notebook_kernel_state", {"notebook_id": nid}).get("state") == "stopped"
