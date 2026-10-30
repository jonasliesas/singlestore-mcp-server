"""Server mode (SAS SpeedyStore Studio behind the hub): the switches, the per-user encrypted secrets with the hub's
key, browser sign-ins and notebooks/assistants off, the browser view's public links and hub-only routes, and the
hub's seed (Viya sign-in + default connection). Offline; normal mode must be unchanged when the switch is off."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import server_mode


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Server mode with its own state folder and files folder (HOME), and the hub's key for secrets."""
    from cryptography.fernet import Fernet

    state, files = tmp_path / "state", tmp_path / "files"
    state.mkdir()
    files.mkdir()
    monkeypatch.setenv("SINGLESTORE_MCP_SERVER_MODE", "1")
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(state))
    monkeypatch.setenv("HOME", str(files))
    monkeypatch.setenv("USERPROFILE", str(files))
    monkeypatch.setenv("SINGLESTORE_MCP_SQL_DIR", str(files / "SQL"))
    for name in ("SINGLESTORE_MCP_NOTEBOOKS", "SINGLESTORE_MCP_ASSISTANT", "SINGLESTORE_MCP_KEYRING",
                 "SINGLESTORE_HOST", "SINGLESTORE_URL"):
        monkeypatch.delenv(name, raising=False)
    key = Fernet.generate_key()
    server_mode.set_secret_key(key)
    try:
        yield {"state": state, "files": files, "key": key}
    finally:
        server_mode.set_secret_key(None)
        server_mode.set_sas_client(None, None)


def test_switches_off_and_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SINGLESTORE_MCP_SERVER_MODE", raising=False)
    assert not server_mode.enabled()
    assert server_mode.notebooks_enabled() and server_mode.assistant_enabled() and server_mode.keyring_enabled()
    monkeypatch.setenv("SINGLESTORE_MCP_SERVER_MODE", "1")
    assert server_mode.enabled()
    assert not server_mode.notebooks_enabled() and not server_mode.assistant_enabled() and not server_mode.keyring_enabled()
    monkeypatch.setenv("SINGLESTORE_MCP_ASSISTANT", "1")
    monkeypatch.setenv("SINGLESTORE_MCP_NOTEBOOKS", "1")
    assert server_mode.assistant_enabled() and server_mode.notebooks_enabled()
    f = server_mode.features()
    assert f["server"] and f["notes"]["assistant"] is None


def test_secrets_use_the_hubs_key_not_a_key_file(server: dict[str, Any]) -> None:
    from cryptography.fernet import Fernet

    from singlestore_mcp import connections

    connections.set_password("demo", "pw-for-test-only")
    assert connections.get_password("demo") == "pw-for-test-only"
    enc = server["state"] / "secrets.enc"
    assert enc.is_file() and not (server["state"] / "secret.key").exists()
    assert b"pw-for-test-only" not in enc.read_bytes()
    assert json.loads(Fernet(server["key"]).decrypt(enc.read_bytes()))["demo"] == "pw-for-test-only"
    assert "server" in connections.password_store()
    server_mode.set_secret_key(Fernet.generate_key())  # another user's key opens nothing
    assert connections.get_password("demo") is None


def test_browser_sign_ins_are_refused_on_the_server(server: dict[str, Any]) -> None:
    from singlestore_mcp import connections

    with pytest.raises(ValueError, match="isn't available on the server"):
        connections.save({"name": "e", "host": "h", "auth": "entra"})
    with pytest.raises(ValueError, match="isn't available on the server"):
        connections.save({"name": "s", "host": "h", "auth": "browser_sso", "user": "a@b.c"})
    assert connections.save({"name": "p", "host": "h", "user": "alice"}).auth == "password"


def test_assistant_and_notebooks_are_off(server: dict[str, Any]) -> None:
    from singlestore_mcp import assistant, notebook_kernel

    st = assistant.status()
    assert st == {"available": False, "disabled": True, "reason": server_mode.ASSISTANT_OFF}
    assert assistant.find_claude() is None
    for fn in (notebook_kernel.setup, lambda: notebook_kernel.start("nb-1"), lambda: notebook_kernel.install_packages(["pandas"])):
        with pytest.raises(PermissionError, match="Notebooks are off"):
            fn()


def test_file_browser_shows_only_the_users_folder(server: dict[str, Any]) -> None:
    from singlestore_mcp.apps import sql_editor

    folders = {f["label"]: f["path"] for f in sql_editor._known_folders()}
    assert set(folders) <= {"SQL folder", "My files"} and Path(folders["My files"]) == server["files"]
    assert all(Path(r).is_relative_to(server["files"].resolve()) for r in sql_editor.file_roots())
    from singlestore_mcp.apps._core import _server_wording

    assert '"My files"' in _server_wording('{ id: "local", label: "This computer", title: "Files on this computer (the SQL folder and your user folder)" }')


class _Tool:
    name = "sql_editor"

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"name": self.name, "_meta": {"ui": {"resourceUri": "ui://singlestore/workspace.html"}}}


class _Result:
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"content": [], "structuredContent": {"ok": True}}


class _Mcp:
    async def list_tools(self) -> list[_Tool]:
        return [_Tool()]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _Result:
        return _Result()


def test_browser_view_public_links_and_hub_routes() -> None:
    from singlestore_mcp.apps.browser_view import BrowserView

    view = BrowserView(_Mcp(), token="t" * 24, public_base="/studio/", allowed_hosts=("studio.example:443",))
    got: list[dict[str, Any]] = []
    view.extra_routes["hub/seed"] = lambda body: got.append(body) or {"ok": True}
    try:
        assert view.url("sql_editor", {}).startswith("/studio/?tool=sql_editor")
        base = f"http://127.0.0.1:{view.port}/{'t' * 24}"
        page = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"{base}/").read().decode()
        assert "window.S2_SERVER" in page

        def post(route: str, origin: str, host: str | None = None) -> int:
            req = urllib.request.Request(f"{base}/{route}", data=b'{"x": 1}', method="POST",
                                         headers={"Origin": origin, "Content-Type": "application/json", **({"Host": host} if host else {})})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as res:
                    return res.status
            except urllib.error.HTTPError as exc:
                return exc.code

        own = f"http://127.0.0.1:{view.port}"
        assert post("hub/seed", own) == 200 and got == [{"x": 1}]
        assert post("hub/seed", "https://evil.example") == 403
        assert post("hub/other", own) == 403
        # An allowed host/origin gets past the checks (400: this body names no tool).
        assert post("call", "http://studio.example:443", host="studio.example:443") == 400
        assert post("call", own, host="evil.example") == 403
    finally:
        view.close()


def test_seed_sets_up_viya_and_a_default_connection(server: dict[str, Any]) -> None:
    from singlestore_mcp import connections, sas_viya
    from singlestore_mcp.studio_server import apply_seed

    seed = {"viya": {"name": "Zeus", "url": "https://zeus.example/", "client_id": "studio-hub", "activate": True,
                     "tokens": {"access_token": "fake-access-1", "refresh_token": "fake-refresh-1", "expires_in": 3600}},
            "singlestore": {"name": "SingleStore", "host": "s2.example", "port": "3306", "database": "SASDP", "user": "alice"}}
    done = apply_seed(seed)
    assert done == {"viya": "Zeus", "viya_token": True, "singlestore": "SingleStore"}
    s = sas_viya.settings()
    assert s["name"] == "Zeus" and s["url"] == "https://zeus.example" and s["client_id"] == "studio-hub"
    assert sas_viya.token_info("Zeus")["signed_in"] is True
    p = connections.active()
    assert p.name == "SingleStore" and p.user == "alice" and p.database == "SASDP" and connections.get_password(p.name) is None
    # The user's own changes stay: a later seed (next sign-in) doesn't overwrite the connection.
    connections.save({"name": "SingleStore", "host": "s2.example", "user": "alice2"}, original_name="SingleStore")
    apply_seed({**seed, "viya": {**seed["viya"], "tokens": {"access_token": "fake-access-2", "expires_in": 3600}}})
    assert connections.get("SingleStore").user == "alice2"
    assert json.loads(connections.get_password("sas-viya:Zeus#token"))["access"] == "fake-access-2"


def test_sas_client_secret_only_for_its_client() -> None:
    server_mode.set_sas_client("studio-hub", "test-secret")
    try:
        assert server_mode.sas_client_secret("studio-hub") == "test-secret"
        assert server_mode.sas_client_secret("vscode") is None
    finally:
        server_mode.set_sas_client(None, None)
