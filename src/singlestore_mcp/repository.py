"""The Studio repository: the collector's stores, kept in a SingleStore database (``SSSTUDIO``) for the server version.

The collector (``collector``) runs the same stores as the desktop - ``history_store`` (query_history.db: runs, plans,
plan_samples) and ``metrics_store`` (metrics.db: metrics) - on SQLite files on the pod volume, and this module keeps
a copy of every row in the repository database:

- **Write-through with a buffer.** Triggers in the SQLite files note each inserted or updated row in an ``_outbox``
  table; ``flush`` upserts those rows into SingleStore (``INSERT … ON DUPLICATE KEY UPDATE`` on the same primary
  keys) and then clears them. While the cluster is down (the repository lives in the cluster it watches) the outbox
  just grows, and the next flush catches up. The Studios read from the collector's SQLite files: fast, and still
  there when the cluster isn't.
- **Restore.** A collector that starts with empty files (a new pod volume) first copies the repository back into them
  (within the retention), so nothing is lost with the volume.
- **Retention** on both sides (``retain``): query runs and plans 90 days, plan-cache counter samples 30 days, raw
  metrics 30 days and 10-minute metrics 1 year (the SQLite side: ``history_store.prune`` and ``metrics_store``'s
  compaction; deleted SQLite rows aren't sent, so the same cuts run on SingleStore).

Only the service account (``svc_studio_collector``: ALL on the repository, PROCESS + SHOW METADATA to collect) reads or
writes the repository; users get no grants on it. Column types suit SQL and SAS readers (DATETIME(6) for times,
DOUBLE for measures); names are those of the SQLite stores, so ``user``, ``database`` and ``rows`` need backticks.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKER = "/* s2-repository */"
SCHEMA_VERSION = 1
CHUNK = 500                # rows per INSERT
FLUSH_MAX = 20_000         # outbox entries per flush

DEFAULT_RETENTION = {"runs_days": 90, "plans_days": 90, "samples_days": 30, "metrics_raw_days": 30, "metrics_days": 365}


@dataclass(frozen=True)
class Table:
    name: str
    file: str                         # SQLite file in the collector's home
    pk: tuple[str, ...]
    columns: tuple[tuple[str, str], ...]   # (name, SingleStore type)
    sort: str
    times: tuple[str, ...] = ()       # DATETIME columns (strings in SQLite)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c for c, _ in self.columns)


_V = "VARCHAR(255)"
TABLES: tuple[Table, ...] = (
    Table("runs", "query_history.db", ("source", "event_key"), (
        ("source", f"{_V} NOT NULL"), ("event_key", "VARCHAR(80) NOT NULL"), ("finished", "DATETIME(6) NOT NULL"),
        ("ms", "BIGINT NOT NULL"), ("user", _V), ("database", _V), ("ok", "TINYINT NOT NULL"), ("error_code", "TEXT"),
        ("category", "TEXT"), ("rows", "BIGINT"), ("shape", "VARCHAR(16) NOT NULL"), ("sample", "TEXT"),
        ("origin", "TEXT"), ("tables", "TEXT"), ("plan_id", "BIGINT"), ("stmt", "VARCHAR(32)")),
        sort="finished", times=("finished",)),
    Table("plans", "query_history.db", ("source", "plan_key"), (
        ("source", f"{_V} NOT NULL"), ("plan_key", "VARCHAR(128) NOT NULL"), ("node_id", "INT"), ("plan_id", "BIGINT"),
        ("database", _V), ("first_load", "DATETIME(6)"), ("last_executed", "DATETIME(6)"), ("runs", "BIGINT"),
        ("failed", "BIGINT"), ("rows", "BIGINT"), ("exec_ms", "DOUBLE"), ("avg_ms", "DOUBLE"), ("cpu_ms", "DOUBLE"),
        ("avg_mem", "DOUBLE"), ("avg_max_mem", "DOUBLE"), ("avg_spill", "DOUBLE"), ("avg_blob_miss", "DOUBLE"),
        ("blob_wait_ms", "DOUBLE"), ("queued_ms", "DOUBLE"), ("warnings", "TEXT"), ("text", "TEXT"), ("text_len", "INT"),
        ("stmt", "VARCHAR(32)"), ("tables", "TEXT"), ("origin", "TEXT"), ("seen", "DATETIME(6)")),
        sort="last_executed", times=("first_load", "last_executed", "seen")),
    Table("plan_samples", "query_history.db", ("source", "plan_key", "t"), (
        ("source", f"{_V} NOT NULL"), ("plan_key", "VARCHAR(128) NOT NULL"), ("t", "DATETIME(6) NOT NULL"),
        ("runs", "BIGINT"), ("exec_ms", "DOUBLE")),
        sort="t", times=("t",)),
    Table("metrics", "metrics.db", ("source", "node_id", "res", "t"), (
        ("source", f"{_V} NOT NULL"), ("node_id", "INT NOT NULL"), ("t", "BIGINT NOT NULL"), ("res", "INT NOT NULL"),
        ("host", _V), ("type", "VARCHAR(32)"), ("n", "INT NOT NULL"),
        *((c, "DOUBLE") for c in ("cpu", "cpu_max", "host_cpu", "mem_b", "mem_max_b", "disk_used_b", "disk_total_b",
                                  "queued", "queued_max", "running", "conns"))),
        sort="t"),
)
_BY_NAME = {t.name: t for t in TABLES}


def _row_errors() -> tuple[type[BaseException], ...]:
    """Errors about a row's values (not the connection)."""
    from singlestoredb import exceptions as e

    return (e.DataError, e.IntegrityError)


def _q(name: str) -> str:
    return f"`{name}`"


def ddl(t: Table) -> str:
    cols = ", ".join(f"{_q(c)} {kind}" for c, kind in t.columns)
    return (f"CREATE TABLE IF NOT EXISTS {_q(t.name)} ({cols}, PRIMARY KEY ({', '.join(map(_q, t.pk))}),"
            f" SORT KEY ({_q(t.sort)}))")


def _to_s2(t: Table, row: sqlite3.Row) -> list[Any]:
    return [row[c] for c in t.names]


def _to_sqlite(t: Table, row: dict[str, Any]) -> list[Any]:
    out = []
    for c in t.names:
        v = row.get(c)
        if isinstance(v, _dt.datetime):
            v = v.isoformat(sep=" ")
        elif isinstance(v, _dt.date):
            v = v.isoformat()
        out.append(v)
    return out


class Repository:
    """The repository database, reached with the service account (``connect_kwargs``: the singlestoredb.connect
    arguments, with the database), and the collector's SQLite files under ``home``."""

    def __init__(self, connect_kwargs: dict[str, Any], database: str, home: Path,
                 retention: dict[str, int] | None = None) -> None:
        self.kwargs = {**connect_kwargs, "database": database}
        self.database = database
        self.home = Path(home)
        self.retention = {**DEFAULT_RETENTION, **(retention or {})}
        self._conn: Any = None
        self._lock = threading.Lock()
        self.status: dict[str, Any] = {"database": database, "last_flush": None, "last_error": None, "sent": 0, "skipped": 0,
                                       "pending": None, "restored": None, "last_retain": None}

    # ------------------------------------------------------------ SingleStore
    def _cursor(self) -> Any:
        import singlestoredb

        if self._conn is None:
            self._conn = singlestoredb.connect(**self.kwargs)
        return self._conn.cursor()

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _run(self, fn: Any) -> Any:
        """``fn(cursor)`` with one reconnect when the connection went away (the cluster restarted, a network break)."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    cur = self._cursor()
                    try:
                        return fn(cur)
                    finally:
                        cur.close()
                except Exception:
                    self._drop_conn()
                    if attempt == 2:
                        raise
        return None

    def ensure_schema(self) -> None:
        def run(cur: Any) -> None:
            for t in TABLES:
                cur.execute(f"{MARKER} {ddl(t)}")
            cur.execute(f"{MARKER} CREATE TABLE IF NOT EXISTS `_schema` (`k` VARCHAR(32) NOT NULL, `v` VARCHAR(255),"
                        " PRIMARY KEY (`k`))")
            cur.execute(f"{MARKER} REPLACE INTO `_schema` VALUES ('version', %s)", (str(SCHEMA_VERSION),))

        self._run(run)

    def close(self) -> None:
        with self._lock:
            self._drop_conn()

    # ------------------------------------------------------------ SQLite side
    def _sqlite(self, file: str) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.home / file), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _files(self) -> list[str]:
        return sorted({t.file for t in TABLES})

    def install_triggers(self) -> None:
        """The outbox and its triggers in each SQLite file (the stores have created their tables by then)."""
        for file in self._files():
            conn = self._sqlite(file)
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS _outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, tbl TEXT NOT NULL,"
                             " rid INTEGER NOT NULL)")
                for t in TABLES:
                    if t.file != file:
                        continue
                    for event in ("INSERT", "UPDATE"):
                        conn.execute(f"CREATE TRIGGER IF NOT EXISTS _outbox_{t.name}_{event.lower()} AFTER {event} ON {t.name}"
                                     f" BEGIN INSERT INTO _outbox (tbl, rid) VALUES ('{t.name}', NEW.rowid); END")
                conn.commit()
            finally:
                conn.close()

    def drop_triggers(self) -> None:
        for file in self._files():
            conn = self._sqlite(file)
            try:
                for t in TABLES:
                    if t.file == file:
                        for event in ("insert", "update"):
                            conn.execute(f"DROP TRIGGER IF EXISTS _outbox_{t.name}_{event}")
                conn.commit()
            finally:
                conn.close()

    def pending(self) -> int:
        n = 0
        for file in self._files():
            conn = self._sqlite(file)
            try:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = '_outbox'").fetchone():
                    n += conn.execute("SELECT COUNT(*) FROM _outbox").fetchone()[0]
            finally:
                conn.close()
        return n

    # ------------------------------------------------------------ write-through
    def _upsert(self, t: Table, rows: list[list[Any]]) -> None:
        cols = ", ".join(map(_q, t.names))
        one = "(" + ", ".join(["%s"] * len(t.names)) + ")"
        update = ", ".join(f"{_q(c)} = VALUES({_q(c)})" for c in t.names if c not in t.pk)

        sql = f"{MARKER} INSERT INTO {_q(t.name)} ({cols}) VALUES {{}} ON DUPLICATE KEY UPDATE {update}"

        def run(cur: Any) -> None:
            for i in range(0, len(rows), CHUNK):
                part = rows[i:i + CHUNK]
                try:
                    cur.execute(sql.format(", ".join([one] * len(part))), [v for r in part for v in r])
                except _row_errors():
                    # One bad row (a value the column can't take) mustn't hold up the outbox: send the rest one by
                    # one and count what is left out. Connection errors still raise (the rows stay noted).
                    for r in part:
                        try:
                            cur.execute(sql.format(one), r)
                        except _row_errors() as exc:
                            self.status["skipped"] += 1
                            self.status["last_skipped"] = f"{t.name}: {str(exc)[:200]}"

        self._run(run)

    def flush(self) -> dict[str, int]:
        """Send the rows noted in the outboxes; raises when the repository can't be reached (they stay noted)."""
        sent: dict[str, int] = {}
        try:
            for file in self._files():
                conn = self._sqlite(file)
                try:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = '_outbox'").fetchone():
                        continue
                    noted = conn.execute("SELECT id, tbl, rid FROM _outbox ORDER BY id LIMIT ?", (FLUSH_MAX,)).fetchall()
                    if not noted:
                        continue
                    by_table: dict[str, set[int]] = {}
                    for r in noted:
                        by_table.setdefault(r["tbl"], set()).add(r["rid"])
                    for name, rids in by_table.items():
                        t = _BY_NAME.get(name)
                        if t is None:
                            continue
                        ids = sorted(rids)
                        rows: list[list[Any]] = []
                        for i in range(0, len(ids), CHUNK):
                            part = ids[i:i + CHUNK]
                            rows += [_to_s2(t, r) for r in conn.execute(
                                f"SELECT {', '.join(t.names)} FROM {t.name} WHERE rowid IN ({', '.join('?' * len(part))})", part)]
                        if rows:  # rows deleted since (compaction, retention) aren't sent
                            self._upsert(t, rows)
                        sent[name] = sent.get(name, 0) + len(rows)
                    conn.execute("DELETE FROM _outbox WHERE id <= ?", (noted[-1]["id"],))
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            self.status["last_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
            self.status["pending"] = self.pending()
            raise
        self.status.update(last_flush=round(time.time(), 1), last_error=None,
                           sent=self.status["sent"] + sum(sent.values()), pending=self.pending())
        return sent

    # ------------------------------------------------------------ restore
    def _cutoff(self, t: Table, now: float) -> tuple[str, Any] | None:
        r = self.retention
        at = lambda days: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - days * 86400))  # noqa: E731
        if t.name == "runs":
            return "finished", at(r["runs_days"])
        if t.name == "plans":
            return "last_executed", at(r["plans_days"])
        if t.name == "plan_samples":
            return "t", at(r["samples_days"])
        if t.name == "metrics":
            return "t", int(now - r["metrics_days"] * 86400)
        return None

    def local_empty(self) -> bool:
        for t in TABLES:
            conn = self._sqlite(t.file)
            try:
                if conn.execute(f"SELECT 1 FROM {t.name} LIMIT 1").fetchone():
                    return False
            finally:
                conn.close()
        return True

    def restore(self, now: float | None = None) -> dict[str, int]:
        """Copy the repository (within the retention) into empty SQLite files. The triggers are off meanwhile, so
        nothing restored is sent back."""
        now = time.time() if now is None else now
        self.drop_triggers()
        got: dict[str, int] = {}
        for t in TABLES:
            cut = self._cutoff(t, now)

            def read(cur: Any, t: Table = t, cut: Any = cut) -> list[dict[str, Any]]:
                where, params = (f" WHERE {_q(cut[0])} >= %s", (cut[1],)) if cut else ("", ())
                cur.execute(f"{MARKER} SELECT {', '.join(map(_q, t.names))} FROM {_q(t.name)}{where}", params)
                return list(cur.fetchall())

            rows = self._run(read) or []
            conn = self._sqlite(t.file)
            try:
                conn.executemany(f"INSERT OR REPLACE INTO {t.name} ({', '.join(t.names)}) VALUES"
                                 f" ({', '.join('?' * len(t.names))})", [_to_sqlite(t, r) for r in rows])
                conn.commit()
            finally:
                conn.close()
            got[t.name] = len(rows)
        self.status["restored"] = {"at": round(time.time(), 1), **got}
        return got

    # ------------------------------------------------------------ retention
    def retain(self, now: float | None = None) -> dict[str, int]:
        """The retention on the SingleStore side (the SQLite side prunes and compacts itself)."""
        now = time.time() if now is None else now
        r = self.retention
        raw_cut = int((now - r["metrics_raw_days"] * 86400) // 600 * 600)
        out: dict[str, int] = {}

        def run(cur: Any) -> None:
            for t in TABLES:
                cut = self._cutoff(t, now)
                if cut:
                    cur.execute(f"{MARKER} DELETE FROM {_q(t.name)} WHERE {_q(cut[0])} < %s", (cut[1],))
                    out[t.name] = cur.rowcount
            # Raw metrics are folded into 10-minute rows after metrics_raw_days (sent as new rows): drop the raw ones.
            cur.execute(f"{MARKER} DELETE FROM `metrics` WHERE `res` <> 600 AND `t` < %s", (raw_cut,))
            out["metrics_raw"] = cur.rowcount

        self._run(run)
        self.status["last_retain"] = {"at": round(time.time(), 1), **out}
        return out
