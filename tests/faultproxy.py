"""A local TCP proxy that injects network faults, for the network-recovery tests (``test_network_recovery``).

Put it between a client and a server (``FaultProxy(target_host, target_port)``; connect to ``proxy.port``) and change
what the "network" does while connections are open:

* ``drop()``      – close every open connection at once (both sides see a reset / EOF): a VPN that drops.
* ``blackhole()`` – connections stop carrying data and never will again; new connections are accepted but nothing
  is forwarded (``accept_new=False`` leaves the listener closed instead). Like a VPN switch or a laptop that slept:
  the old TCP connections are dead, but nothing tells the client.
* ``refuse()``    – close the listener: new connections are refused (nothing listens), open ones keep working.
* ``slow(s)``     – every chunk waits ``s`` seconds before it's forwarded.
* ``restore()``   – new connections work again (connections that were blackholed stay dead, as in real life).
* ``move()``      – listen on a new port from now on (the old one refuses): the server "came back" elsewhere.

Counters: ``accepted`` (connections accepted so far), ``open_count()``.
"""

from __future__ import annotations

import socket
import threading
import time


class _Pair:
    def __init__(self, client: socket.socket, upstream: socket.socket | None) -> None:
        self.client, self.upstream = client, upstream
        self.dead = False      # blackholed: never forwards again
        self.closed = False

    def close(self) -> None:
        self.closed = True
        for s in (self.client, self.upstream):
            if s is None:
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


class FaultProxy:
    def __init__(self, target_host: str, target_port: int, host: str = "127.0.0.1", port: int = 0) -> None:
        self.target = (target_host, target_port)
        self.host = host
        self.mode = "pass"          # pass | blackhole
        self.delay = 0.0
        self.accepted = 0
        self._pairs: list[_Pair] = []
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._stop = False
        self.port = port
        self._listen(port)

    # ------------------------------------------------------------ listener
    def _listen(self, port: int) -> None:
        deadline = time.monotonic() + 5
        while True:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind((self.host, port))
                break
            except OSError:
                s.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        s.listen(64)
        self._listener = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._accept, args=(s,), name="faultproxy-accept", daemon=True).start()

    def _close_listener(self) -> None:
        s, self._listener = self._listener, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _accept(self, listener: socket.socket) -> None:
        while not self._stop:
            try:
                client, _ = listener.accept()
            except OSError:
                return  # listener closed
            self.accepted += 1
            if self.mode == "blackhole":
                pair = _Pair(client, None)
                pair.dead = True
                with self._lock:
                    self._pairs.append(pair)
                threading.Thread(target=self._swallow, args=(pair,), daemon=True).start()
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=5)
                upstream.settimeout(None)
            except OSError:
                client.close()
                continue
            pair = _Pair(client, upstream)
            with self._lock:
                self._pairs.append(pair)
            threading.Thread(target=self._pump, args=(pair, client, upstream), daemon=True).start()
            threading.Thread(target=self._pump, args=(pair, upstream, client), daemon=True).start()

    # ------------------------------------------------------------ data
    @staticmethod
    def _swallow(pair: _Pair) -> None:
        """A blackholed connection: read whatever the client sends (so its sends don't block) and answer nothing."""
        while not pair.closed:
            try:
                if not pair.client.recv(65536):
                    return
            except OSError:
                return

    def _pump(self, pair: _Pair, src: socket.socket, dst: socket.socket) -> None:
        while not pair.closed:
            try:
                data = src.recv(65536)
            except OSError:
                break
            if not data:
                break
            if pair.dead:
                continue  # swallowed: the network went away under this connection
            if self.delay:
                time.sleep(self.delay)
                if pair.dead:
                    continue
            try:
                dst.sendall(data)
            except OSError:
                break
        if not pair.dead:
            pair.close()

    # ------------------------------------------------------------ faults
    def drop(self) -> int:
        """Close every open connection now. Returns how many there were."""
        with self._lock:
            pairs, self._pairs = self._pairs, []
        for p in pairs:
            p.close()
        return len(pairs)

    def blackhole(self, accept_new: bool = True) -> None:
        self.mode = "blackhole"
        with self._lock:
            for p in self._pairs:
                p.dead = True
        if not accept_new:
            self._close_listener()

    def refuse(self) -> None:
        self._close_listener()

    def slow(self, seconds: float) -> None:
        self.delay = seconds

    def restore(self) -> None:
        self.mode, self.delay = "pass", 0.0
        if self._listener is None:
            self._listen(self.port)

    def move(self) -> int:
        """Listen on a new port (the old one is closed). Returns the new port."""
        self._close_listener()
        self.mode, self.delay = "pass", 0.0
        self._listen(0)
        return self.port

    def open_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._pairs if not p.closed)

    def close(self) -> None:
        self._stop = True
        self._close_listener()
        self.drop()

    def __enter__(self) -> FaultProxy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
