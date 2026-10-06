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
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

RESTART_TOOL = "restart_server"
# Requests of MCP protocol revision 2026-07-28+ carry this key in params._meta
# and need no initialize handshake; a server that saw `initialize` first only
# speaks the older handshake protocol.
_MODERN_META_KEY = "io.modelcontextprotocol/protocolVersion"
_INIT_ID = "s2-supervisor-init"


class Supervisor:
    def __init__(self) -> None:
        self.out_lock = threading.Lock()
        self.child_lock = threading.RLock()
        self.child: subprocess.Popen[bytes] | None = None
        self.generation = 0
        self.init_request: dict[str, Any] | None = None
        self.initialized_note: dict[str, Any] | None = None
        self.pending: set[Any] = set()  # ids of client requests the child hasn't answered
        self.replay_done = threading.Event()
        self.restarting = False
        self.modern = False  # the client sends 2026-07-28 envelopes: don't replay initialize

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

    def start_child(self) -> None:
        env = dict(os.environ, SINGLESTORE_MCP_SUPERVISED="1")
        with self.child_lock:
            self.generation += 1
            self.child = subprocess.Popen(
                [sys.executable, "-m", "singlestore_mcp.server"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            threading.Thread(target=self.pump_child, args=(self.child, self.generation), daemon=True).start()

    def pump_child(self, child: subprocess.Popen[bytes], generation: int) -> None:
        assert child.stdout is not None
        for raw in child.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") == _INIT_ID and "method" not in msg:
                self.replay_done.set()  # our replayed initialize: not for the client
                continue
            if "method" not in msg and "id" in msg:
                self.pending.discard(msg["id"])
            self.write_client(msg)
        # Child ended. If it wasn't a deliberate restart, bring a new one up.
        with self.child_lock:
            current = generation == self.generation and not self.restarting
        if current and not self.stdin_closed:
            print("singlestore-mcp supervisor: server exited unexpectedly; restarting", file=sys.stderr)
            self.restart()

    # ------------------------------------------------------------ restart
    def restart(self) -> str:
        with self.child_lock:
            self.restarting = True
            old = self.child
            orphaned, self.pending = self.pending, set()
        try:
            if old and old.poll() is None:
                try:
                    old.stdin and old.stdin.close()
                    old.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    old.kill()
            for rid in orphaned:
                self.write_client({"jsonrpc": "2.0", "id": rid,
                                   "error": {"code": -32603, "message": "The SingleStore MCP server restarted; try again."}})
            started = time.time()
            self.start_child()
            if self.init_request and not self.modern:
                self.replay_done.clear()
                self.write_child({**self.init_request, "id": _INIT_ID})
                if not self.replay_done.wait(60):
                    return "The new server didn't answer the initialize handshake within 60 s."
                if self.initialized_note:
                    self.write_child(self.initialized_note)
            for what in ("tools", "prompts", "resources"):
                self.write_client({"jsonrpc": "2.0", "method": f"notifications/{what}/list_changed"})
            return f"Restarted the SingleStore MCP server in {time.time() - started:.1f} s; code and app changes are loaded."
        finally:
            with self.child_lock:
                self.restarting = False

    # ------------------------------------------------------------ main loop
    stdin_closed = False

    def run(self) -> None:
        self.start_child()
        for raw in sys.stdin.buffer:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
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
                continue
            if method and "id" in msg:
                self.pending.add(msg["id"])
            try:
                self.write_child(msg)
            except OSError:
                if method and "id" in msg:
                    self.write_client({"jsonrpc": "2.0", "id": msg["id"],
                                       "error": {"code": -32603, "message": "The SingleStore MCP server is restarting; try again."}})
        # Client went away: stop the server too.
        self.stdin_closed = True
        with self.child_lock:
            child = self.child
        if child and child.poll() is None:
            try:
                child.stdin and child.stdin.close()
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                child.kill()

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
