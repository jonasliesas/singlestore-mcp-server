"""Restartable stdio front for the SingleStore MCP server.

Claude Code talks to an MCP server over the process's stdin/stdout, so a
server that exits takes the connection with it. To make ``restart_server``
possible, the process Claude starts is this small relay: it runs the real
server as a child process and copies JSON-RPC lines between the two.

On restart it starts a fresh child, replays the client's ``initialize``
handshake to it, answers any requests the old child left open with an error,
and tells the client that the tool, prompt and resource lists changed. The
child is started with SINGLESTORE_MCP_SUPERVISED=1 so it runs the server
directly.

Most of a fresh child's start-up is importing the MCP SDK and the database
driver (~2-3 s), which don't change when our code does. So a while after each
start the supervisor keeps a *standby* process that has imported only those
third-party packages and waits for "go"; a restart hands the server to it, and
it then imports this package fresh from disk (code and app changes load as
before) in well under a second. A standby that finds the installed packages
changed since it loaded them declines, and a fresh child is started instead.
The standby also opens the server's database connections ahead (``preconnect``)
and hands them over, so the first call after a restart doesn't wait for one.
SINGLESTORE_MCP_NO_STANDBY=1 turns the standby off.

The supervisor also holds a browser-view slot (``browser_address``) for its
whole life and hands its number to every server it starts, so they all listen
on the same browser address and open browser tabs reconnect after a restart.

A server that exits on its own (say this package has a syntax or import error)
is started again after a back-off (1, 2, 5, 10, 20, then every 30 s; back to
1 s once a server has run for a minute), not in a tight loop. The error is
logged to stderr (the MCP log), requests from the client are answered at once
with it instead of waiting, and ``restart_server`` (which the supervisor
answers itself) tries again right away.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any

RESTART_TOOL = "restart_server"
# Requests of MCP protocol revision 2026-07-28+ carry this key in params._meta
# and need no initialize handshake; a server that saw `initialize` first only
# speaks the older handshake protocol.
_MODERN_META_KEY = "io.modelcontextprotocol/protocolVersion"
_INIT_ID = "s2-supervisor-init"

# Third-party packages the server imports (the slow part of its start). Never
# this package itself: the standby must load our code only after "go".
_PRELOAD = ("mcp.server.mcpserver", "mcp.server.apps", "mcp.server.stdio", "mcp.types", "mcp.client",
            "singlestoredb", "singlestoredb.mysql.connection", "pydantic", "anyio", "rich.console", "cryptography.fernet")
_STANDBY_ACK = b'{"s2-standby":"go"}'
# Run with `python -c`. Answers "go" with _STANDBY_ACK (or "stale") on stdout, then
# runs the server exactly like `python -m singlestore_mcp.server`.
_STANDBY_CODE = r"""
import os, sys
def stamp():
    return [os.stat(p).st_mtime_ns for p in sys.path if p.endswith("site-packages") and os.path.isdir(p)]
for name in PRELOAD:
    try:
        __import__(name)
    except Exception:
        pass
try:  # choosing the OS credential store backend takes ~0.3 s on its own
    import keyring
    keyring.get_keyring()
except Exception:
    pass
pre = None
try:  # database connections for the server this becomes (see singlestore_mcp.preconnect)
    from singlestore_mcp import preconnect
    pre = preconnect.start()
except Exception:
    pass
before = stamp()
line = sys.stdin.buffer.readline()
if line.strip() != b"go":
    sys.exit(0)
if stamp() != before:  # packages were installed or removed meanwhile: decline (a fresh child starts)
    sys.stdout.buffer.write(b'{"s2-standby":"stale"}\n'); sys.stdout.buffer.flush()
    sys.exit(0)
sys.stdout.buffer.write(ACK + b"\n"); sys.stdout.buffer.flush()
if pre is not None:
    try:
        sys.modules[preconnect.HOLDER_MODULE] = pre.handover()
    except Exception:
        pass
# This package loads fresh from disk (code changed since the standby started): forget what preconnect imported.
for name in [m for m in sys.modules if m == "singlestore_mcp" or m.startswith("singlestore_mcp.")]:
    del sys.modules[name]
import runpy
runpy.run_module("singlestore_mcp.server", run_name="__main__", alter_sys=True)
""".replace("PRELOAD", repr(_PRELOAD)).replace("ACK", repr(_STANDBY_ACK))
_STANDBY_DELAY_S = float(os.environ.get("SINGLESTORE_MCP_STANDBY_DELAY", "5"))
# Waits before starting a server again after it exited on its own; the count goes
# back to the start once a server has run for _HEALTHY_S.
_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
_HEALTHY_S = 60.0
_STDERR_TAIL = 40  # lines of a server's stderr kept for the error message


def _log(text: str) -> None:
    try:
        print(f"singlestore-mcp supervisor: {text}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _forward_stderr(proc: subprocess.Popen[bytes]) -> None:
    """Copy a server's stderr to ours (the MCP log), keeping its last lines for error messages."""
    tail: deque[str] = deque(maxlen=_STDERR_TAIL)
    proc._s2_tail = tail  # type: ignore[attr-defined]
    if proc.stderr is None:
        return

    def pump() -> None:
        for raw in proc.stderr:  # type: ignore[union-attr]
            line = raw.decode("utf-8", errors="replace")
            tail.append(line.rstrip("\r\n"))
            try:
                sys.stderr.write(line)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass

    t = threading.Thread(target=pump, daemon=True, name="s2-supervisor-stderr")
    t.start()
    proc._s2_stderr_thread = t  # type: ignore[attr-defined]


def _error_text(proc: subprocess.Popen[bytes] | None, max_lines: int = 8) -> str:
    """The end of a server's stderr: a traceback's last lines (the error itself is the last one)."""
    if proc is None:
        return ""
    t = getattr(proc, "_s2_stderr_thread", None)
    if t is not None:
        t.join(1)  # the rest of the traceback may still be in the pipe
    lines = [x for x in getattr(proc, "_s2_tail", ()) if x.strip()]
    start = max((i for i, x in enumerate(lines) if x.startswith("Traceback")), default=None)
    if start is not None:
        lines = lines[start + 1:]
    return "\n".join(lines[-max_lines:])


class Supervisor:
    def __init__(self) -> None:
        self.out_lock = threading.Lock()
        self.child_lock = threading.RLock()
        self.child: subprocess.Popen[bytes] | None = None
        self.generation = 0
        self.init_request: dict[str, Any] | None = None
        self.initialized_note: dict[str, Any] | None = None
        self.pending: dict[Any, str] = {}  # id -> method of client requests the child hasn't answered
        self.replay_done = threading.Event()
        self.restart_lock = threading.Lock()  # one restart at a time (a crash retry and restart_server)
        self.retired: set[int] = set()  # generations stopped on purpose: their exit isn't a crash
        self.modern = False  # the client sends 2026-07-28 envelopes: don't replay initialize
        self.child_started = time.monotonic()
        self.failures = 0  # exits on its own in a row (reset once a server has run _HEALTHY_S)
        self.broken: str | None = None  # why the server is down, until a new one answers
        self.retry_timer: threading.Timer | None = None

    # ------------------------------------------------------------ plumbing
    def write_client(self, msg: dict[str, Any]) -> None:
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self.out_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def write_child(self, msg: dict[str, Any]) -> None:
        with self.child_lock:
            child = self.child
        if child is None or child.stdin is None:
            raise OSError("server not running")
        child.stdin.write((json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8"))
        child.stdin.flush()

    @staticmethod
    def _spawn(standby: bool = False) -> subprocess.Popen[bytes]:
        env = dict(os.environ, SINGLESTORE_MCP_SUPERVISED="1")
        args = ["-c", _STANDBY_CODE] if standby else ["-m", "singlestore_mcp.server"]
        exe = sys.executable
        base = getattr(sys, "_base_executable", None)
        if sys.platform == "win32" and base and os.path.normcase(base) != os.path.normcase(exe):
            # A venv on Windows: its python.exe is only a launcher that starts the base interpreter as a second
            # process. Start the base interpreter directly, as multiprocessing does (bpo-35797): one process per
            # server instead of two and ~60 ms sooner; the child's sys.executable is still the venv's.
            env["__PYVENV_LAUNCHER__"] = exe
            exe = base
        return subprocess.Popen(
            [exe, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def start_child(self, proc: subprocess.Popen[bytes] | None = None) -> bool:
        """Start the server: ``proc`` (a standby that took "go"), else a fresh process. True for the standby."""
        from_standby = proc is not None
        with self.child_lock:
            self.generation += 1
            if proc is None:
                proc = self._spawn()
                _forward_stderr(proc)
            self.child = proc
            self.child_started = time.monotonic()
            threading.Thread(target=self.pump_child, args=(self.child, self.generation), daemon=True).start()
        self._schedule_standby()
        return from_standby

    # ------------------------------------------------------------ standby
    standby: subprocess.Popen[bytes] | None = None
    _standby_timer: threading.Timer | None = None

    def _standby_enabled(self) -> bool:
        return not os.environ.get("SINGLESTORE_MCP_NO_STANDBY") and not self.stdin_closed

    def _schedule_standby(self) -> None:
        """Prepare a standby a while after a start (so it doesn't slow the start down)."""
        if not self._standby_enabled():
            return
        with self.child_lock:
            if self._standby_timer is not None:
                self._standby_timer.cancel()
            self._standby_timer = threading.Timer(_STANDBY_DELAY_S, self._start_standby)
            self._standby_timer.daemon = True
            self._standby_timer.start()

    def _start_standby(self) -> None:
        with self.child_lock:
            if not self._standby_enabled() or (self.standby is not None and self.standby.poll() is None):
                return
            try:
                self.standby = self._spawn(standby=True)
                _forward_stderr(self.standby)
            except OSError:
                self.standby = None

    def _take_standby(self) -> subprocess.Popen[bytes] | None:
        """Hand the server to the standby: "go", then wait for its acknowledgement (it may still be loading)."""
        with self.child_lock:
            sb, self.standby = self.standby, None
        if sb is None or sb.poll() is not None or sb.stdin is None or sb.stdout is None:
            return None
        ack: list[bytes] = []

        def read_ack() -> None:
            try:
                sb.stdin.write(b"go\n")  # type: ignore[union-attr]
                sb.stdin.flush()  # type: ignore[union-attr]
                ack.append(sb.stdout.readline())  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=read_ack, daemon=True)
        reader.start()
        reader.join(30)
        if ack and ack[0].strip() == _STANDBY_ACK:
            return sb
        self._stop_process(sb)
        return None

    @staticmethod
    def _stop_process(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin and proc.stdin.close()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def pump_child(self, child: subprocess.Popen[bytes], generation: int) -> None:
        assert child.stdout is not None
        for raw in child.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == _INIT_ID and "method" not in msg:
                self.replay_done.set()  # our replayed initialize: not for the client
                continue
            if "method" not in msg and "id" in msg:
                self.pending.pop(msg["id"], None)
            self._healthy(generation)
            self.write_client(msg)
        # Child ended. If it wasn't stopped on purpose, start a new one after a back-off.
        with self.child_lock:
            current = generation == self.generation and generation not in self.retired
        if current and not self.stdin_closed:
            self._crashed(child, generation)

    # ------------------------------------------------------------ a server that exits on its own
    def _healthy(self, generation: int) -> None:
        """The server answered: it runs (again)."""
        if self.broken is None:
            return
        with self.child_lock:
            if generation != self.generation or self.broken is None:
                return
            self.broken = None
        _log("the server is running again.")

    def _backoff(self, uptime: float) -> float:
        """Seconds to wait before the next start, after a server ran ``uptime`` s and exited on its own."""
        with self.child_lock:
            if uptime >= _HEALTHY_S:
                self.failures = 0
            self.failures += 1
            return _BACKOFF_S[min(self.failures, len(_BACKOFF_S)) - 1]

    def _crashed(self, child: subprocess.Popen[bytes], generation: int) -> None:
        """The current server exited on its own: answer what it left open, log why, start it again later."""
        try:
            code: int | None = child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        uptime = time.monotonic() - self.child_started
        delay = self._backoff(uptime)
        err = _error_text(child)
        last = err.splitlines()[-1] if err else ""
        text = (f"The SingleStore MCP server exited (exit code {code}) {uptime:.1f} s after it started"
                + (f":\n{err}\n" if err else ". ")
                + f"The supervisor starts it again in {delay:g} s (attempt {self.failures}); "
                "after fixing the error, restart_server starts it at once.")
        with self.child_lock:
            if generation != self.generation or generation in self.retired or self.stdin_closed:
                return  # restart_server got there first, or the client went away
            self.broken = text
            orphaned, self.pending = self.pending, {}
            if self.retry_timer is not None:
                self.retry_timer.cancel()
            timer = self.retry_timer = threading.Timer(delay, self._retry, args=(generation,))
            timer.daemon = True
        _log(f"server exited (exit code {code}) after {uptime:.1f} s{': ' + last if last else ''}; "
             f"starting it again in {delay:g} s (attempt {self.failures}).")
        for rid, method in orphaned.items():
            self._reply_error(rid, method, text)
        timer.start()

    def _retry(self, generation: int) -> None:
        with self.child_lock:
            if generation != self.generation or generation in self.retired or self.stdin_closed:
                return  # restarted meanwhile (restart_server), or the client went away
        try:
            self.restart()
        except Exception as exc:  # noqa: BLE001 - the new server's own exit schedules the next try
            _log(f"start failed: {str(exc).splitlines()[0] if str(exc) else repr(exc)}")

    def _reply_error(self, rid: Any, method: str | None, text: str) -> None:
        """Answer a client request the server can't: a tool call gets a readable tool error."""
        if method == "tools/call":
            self.write_client({"jsonrpc": "2.0", "id": rid,
                               "result": {"content": [{"type": "text", "text": text}], "isError": True,
                                          "resultType": "complete"}})
        else:
            self.write_client({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": text}})

    def _child_alive(self) -> bool:
        child = self.child
        return child is not None and child.poll() is None

    @staticmethod
    def _start_failure(child: subprocess.Popen[bytes] | None) -> str:
        code = child.poll() if child is not None else None
        err = _error_text(child)
        return (f"The new server exited during start-up (exit code {code})" + (f":\n{err}" if err else ".")
                + "\nFix the error and call restart_server again (the supervisor also retries on its own).")

    # ------------------------------------------------------------ restart
    def restart(self) -> str:
        """Replace the server. Raises RuntimeError when the new one exits or doesn't answer during start-up."""
        with self.restart_lock:
            return self._restart()

    def _restart(self) -> str:
        with self.child_lock:
            if self.retry_timer is not None:
                self.retry_timer.cancel()
                self.retry_timer = None
            old = self.child
            self.retired.add(self.generation)
            orphaned, self.pending = self.pending, {}
        started = time.time()
        # The standby loads our code and opens its database connections while the old server stops.
        standby = self._take_standby()
        self._stop_process(old)
        for rid, method in orphaned.items():
            self._reply_error(rid, method, "The SingleStore MCP server restarted; try again.")
        from_standby = self.start_child(standby)
        child = self.child
        if self.init_request and not self.modern:
            self.replay_done.clear()
            try:
                self.write_child({**self.init_request, "id": _INIT_ID})
            except OSError:
                raise RuntimeError(self._start_failure(child)) from None
            give_up = time.monotonic() + 60
            while not self.replay_done.wait(0.05):
                if child is not None and child.poll() is not None:
                    raise RuntimeError(self._start_failure(child))
                if time.monotonic() > give_up:
                    raise RuntimeError("The new server didn't answer the initialize handshake within 60 s.")
            with self.child_lock:
                generation = self.generation
            self._healthy(generation)
            if self.initialized_note:
                self.write_child(self.initialized_note)
        for what in ("tools", "prompts", "resources"):
            self.write_client({"jsonrpc": "2.0", "method": f"notifications/{what}/list_changed"})
        how = " (from the standby)" if from_standby else ""
        return f"Restarted the SingleStore MCP server in {time.time() - started:.1f} s{how}; code and app changes are loaded."

    # ------------------------------------------------------------ main loop
    stdin_closed = False

    @staticmethod
    def _claim_browser_slot() -> None:
        """Hold a browser-view slot for this supervisor's life, so every server it starts (restart_server, the
        standby, a crash restart) listens on the same browser address and open tabs reconnect (browser_address)."""
        try:
            from . import browser_address

            slot = browser_address.claim_slot()
        except Exception as exc:  # noqa: BLE001 - browser links then just get a new address each start
            _log(f"no browser-view slot: {exc!r}")
            return
        if slot is not None:
            os.environ[browser_address.ENV_SLOT] = str(slot)

    def run(self) -> None:
        self._claim_browser_slot()
        self.start_child()
        for raw in sys.stdin.buffer:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.from_client(msg)
        # Client went away: stop the server too.
        self.stdin_closed = True
        with self.child_lock:
            child, standby, self.standby = self.child, self.standby, None
            if self._standby_timer is not None:
                self._standby_timer.cancel()
            if self.retry_timer is not None:
                self.retry_timer.cancel()
        self._stop_process(standby)  # it reads EOF and exits
        self._stop_process(child)

    def from_client(self, msg: dict[str, Any]) -> None:
        """One JSON-RPC message from the client: pass it to the server, or answer it here."""
        method = msg.get("method")
        meta = (msg.get("params") or {}).get("_meta") if isinstance(msg.get("params"), dict) else None
        if isinstance(meta, dict) and _MODERN_META_KEY in meta:
            self.modern = True
        if method == "initialize":
            self.init_request = msg
        elif method == "notifications/initialized":
            self.initialized_note = msg
        if method == "tools/call" and (msg.get("params") or {}).get("name") == RESTART_TOOL:
            threading.Thread(target=self._restart_call, args=(msg,), daemon=True).start()
            return
        request = bool(method) and "id" in msg
        if request:
            with self.child_lock:
                broken = self.broken if not self._child_alive() else None
                if not broken:
                    self.pending[msg["id"]] = method
            if broken:  # down (exited, waiting to start again): say why at once
                self._reply_error(msg["id"], method, broken)
                return
        try:
            self.write_child(msg)
        except OSError:
            if request:
                self.pending.pop(msg["id"], None)
                self._reply_error(msg["id"], method, self.broken or "The SingleStore MCP server is restarting; try again.")

    def _restart_call(self, msg: dict[str, Any]) -> None:
        try:
            text, error = self.restart(), False
        except Exception as exc:  # noqa: BLE001 - report to the client
            text, error = f"Restart failed: {exc}", True
        # Same shape as server.restart_server's result (its output schema).
        structured = {"restarted": not error, "message": text}
        self.write_client({"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"content": [{"type": "text", "text": json.dumps(structured)}],
                                      "structuredContent": structured, "isError": error,
                                      # Required by MCP protocol revision 2026-07-28 and later.
                                      "resultType": "complete"}})


def run() -> None:
    Supervisor().run()
