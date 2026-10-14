"""Shared test plumbing (used by tests/conftest.py and perf/perf.py).

* ``isolated_home``: a throwaway SINGLESTORE_MCP_HOME seeded from the real ~/.singlestore-mcp
  (SAS settings, query history cache, CA bundle; the notebook Python env through a directory junction).
* ``server_env``: the environment the test server runs with (cluster credentials from the
  ``singlestore`` entry of ~/.claude.json, plus the isolated home).
* ``McpSession``: a synchronous facade over one ``mcp.client.Client`` connected over stdio, running its
  asyncio loop in a background thread so tests stay plain synchronous functions.

Credential values are never printed: only the names of the variables are ever shown.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
APPS_DIR = REPO / "src" / "singlestore_mcp" / "apps"
PYTHON = REPO / ".venv" / "Scripts" / "python.exe" if sys.platform == "win32" else REPO / ".venv" / "bin" / "python"
REAL_HOME = Path(os.environ.get("SINGLESTORE_MCP_HOME") or Path.home() / ".singlestore-mcp").expanduser()

# Copied from the real home into the test home when present (connections.json deliberately is not:
# the test server uses the built-in "Environment variables" connection).
SEED_FILES = ("sas.json", "query_history.db", "singlestore_bundle.pem")
# Opt-in (S2_TEST_SAS_SECRETS=1): also copy the encrypted secrets file, which holds the SAS Viya sign-in
# (too large for the Windows credential store). Off by default: the test server may refresh the token in
# its copy, and the real file would then keep the old one.
SECRET_FILES = ("secrets-large.enc", "secrets.enc", "secret.key")
JUNCTION_DIRS = ("notebook-env",)

DB = "SASDP"
TABLE = "CARS"
NEW_TABLE = "SASDP.CARS_NEW"  # same data in another table: a read-only stand-in for "the new table"
RESULT_LIMIT = 50_000  # Claude's tool-result size limit for model-visible tools (characters)


# ----------------------------------------------------------------------------- environment

def claude_server_env() -> dict[str, str]:
    """The ``env`` of the ``singlestore`` MCP server entry in ~/.claude.json (empty if there is none)."""
    path = Path.home() / ".claude.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found: list[dict[str, Any]] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "singlestore" and isinstance(v, dict) and "command" in v:
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(cfg)
    env = (found[0].get("env") if found else None) or {}
    return {str(k): str(v) for k, v in env.items()}


def _make_junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        link.symlink_to(target, target_is_directory=True)
        return
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except Exception:  # noqa: BLE001 - fall back to mklink
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path))


def remove_links(home: Path) -> None:
    """Remove the junctions (never their targets) from a test home."""
    for name in JUNCTION_DIRS:
        p = home / name
        if _is_link(p):
            if p.is_symlink():
                p.unlink()
            else:
                os.rmdir(p)  # removes the junction itself, not the directory it points to


def seed_home(home: Path) -> dict[str, Any]:
    """Fill a fresh test home from the real one. Returns what was copied/linked."""
    home.mkdir(parents=True, exist_ok=True)
    done: dict[str, Any] = {"copied": [], "linked": []}
    names = SEED_FILES + (SECRET_FILES if os.environ.get("S2_TEST_SAS_SECRETS") == "1" else ())
    for name in names:
        src = REAL_HOME / name
        if src.is_file():
            shutil.copy2(src, home / name)
            done["copied"].append(name)
    for name in JUNCTION_DIRS:
        src = REAL_HOME / name
        if src.is_dir() and not (home / name).exists():
            _make_junction(home / name, src)
            done["linked"].append(name)
    return done


def server_env(home: Path, sql_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(claude_server_env())
    env.update({
        "SINGLESTORE_MCP_HOME": str(home),
        "SINGLESTORE_MCP_SQL_DIR": str(sql_dir),
        "SINGLESTORE_MCP_ALERTS": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(extra or {})
    return env


# ----------------------------------------------------------------------------- MCP session

@dataclass
class Result:
    name: str
    data: Any
    text: str
    is_error: bool
    ms: float
    text_chars: int
    struct_chars: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def chars(self) -> int:
        return self.text_chars + self.struct_chars

    def blob(self) -> str:
        """Text + structured content as one string, for substring checks."""
        return self.text + (json.dumps(self.raw.get("structured_content"), ensure_ascii=False) if self.raw.get("structured_content") is not None else "")


def _to_result(name: str, r: Any, ms: float) -> Result:
    d = r.model_dump(mode="json", exclude_none=True)
    text = "".join(x.get("text", "") for x in d.get("content", []) if x.get("type") == "text")
    sc = d.get("structured_content")
    try:
        data = sc if sc is not None else json.loads(text)
    except ValueError:
        data = text
    return Result(name, data, text, bool(d.get("is_error")), round(ms, 1), len(text),
                  len(json.dumps(sc)) if sc is not None else 0, d)


class McpSession:
    """One stdio MCP client to ``python -m singlestore_mcp.server`` (through its supervisor, like Claude)."""

    def __init__(self, env: dict[str, str], *, read_timeout: float = 600, startup_timeout: float = 120,
                 supervised: bool = True) -> None:
        from mcp.client import Client
        from mcp.client.stdio import StdioServerParameters

        if not supervised:
            env = {**env, "SINGLESTORE_MCP_NO_SUPERVISOR": "1"}
        self.params = StdioServerParameters(command=str(PYTHON), args=["-m", "singlestore_mcp.server"], env=env, cwd=str(REPO))
        self._Client = Client
        self.read_timeout = read_timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-test-client", daemon=True)
        self._thread.start()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._client: Any = None
        self._closing: asyncio.Event | None = None
        t0 = time.perf_counter()
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        if not self._ready.wait(startup_timeout):
            raise TimeoutError(f"MCP server didn't start within {startup_timeout} s")
        if self._error:
            raise RuntimeError(f"MCP server failed to start: {self._error!r}") from self._error
        self.startup_s = time.perf_counter() - t0

    async def _main(self) -> None:
        self._closing = asyncio.Event()
        try:
            async with self._Client(self.params, read_timeout_seconds=self.read_timeout) as c:
                self._client = c
                self._ready.set()
                await self._closing.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            self._error = exc
            self._ready.set()

    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout or self.read_timeout + 30)

    # -- tools
    async def _call(self, name: str, args: dict[str, Any] | None, timeout: float | None) -> Result:
        t0 = time.perf_counter()
        try:
            r = await self._client.call_tool(name, args or {}, read_timeout_seconds=timeout)
        except Exception as exc:  # noqa: BLE001 - protocol-level failure: report as an error result
            return Result(name, None, f"{type(exc).__name__}: {exc}", True, (time.perf_counter() - t0) * 1000, 0, 0, {})
        return _to_result(name, r, (time.perf_counter() - t0) * 1000)

    def call(self, name: str, args: dict[str, Any] | None = None, *, timeout: float | None = None) -> Result:
        return self._run(self._call(name, args, timeout), (timeout or self.read_timeout) + 30)

    def gather(self, calls: list[tuple[str, dict[str, Any]]], *, timeout: float | None = None) -> list[Result]:
        """Make several calls at once (concurrently over the one connection)."""
        async def go() -> list[Result]:
            return list(await asyncio.gather(*(self._call(n, a, timeout) for n, a in calls)))

        return self._run(go(), (timeout or self.read_timeout) + 30)

    def ok(self, name: str, args: dict[str, Any] | None = None, **kw: Any) -> Any:
        r = self.call(name, args, **kw)
        assert not r.is_error, f"{name}({_short(args)}) failed: {r.text[:800]}"
        return r.data

    def err(self, name: str, args: dict[str, Any] | None = None, **kw: Any) -> str:
        r = self.call(name, args, **kw)
        assert r.is_error, f"{name}({_short(args)}) should have failed but returned: {r.text[:500]}"
        return r.text

    def wait_until(self, name: str, args: dict[str, Any], done: Any, timeout: float = 240, every: float = 1.0) -> tuple[Any, float | None]:
        """Call ``name`` until ``done(data)`` is true. Returns (last data, seconds) or (last data, None) on timeout."""
        t0 = time.perf_counter()
        d: Any = None
        while time.perf_counter() - t0 < timeout:
            d = self.call(name, args).data
            if done(d):
                return d, time.perf_counter() - t0
            time.sleep(every)
        return d, None

    # -- everything else
    def list_tools(self) -> list[Any]:
        return self._run(self._client.list_tools()).tools

    def list_resources(self) -> list[Any]:
        return self._run(self._client.list_resources()).resources

    def read_resource(self, uri: Any) -> Any:
        return self._run(self._client.read_resource(uri))

    def list_prompts(self) -> list[Any]:
        return self._run(self._client.list_prompts()).prompts

    def get_prompt(self, name: str, args: dict[str, str] | None = None) -> Any:
        return self._run(self._client.get_prompt(name, args or {}))

    def close(self) -> None:
        if self._closing is not None:
            self._loop.call_soon_threadsafe(self._closing.set)
            try:
                self._main_future.result(30)
            except Exception:  # noqa: BLE001
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    def __enter__(self) -> "McpSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _short(args: Any) -> str:
    s = json.dumps(args, ensure_ascii=False, default=str) if args else ""
    return s if len(s) < 200 else s[:200] + "…"
