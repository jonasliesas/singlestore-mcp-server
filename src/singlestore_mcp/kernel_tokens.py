"""Fresh tokens for notebook kernels in server mode (``STUDIO_NOTEBOOKS=local``).

On a laptop a kernel asks for a fresh SAS Viya / SingleStore token by running ``python -m singlestore_mcp.sas_viya
token <name>``, which reads the user's secrets itself. On the server that can't work and mustn't: the user's secrets
file is encrypted with a key that only the user's Studio process holds (handed over by the hub, never on disk or in
an environment). So the Studio serves its own kernels instead:

- ``start()`` (in the Studio): a small HTTP server on 127.0.0.1 under a random secret path, answering
  ``GET /<secret>/sas/<connection>`` and ``GET /<secret>/s2/<connection>`` with a valid token of *this* user's
  connection (renewed when needed) - nothing else: no hub key, no secrets file key, no other user's anything.
- ``python -m singlestore_mcp.kernel_tokens sas|s2 <connection>`` (in the kernel, as its token command): asks that
  server (``SINGLESTORE_MCP_KERNEL_TOKENS`` = its base URL) and prints the token. Stdlib only, quick to start.

No isolation between users' kernels on a shared server (they run as one OS user): another user's code could read
this kernel's environment. That is the known limit of local notebooks (test use with a few trusted users only).

Kernel gateway (``STUDIO_NOTEBOOKS=gateway``, step 2a): the kernels run in another pod and can't reach 127.0.0.1 here.
They ask the hub's token relay instead (``hub/gateway.py``: ``<relay>/k/<kernel secret>/sas|s2/<connection>``), which
finds the kernel's user and asks that user's Studio's service above. The kernel's token command is then
``<kernel python> -I -c RELAY_CLIENT sas|s2 <connection>`` (the same client as ``_main``; the kernel's environment
has no singlestore_mcp). ``s2`` answers the connection's password for password logins (the gateway kernel gets no
password in its environment), its token otherwise.
"""

from __future__ import annotations

import secrets
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ENV = "SINGLESTORE_MCP_KERNEL_TOKENS"
_state: dict[str, Any] = {"server": None, "base": None}
_lock = threading.Lock()


def _token(kind: str, name: str) -> str:
    if kind == "sas":
        from . import sas_viya

        return sas_viya.token(name=name or None)
    if kind == "s2":
        from . import connections

        return connections.password_for(connections.get(name))
    raise LookupError(f"unknown token kind {kind!r}")


def start() -> str:
    """The base URL kernels ask (started once per Studio process)."""
    with _lock:
        if _state["base"]:
            return _state["base"]
        secret = secrets.token_urlsafe(32)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                parts = urllib.parse.urlsplit(self.path).path.split("/")
                status, body = 404, b"not found"
                if len(parts) == 4 and secrets.compare_digest(parts[1], secret):
                    try:
                        status, body = 200, _token(parts[2], urllib.parse.unquote(parts[3])).encode()
                    except Exception as exc:  # noqa: BLE001 - the kernel shows the reason (no secrets in it)
                        status, body = 400, str(exc)[:500].encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="kernel-tokens", daemon=True).start()
        _state["server"] = srv
        _state["base"] = f"http://127.0.0.1:{srv.server_address[1]}/{secret}"
        return _state["base"]


def stop() -> None:
    with _lock:
        srv, _state["server"], _state["base"] = _state["server"], None, None
    if srv is not None:
        srv.shutdown()
        srv.server_close()


def command(kind: str, name: str) -> list[str]:
    """The kernel's token command for one connection."""
    return [sys.executable, "-m", "singlestore_mcp.kernel_tokens", kind, name]


# The kernel's side: ask the token service (local) or the hub's relay (gateway) and print the token. Stdlib only,
# runs as ``python -I -c RELAY_CLIENT kind name`` in a kernel without singlestore_mcp.
RELAY_CLIENT = r"""
import os, sys, urllib.error, urllib.parse, urllib.request
def _relay(argv):
    if len(argv) != 2 or argv[0] not in ("sas", "s2"):
        print("usage: sas|s2 <connection>", file=sys.stderr)
        return 2
    base = os.environ.get("SINGLESTORE_MCP_KERNEL_TOKENS")
    if not base:
        print("This kernel has no token service (start it from Studio again).", file=sys.stderr)
        return 1
    url = f"{base}/{argv[0]}/{urllib.parse.quote(argv[1], safe='')}"
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=60) as res:
            print(res.read().decode())
        return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8", "replace") or f"HTTP {exc.code}", file=sys.stderr)
    except OSError as exc:
        print(f"Studio didn't answer the token request: {exc}", file=sys.stderr)
    return 1
if __name__ == "__main__":
    sys.exit(_relay(sys.argv[1:]))
"""


def _main(argv: list[str]) -> int:
    ns: dict[str, Any] = {"__name__": "kernel_tokens_client"}
    exec(RELAY_CLIENT, ns)  # noqa: S102 - our own constant source
    return ns["_relay"](argv)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
