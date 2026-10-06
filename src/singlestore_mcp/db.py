"""Connection handling for the SingleStore MCP server.

Built directly on top of ``singlestoredb``, SingleStore's own official Python
client (https://github.com/singlestore-labs/singlestoredb-python). It speaks
the MySQL wire protocol, so it works identically against SingleStore Helios
and against a self-managed SingleStore cluster -- just point it at a host,
port, user and password.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import singlestoredb as s2

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


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (s2.InterfaceError, OSError)):
        return True
    if isinstance(exc, s2.OperationalError):
        code = getattr(exc, "errno", None) or (exc.args[0] if exc.args else None)
        return code in _CONNECTION_LOST_CODES
    return False


# Statements that change session state behind our back (USE, SET ...).
_SESSION_CHANGE = re.compile(r"^\s*(USE|SET)\b", re.IGNORECASE)
_UNKNOWN = object()


class _PooledConnection:
    """A connection plus the session state we last set on it.

    Every round trip to the cluster costs real time (~120 ms over a WAN), so
    ``USE`` and ``SET sql_select_limit`` are only sent when they change.
    """

    def __init__(self, conn: Any, database: str | None) -> None:
        self.conn = conn
        self.database: Any = database      # current database, or _UNKNOWN
        self.select_limit: Any = None      # None = DEFAULT, int, or _UNKNOWN


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
        self._cond = threading.Condition()
        self._default_db: str | None = None

    def _connect(self) -> _PooledConnection:
        settings = ConnectionSettings.from_env()
        self._default_db = settings.database
        conn = s2.connect(**settings.connect_kwargs(), autocommit=True)
        return _PooledConnection(conn, settings.database)

    def _acquire(self) -> _PooledConnection:
        with self._cond:
            while True:
                if self._idle:
                    return self._idle.pop()
                if self._open < self._max:
                    self._open += 1
                    break
                self._cond.wait()
        try:
            return self._connect()
        except BaseException:
            with self._cond:
                self._open -= 1
                self._cond.notify()
            raise

    def _release(self, pc: _PooledConnection | None) -> None:
        with self._cond:
            if pc is None:
                self._open -= 1
            else:
                self._idle.append(pc)
            self._cond.notify()

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
                self._release(self._acquire())
            except Exception:  # noqa: BLE001 - the first real call reports the problem
                pass

        for _ in range(min(connections, self._max)):
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
            pc = self._acquire()
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
            except (s2.Error, OSError) as exc:
                if _is_connection_error(exc):
                    self._discard(pc)
                    self._release(None)
                    if attempt == 0:
                        continue  # stale connection (idle timeout, failover): retry once on a fresh one
                else:
                    self._release(pc)
                raise
            self._release(pc)
            return columns, rows, rowcount
        raise AssertionError("unreachable")

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
