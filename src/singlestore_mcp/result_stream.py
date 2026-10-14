"""Streamed query results for the SQL Editor.

A read-only statement runs in a background thread on an unbuffered cursor
(``db.stream``): the editor gets the first page as soon as those rows have
arrived, while the rest keeps loading here; the grid then pages through the
result (``page``), with the total once it's known, and Stop cancels it.

Memory is bounded: a result keeps at most ``MAX_ROWS`` rows and about
``MAX_BYTES`` (estimated) - beyond that it's cut off and flagged ``capped``
(the statement is ended on the cluster) - and all kept results together stay
under ``TOTAL_BYTES`` (the least recently used finished ones are dropped).
A result nobody asked about for ``IDLE_SECONDS`` is dropped too.

Stop: KILL QUERY on the statement's connection, sent only after checking on
PROCESSLIST that this connection still runs this very statement (connection
ids are per aggregator), plus closing the stream.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from .db import db

FIRST_PAGE = 1000
CHUNK = 1000
MAX_ROWS = max(1000, int(os.environ.get("SINGLESTORE_MCP_EDITOR_MAX_ROWS", "100000")))
MAX_BYTES = 128 * 1024 * 1024
TOTAL_BYTES = 384 * 1024 * 1024
PAGE_BYTES = 2 * 1024 * 1024   # one page sent to the browser
MAX_PAGE_ROWS = 20_000
IDLE_SECONDS = 20 * 60
_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def row_bytes(row: dict[str, Any]) -> int:
    """Rough size of a row as JSON (good enough for the memory limits)."""
    n = 2
    for k, v in row.items():
        n += len(k) + 6 + (len(v) if isinstance(v, str) else 4 if v is None else len(str(v)))
    return n


class Stream:
    def __init__(self, stream_id: str, sql: str, database: str | None, max_rows: int) -> None:
        self.id = stream_id
        self.sql = sql
        self.database = database
        self.max_rows = max_rows
        self.columns: list[str] | None = None
        self.rows: list[dict[str, Any]] = []
        self.bytes = 0
        self.done = False
        self.capped = False      # more rows exist than were kept (MAX_ROWS / MAX_BYTES)
        self.stopped = False     # the user pressed Stop
        self.error: BaseException | None = None
        self.started = time.perf_counter()
        self.first_ms: float | None = None
        self.total_ms: float | None = None
        self.last_access = time.monotonic()
        self.conn_id: int | None = None
        self.cond = threading.Condition()
        self.on_finish: Callable[[Stream], None] | None = None
        self.finished = threading.Event()  # set after on_finish (the history entry) has run

    # ------------------------------------------------------------ reading (background thread)
    def _run(self) -> None:
        from .apps._core import jsonable_rows  # here: the apps package imports this module

        gen = db.stream(self.sql, self.database, max_rows=self.max_rows + 1, chunk=CHUNK,
                        on_connection=self._set_conn)
        try:
            for columns, rows in gen:
                with self.cond:
                    if self.columns is None:
                        self.columns = columns
                    if rows:
                        clean = jsonable_rows(rows)
                        room = self.max_rows - len(self.rows)
                        if len(clean) > room:
                            clean, self.capped = clean[:room], True
                        size = 0
                        for i, r in enumerate(clean):
                            size += row_bytes(r)
                            if self.bytes + size > MAX_BYTES:
                                clean, self.capped = clean[:i], True
                                break
                        self.rows.extend(clean)
                        self.bytes += sum(row_bytes(r) for r in clean)
                    if self.first_ms is None and (len(self.rows) >= FIRST_PAGE or not columns):
                        self.first_ms = self._ms()
                    self.cond.notify_all()
                    if self.capped or self.stopped:
                        break
        except BaseException as exc:  # noqa: BLE001 - reported to the editor
            with self.cond:
                if not self.stopped:
                    self.error = exc
        finally:
            gen.close()  # stops reading: the rest is drained or the connection closed (db.stream)
            with self.cond:
                self.done = True
                self.total_ms = self._ms()
                if self.first_ms is None:
                    self.first_ms = self.total_ms
                self.cond.notify_all()
            _enforce_limits()
            if self.on_finish:
                try:
                    self.on_finish(self)
                except Exception:  # noqa: BLE001 - history is best effort
                    pass
            self.finished.set()

    def _set_conn(self, conn_id: int) -> None:
        self.conn_id = conn_id

    def _ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)

    # ------------------------------------------------------------ for the tools
    def wait_first(self, rows: int = FIRST_PAGE, timeout: float | None = None) -> None:
        """Until ``rows`` rows are in, or the result is complete (or failed / stopped)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while not self.done and len(self.rows) < rows:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return
                self.cond.wait(timeout=left if left is not None else 1.0)

    def status(self) -> dict[str, Any]:
        self.last_access = time.monotonic()
        with self.cond:
            return {
                "stream_id": self.id,
                "loaded": len(self.rows),        # rows read so far (all of them once done)
                "done": self.done,
                "capped": self.capped,
                "stopped": self.stopped,
                "error": _error_text(self.error) if self.error else None,
                "first_ms": self.first_ms,
                "total_ms": self.total_ms,
                "elapsed_ms": self.total_ms if self.done else self._ms(),
                "max_rows": self.max_rows,
                "bytes": self.bytes,
            }

    def page(self, offset: int, limit: int) -> list[dict[str, Any]]:
        """Rows from ``offset``: at most ``limit`` and about PAGE_BYTES."""
        self.last_access = time.monotonic()
        offset, limit = max(0, int(offset)), max(0, min(int(limit), MAX_PAGE_ROWS))  # 0: progress only
        with self.cond:
            out, size = [], 0
            for r in self.rows[offset:offset + limit]:
                size += row_bytes(r)
                if out and size > PAGE_BYTES:
                    break
                out.append(r)
            return out

    def stop(self) -> bool:
        """Stop reading; cancel the statement on the cluster if it's still running. True if it was running."""
        with self.cond:
            if self.done:
                return False
            self.stopped = True
            self.cond.notify_all()
            conn_id = self.conn_id
        if conn_id:
            try:
                _kill(conn_id, self.sql)
            except Exception:  # noqa: BLE001 - the reader stops at its next chunk anyway
                pass
        return True


def _error_text(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 2 and isinstance(args[0], int):
        return f"SingleStore error {args[0]}: {args[1]}"
    return str(exc) or type(exc).__name__


def _kill(conn_id: int, sql: str) -> bool:
    """KILL QUERY on ``conn_id`` if PROCESSLIST shows it still running this statement (ids are per
    aggregator, so a bare KILL from another connection could hit someone else's query)."""
    head = sql[:200]
    _, rows, _ = db.execute(
        "SELECT ID FROM information_schema.PROCESSLIST WHERE ID = %s AND LEFT(INFO, %s) = %s",
        (int(conn_id), len(head), head),
    )
    if not rows:
        return False
    db.execute(f"KILL QUERY {int(conn_id)}")
    return True


# ---------------------------------------------------------------- registry
_streams: OrderedDict[str, Stream] = OrderedDict()
_lock = threading.Lock()


def new_id() -> str:
    return "r" + secrets.token_hex(8)


def start(sql: str, database: str | None, stream_id: str | None = None, max_rows: int = MAX_ROWS,
          replaces: str | None = None, on_finish: Callable[[Stream], None] | None = None) -> Stream:
    """Start reading ``sql`` in the background. ``replaces``: the tab's previous result, stopped and dropped."""
    stream_id = stream_id or new_id()
    if not _ID.match(stream_id):
        raise ValueError("stream_id: 6-64 letters, digits, - or _")
    if replaces:
        drop(replaces)
    s = Stream(stream_id, sql, database, max(1, min(int(max_rows), MAX_ROWS)))
    s.on_finish = on_finish
    with _lock:
        old = _streams.pop(stream_id, None)
        _streams[stream_id] = s
    if old:
        old.stop()
    _enforce_limits()
    threading.Thread(target=s._run, name=f"stream-{stream_id}", daemon=True).start()
    return s


def get(stream_id: str) -> Stream:
    with _lock:
        s = _streams.get(stream_id)
        if s is not None:
            _streams.move_to_end(stream_id)
    if s is None:
        raise LookupError("This result is no longer kept on the server (too old, or memory was needed): run the query again.")
    return s


def find(stream_id: str) -> Stream | None:
    with _lock:
        return _streams.get(stream_id)


def drop(stream_id: str) -> None:
    with _lock:
        s = _streams.pop(stream_id, None)
    if s:
        s.stop()


def _enforce_limits() -> None:
    """Drop idle results, then the least recently used finished ones while all together are too big."""
    now = time.monotonic()
    with _lock:
        for sid, s in list(_streams.items()):
            if s.done and now - s.last_access > IDLE_SECONDS:
                del _streams[sid]
        total = sum(s.bytes for s in _streams.values())
        for sid, s in list(_streams.items()):  # oldest first
            if total <= TOTAL_BYTES:
                break
            if s.done:
                total -= s.bytes
                del _streams[sid]


def stats() -> dict[str, Any]:
    with _lock:
        return {"results": len(_streams), "bytes": sum(s.bytes for s in _streams.values()),
                "running": sum(not s.done for s in _streams.values())}
