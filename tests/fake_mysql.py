"""A tiny MySQL-protocol server for offline tests of the connection pool (no cluster needed).

Speaks just enough of the MySQL client/server protocol (4.1, ``mysql_native_password``, no TLS) for the
``singlestoredb`` driver: any user / password logs in; ``COM_PING`` and ``COM_INIT_DB`` answer OK; queries:

* ``SELECT SLEEP(n)`` waits ``n`` seconds, then returns one row ``0`` (any other statement with ``SLEEP(n)`` in it,
  e.g. ``DO SLEEP(n)``, waits and answers OK: a write that takes a while);
* any other ``SELECT`` / ``SHOW`` returns one column ``v`` with one row: the server's ``answer`` (default ``"1"``);
* everything else (``SET``, ``USE``, …) answers OK.

``queries`` records the statements received (``SET`` statements of the driver included).
"""

from __future__ import annotations

import re
import socket
import struct
import threading
import time

_CAPS = (0x00000001 | 0x00000004 | 0x00000008 | 0x00000200 | 0x00002000 | 0x00008000 | 0x00020000 | 0x00080000)
_SLEEP = re.compile(r"\bSLEEP\(\s*([\d.]+)\s*\)", re.I)


def _lenenc_int(n: int) -> bytes:
    if n < 251:
        return bytes([n])
    if n < 1 << 16:
        return b"\xfc" + struct.pack("<H", n)
    if n < 1 << 24:
        return b"\xfd" + struct.pack("<I", n)[:3]
    return b"\xfe" + struct.pack("<Q", n)


def _lenenc_str(b: bytes) -> bytes:
    return _lenenc_int(len(b)) + b


class FakeMySQL:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.answer = "1"
        self.queries: list[str] = []
        self.connections = 0
        self._next_id = 100
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind((host, 0))
        self._sock.listen(64)
        self.host, self.port = self._sock.getsockname()[:2]
        self._stop = False
        threading.Thread(target=self._accept, name="fake-mysql", daemon=True).start()

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------ plumbing
    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                self._next_id += 1
                cid = self._next_id
            threading.Thread(target=self._serve, args=(conn, cid), daemon=True).start()

    @staticmethod
    def _read(conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _packet(self, conn: socket.socket) -> tuple[int, bytes]:
        head = self._read(conn, 4)
        length = head[0] | head[1] << 8 | head[2] << 16
        return head[3], self._read(conn, length)

    @staticmethod
    def _send(conn: socket.socket, seq: int, payload: bytes) -> int:
        conn.sendall(struct.pack("<I", len(payload))[:3] + bytes([seq & 0xFF]) + payload)
        return seq + 1

    @staticmethod
    def _ok() -> bytes:
        return b"\x00\x00\x00" + struct.pack("<HH", 0x0002, 0)

    @staticmethod
    def _eof() -> bytes:
        return b"\xfe" + struct.pack("<HH", 0, 0x0002)

    @staticmethod
    def _column(name: str) -> bytes:
        return (_lenenc_str(b"def") + _lenenc_str(b"") + _lenenc_str(b"") + _lenenc_str(b"") + _lenenc_str(name.encode())
                + _lenenc_str(name.encode()) + b"\x0c" + struct.pack("<HIBHB", 33, 255, 0xFD, 0, 0) + b"\x00\x00")

    def _result(self, conn: socket.socket, seq: int, value: str) -> None:
        seq = self._send(conn, seq, _lenenc_int(1))
        seq = self._send(conn, seq, self._column("v"))
        seq = self._send(conn, seq, self._eof())
        seq = self._send(conn, seq, _lenenc_str(value.encode()))
        self._send(conn, seq, self._eof())

    def _serve(self, conn: socket.socket, cid: int) -> None:
        try:
            salt = b"abcdefgh" + b"ijklmnopqrst"
            greeting = (b"\x0a" + b"5.7.32-fake\x00" + struct.pack("<I", cid) + salt[:8] + b"\x00"
                        + struct.pack("<H", _CAPS & 0xFFFF) + b"\x21" + struct.pack("<H", 0x0002)
                        + struct.pack("<H", _CAPS >> 16) + bytes([21]) + b"\x00" * 10 + salt[8:] + b"\x00"
                        + b"mysql_native_password\x00")
            self._send(conn, 0, greeting)
            seq, _login = self._packet(conn)
            self._send(conn, seq + 1, self._ok())
            while True:
                _, pkt = self._packet(conn)
                cmd, body = pkt[0], pkt[1:]
                if cmd == 0x01:  # COM_QUIT
                    return
                if cmd == 0x03:  # COM_QUERY
                    sql = body.decode("utf-8", "replace")
                    with self._lock:
                        self.queries.append(sql)
                    m = _SLEEP.search(sql)
                    if m:
                        time.sleep(float(m.group(1)))
                    if m and re.match(r"^\s*SELECT\b", sql, re.I):
                        self._result(conn, 1, "0")
                    elif re.match(r"^\s*(SELECT|SHOW)\b", sql, re.I) and not m:
                        self._result(conn, 1, self.answer)
                    else:
                        self._send(conn, 1, self._ok())
                else:  # COM_PING, COM_INIT_DB, anything else
                    self._send(conn, 1, self._ok())
        except (EOFError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def __enter__(self) -> FakeMySQL:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
