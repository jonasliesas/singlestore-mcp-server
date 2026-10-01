"""Connection handling for the SingleStore MCP server.

Built directly on top of ``singlestoredb``, SingleStore's own official Python
client (https://github.com/singlestore-labs/singlestoredb-python). It speaks
the MySQL wire protocol, so it works identically against SingleStore Helios
and against a self-managed SingleStore cluster -- just point it at a host,
port, user and password.
"""

from __future__ import annotations

import os
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


class Database:
    """Thread-safe, lazily-connecting wrapper around a singlestoredb connection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Any | None = None
        self._default_db: str | None = None

    def _connect(self) -> Any:
        settings = ConnectionSettings.from_env()
        self._default_db = settings.database
        return s2.connect(**settings.connect_kwargs())

    def _get_conn(self) -> Any:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

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

        The connection is shared, so a call without ``database`` switches
        back to the configured default rather than inheriting whatever an
        earlier call selected.

        ``max_rows`` caps the rows a SELECT returns server-side (via the
        session's sql_select_limit), so an unbounded query against a huge
        table never streams the whole result to the client.
        """
        with self._lock:
            for attempt in range(2):
                try:
                    conn = self._get_conn()
                    cur = conn.cursor()
                    try:
                        target_db = database or self._default_db
                        if target_db:
                            cur.execute(f"USE {quote_identifier(target_db)}")
                        if max_rows is not None:
                            cur.execute(f"SET SESSION sql_select_limit = {max(0, int(max_rows))}")
                        try:
                            cur.execute(sql, params or ())
                            rowcount = cur.rowcount
                            if fetch and cur.description is not None:
                                rows = cur.fetchall()
                                columns = [d[0] for d in cur.description]
                            else:
                                rows = []
                                columns = []
                        finally:
                            if max_rows is not None:
                                cur.execute("SET SESSION sql_select_limit = DEFAULT")
                        conn.commit()
                        return columns, rows, rowcount
                    finally:
                        cur.close()
                except (s2.Error, OSError) as exc:
                    if attempt == 1 or not _is_connection_error(exc):
                        raise
                    self._conn = None
            raise AssertionError("unreachable")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


db = Database()
