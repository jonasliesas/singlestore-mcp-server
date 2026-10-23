"""The local query history never keeps passwords: new rows are masked, and rows saved before that are masked once."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from singlestore_mcp import history_store

SECRET = "s3cret-not-real"


@pytest.mark.parametrize("text,kept", [
    (f'select sasepFunction(databasePassword="{SECRET}", user="x")', 'databasePassword="***"'),
    (f"libname a sstore pwd='{SECRET}' user=u", "pwd='***'"),
    (f"password={SECRET}, more", "password=***"),
    (rf'a \"databasePassword\": \"{SECRET}\" b', r'\"databasePassword\": \"***\"'),
    (f'{{"token": "{SECRET}"}}', '"token": "***"'),
])
def test_mask_secrets(text: str, kept: str) -> None:
    out = history_store.mask_secrets(text)
    assert SECRET not in out and kept in out, out


def test_mask_leaves_other_sql_alone() -> None:
    for sql in ("select password_hash from t where x = 1", "SELECT * FROM CARS", None, ""):
        assert history_store.mask_secrets(sql) == sql


def test_old_rows_are_masked_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    history_store._schema_ready.clear()
    conn = history_store.connect()  # creates the schema at the current version
    cols = ", ".join(history_store._RUN_COLUMNS)
    conn.execute(f"INSERT INTO runs ({cols}) VALUES ({', '.join('?' * len(history_store._RUN_COLUMNS))})",
                 ("s", "k1", "2026-10-01 10:00:00", 5000, "u", "db", 1, None, "SELECT", 1, "shape",
                  f'select sasepFunction(databasePassword="{SECRET}")', "SAS EP", "[]", None, "stmt"))
    conn.execute("PRAGMA user_version = 1")  # as a store from before the masking
    conn.commit()
    conn.close()
    history_store._schema_ready.clear()
    history_store.connect().close()  # migrates
    raw = Path(tmp_path, "query_history.db").read_bytes()
    assert SECRET.encode() not in raw, "the old password is still in the file"
    with sqlite3.connect(tmp_path / "query_history.db") as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] >= history_store._SECRETS_MASKED
        assert 'databasePassword="***"' in c.execute("SELECT sample FROM runs").fetchone()[0]
