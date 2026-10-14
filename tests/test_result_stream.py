"""SQL Editor streamed results (singlestore_mcp.result_stream), offline: db.stream is replaced by a fake that
yields chunks, so the paging, limits, Stop and history hook are tested without a cluster."""

from __future__ import annotations

import threading
import time

import pytest

from singlestore_mcp import result_stream as rs


class FakeStream:
    """Stands in for db.stream: yields (columns, []) then chunks of n rows; can block between chunks."""

    def __init__(self, n_rows: int, chunk: int = 1000, gate: threading.Event | None = None, error_at: int | None = None,
                 width: int = 10) -> None:
        self.n, self.chunk, self.gate, self.error_at, self.width = n_rows, chunk, gate, error_at, width
        self.closed = False
        self.sent = 0

    def __call__(self, sql, database=None, max_rows=None, chunk=1000, on_connection=None):
        self.max_rows = max_rows
        if on_connection:
            on_connection(4242)
        return self._gen()

    def _gen(self):
        try:
            yield ["id", "txt"], []
            while self.sent < self.n:
                if self.gate is not None:
                    self.gate.wait(5)
                if self.error_at is not None and self.sent >= self.error_at:
                    raise RuntimeError("connection lost")
                k = min(self.chunk, self.n - self.sent)
                rows = [{"id": self.sent + i, "txt": "x" * self.width} for i in range(k)]
                self.sent += k
                yield ["id", "txt"], rows
        finally:
            self.closed = True


@pytest.fixture(autouse=True)
def _clean():
    rs._streams.clear()
    yield
    for s in list(rs._streams.values()):
        s.stop()
    rs._streams.clear()


def _done(s: rs.Stream, timeout: float = 5) -> None:
    assert s.finished.wait(timeout), "stream didn't finish"


def test_first_page_then_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    fake = FakeStream(5000, gate=gate)
    monkeypatch.setattr(rs.db, "stream", fake)
    gate.set()  # let the first chunk through, then hold the rest
    s = rs.start("SELECT 1", "SASDP", stream_id="r_test_1")
    s.wait_first(1000, timeout=5)
    assert len(s.rows) >= 1000 and s.columns == ["id", "txt"]
    _done(s)
    st = s.status()
    assert st["done"] and st["loaded"] == 5000 and not st["capped"] and st["first_ms"] is not None
    assert fake.max_rows == rs.MAX_ROWS + 1  # sql_select_limit for statements without a LIMIT
    assert [r["id"] for r in s.page(4990, 100)] == list(range(4990, 5000))
    assert s.page(10, 0) == []  # progress only


def test_row_cap_and_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeStream(50_000)
    monkeypatch.setattr(rs.db, "stream", fake)
    s = rs.start("SELECT 1", None, max_rows=12_345)
    _done(s)
    assert s.status()["loaded"] == 12_345 and s.capped and fake.closed and fake.sent < 50_000

    monkeypatch.setattr(rs, "MAX_BYTES", 200_000)
    fake = FakeStream(50_000, width=1000)
    monkeypatch.setattr(rs.db, "stream", fake)
    s = rs.start("SELECT 1", None)
    _done(s)
    assert s.capped and 0 < s.bytes <= 200_000 and len(s.rows) < 1000


def test_pages_stay_under_the_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs.db, "stream", FakeStream(3000, width=5000))
    monkeypatch.setattr(rs, "PAGE_BYTES", 100_000)
    s = rs.start("SELECT 1", None)
    _done(s)
    page = s.page(0, 5000)
    assert 0 < len(page) < 25


def test_stop_keeps_the_rows_so_far(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    fake = FakeStream(10_000, gate=gate)
    monkeypatch.setattr(rs.db, "stream", fake)
    killed = []
    monkeypatch.setattr(rs, "_kill", lambda conn_id, sql: killed.append(conn_id) or True)
    s = rs.start("SELECT 1", None)
    assert s.stop() is True and killed == [4242]
    gate.set()
    _done(s)
    st = s.status()
    assert st["stopped"] and st["done"] and st["error"] is None and st["loaded"] < 10_000 and fake.closed
    assert s.stop() is False  # already done


def test_error_mid_stream_and_history_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs.db, "stream", FakeStream(5000, error_at=2000))
    seen = []
    s = rs.start("SELECT 1", None, on_finish=lambda st: seen.append((len(st.rows), str(st.error))))
    _done(s)
    assert s.status()["error"] == "connection lost" and len(s.rows) == 2000
    assert seen == [(2000, "connection lost")]


def test_replace_drop_and_memory_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs.db, "stream", lambda *a, **kw: FakeStream(2000)(*a, **kw))
    a = rs.start("SELECT 1", None, stream_id="r_aaaaaa")
    _done(a)
    b = rs.start("SELECT 2", None, stream_id="r_bbbbbb", replaces="r_aaaaaa")
    _done(b)
    assert b.bytes > 0
    with pytest.raises(LookupError):
        rs.get("r_aaaaaa")
    assert rs.get("r_bbbbbb") is b
    # Over the budget: the least recently used finished results go first.
    monkeypatch.setattr(rs, "TOTAL_BYTES", int(b.bytes * 2.5))
    c = rs.start("SELECT 3", None, stream_id="r_cccccc")
    _done(c)
    rs.get("r_bbbbbb")  # touched: now c is the older one
    d = rs.start("SELECT 4", None, stream_id="r_dddddd")
    _done(d)
    time.sleep(0.05)
    rs._enforce_limits()
    kept = set(rs._streams)
    assert "r_dddddd" in kept and "r_bbbbbb" in kept and "r_cccccc" not in kept


def test_stream_ids_are_checked() -> None:
    with pytest.raises(ValueError):
        rs.start("SELECT 1", None, stream_id="../x")


def test_duplicate_columns_take_the_row_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp.apps import sql_editor

    s = rs.Stream("r_dupes1", "SELECT a.x, b.x", None, 10)
    s.columns = ["x", "x"]
    cols, warnings = sql_editor._stream_columns(s, [{"x": 1, "b.x": 2}])
    assert cols == ["x", "b.x"] and "Duplicate column names (x)" in warnings[0]
