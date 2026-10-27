"""A small stand-in for the SAS Viya REST endpoints ``sas_rest`` uses (Compute and CAS sessions), for offline tests.

HTTP/1.1 with keep-alive (like Viya's ingress), no TLS. Sessions live in memory; ``forget_sessions()`` makes Viya
"end" them (404 for the session, as after Viya's idle timeout or a restart), ``answer_delay`` slows every answer.
``requests`` records ``(method, path)``.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeViya:
    def __init__(self) -> None:
        self.compute: set[str] = set()
        self.cas: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self.answer_delay = 0.0
        self.created = 0
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        viya = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # quiet
                pass

            def _reply(self, status: int, body: object = None, ctype: str = "application/json") -> None:
                data = b"" if body is None else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                path = self.path.split("?")[0]
                if "://" in path:  # sent through an HTTP proxy: the absolute URL
                    path = "/" + path.split("://", 1)[1].split("/", 1)[-1]
                with viya._lock:
                    viya.requests.append((self.command, path))
                if viya.answer_delay:
                    time.sleep(viya.answer_delay)
                status, body, ctype = viya.route(self.command, path)
                self._reply(status, body, ctype)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, name="fake-viya", daemon=True).start()

    def route(self, method: str, path: str) -> tuple[int, object, str]:
        js = "application/json"
        gone = (404, {"errorCode": 5837, "message": "A session with the ID could not be found."}, js)
        if method == "GET" and path == "/compute/contexts":
            return 200, {"items": [{"id": "ctx1", "name": "SAS Studio compute context"}]}, js
        if method == "POST" and path == "/compute/contexts/ctx1/sessions":
            sid = f"s{next(self._ids)}"
            with self._lock:
                self.compute.add(sid)
                self.created += 1
            return 201, {"id": sid}, js
        m = re.fullmatch(r"/compute/sessions/([^/]+)(/.*)?", path)
        if m:
            sid, rest = m.group(1), m.group(2) or ""
            if sid not in self.compute:
                return gone
            if method == "DELETE" and not rest:
                self.compute.discard(sid)
                return 204, None, js
            if method == "POST" and rest == "/jobs":
                return 201, {"id": f"j{next(self._ids)}"}, js
            if rest == "/state":
                return 200, "idle", "text/plain"
            if rest.endswith("/state"):
                return 200, "completed", "text/plain"
            if rest.endswith("/log") or rest.endswith("/listing"):
                return 200, {"items": [{"line": "NOTE: fake job ran.", "type": "note"}]}, js
            if method == "DELETE":
                return 204, None, js
            return 200, {"items": []}, js
        if method == "POST" and path == "/cas-shared-default-http/cas/sessions":
            sid = f"c{next(self._ids)}"
            with self._lock:
                self.cas.add(sid)
            return 200, {"session": sid}, js
        m = re.fullmatch(r"/cas-shared-default-http/cas/sessions/([^/]+)(/.*)?", path)
        if m:
            if m.group(1) not in self.cas:
                return 404, {"message": "session not found"}, js
            if method == "POST":
                return 200, {"results": {}, "disposition": {"severity": "Normal"}, "log": "NOTE: fake action."}, js
            return 200, {"session": m.group(1)}, js
        return 404, {"message": f"no route {method} {path}"}, js

    def forget_sessions(self) -> None:
        with self._lock:
            self.compute.clear()
            self.cas.clear()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
