"""Notebook kernels behind the kernel gateway (server mode, ``STUDIO_NOTEBOOKS=gateway``, step 2a).

The user's Studio runs no kernel itself: it asks the hub's kernel broker (``hub/gateway.py``; URL and this Studio's
key from the hub over stdin, ``server_mode.gateway()``) to start one on Jupyter Enterprise Gateway, and speaks the
Jupyter messaging protocol over the kernel's WebSocket channels, relayed by the broker. ``GatewayKernel`` has the
same face as ``notebook_kernel._Kernel`` (``send`` the bridge's ops, ``ready`` / ``error`` / ``busy_runs``,
``alive()``, ``close()``) and emits the bridge's events, so runs, output, interrupt, restart, idle stop and the
kernel limit work as with local kernels. It reuses the bridge's start-up code (``notebook_bridge.STARTUP``) and
output conversion.

Before that start-up code, a short prelude (over the kernel channel, not in the gateway's environment) gives the kernel
the active connections' non-secret settings and its token commands: a fresh SingleStore password / token and SAS
Viya token come from the hub's relay with this kernel's own secret (``kernel_tokens.RELAY_CLIENT``), which asks this
Studio's kernel token service - the user's secrets never leave the Studio except as the one token asked for.

Package installs (``install`` / ``list_packages``) run in a short-lived kernel of the same user: ``pip install
--target`` into the user's folder on the kernel volume, within ``STUDIO_PIP_MODE`` and the per-user quota.

Stdlib only: the WebSocket client is the small one below (text frames, ping/pong, close).
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

RESTART_GRACE_S = 2.0
BOOT_TIMEOUT_S = 90.0
WATCH_S = 10.0  # how often a kernel asks the broker whether the gateway still has it (culled when idle, died)


class GatewayUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------ the broker's REST API


def _settings() -> dict[str, Any]:
    from . import server_mode

    gw = server_mode.gateway()
    if not gw or not gw.get("url") or not gw.get("key"):
        raise GatewayUnavailable("The kernel gateway isn't set up for this Studio (open Studio again from the hub).")
    return gw


def _broker(method: str, path: str, kernel_key: str | None = None, body: Any = None, timeout: float = 60) -> tuple[int, Any]:
    gw = _settings()
    headers = {"X-Studio-Key": gw["key"], "Accept": "application/json"}
    if kernel_key:
        headers["X-Kernel-Key"] = kernel_key
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(gw["url"].rstrip("/") + path, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as res:
            raw = res.read()
            return res.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw.decode("utf-8", "replace")[:300]}
    except OSError as exc:
        raise GatewayUnavailable(f"The kernel broker didn't answer ({type(exc).__name__}).") from exc


# ------------------------------------------------------------------ a small WebSocket client (RFC 6455)


class WebSocket:
    def __init__(self, url: str, headers: dict[str, str], timeout: float = 30) -> None:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname or "127.0.0.1", parts.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", *[f"{k}: {v}" for k, v in headers.items()]]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 65536:
                break
        head, _, self._buf = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status + " ":
            body = self._buf.decode("utf-8", "replace")
            self.sock.close()
            try:
                msg = json.loads(body).get("error") or status
            except ValueError:
                msg = status
            raise GatewayUnavailable(f"The kernel's channels didn't open: {msg}")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()  # noqa: S324
        got = {line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
               for line in head.decode("latin-1").split("\r\n")[1:] if ":" in line}
        if got.get("sec-websocket-accept") != accept:
            self.sock.close()
            raise GatewayUnavailable("The kernel's channels answered with a wrong WebSocket handshake.")
        self.sock.settimeout(None)
        self._send_lock = threading.Lock()
        self.closed = False

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _mask(payload, mask)
        with self._send_lock:
            self.sock.sendall(head + mask + masked)

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode("utf-8"))

    def recv(self) -> str | None:
        """The next text message; None when the connection closed. Binary messages (buffers) are skipped."""
        parts: list[bytes] = []
        kind = None
        try:
            while True:
                b0, b1 = self._read(2)
                opcode, fin, n = b0 & 0x0F, b0 & 0x80, b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else b""
                payload = self._read(n)
                if mask:
                    payload = _mask(payload, mask)
                if opcode == 0x8:
                    self.close()
                    return None
                if opcode == 0x9:
                    self._frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    kind, parts = opcode, [payload]
                elif opcode == 0x0:
                    parts.append(payload)
                if fin:
                    if kind == 0x1:
                        return b"".join(parts).decode("utf-8", "replace")
                    parts, kind = [], None
        except (OSError, ConnectionError, ValueError):
            self.closed = True
            return None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def _mask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    full = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(full, "big")).to_bytes(n, "big") if n else b""


# ------------------------------------------------------------------ the kernel


def _message(msg_type: str, content: dict[str, Any], session: str, channel: str = "shell") -> dict[str, Any]:
    return {"header": {"msg_id": uuid.uuid4().hex, "username": "studio", "session": session, "msg_type": msg_type,
                       "version": "5.3", "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "parent_header": {}, "metadata": {}, "content": content, "channel": channel, "buffers": []}


def prelude(settings: dict[str, str], relay: str, s2_connection: str | None, sas_connection: str | None) -> str:
    """Code run first in a gateway kernel: settings, the relay, token commands with the kernel's own Python, and the
    SingleStore password / token fetched once (renewed through the same command when a token runs out)."""
    from .kernel_tokens import ENV, RELAY_CLIENT

    return f"""
import os as _p_os, json as _p_json, sys as _p_sys, subprocess as _p_sub
_p_os.environ.update({settings!r})
_p_os.environ[{ENV!r}] = {relay!r}
def _p_cmd(kind, name):
    return _p_json.dumps([_p_sys.executable, "-I", "-c", {RELAY_CLIENT!r}, kind, name])
if {sas_connection!r}:
    _p_os.environ["SINGLESTORE_MCP_SAS_TOKEN_CMD"] = _p_cmd("sas", {sas_connection!r})
if {s2_connection!r}:
    _p_os.environ["SINGLESTORE_MCP_TOKEN_CMD"] = _p_cmd("s2", {s2_connection!r})
    try:
        _p_out = _p_sub.run(_p_json.loads(_p_os.environ["SINGLESTORE_MCP_TOKEN_CMD"]), capture_output=True, text=True, timeout=60)
        if _p_out.returncode == 0 and _p_out.stdout.strip():
            _p_os.environ["SINGLESTORE_PASSWORD"] = _p_out.stdout.strip()
    except Exception:
        pass
del _p_os, _p_json, _p_sys, _p_sub, _p_cmd
"""


class GatewayKernel:
    """One notebook kernel on the gateway, driven like the local bridge."""

    def __init__(self, notebook_id: str, on_event: Callable[[Any, dict[str, Any]], None],
                 on_end: Callable[[Any], None], code_before: str = "", startup: bool = True, purpose: str = "notebook") -> None:
        self.notebook_id = notebook_id
        self.ready = threading.Event()
        self.error: str | None = None
        self.last_used = time.time()
        self.busy_runs: set[str] = set()
        self.stderr: list[str] = []
        self.kernel_id: str | None = None
        self._key: str | None = None
        self._on_event, self._on_end = on_event, on_end
        self._code_before, self._startup, self._purpose = code_before, startup, purpose
        self._prelude = ""
        self._session = uuid.uuid4().hex
        self._ws: WebSocket | None = None
        self._waiters: dict[str, queue.Queue] = {}
        self._jobs: queue.Queue = queue.Queue()
        self._dead = False
        self._closing = False
        self._reconnecting = False
        self.restart_requested = threading.Event()
        self.restart_at = 0.0
        threading.Thread(target=self._boot, name=f"gw-kernel-{notebook_id}", daemon=True).start()

    # -------------------------------------------------- lifecycle
    def alive(self) -> bool:
        return not self._dead

    def _boot(self) -> None:
        try:
            from . import kernel_tokens

            _wait_for_closing()
            status, data = _broker("POST", "/kernels", body={"tokens": kernel_tokens.start(), "purpose": self._purpose},
                                   timeout=BOOT_TIMEOUT_S)
            if status != 201:
                raise GatewayUnavailable(str(data.get("error") or f"HTTP {status}"))
            self.kernel_id, self._key, relay = data["id"], data["key"], data["relay"]
            if self._closing:
                self._delete()
                return
            self._connect()
            self._wait_kernel()
            self._prelude = self._code_before.replace("__RELAY__", relay)
            self._run(None, self._prelude, silent=True)
            if self._startup:
                from .notebook_bridge import STARTUP

                self._run(None, STARTUP, silent=True)
        except Exception as exc:  # noqa: BLE001 - shown as the kernel's state
            self._emit({"event": "error", "message": f"Couldn't start the Python kernel: {exc}"})
            self._die()
            return
        threading.Thread(target=self._worker, name=f"gw-worker-{self.notebook_id}", daemon=True).start()
        threading.Thread(target=self._watch, name=f"gw-watch-{self.notebook_id}", daemon=True).start()
        self._emit({"event": "ready"})

    def _connect(self) -> None:
        gw = _settings()
        url = gw["url"].rstrip("/") + f"/kernels/{self.kernel_id}/channels?session_id={self._session}"
        self._ws = WebSocket(url, {"X-Studio-Key": gw["key"], "X-Kernel-Key": self._key or ""})
        threading.Thread(target=self._reader, args=(self._ws,), name=f"gw-reader-{self.notebook_id}", daemon=True).start()

    def _reader(self, ws: WebSocket) -> None:
        while True:
            raw = ws.recv()
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            parent = (msg.get("parent_header") or {}).get("msg_id")
            q = self._waiters.get(parent or "")
            if q is not None:
                q.put(msg)
        if ws is self._ws and not self._closing and not self._reconnecting:
            self._die()

    def _watch(self) -> None:
        """The gateway may drop a kernel without closing its channels (idle culling, a crash): ask now and then."""
        while not self._dead and not self._closing:
            time.sleep(WATCH_S)
            if self._dead or self._closing or self._reconnecting or not self.kernel_id:
                continue
            try:
                status, _ = _broker("GET", f"/kernels/{self.kernel_id}", self._key, timeout=20)
            except GatewayUnavailable:
                continue
            if status == 404 and not self._closing and not self._reconnecting:
                self.error = "The kernel stopped (idle on the server, or it died)."
                if self._ws is not None:
                    self._ws.close()
                self._die()

    def _die(self) -> None:
        if self._dead:
            return
        self._dead = True
        for q in list(self._waiters.values()):
            q.put(None)
        self._on_end(self)

    def _request(self, msg_type: str, content: dict[str, Any], channel: str = "shell") -> tuple[str, queue.Queue]:
        msg = _message(msg_type, content, self._session, channel)
        mid = msg["header"]["msg_id"]
        q: queue.Queue = queue.Queue()
        self._waiters[mid] = q
        if self._ws is None:
            raise ConnectionError("no channels")
        self._ws.send(json.dumps(msg))
        return mid, q

    def _wait_kernel(self, timeout: float = BOOT_TIMEOUT_S) -> None:
        """kernel_info until the kernel answers (it may still be starting behind the gateway)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            mid, q = self._request("kernel_info_request", {})
            try:
                while True:
                    msg = q.get(timeout=2)
                    if msg is None:
                        raise ConnectionError("The kernel's channels closed while it started.")
                    if msg["header"]["msg_type"] == "kernel_info_reply":
                        return
            except queue.Empty:
                continue
            finally:
                self._waiters.pop(mid, None)
        raise TimeoutError("The kernel didn't answer in time.")

    def _emit(self, msg: dict[str, Any]) -> None:
        self._on_event(self, msg)

    # -------------------------------------------------- the bridge's ops
    def send(self, msg: dict[str, Any]) -> None:
        if self._dead:
            raise OSError("The Python kernel stopped.")
        op = msg.get("op")
        if op == "execute":
            self._jobs.put(msg)
        elif op == "interrupt":
            threading.Thread(target=self._control, args=("interrupt",), daemon=True).start()
        elif op == "restart":
            while not self._jobs.empty():
                job = self._jobs.get_nowait()
                if job and job.get("id"):
                    self._emit({"id": job["id"], "event": "done", "status": "aborted", "execution_count": None})
            threading.Thread(target=self._control, args=("interrupt",), daemon=True).start()
            self.restart_at = time.monotonic()
            self.restart_requested.set()
            self._jobs.put({"restart": True})
        elif op == "shutdown":
            self.close()

    def _control(self, action: str) -> bool:
        if not self.kernel_id:
            return False
        try:
            status, _ = _broker("POST", f"/kernels/{self.kernel_id}/{action}", self._key, body={}, timeout=90)
            return status == 200
        except GatewayUnavailable:
            return False

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            if job.get("restart"):
                self.restart_requested.clear()
                try:
                    self._restart()
                except Exception as exc:  # noqa: BLE001
                    self._emit({"event": "error", "message": f"Kernel restart failed: {exc}"})
                continue
            try:
                self._run(job.get("id"), job["code"], silent=bool(job.get("silent")))
            except Exception as exc:  # noqa: BLE001 - report and keep serving
                if job.get("id"):
                    self._emit({"id": job["id"], "event": "output", "output": {"output_type": "error", "ename": type(exc).__name__,
                                                                                "evalue": str(exc), "traceback": []}})
                    self._emit({"id": job["id"], "event": "done", "status": "error", "execution_count": None})

    def _restart(self) -> None:
        self._reconnecting = True
        try:
            if not self._control("restart"):
                raise RuntimeError("the gateway refused the restart")
            old, self._ws = self._ws, None
            if old is not None:
                old.close()
            self._session = uuid.uuid4().hex
            self._connect()
            self._wait_kernel()
        finally:
            self._reconnecting = False
        self._run(None, self._prelude, silent=True)
        if self._startup:
            from .notebook_bridge import STARTUP

            self._run(None, STARTUP, silent=True)
        self._emit({"event": "restarted"})

    def _run(self, run_id: str | None, code: str, silent: bool = False) -> None:
        from .notebook_bridge import _to_output

        if not code.strip():
            return
        mid, q = self._request("execute_request", {"code": code, "silent": silent, "store_history": not silent,
                                                   "user_expressions": {}, "allow_stdin": False, "stop_on_error": True})
        status, count = "ok", None
        last = time.monotonic()
        try:
            while True:
                if self.restart_requested.is_set() and time.monotonic() - self.restart_at > RESTART_GRACE_S:
                    status = "aborted"
                    break
                try:
                    msg = q.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - last > 3600:
                        status = "aborted"
                        break
                    continue
                if msg is None:
                    status = "error"
                    break
                last = time.monotonic()
                if msg.get("channel") not in (None, "iopub"):
                    continue
                kind = msg["header"]["msg_type"]
                if kind == "status" and msg["content"].get("execution_state") == "idle":
                    break
                if kind == "execute_input":
                    count = msg["content"].get("execution_count")
                if kind == "error":
                    status = "error"
                if kind == "clear_output" and run_id:
                    self._emit({"id": run_id, "event": "clear"})
                    continue
                out = _to_output(msg)
                if out and run_id and not silent:
                    self._emit({"id": run_id, "event": "output", "output": out})
                elif out and silent and out["output_type"] == "error":
                    self._emit({"event": "error", "message": f"Kernel startup failed: {out['ename']}: {out['evalue']}"})
        finally:
            self._waiters.pop(mid, None)
        if run_id:
            self._emit({"id": run_id, "event": "done", "status": status, "execution_count": count})

    def _delete(self) -> None:
        if self.kernel_id and self._key:
            try:
                _broker("DELETE", f"/kernels/{self.kernel_id}", self._key, timeout=30)
            except GatewayUnavailable:
                pass

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._jobs.put(None)
        with _closing_cv:
            _closing.add(id(self))

        def stop() -> None:
            try:
                self._delete()
                if self._ws is not None:
                    self._ws.close()
                self._dead = True
            finally:
                with _closing_cv:
                    _closing.discard(id(self))
                    _closing_cv.notify_all()

        threading.Thread(target=stop, daemon=True).start()


# Kernels being shut down right now: a new kernel waits for them first, so a kernel the Studio closed to stay within
# the per-user limit (notebook_kernel closes its oldest idle one) no longer counts at the broker.
_closing: set[int] = set()
_closing_cv = threading.Condition()


def _wait_for_closing(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    with _closing_cv:
        while _closing and time.time() < deadline:
            _closing_cv.wait(max(0.05, deadline - time.time()))


# ------------------------------------------------------------------ one-shot kernels: package installs and listing


def run_once(code: str, on_text: Callable[[str], None] | None = None, timeout: float = 1800) -> tuple[str, str]:
    """Run ``code`` in a short-lived kernel of this user (no start-up code); returns (status, all stream text)."""
    done = threading.Event()
    result: dict[str, Any] = {"status": "error", "text": [], "error": None}

    def on_event(_k: Any, msg: dict[str, Any]) -> None:
        ev = msg.get("event")
        if ev == "ready":
            result["ready"] = True
        elif ev == "error" and "id" not in msg:
            result["error"] = msg.get("message")
            done.set()
        elif ev == "output":
            out = msg["output"]
            text = out.get("text") if out.get("output_type") == "stream" else \
                f"{out.get('ename')}: {out.get('evalue')}" if out.get("output_type") == "error" else None
            if text:
                result["text"].append(text)
                if on_text:
                    on_text(text)
        elif ev == "done":
            result["status"] = msg.get("status")
            done.set()

    k = GatewayKernel("__packages__", on_event, lambda _k: done.set(), startup=False, purpose="packages")
    try:
        deadline = time.time() + BOOT_TIMEOUT_S
        while not result.get("ready") and not done.is_set() and time.time() < deadline:
            time.sleep(0.1)
        if result.get("error") or not result.get("ready"):
            raise RuntimeError(result.get("error") or "The package kernel didn't start.")
        k.send({"id": "once", "op": "execute", "code": code})
        if not done.wait(timeout):
            raise TimeoutError("The package command took too long.")
        if result.get("error"):
            raise RuntimeError(result["error"])
        return str(result["status"]), "".join(result["text"])
    finally:
        k.close()


INSTALL_SCRIPT = r'''
import json, os, subprocess, sys
def _size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total
user_dir, target = os.environ["S2_KERNEL_USER_DIR"], os.environ["S2_KERNEL_PIP_DIR"]
quota = int(os.environ.get("S2_KERNEL_QUOTA_BYTES") or 0)
gb = lambda n: f"{n / 1024 ** 3:.2f} GB"
if os.environ.get("S2_KERNEL_PIP_MODE") == "off":
    raise SystemExit("Package installs are off on this server (STUDIO_PIP_MODE=off).")
used = _size(user_dir)
if quota and used >= quota:
    raise SystemExit(f"Your folder on the kernel volume uses {gb(used)} of {gb(quota)}: remove packages or files first.")
print(f"Installing into your own package folder ({gb(used)} of {gb(quota)} used)…", flush=True)
proc = subprocess.Popen([sys.executable, "-m", "pip", "install", "--target", target, "--upgrade", *SPECS],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
for line in proc.stdout:
    if line.strip():
        print(line.rstrip()[:300], flush=True)
code = proc.wait()
used = _size(user_dir)
print(f"Your folder now uses {gb(used)} of {gb(quota)}." + (" Over the quota: further installs are refused until "
      "you remove packages." if quota and used > quota else ""), flush=True)
if code != 0:
    raise SystemExit(f"pip install failed (exit code {code}).")
'''


def install(specs: list[str], log: Callable[[str], None]) -> None:
    """``pip install --target`` the user's packages in a short-lived kernel; progress lines go to ``log``."""
    from . import server_mode

    if server_mode.pip_settings()[0] == "off":
        raise server_mode.FeatureOff(server_mode.PIP_OFF)
    code = f"SPECS = {specs!r}\n" + INSTALL_SCRIPT
    status, text = run_once(code, on_text=lambda t: [log(line) for line in t.splitlines() if line.strip()])
    if status != "ok":
        tail = [line for line in text.splitlines() if line.strip()][-1:] or ["the install failed"]
        raise RuntimeError(tail[0])


def list_packages(script: str) -> list[dict[str, str]]:
    status, text = run_once(script, timeout=120)
    if status != "ok":
        raise RuntimeError(f"Couldn't list packages: {text.strip()[-300:]}")
    return json.loads(text.strip().splitlines()[-1])


def kernel_settings(env: dict[str, str]) -> dict[str, str]:
    """The non-secret part of a local kernel's environment that a gateway kernel gets in its prelude: the active
    SingleStore connection's address and options, the SAS Viya address / context / connection name."""
    keep = ("SINGLESTORE_HOST", "SINGLESTORE_PORT", "SINGLESTORE_USER", "SINGLESTORE_DATABASE", "SINGLESTORE_SSL_DISABLED",
            "SINGLESTORE_SSL_VERIFY", "SINGLESTORE_CREDENTIAL_TYPE", "SINGLESTORE_URL", "SINGLESTORE_MCP_TCP_KEEPALIVE",
            "SINGLESTORE_MCP_NOTEBOOK_FETCH_LIMIT", "SINGLESTORE_MCP_SAS_CONNECTION", "SINGLESTORE_MCP_SAS_URL",
            "SINGLESTORE_MCP_SAS_CONTEXT", "SINGLESTORE_MCP_SAS_VERIFY", "SINGLESTORE_MCP_SAS_LIBREF")
    return {k: v for k, v in env.items() if k in keep and v is not None}
