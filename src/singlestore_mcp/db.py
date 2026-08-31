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

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_$]+$")


class ConfigurationError(RuntimeError):
    """Raised when required connection settings are missing."""


class InvalidIdentifierError(ValueError):
    """Raised when a database/pipeline identifier looks unsafe to interpolate."""


def quote_identifier(name: str) -> str:
    """Backtick-quote a SQL identifier after validating its shape.

    SingleStore (like MySQL) does not support bind parameters for identifiers
    (table/database/pipeline names), so callers that need to build a
    statement dynamically must quote the identifier themselves. Restricting
    the accepted character set is what keeps that safe.
    """
    if not name or not _IDENTIFIER_RE.match(name):
        raise InvalidIdentifierError(
            f"{name!r} is not a valid identifier "
            "(letters, digits, underscore and $ only)"
        )
    return f"`{name}`"


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


class Database:
    """Thread-safe, lazily-connecting wrapper around a singlestoredb connection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Any | None = None

    def _connect(self) -> Any:
        settings = ConnectionSettings.from_env()
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
    ) -> tuple[list[str], list[dict[str, Any]], int]:
        """Run one SQL statement.

        Returns (column_names, rows_as_dicts, rowcount). Reconnects once,
        transparently, if the cached connection has gone stale (idle
        timeout, cluster failover, etc).
        """
        with self._lock:
            for attempt in range(2):
                try:
                    conn = self._get_conn()
                    cur = conn.cursor()
                    try:
                        if database:
                            cur.execute(f"USE {quote_identifier(database)}")
                        cur.execute(sql, params or ())
                        rowcount = cur.rowcount
                        if fetch and cur.description is not None:
                            rows = cur.fetchall()
                            columns = [d[0] for d in cur.description]
                        else:
                            rows = []
                            columns = []
                        conn.commit()
                        return columns, rows, rowcount
                    finally:
                        cur.close()
                except (s2.Error, OSError) as exc:
                    self._conn = None
                    if attempt == 1:
                        raise
                    # First failure: assume a dead connection, retry once
                    # after reconnecting. Re-raise anything past that.
                    del exc
            raise AssertionError("unreachable")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


db = Database()
