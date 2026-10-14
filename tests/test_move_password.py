"""move_password: SingleStore passwords leave the Claude config files for the credential store.

Offline: config files and SINGLESTORE_MCP_HOME are temporary, and the credential store is a dict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from singlestore_mcp import connections as conn
from singlestore_mcp import move_password as mp

SECRET = "s3cret-not-real"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path / "home"))
    for k in mp.CONNECTION_VARS:
        monkeypatch.delenv(k, raising=False)
    secrets: dict[str, str] = {}
    monkeypatch.setattr(conn, "get_password", lambda name: secrets.get(name))
    monkeypatch.setattr(conn, "set_password", lambda name, pw: secrets.__setitem__(name, pw))
    monkeypatch.setattr(conn, "delete_password", lambda name: secrets.pop(name, None))
    monkeypatch.setattr(conn, "password_store", lambda: "test store")
    return secrets


def _config(path: Path, env: dict[str, str], indent: int | None = 2) -> Path:
    cfg = {"numStartups": 3, "mcpServers": {"singlestore": {"command": "uv", "args": ["run"], "env": env},
                                            "other": {"command": "x", "env": {"KEEP": "1"}}}}
    path.write_text(json.dumps(cfg, indent=indent), encoding="utf-8")
    return path


ENV = {"SINGLESTORE_HOST": "10.0.0.1", "SINGLESTORE_USER": "admin", "SINGLESTORE_PASSWORD": SECRET,
       "SINGLESTORE_MCP_ALERTS": "0"}


def test_dry_run_changes_nothing(tmp_path: Path, store: dict[str, str]) -> None:
    f = _config(tmp_path / "claude.json", dict(ENV))
    before = f.read_text(encoding="utf-8")
    lines: list[str] = []
    assert mp.run(False, [f], out=lines.append) == 0
    assert f.read_text(encoding="utf-8") == before
    assert not store and not conn._read().get("profiles")
    assert SECRET not in "\n".join(lines)
    assert any("save as new connection 'admin@10.0.0.1'" in line for line in lines)


def test_apply_creates_connection_and_strips_config(tmp_path: Path, store: dict[str, str]) -> None:
    f1 = _config(tmp_path / "claude.json", dict(ENV), indent=None)
    f2 = _config(tmp_path / "claude_desktop_config.json", dict(ENV))
    lines: list[str] = []
    assert mp.run(True, [f1, f2], out=lines.append) == 0
    assert SECRET not in "\n".join(lines)
    for f in (f1, f2):
        text = f.read_text(encoding="utf-8")
        assert SECRET not in text and "SINGLESTORE_HOST" not in text
        cfg = json.loads(text)
        assert cfg["mcpServers"]["singlestore"]["env"] == {"SINGLESTORE_MCP_ALERTS": "0"}  # other variables stay
        assert cfg["mcpServers"]["other"]["env"] == {"KEEP": "1"} and cfg["numStartups"] == 3
    assert "\n" not in f1.read_text(encoding="utf-8").strip()  # compact file stays compact
    names = [p.name for p in conn.profiles()]
    assert names == ["admin@10.0.0.1"]  # one connection for both files
    assert store == {"admin@10.0.0.1": SECRET}
    assert conn.active().name == "admin@10.0.0.1"


def test_reuses_saved_connection_and_keeps_active(tmp_path: Path, store: dict[str, str]) -> None:
    conn.save({"name": "Zeus", "host": "10.0.0.1", "user": "admin", "database": "MyDB"}, password=SECRET)
    conn.save({"name": "Other", "host": "10.9.9.9", "user": "x"}, password="pw")
    conn.activate("Other")
    f = _config(tmp_path / "claude.json", dict(ENV))
    assert mp.run(True, [f], out=lambda _: None) == 0
    assert [p.name for p in conn.profiles()] == ["Other", "Zeus"]
    assert conn.active().name == "Other"
    assert "SINGLESTORE_PASSWORD" not in f.read_text(encoding="utf-8")


def test_saved_connection_without_password_gets_it(tmp_path: Path, store: dict[str, str]) -> None:
    conn.save({"name": "Zeus", "host": "10.0.0.1", "user": "admin"})
    f = _config(tmp_path / "claude.json", dict(ENV))
    assert mp.run(True, [f], out=lambda _: None) == 0
    assert store == {"Zeus": SECRET}
    assert conn.active().name == "Zeus"


def test_different_saved_password_is_a_conflict(tmp_path: Path, store: dict[str, str]) -> None:
    conn.save({"name": "Zeus", "host": "10.0.0.1", "user": "admin"}, password="another")
    f = _config(tmp_path / "claude.json", dict(ENV))
    before = f.read_text(encoding="utf-8")
    lines: list[str] = []
    assert mp.run(True, [f], out=lines.append) == 1
    assert f.read_text(encoding="utf-8") == before  # left alone
    assert store == {"Zeus": "another"}
    assert any("NOT moved" in line for line in lines)


def test_placeholders_and_broken_files_are_left_alone(tmp_path: Path, store: dict[str, str]) -> None:
    f = _config(tmp_path / ".mcp.json", {"SINGLESTORE_HOST": "${SINGLESTORE_HOST}",
                                         "SINGLESTORE_PASSWORD": "${SINGLESTORE_PASSWORD}"})
    broken = tmp_path / "claude_desktop_config - Copy.json"
    broken.write_text("{ not json", encoding="utf-8")
    before = f.read_text(encoding="utf-8")
    assert mp.run(True, [f, broken], out=lambda _: None) == 0
    assert f.read_text(encoding="utf-8") == before
    assert broken.read_text(encoding="utf-8") == "{ not json"
    assert not conn._read().get("profiles")
