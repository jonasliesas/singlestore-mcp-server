"""Fixtures for the SingleStore MCP server test suite.

One test server process (stdio, through the restart supervisor like Claude starts it) is shared by the
whole session. It runs against an isolated SINGLESTORE_MCP_HOME (see support.seed_home) and a temporary
SQL folder, with the background alert checker off.

Markers (see pyproject.toml): ``cluster`` tests are skipped when the cluster can't be reached, ``sas``
tests when SAS Viya can't, and ``assistants`` tests unless ``--assistants`` is given. ``ui`` tests that
need the cluster are marked ``cluster`` too.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from support import DB, McpSession, remove_links, seed_home, server_env


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--assistants", action="store_true", default=False,
                     help="also run the tests that ask the in-app Claude assistants (uses Claude)")


# ----------------------------------------------------------------------------- isolated server state

@pytest.fixture(scope="session")
def test_home(tmp_path_factory: pytest.TempPathFactory):
    home = tmp_path_factory.mktemp("s2home")
    seed_home(home)
    try:
        yield home
    finally:
        remove_links(home)


@pytest.fixture(scope="session")
def sql_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("sqlfiles")


@pytest.fixture(scope="session")
def mcp(test_home: Path, sql_dir: Path):
    """The shared test server session (sync facade)."""
    session = McpSession(server_env(test_home, sql_dir), read_timeout=600)
    try:
        yield session
    finally:
        try:
            # Don't leave a workload running or a kernel behind.
            listing = session.call("workload_list", timeout=20).data
            running = listing.get("running") if isinstance(listing, dict) else None
            if isinstance(running, dict) and running.get("id"):
                session.call("workload_stop", {"run_id": running["id"]}, timeout=20)
        except Exception:  # noqa: BLE001
            pass
        session.close()


# ----------------------------------------------------------------------------- gating

@pytest.fixture(scope="session")
def cluster(mcp: McpSession) -> McpSession:
    """The session, if the cluster answers; skips the test otherwise."""
    r = mcp.call("list_databases", timeout=45)
    if r.is_error:
        pytest.skip(f"SingleStore cluster not reachable (VPN?): {r.text[:200]}")
    return mcp


@pytest.fixture(scope="session")
def sas(cluster: McpSession) -> McpSession:
    r = cluster.call("sas_viya_test", timeout=60)
    if r.is_error or not (isinstance(r.data, dict) and r.data.get("ok")):
        reason = r.data.get("error") if isinstance(r.data, dict) else r.text
        pytest.skip(f"SAS Viya not reachable or not signed in: {str(reason)[:200]}")
    return cluster


@pytest.fixture(autouse=True)
def _gates(request: pytest.FixtureRequest) -> None:
    node = request.node
    if node.get_closest_marker("assistants") and not request.config.getoption("--assistants"):
        pytest.skip("assistant tests use Claude: run with --assistants")
    if node.get_closest_marker("cluster"):  # UI tests that need the cluster say so (``ui`` alone doesn't)
        request.getfixturevalue("cluster")
    if node.get_closest_marker("sas"):
        request.getfixturevalue("sas")


# ----------------------------------------------------------------------------- browser (pytest-playwright)

@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    channel = os.environ.get("S2_TEST_BROWSER_CHANNEL", "msedge")
    args = dict(browser_type_launch_args)
    if channel and channel.lower() not in ("", "bundled", "chromium"):
        args["channel"] = channel
    return args


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    return {**browser_context_args, "viewport": {"width": 1400, "height": 900}}


@pytest.fixture
def app_url(mcp: McpSession):
    """``app_url(tool, args)``: a browser-view link served by the test server."""

    def make(tool: str, arguments: dict[str, Any] | None = None) -> str:
        return mcp.ok("browser_link", {"tool": tool, "arguments": arguments or {}})["url"]

    return make


@pytest.fixture(scope="session")
def database() -> str:
    return DB
