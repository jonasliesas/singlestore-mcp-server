"""Connection handling for the SingleStore MCP server.

Built directly on top of ``singlestoredb``, SingleStore's own official Python
client (https://github.com/singlestore-labs/singlestoredb-python). It speaks
the MySQL wire protocol, so it works identically against SingleStore Helios
and against a self-managed SingleStore cluster -- just point it at a host,
port, user and password.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import singlestoredb as s2

from . import netguard


class ConfigurationError(RuntimeError):
    """Raised when required connection settings are missing."""


class InvalidIdentifierError(ValueError):
    """Raised when a database/pipeline identifier looks unsafe to interpolate."""


def quote_identifier(name: str) -> str:
    """Backtick-quote a SQL identifier, escaping embedded backticks.

    SingleStore (like MySQL) has no bind parameters for identifiers, so names
    are quoted with backticks and any backtick inside is doubled -- the
    standard escaping rule, which makes any input a single inert identifier.
    """
    if not name or "\x00" in name:
        raise InvalidIdentifierError(f"{name!r} is not a valid identifier")
    return "`" + name.replace("`", "``") + "`"


@dataclass
class ConnectionSettings:
    host: str
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str | None = None
    ssl_disabled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ConnectionSettings":
        url = os.environ.get("SINGLESTORE_URL")
        if url:
            # singlestoredb.connect accepts a full DB-API style URL directly,
            # e.g. "user:password@host:port/database". Connection details are
            # parsed by the driver, so no manual settings object is needed.
            return cls(host="", extra={"url": url})

        host = os.environ.get("SINGLESTORE_HOST")
        if not host:
            from . import server_mode

            if server_mode.enabled():
                raise ConfigurationError("No SingleStore connection yet: add one in Connections (+ New connection).")
            raise ConfigurationError(
                "Set SINGLESTORE_HOST (and SINGLESTORE_USER / "
                "SINGLESTORE_PASSWORD / SINGLESTORE_DATABASE), or set "
                "SINGLESTORE_URL to a full connection string. These are read "
                "from the MCP server's environment, e.g. the \"env\" block "
                "of your VS Code mcp.json entry."
            )

        ssl_disabled_raw = os.environ.get("SINGLESTORE_SSL_DISABLED", "")
        return cls(
            host=host,
            port=int(os.environ.get("SINGLESTORE_PORT", "3306")),
            user=os.environ.get("SINGLESTORE_USER", "root"),
            password=os.environ.get("SINGLESTORE_PASSWORD", ""),
            database=os.environ.get("SINGLESTORE_DATABASE") or None,
            ssl_disabled=ssl_disabled_raw.strip().lower() in ("1", "true", "yes"),
        )

    def connect_kwargs(self) -> dict[str, Any]:
        if "url" in self.extra:
            return {"host": self.extra["url"], "results_type": "dict"}
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "results_type": "dict",
            "ssl_disabled": self.ssl_disabled,
        }
        if self.database:
            kwargs["database"] = self.database
        return kwargs


# MySQL client error codes for a connection that is gone or unreachable.
_CONNECTION_LOST_CODES = {2003, 2006, 2013, 2055}
# A pooled connection idle this long is pinged (bounded) before it's used; after a network event (sleep / resume,
# another route, a failure) every idle connection is (netguard.epoch).
IDLE_PING_S = float(os.environ.get("SINGLESTORE_MCP_IDLE_PING", "300"))
# A call waits at most this long for a free pooled connection (all of them busy) before it gives up readably.
POOL_WAIT_S = float(os.environ.get("SINGLESTORE_MCP_POOL_WAIT", "120"))


class ConnectionLost(s2.OperationalError):
    """The cluster couldn't be reached, or the connection broke, also on the one retry (VPN / Wi-Fi gone)."""


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (s2.InterfaceError, OSError, ConnectionLost)):
        return True
    if isinstance(exc, s2.OperationalError):
        code = getattr(exc, "errno", None) or (exc.args[0] if exc.args else None)
        return code in _CONNECTION_LOST_CODES
    return False


def _lost(exc: BaseException) -> ConnectionLost:
    """The readable error for a connection problem that the retry didn't fix."""
    if isinstance(exc, ConnectionLost):
        return exc
    where = ""
    try:
        from . import connections

        p = connections.active()
        if p is not None and p.host:
            where = f" at {p.host}:{p.port}"
    except Exception:  # noqa: BLE001 - only for the message
        pass
    detail = getattr(exc, "errmsg", None) or str(exc) or type(exc).__name__
    return ConnectionLost(None, f"Lost the connection to SingleStore{where} (network or VPN down?): {detail}. The server "
                                "reconnects by itself as soon as the cluster can be reached again – no restart needed; "
                                "try again in a moment.")


def _raw(conn: Any) -> Any:
    return getattr(conn, "_conn", conn)


def open_connection(kwargs: dict[str, Any], **extra: Any) -> Any:
    """``s2.connect(**kwargs, **extra)`` that gives up after ``netguard.CONNECT_DEADLINE_S`` (a login that hangs after
    the TCP connect – a half-open network – would otherwise wait for good); the socket is watched (``netguard.track``)."""
    box: dict[str, Any] = {}
    lock = threading.Lock()
    done = threading.Event()

    def run() -> None:
        try:
            conn = s2.connect(**kwargs, **extra)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            box["error"] = exc
            done.set()
            return
        with lock:
            late = box.get("late")
            box["conn"] = conn
        done.set()
        if late:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=run, name="db-connect", daemon=True).start()
    if not done.wait(netguard.CONNECT_DEADLINE_S):
        with lock:
            box["late"] = True
            conn = box.get("conn")
        if conn is None:
            raise s2.OperationalError(2013, f"Connecting to SingleStore took longer than {netguard.CONNECT_DEADLINE_S:g} s "
                                            "(the network answers but the login doesn't finish)")
    if "error" in box:
        raise box["error"]
    conn = box["conn"]
    sock = getattr(_raw(conn), "_sock", None)
    if sock is not None:
        netguard.track(sock, "singlestore")
    return conn


def _unbuffered_cursor(raw: Any) -> Any:
    """An unbuffered (streaming) dict cursor on a pooled connection, which is otherwise buffered."""
    from singlestoredb.mysql import cursors

    cls = {cursors.DictCursorSV: cursors.SSDictCursorSV, cursors.DictCursor: cursors.SSDictCursor}.get(raw.cursorclass)
    if cls is None:
        raise RuntimeError(f"No streaming cursor for {raw.cursorclass.__name__}")
    return cls(raw)


DRAIN_SECONDS = 3.0  # a stream closed early: read the rest this long to keep the connection, else close it


def _drain_later(pool: "Database", pc: Any, sscur: Any) -> None:
    """Read and drop the rest of an abandoned unbuffered result in the background; give the connection
    back to the pool if that's quick, else close it (the cluster then ends the statement)."""
    import time

    def run() -> None:
        deadline = time.monotonic() + DRAIN_SECONDS
        try:
            while sscur.fetchmany(5000):
                if time.monotonic() > deadline:
                    raise TimeoutError
            sscur.close()
        except BaseException:  # noqa: BLE001 - too long or broken: close it
            pool._discard(pc)
            pool._release(None)
            return
        pool._release(pc)

    threading.Thread(target=run, name="db-drain", daemon=True).start()


# Statements that change session state behind our back (USE, SET ...).
_SESSION_CHANGE = re.compile(r"^\s*(USE|SET)\b", re.IGNORECASE)
_UNKNOWN = object()
_READ_ONLY = re.compile(r"^\s*(SELECT|SHOW|DESC|DESCRIBE|EXPLAIN|PROFILE|WITH)\b", re.IGNORECASE)


class _PooledConnection:
    """A connection plus the session state we last set on it.

    Every round trip to the cluster costs real time (~120 ms over a WAN), so
    ``USE`` and ``SET sql_select_limit`` are only sent when they change.
    """

    def __init__(self, conn: Any, database: str | None) -> None:
        self.conn = conn
        self.database: Any = database      # current database, or _UNKNOWN
        self.select_limit: Any = None      # None = DEFAULT, int, or _UNKNOWN
        self.last_used = time.monotonic()  # last time it answered (validated again after IDLE_PING_S)
        self.epoch = netguard.epoch()      # network events seen when it last answered
        self.info = netguard.sock_info(getattr(_raw(conn), "_sock", None))  # (local address, peer, family)


class Database:
    """Thread-safe, lazily-connecting pool of singlestoredb connections.

    Up to SINGLESTORE_MCP_POOL_SIZE (default 8) connections, so a slow query
    in one app doesn't hold up the others. Connections use autocommit (the
    SingleStore default), so no extra COMMIT round trip per statement.
    """

    def __init__(self) -> None:
        self._max = max(1, int(os.environ.get("SINGLESTORE_MCP_POOL_SIZE", "8")))
        self._idle: list[_PooledConnection] = []
        self._open = 0
        self._generation = 0
        self._warming = 0  # background connects in flight (warm)
        self._cond = threading.Condition()
        self._default_db: str | None = None
        self._adopted = False

    def _connect(self) -> _PooledConnection:
        # The active saved connection (connections.py); the SINGLESTORE_* settings
        # are its "Environment variables" profile.
        from . import connections

        profile = connections.active()
        if profile is None:
            ConnectionSettings.from_env()  # raises the "set SINGLESTORE_HOST…" guidance
        generation = self._generation
        self._default_db = profile.database
        conn = open_connection(connections.connect_kwargs(profile), autocommit=True)
        pc = _PooledConnection(conn, profile.database)
        pc.generation = generation
        return pc

    @property
    def generation(self) -> int:
        """Goes up whenever the pool is reset (another active connection): data read before belongs to the old one."""
        return self._generation

    def _adopt_preconnected(self) -> None:
        """Take the connections a standby opened before it became this server (``preconnect``), if they were opened
        with the active connection's current settings; otherwise they're closed and the pool connects as usual."""
        from . import preconnect

        holder = preconnect.pop_holder()
        if holder is None:
            return
        try:
            from . import connections

            profile = connections.active()
            if profile is None or not preconnect.supported(profile):
                holder.take(None)  # closes them
                return
            conns = holder.take(connections.connect_kwargs(profile))
        except Exception:  # noqa: BLE001 - the pool just connects as usual
            return
        extra = []
        with self._cond:
            for conn in conns:
                if self._open >= self._max:
                    extra.append(conn)
                    continue
                pc = _PooledConnection(conn, profile.database)
                pc.generation = self._generation
                sock = getattr(_raw(conn), "_sock", None)
                if sock is not None:
                    netguard.track(sock, "singlestore")
                self._idle.append(pc)
                self._open += 1
            if conns:
                self._default_db = profile.database
            self._cond.notify_all()
        for conn in extra:
            self._discard(_PooledConnection(conn, None))

    def reset(self) -> None:
        """Drop every connection (after switching the active connection); busy ones close when released."""
        with self._cond:
            self._generation += 1
            idle, self._idle = self._idle, []
            self._open -= len(idle)
            self._cond.notify_all()
        for pc in idle:
            self._discard(pc)

    def _acquire(self, warming: bool = False) -> _PooledConnection:
        give_up = time.monotonic() + POOL_WAIT_S
        while True:
            pc = None
            with self._cond:
                while True:
                    if self._idle:
                        pc = self._idle.pop()
                        break
                    # Connections are being opened in the background (warm): wait for
                    # the first instead of paying for yet another connect.
                    if not warming and self._warming > 0:
                        self._cond.wait(timeout=0.5)
                        continue
                    if self._open < self._max:
                        self._open += 1
                        break
                    left = give_up - time.monotonic()
                    if left <= 0:
                        raise ConnectionLost(None, f"All {self._max} SingleStore connections stayed busy for "
                                                   f"{POOL_WAIT_S:g} s (long queries, or a network that stopped "
                                                   "answering); try again in a moment.")
                    self._cond.wait(timeout=min(left, 1.0))
            if pc is None:
                break
            why = self._unusable(pc)
            if why is None:
                return pc
            # Dead after a network break (sleep, VPN switch): replace it, and the idle ones from before too.
            self._discard(pc)
            self._release(None)
            if why == "ping":
                self._drop_idle()
        try:
            return self._connect()
        except BaseException:
            with self._cond:
                self._open -= 1
                self._cond.notify()
            raise

    @staticmethod
    def _unusable(pc: _PooledConnection) -> str | None:
        """Why an idle connection can't be used ("closed", "moved", "ping"), or None. Cheap unless it was idle long
        or a network event happened since it last answered: then a ping, bounded by ``netguard.PING_TIMEOUT_S``."""
        raw = _raw(pc.conn)
        if getattr(raw, "_sock", "?") is None:
            return "closed"
        if netguard.moved(pc.info):
            return "moved"  # opened from an address the route no longer uses (the VPN moved): dead
        now = time.monotonic()
        if pc.epoch != netguard.epoch() or now - pc.last_used > IDLE_PING_S:
            try:
                netguard.ping(raw)
            except Exception:  # noqa: BLE001 - half-open after sleep / VPN change
                return "ping"
            pc.epoch, pc.last_used = netguard.epoch(), time.monotonic()
        return None

    def _release(self, pc: _PooledConnection | None) -> None:
        stale = pc is not None and getattr(pc, "generation", self._generation) != self._generation
        if pc is not None and not stale:
            pc.last_used = time.monotonic()
        with self._cond:
            if pc is None or stale:
                self._open -= 1
            else:
                self._idle.append(pc)
            self._cond.notify()
        if stale:
            self._discard(pc)

    def _drop_idle(self) -> None:
        with self._cond:
            idle, self._idle = self._idle, []
            self._open -= len(idle)
            self._cond.notify_all()
        for pc in idle:
            self._discard(pc)

    @staticmethod
    def _discard(pc: _PooledConnection) -> None:
        try:
            pc.conn.close()
        except Exception:  # noqa: BLE001 - it's already broken
            pass

    def warm(self, connections: int = 4) -> None:
        """Open a few connections in the background (each takes 1-2 s over a WAN),
        so the first tool calls, and the monitors' parallel queries, don't wait."""

        def run() -> None:
            try:
                self._release(self._acquire(warming=True))
            except Exception:  # noqa: BLE001 - the first real call reports the problem
                pass
            finally:
                with self._cond:
                    self._warming -= 1
                    self._cond.notify_all()

        if not self._adopted:
            # A server that took over from a standby (restart_server) starts with the standby's open connections.
            self._adopted = True
            try:
                self._adopt_preconnected()
            except Exception:  # noqa: BLE001 - never stops the start
                pass
        with self._cond:
            # Idempotent: a second warm (e.g. server start, then the workspace) only tops the pool up.
            n = max(0, min(connections, self._max) - self._open - self._warming)
            self._warming += n
        for _ in range(n):
            threading.Thread(target=run, name="db-warm", daemon=True).start()

    def execute(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
        database: str | None = None,
        fetch: bool = True,
        max_rows: int | None = None,
    ) -> tuple[list[str], list[dict[str, Any]], int]:
        """Run one SQL statement.

        Returns (column_names, rows_as_dicts, rowcount). Reconnects once,
        transparently, if the cached connection has gone stale (idle
        timeout, cluster failover, etc).

        Connections are pooled, so a call without ``database`` switches
        to the configured default rather than inheriting whatever an
        earlier call selected.

        ``max_rows`` caps the rows a SELECT returns server-side (via the
        session's sql_select_limit), so an unbounded query against a huge
        table never streams the whole result to the client.
        """
        limit = None if max_rows is None else max(0, int(max_rows))
        for attempt in range(2):
            pc = self._acquire_or_lost()
            sent = False  # the statement itself went out (a write is then never sent a second time)
            try:
                cur = pc.conn.cursor()
                try:
                    target_db = database or self._default_db
                    if target_db and pc.database != target_db:
                        pc.database = _UNKNOWN
                        cur.execute(f"USE {quote_identifier(target_db)}")
                        pc.database = target_db
                    if pc.select_limit != limit:
                        pc.select_limit = _UNKNOWN
                        cur.execute(f"SET SESSION sql_select_limit = {'DEFAULT' if limit is None else limit}")
                        pc.select_limit = limit
                    sent = True
                    try:
                        cur.execute(sql, params or ())
                    finally:
                        if _SESSION_CHANGE.match(sql):
                            pc.database = pc.select_limit = _UNKNOWN
                    rowcount = cur.rowcount
                    if fetch and cur.description is not None:
                        rows = cur.fetchall()
                        columns = [d[0] for d in cur.description]
                    else:
                        rows = []
                        columns = []
                finally:
                    cur.close()
            except json.JSONDecodeError:
                # The driver couldn't decode a JSON column of the result (e.g. empty JSON values in
                # information_schema.USERS). The connection is mid-result: drop it, and for a read-only
                # statement read the result again with JSON left as text.
                self._discard(pc)
                self._release(None)
                if not _READ_ONLY.match(sql):
                    raise
                return self._execute_json_as_text(sql, params, database, fetch, limit)
            except (s2.Error, OSError) as exc:
                if _is_connection_error(exc):
                    self._discard(pc)
                    self._release(None)
                    # The idle ones are usually just as dead (the network closed idle connections): drop
                    # them too, so the retry opens a fresh connection instead of picking another dead one.
                    self._drop_idle()
                    # Retry once on a fresh connection (stale after an idle timeout, failover, a network break) –
                    # a statement that may have changed something only if it can't have reached the cluster (the
                    # connection was found gone when sending: 2006).
                    resend = not sent or _READ_ONLY.match(sql) or getattr(exc, "errno", None) == 2006
                    if attempt == 0 and resend:
                        continue
                    netguard.failure("singlestore", exc)
                    if not resend:
                        raise ConnectionLost(None, f"{_lost(exc)} The statement was sent before the connection broke, so "
                                                   "it may or may not have run: check before running it again.") from exc
                    raise _lost(exc) from exc
                self._release(pc)
                raise
            self._release(pc)
            netguard.success("singlestore")
            return columns, rows, rowcount
        raise AssertionError("unreachable")

    def stream(
        self,
        sql: str,
        database: str | None = None,
        max_rows: int | None = None,
        chunk: int = 1000,
        on_connection: Any = None,
        write: bool = False,
        on_rowcount: Any = None,
    ) -> Iterator[tuple[list[str], list[dict[str, Any]]]]:
        """Run one statement and yield its result in chunks as it arrives: first ``(columns, [])``, then
        ``(columns, rows)`` per chunk of up to ``chunk`` rows. Nothing is buffered beyond one chunk, so the
        first rows show long before a big result has been read, and memory stays flat.

        ``max_rows`` sets sql_select_limit as in ``execute`` (an explicit LIMIT in the SQL overrides it).
        ``on_connection(connection_id)`` is called before the statement is sent (for a KILL QUERY).
        ``write``: a statement that may change data (confirmed in the editor): never sent a second time, even when
        the connection drops right after sending it. ``on_rowcount(n)``: the affected-rows count, once it's known.

        Stop early by closing the generator (``close()`` / leaving a ``for`` loop): the rest of the result
        is read and dropped in the background for a few seconds so the connection can go back to the
        pool, else the connection is closed (which ends the statement on the cluster). A connection that
        has gone stale is retried once, but only before any row was yielded.
        """
        limit = None if max_rows is None else max(0, int(max_rows))
        for attempt in range(2):
            pc = self._acquire_or_lost()
            sscur = None
            state = "start"  # start -> sent -> done (fully read) | closed (consumer stopped early) | failed
            try:
                cur = pc.conn.cursor()
                try:
                    target_db = database or self._default_db
                    if target_db and pc.database != target_db:
                        pc.database = _UNKNOWN
                        cur.execute(f"USE {quote_identifier(target_db)}")
                        pc.database = target_db
                    if pc.select_limit != limit:
                        pc.select_limit = _UNKNOWN
                        cur.execute(f"SET SESSION sql_select_limit = {'DEFAULT' if limit is None else limit}")
                        pc.select_limit = limit
                finally:
                    cur.close()
                raw = getattr(pc.conn, "_conn", pc.conn)
                if on_connection is not None:
                    on_connection(raw.thread_id())
                sscur = _unbuffered_cursor(raw)
                state = "sent"
                try:
                    sscur.execute(sql)
                finally:
                    if _SESSION_CHANGE.match(sql):
                        pc.database = pc.select_limit = _UNKNOWN
                if write:
                    state = "executed"  # a write that ran is never sent again
                columns = [d[0] for d in sscur.description] if sscur.description is not None else []
                if on_rowcount is not None and not columns:
                    on_rowcount(getattr(sscur, "rowcount", None))
                state = "yielding"
                yield columns, []
                while columns:
                    rows = sscur.fetchmany(chunk)
                    if not rows:
                        break
                    yield columns, rows
                sscur.close()
                state = "done"
                netguard.success("singlestore")
            except GeneratorExit:
                state = "closed"
                raise
            except (s2.Error, OSError) as exc:
                if _is_connection_error(exc):
                    self._drop_idle()
                    if attempt == 0 and (state == "start" or (state == "sent" and not write)):
                        state = "retry"
                        continue
                    state = "failed"
                    netguard.failure("singlestore", exc)
                    raise _lost(exc) from exc
                # A plain SQL error (syntax, unknown table) before any row leaves the connection usable.
                state = "error_ok" if state in ("start", "sent", "executed") else "failed"
                raise
            except BaseException:
                state = "failed"
                raise
            finally:
                if state in ("done", "error_ok"):
                    if sscur is not None and state == "error_ok":
                        try:
                            sscur.close()
                        except Exception:  # noqa: BLE001 - then it isn't reusable after all
                            self._discard(pc)
                            pc = None
                    self._release(pc)
                elif state == "closed" and sscur is not None:
                    _drain_later(self, pc, sscur)
                else:
                    self._discard(pc)
                    self._release(None)
            return

    def _execute_json_as_text(self, sql: str, params: tuple[Any, ...] | None, database: str | None, fetch: bool,
                              limit: int | None) -> tuple[list[str], list[dict[str, Any]], int]:
        """``execute`` on a one-off connection that returns JSON columns as text (no decoding)."""
        from . import connections

        profile = connections.active()
        conn = open_connection(connections.connect_kwargs(profile), autocommit=True, parse_json=False)
        try:
            cur = conn.cursor()
            target_db = database or self._default_db
            if target_db:
                cur.execute(f"USE {quote_identifier(target_db)}")
            if limit is not None:
                cur.execute(f"SET SESSION sql_select_limit = {limit}")
            cur.execute(sql, params or ())
            if fetch and cur.description is not None:
                return [d[0] for d in cur.description], cur.fetchall(), cur.rowcount
            return [], [], cur.rowcount
        finally:
            conn.close()

    def _acquire_or_lost(self) -> _PooledConnection:
        """``_acquire`` whose connection failures (cluster unreachable) are recorded and reported readably."""
        try:
            return self._acquire()
        except (s2.Error, OSError) as exc:
            if not _is_connection_error(exc):
                raise
            netguard.failure("singlestore", exc)
            raise _lost(exc) from exc

    def ping(self) -> None:
        """Check the cluster answers (COM_PING on a pooled connection, nothing in the plan cache): the health probe
        after a network break. Records success / failure; raises ``ConnectionLost``."""
        pc = self._acquire_or_lost()
        try:
            netguard.ping(_raw(pc.conn))
        except Exception as exc:  # noqa: BLE001
            self._discard(pc)
            self._release(None)
            self._drop_idle()
            netguard.failure("singlestore", exc)
            raise _lost(exc) from exc
        self._release(pc)
        netguard.success("singlestore")

    def close(self) -> None:
        with self._cond:
            idle, self._idle = self._idle, []
            self._open -= len(idle)
        for pc in idle:
            self._discard(pc)


    def parallel(self, *calls: Any) -> list[Any]:
        """Run independent zero-argument callables (each doing its own queries) concurrently.

        Monitors that need several information_schema queries use this so the
        round trips overlap instead of adding up.
        """
        if len(calls) <= 1:
            return [c() for c in calls]
        results: list[Any] = [None] * len(calls)
        errors: list[BaseException] = []

        def run(i: int) -> None:
            try:
                results[i] = calls[i]()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(1, len(calls))]
        for t in threads:
            t.start()
        run(0)
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
        return results


db = Database()


def _on_connection_change() -> None:
    db.reset()


def _probe() -> None:
    # Only once the pool has been used (the active connection may not even be set up).
    if db._open or db._idle or netguard.is_down("singlestore"):
        db.ping()


netguard.set_probe("singlestore", _probe)


from . import connections as _connections  # noqa: E402

_connections.on_change(_on_connection_change)
