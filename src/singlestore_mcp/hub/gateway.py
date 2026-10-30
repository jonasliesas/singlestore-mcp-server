"""The hub's kernel broker and token relay (``STUDIO_NOTEBOOKS=gateway``, step 2a).

Notebook kernels run behind Jupyter Enterprise Gateway (its own pod, ``kernel_gateway.py``). Only the hub holds the
gateway's auth token; Studios never see it. Each Studio reaches the gateway through this broker instead:

Broker (``127.0.0.1:<random port>``, inside the hub pod only; the URL and a per-Studio key go to each Studio over its
stdin)::

    POST   /kernels                     start a kernel for this Studio's user -> {"id", "key"}
    GET    /kernels/<id>                its state (the gateway's model: execution_state, last_activity)
    POST   /kernels/<id>/interrupt      (and /restart)
    DELETE /kernels/<id>
    GET    /kernels/<id>/channels       WebSocket (Jupyter kernel channels), relayed byte for byte to the gateway

Every request carries ``X-Studio-Key`` (which Studio = which user) and, for a kernel, ``X-Kernel-Key`` (that kernel's
own random key, handed to the Studio that started it). A kernel answers only to its own key from the Studio that
started it; anything else gets the same 404 as an unknown kernel. Limits: ``STUDIO_KERNELS_PER_USER`` and
``STUDIO_KERNELS_TOTAL`` (plus one package-install kernel per user), and only users with notebook access.

Token relay (``STUDIO_RELAY_BIND:STUDIO_RELAY_PORT``, reachable only from the gateway pod - NetworkPolicy)::

    GET /k/<kernel secret>/sas|s2/<connection>

A kernel asks for a fresh SAS Viya / SingleStore token of its own user with its own random secret (sent to it over
its kernel channel, never in the gateway's request or environment). The relay finds the kernel's user and asks that
user's Studio (its kernel token service on 127.0.0.1, ``kernel_tokens``), which holds the user's secrets; the relay
keeps nothing.

Kernels stop when the Studio closes them (idle, shutdown, notebook closed), when the user's Studio stops, at
``STUDIO_KERNEL_IDLE_MINUTES`` without activity (the gateway's own culler; busy kernels stay), and when the hub stops.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .audit import Audit

KERNEL_NAME = "studio-python"
_ID_OK = set("0123456789abcdefABCDEF-")


class GatewayError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class GatewaySettings:
    url: str                        # the gateway (http://studio-gateway:8888)
    token: str                      # its auth token (a Secret; only the hub has it)
    kernels_per_user: int = 3
    kernels_total: int = 20
    relay_bind: str = "127.0.0.1"
    relay_port: int = 0
    relay_url: str = ""             # what kernels use to reach the relay (default: http://<relay bind>:<port>)
    verify: bool = True
    start_timeout_s: int = 90


@dataclass
class Kernel:
    id: str
    user: str
    key_hash: str
    relay_secret: str
    tokens_base: str
    purpose: str = "notebook"
    studio: str = ""                # hash of the key of the Studio that started it (only that Studio reaches it)
    created: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    channels: int = 0


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KernelBroker:
    def __init__(self, settings: GatewaySettings, audit: Audit, allowed: Callable[[str], bool] | None = None,
                 folder: Callable[[str], str] | None = None) -> None:
        self.s = settings
        self.audit = audit
        self.allowed = allowed or (lambda user: True)
        self.folder = folder or (lambda user: user)   # the user's folder-safe id (KERNEL_USERNAME)
        self._studios: dict[str, str] = {}             # key hash -> user
        self._kernels: dict[str, Kernel] = {}
        self._relay: dict[str, str] = {}               # relay secret hash -> kernel id
        self._lock = threading.Lock()
        self._servers: list[ThreadingHTTPServer] = []
        self._stop = threading.Event()
        self.url = ""
        self.relay_url = ""
        parts = urllib.parse.urlsplit(settings.url.rstrip("/"))
        self._scheme, self._host, self._port = parts.scheme or "http", parts.hostname or "127.0.0.1", parts.port
        self._base_path = parts.path.rstrip("/")
        if self._port is None:
            self._port = 443 if self._scheme == "https" else 80

    # ------------------------------------------------------------ studios
    def register_studio(self, user: str) -> str:
        """A new key for the user's (re)started Studio; an older key of the same user stops working."""
        key = secrets.token_urlsafe(32)
        with self._lock:
            for h in [h for h, u in self._studios.items() if u == user]:
                self._studios.pop(h)
            self._studios[_hash(key)] = user
        return key

    def forget_studio(self, user: str, reason: str = "studio stopped", key: str | None = None) -> int:
        """A Studio stopped: its key stops working and its kernels are shut down. With ``key``: only that Studio
        (a newer Studio of the same user, started meanwhile, keeps its key and kernels); without: all of the user's."""
        with self._lock:
            hashes = {_hash(key)} if key else {h for h, u in self._studios.items() if u == user}
            for h in hashes:
                if self._studios.get(h) == user:
                    self._studios.pop(h)
            gone = [k for k in self._kernels.values() if k.user == user and (not key or k.studio in hashes)]
        for k in gone:
            self._shutdown(k, reason)
        return len(gone)

    def _studio_user(self, key: str | None) -> str | None:
        if not key:
            return None
        with self._lock:
            return self._studios.get(_hash(key))

    def kernels(self) -> list[Kernel]:
        with self._lock:
            return list(self._kernels.values())

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for k in self.kernels():
            out[k.user] = out.get(k.user, 0) + 1
        return out

    # ------------------------------------------------------------ the gateway's REST API
    def _conn(self, timeout: float) -> http.client.HTTPConnection:
        if self._scheme == "https":
            ctx = ssl.create_default_context() if self.s.verify else ssl._create_unverified_context()  # noqa: S323
            return http.client.HTTPSConnection(self._host, self._port, timeout=timeout, context=ctx)
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout)

    def gateway(self, method: str, path: str, body: Any = None, timeout: float = 30) -> tuple[int, Any]:
        conn = self._conn(timeout)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"token {self.s.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, self._base_path + path, body=data, headers=headers)
            res = conn.getresponse()
            raw = res.read()
        except (OSError, http.client.HTTPException) as exc:
            raise GatewayError(502, f"The kernel gateway didn't answer ({type(exc).__name__}).") from exc
        finally:
            conn.close()
        try:
            return res.status, json.loads(raw) if raw else None
        except ValueError:
            return res.status, raw.decode("utf-8", "replace")[:500]

    def healthy(self) -> bool:
        try:
            status, _ = self.gateway("GET", "/api", timeout=5)
            return status == 200
        except GatewayError:
            return False

    # ------------------------------------------------------------ kernels
    def start_kernel(self, user: str, tokens_base: str, purpose: str = "notebook", studio: str = "") -> tuple[Kernel, str, str]:
        """Start a kernel for ``user``: returns the record, the kernel's key (for the Studio) and its relay secret (for
        the kernel; only hashes are kept)."""
        if not self.allowed(user):
            raise GatewayError(403, "Notebooks aren't available for your SAS user.")
        if purpose not in ("notebook", "packages"):
            raise GatewayError(400, "bad purpose")
        if tokens_base and not tokens_base.startswith("http://127.0.0.1:"):
            raise GatewayError(400, "The token service must be the Studio's own (127.0.0.1).")
        with self._lock:
            mine = [k for k in self._kernels.values() if k.user == user and k.purpose == purpose]
            total = [k for k in self._kernels.values() if k.purpose == "notebook"]
            if purpose == "packages" and mine:
                raise GatewayError(429, "Another package install is still running.")
            if purpose == "notebook" and len(mine) >= self.s.kernels_per_user:
                raise GatewayError(429, f"You have {len(mine)} notebook kernels running (the most per user on this server). "
                                        "Shut one down (or close a notebook) first.")
            if purpose == "notebook" and len(total) >= self.s.kernels_total:
                raise GatewayError(429, f"The server runs the most notebook kernels it allows ({self.s.kernels_total}). "
                                        "Try again later.")
            placeholder = Kernel(id=f"pending-{secrets.token_hex(8)}", user=user, key_hash="", relay_secret="",
                                 tokens_base=tokens_base, purpose=purpose, studio=studio)
            self._kernels[placeholder.id] = placeholder  # holds the place while the gateway starts it
        t0 = time.time()
        try:
            status, model = self.gateway("POST", "/api/kernels", {"name": KERNEL_NAME, "env": {"KERNEL_USERNAME": self.folder(user)}},
                                         timeout=self.s.start_timeout_s)
        except GatewayError:
            with self._lock:
                self._kernels.pop(placeholder.id, None)
            self.audit.record("kernel_start_failed", user=user, reason="gateway unreachable")
            raise
        if status != 201 or not isinstance(model, dict) or not model.get("id"):
            with self._lock:
                self._kernels.pop(placeholder.id, None)
            self.audit.record("kernel_start_failed", user=user, status=status)
            raise GatewayError(502 if status < 500 else status, f"The kernel gateway couldn't start a kernel (HTTP {status}).")
        key, relay = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        k = Kernel(id=str(model["id"]), user=user, key_hash=_hash(key), relay_secret=_hash(relay), tokens_base=tokens_base,
                   purpose=purpose, studio=studio)
        with self._lock:
            self._kernels.pop(placeholder.id, None)
            self._kernels[k.id] = k
            self._relay[k.relay_secret] = k.id
        self.audit.record("kernel_start", user=user, kernel=k.id[:8], purpose=purpose, ms=round((time.time() - t0) * 1000))
        return k, key, relay

    def _kernel_for(self, user: str | None, kernel_id: str, key: str | None, studio: str = "") -> Kernel:
        with self._lock:
            k = self._kernels.get(kernel_id)
        if user is None or k is None or not key or k.user != user or not secrets.compare_digest(k.key_hash, _hash(key)) \
                or (k.studio and k.studio != studio):
            raise GatewayError(404, "No such kernel.")
        return k

    def _shutdown(self, k: Kernel, reason: str) -> None:
        with self._lock:
            self._kernels.pop(k.id, None)
            self._relay.pop(k.relay_secret, None)
        if k.id.startswith("pending-"):
            return
        try:
            self.gateway("DELETE", f"/api/kernels/{k.id}", timeout=20)
        except GatewayError:
            pass
        self.audit.record("kernel_stop", user=k.user, kernel=k.id[:8], reason=reason,
                          minutes=round((time.time() - k.created) / 60, 1))

    def _forget(self, k: Kernel, reason: str) -> None:
        with self._lock:
            known = self._kernels.pop(k.id, None) is not None
            self._relay.pop(k.relay_secret, None)
        if known:
            self.audit.record("kernel_stop", user=k.user, kernel=k.id[:8], reason=reason,
                              minutes=round((time.time() - k.created) / 60, 1))

    def reap_once(self) -> list[str]:
        """Forget kernels the gateway no longer has (culled when idle, died) and stop those of Studios that are gone."""
        dropped = []
        with self._lock:
            studios = set(self._studios)
        for k in self.kernels():
            if k.id.startswith("pending-"):
                continue
            if k.studio not in studios:
                self._shutdown(k, "studio gone")
                dropped.append(k.id)
                continue
            try:
                status, _ = self.gateway("GET", f"/api/kernels/{k.id}", timeout=10)
            except GatewayError:
                continue
            if status == 404:
                self._forget(k, "gone from the gateway (idle or died)")
                dropped.append(k.id)
        return dropped

    def stop_all(self) -> None:
        self._stop.set()
        threads = [threading.Thread(target=self._shutdown, args=(k, "hub stopped"), daemon=True) for k in self.kernels()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

    # ------------------------------------------------------------ relay
    def relay_token(self, secret: str, kind: str, connection: str) -> tuple[int, bytes]:
        with self._lock:
            kid = self._relay.get(_hash(secret))
            k = self._kernels.get(kid) if kid else None
        if k is None or kind not in ("sas", "s2") or not k.tokens_base:
            return 404, b"not found"
        url = f"{k.tokens_base}/{kind}/{urllib.parse.quote(connection, safe='')}"
        parts = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=60)
        try:
            conn.request("GET", parts.path)
            res = conn.getresponse()
            body = res.read()
        except (OSError, http.client.HTTPException):
            return 502, b"Your Studio didn't answer the token request (open Studio again)."
        finally:
            conn.close()
        k.last_activity = time.time()
        return res.status, body[:16384]

    # ------------------------------------------------------------ servers
    def serve(self) -> None:
        broker = _Server(("127.0.0.1", 0), _broker_handler(self))
        relay = _Server((self.s.relay_bind, self.s.relay_port), _relay_handler(self))
        for srv in (broker, relay):
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, name="kernel-broker", daemon=True).start()
            self._servers.append(srv)
        self.url = f"http://127.0.0.1:{broker.server_address[1]}"
        self.relay_url = (self.s.relay_url or f"http://{self._relay_host()}:{relay.server_address[1]}").rstrip("/")

        def reap() -> None:
            while not self._stop.wait(60):
                try:
                    self.reap_once()
                except Exception:  # noqa: BLE001 - keep reaping
                    pass

        threading.Thread(target=reap, name="kernel-reaper", daemon=True).start()

    def _relay_host(self) -> str:
        return "127.0.0.1" if self.s.relay_bind in ("", "0.0.0.0", "::") else self.s.relay_bind

    def close(self) -> None:
        self.stop_all()
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        self._servers.clear()

    # ------------------------------------------------------------ WebSocket relay
    def open_channels(self, k: Kernel, query: str, client_headers: Any) -> tuple[socket.socket, bytes, bytes]:
        """Open the kernel's channels on the gateway (WebSocket upgrade with the hub's token). Returns the socket, the
        gateway's response head and any bytes after it."""
        raw = socket.create_connection((self._host, self._port), timeout=20)
        sock: socket.socket = raw
        if self._scheme == "https":
            ctx = ssl.create_default_context() if self.s.verify else ssl._create_unverified_context()  # noqa: S323
            sock = ctx.wrap_socket(raw, server_hostname=self._host)
        path = f"{self._base_path}/api/kernels/{k.id}/channels" + (f"?{query}" if query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {self._host}:{self._port}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {client_headers.get('Sec-WebSocket-Key', '')}",
                 f"Sec-WebSocket-Version: {client_headers.get('Sec-WebSocket-Version', '13')}",
                 f"Authorization: token {self.s.token}"]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return sock, head, rest


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that went away (a closed kernel channel, a kernel's token request cut short) is no error."""
        import sys

        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


def _broker_handler(broker: KernelBroker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 120

        def log_message(self, *args: Any) -> None:
            pass

        def _json(self, status: int, data: Any) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            if self.headers.get("Transfer-Encoding"):
                raise GatewayError(400, "chunked bodies aren't accepted")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise GatewayError(413, "too large")
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else {}

        def _route(self) -> tuple[str | None, list[str], str]:
            parts = urllib.parse.urlsplit(self.path)
            segs = [s for s in parts.path.split("/") if s]
            return broker._studio_user(self.headers.get("X-Studio-Key")), segs, parts.query

        def _handle(self, method: str) -> None:
            user, segs, query = self._route()
            try:
                if user is None:
                    raise GatewayError(403, "Unknown Studio.")
                if segs == ["kernels"] and method == "POST":
                    body = self._body()
                    k, key, relay = broker.start_kernel(user, str(body.get("tokens") or ""), str(body.get("purpose") or "notebook"),
                                                        studio=_hash(self.headers.get("X-Studio-Key") or ""))
                    return self._json(201, {"id": k.id, "key": key, "relay": f"{broker.relay_url}/k/{relay}"})
                if len(segs) < 2 or segs[0] != "kernels" or not set(segs[1]) <= _ID_OK:
                    raise GatewayError(404, "Not found.")
                self._body() if method != "GET" else None
                k = broker._kernel_for(user, segs[1], self.headers.get("X-Kernel-Key"), _hash(self.headers.get("X-Studio-Key") or ""))
                k.last_activity = time.time()
                if len(segs) == 2 and method == "GET":
                    status, model = broker.gateway("GET", f"/api/kernels/{k.id}")
                    if status == 404:
                        broker._forget(k, "gone from the gateway (idle or died)")
                        return self._json(404, {"error": "The kernel stopped (idle, or it died)."})
                    m = model if isinstance(model, dict) else {}
                    return self._json(200, {"id": k.id, "execution_state": m.get("execution_state"),
                                            "last_activity": m.get("last_activity")})
                if len(segs) == 2 and method == "DELETE":
                    broker._shutdown(k, "closed")
                    return self._json(200, {"ok": True})
                if len(segs) == 3 and method == "POST" and segs[2] in ("interrupt", "restart"):
                    status, _ = broker.gateway("POST", f"/api/kernels/{k.id}/{segs[2]}", {}, timeout=90)
                    return self._json(200 if status < 300 else 502, {"ok": status < 300, "status": status})
                if len(segs) == 3 and method == "GET" and segs[2] == "channels":
                    return self._channels(k, query)
                raise GatewayError(404, "Not found.")
            except GatewayError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (ValueError, KeyError):
                self._json(400, {"error": "Bad request."})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def _channels(self, k: Kernel, query: str) -> None:
            if self.headers.get("Upgrade", "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
                raise GatewayError(400, "WebSocket upgrade expected.")
            q = urllib.parse.urlencode({"session_id": urllib.parse.parse_qs(query).get("session_id", [""])[0]}) if query else ""
            try:
                up, head, rest = broker.open_channels(k, q, self.headers)
            except OSError:
                raise GatewayError(502, "The kernel gateway didn't answer.") from None
            first = head.split(b"\r\n", 1)[0]
            if b" 101 " not in first + b" ":
                up.close()
                raise GatewayError(502, f"The kernel gateway refused the channels ({first.decode('latin-1')[:60]}).")
            # Pass the gateway's 101 on (its own Sec-WebSocket-Accept matches the Studio's key), then relay raw bytes.
            self.close_connection = True
            client = self.connection
            client.settimeout(None)
            up.settimeout(None)
            client.sendall(head + b"\r\n\r\n" + rest)
            k.channels += 1

            def pump(src_read: Callable[[], bytes], dst: socket.socket) -> None:
                try:
                    while True:
                        data = src_read()
                        if not data:
                            break
                        k.last_activity = time.time()
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    for s in (dst,):
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            back = threading.Thread(target=pump, args=(lambda: up.recv(65536), client), daemon=True)
            back.start()
            pump(lambda: self.rfile.read1(65536), up)
            back.join(5)
            k.channels -= 1
            try:
                up.close()
            except OSError:
                pass

    return Handler


def _relay_handler(broker: KernelBroker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = 60

        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            segs = urllib.parse.urlsplit(self.path).path.split("/")
            status, body = 404, b"not found"
            if len(segs) == 5 and segs[1] == "k" and segs[2]:
                status, body = broker.relay_token(segs[2], segs[3], urllib.parse.unquote(segs[4]))
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler

