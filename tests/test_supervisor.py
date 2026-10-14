"""The restart supervisor with a server that exits on start (offline: fake child processes, no cluster).

Regression: with a syntax or import error in the server code the supervisor started it again in a tight
loop (or got stuck), and tool calls waited. Now it backs off, logs the error and answers at once."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from singlestore_mcp import supervisor as sup_mod

# A "server" that fails to import, like one with a syntax error in server_impl.py.
_CRASH = ("import sys; sys.stderr.write('Traceback (most recent call last):\\n  File \"server_impl.py\", line 12\\n"
          "SyntaxError: invalid syntax\\n'); sys.exit(1)")
# A "server" that answers every request with an empty result.
_HEALTHY = ("import json, sys\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if 'id' in m and 'method' in m:\n"
            "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {}}), flush=True)\n")


def _popen(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)


@pytest.fixture
def sup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SINGLESTORE_MCP_NO_STANDBY", "1")
    monkeypatch.setattr(sup_mod, "_BACKOFF_S", (0.2, 0.4, 0.8))
    s = sup_mod.Supervisor()
    s.sent = []  # type: ignore[attr-defined]
    s.spawns = []  # type: ignore[attr-defined]
    s.code = _CRASH  # type: ignore[attr-defined]
    s.write_client = lambda msg: s.sent.append(msg)  # type: ignore[method-assign]

    def spawn(standby: bool = False) -> subprocess.Popen[bytes]:
        s.spawns.append(time.monotonic())  # type: ignore[attr-defined]
        return _popen(s.code)  # type: ignore[attr-defined]

    s._spawn = spawn  # type: ignore[method-assign]
    yield s
    s.stdin_closed = True
    if s.retry_timer is not None:
        s.retry_timer.cancel()
    s._stop_process(s.child)


def _wait(cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_backoff_sequence_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    s = sup_mod.Supervisor()
    delays = [s._backoff(0.5) for _ in range(8)]
    assert delays == [1, 2, 5, 10, 20, 30, 30, 30]
    assert s._backoff(sup_mod._HEALTHY_S + 1) == 1, "a server that ran fine for a while starts the count again"
    assert s._backoff(0.5) == 2


def test_crashing_server_is_restarted_with_back_off(sup, capsys: pytest.CaptureFixture[str]) -> None:
    sup.start_child()
    assert _wait(lambda: len(sup.spawns) >= 4), sup.spawns
    gaps = [b - a for a, b in zip(sup.spawns, sup.spawns[1:])]
    # Each start waits for the back-off (0.2, 0.4, 0.8 s), not a tight loop.
    for gap, want in zip(gaps, (0.2, 0.4, 0.8)):
        assert gap >= want * 0.9, gaps
    assert sup.failures >= 3
    err = capsys.readouterr().err
    assert "SyntaxError: invalid syntax" in err  # the server's own stderr is passed on to the log
    assert "server exited (exit code 1)" in err and "starting it again in 0.4 s (attempt 2)" in err, err


def test_tool_call_while_broken_gets_the_error_at_once(sup) -> None:
    sup.start_child()
    assert _wait(lambda: sup.broken is not None and not sup._child_alive())
    t = time.monotonic()
    sup.from_client({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "run_sql", "arguments": {}}})
    sup.from_client({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    assert time.monotonic() - t < 0.5
    by_id = {m["id"]: m for m in sup.sent if "id" in m}
    res = by_id[7]["result"]
    assert res["isError"] and "SyntaxError: invalid syntax" in res["content"][0]["text"], res
    assert "exit code 1" in res["content"][0]["text"] and "restart_server" in res["content"][0]["text"]
    assert "SyntaxError" in by_id[8]["error"]["message"]


def test_request_left_open_by_a_crash_is_answered(sup) -> None:
    sup.code = "import sys, time; sys.stdin.readline(); time.sleep(0.2); sys.stderr.write('ImportError: boom\\n'); sys.exit(3)"
    sup.start_child()
    sup.from_client({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_sql"}})
    assert _wait(lambda: any(m.get("id") == 1 for m in sup.sent)), sup.sent
    [m] = [m for m in sup.sent if m.get("id") == 1]
    assert m["result"]["isError"] and "ImportError: boom" in m["result"]["content"][0]["text"]


def test_restart_server_reports_the_error_then_recovers(sup) -> None:
    sup.init_request = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    sup.start_child()
    assert _wait(lambda: sup.broken is not None)
    sup._restart_call({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "restart_server"}})
    [r] = [m for m in sup.sent if m.get("id") == 5]
    assert r["result"]["isError"] and not r["result"]["structuredContent"]["restarted"]
    assert "SyntaxError: invalid syntax" in r["result"]["structuredContent"]["message"]
    # The code is fixed: restart_server works at once, and calls reach the server again.
    sup.code = _HEALTHY
    sup._restart_call({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "restart_server"}})
    [r] = [m for m in sup.sent if m.get("id") == 6]
    assert not r["result"]["isError"], r
    assert sup.broken is None
    sup.from_client({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    assert _wait(lambda: any(m.get("id") == 9 and "result" in m for m in sup.sent)), sup.sent
    spawns = len(sup.spawns)
    time.sleep(1.0)
    assert len(sup.spawns) == spawns, "no more restarts once the server runs"


def test_deliberate_restart_is_not_a_crash(sup) -> None:
    sup.code = _HEALTHY
    sup.start_child()
    msg = sup.restart()
    assert msg.startswith("Restarted"), msg
    time.sleep(0.5)
    assert len(sup.spawns) == 2 and sup.failures == 0 and sup.broken is None

