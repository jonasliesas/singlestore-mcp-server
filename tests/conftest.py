"""Fixtures for the SingleStore MCP server test suite.

One test server process (stdio, through the restart supervisor like Claude starts it) is shared by the
whole session. It runs against an isolated SINGLESTORE_MCP_HOME (see support.seed_home) and a temporary
SQL folder, with the background alert checker off.

Markers (see pyproject.toml): ``cluster`` tests are skipped when the cluster can't be reached, ``sas``
tests unless ``--sas`` is given (a cold SAS session takes ~35 s; run them before a release or after SAS
changes) and when SAS Viya can't be reached, and ``assistants`` tests unless ``--assistants`` is given. ``ui`` tests that
need the cluster are marked ``cluster`` too.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from support import DB, McpSession, remove_links, sas_signin_mode, seed_home, server_env

# In-process tests never start a SAS session in the background (test_sas_prewarm turns it on for itself).
os.environ["SINGLESTORE_MCP_SAS_PREWARM"] = "0"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--assistants", action="store_true", default=False,
                     help="also run the tests that ask the in-app Claude assistants (uses Claude)")
    parser.addoption("--sas", action="store_true", default=False,
                     help="also run the tests that need SAS Viya (slow: they start SAS sessions)")


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
def sas(request: pytest.FixtureRequest, cluster: McpSession) -> McpSession:
    if not request.config.getoption("--sas"):
        pytest.skip("SAS tests are slow: run with --sas")
    mode = sas_signin_mode()
    r = cluster.call("sas_viya_test", timeout=60)
    if r.is_error or not (isinstance(r.data, dict) and r.data.get("ok")):
        reason = r.data.get("error") if isinstance(r.data, dict) else r.text
        pytest.skip(f"SAS Viya not reachable or not signed in (S2_TEST_SAS_SIGNIN={mode}): {str(reason)[:200]}")
    # Say which sign-in the SAS tests run with: without S2_TEST_SAS_SECRETS the test home has none of its own, and
    # the server reuses the SAS Viya MCP server's / CLI's cached sign-in (see support.sas_signin_mode).
    # (own: importing is off in the test server, so this is the copied sign-in, whatever it was first made with.)
    used = r.data.get("signed_in_with") or "unknown"
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"SAS tests sign in to SAS Viya with: {used} (S2_TEST_SAS_SIGNIN={mode})")
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


# ----------------------------------------------------------------------------- UI tests (tests/ui)
# These live here, not in tests/ui/conftest.py: pytest 9 binds a conftest's fixtures to the Directory
# node it was collected under, and a call that mixes files (``pytest tests/test_x.py tests/ui/test_y.py
# tests/test_z.py tests/ui/test_w.py``) builds a second ``tests/ui`` node for the later file, which then
# didn't see them ("fixture 'ui' not found"). The ``tests`` node is shared by every file, so this works.

UI_ARTIFACTS = Path(__file__).parent / "ui" / "_artifacts"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: Any):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture
def ui(page: Any, request: pytest.FixtureRequest):
    """The Playwright page plus an error collector; screenshots the page into tests/ui/_artifacts/ if the test fails."""
    from uihelpers import Errors  # tests/ui is on sys.path once a UI test module is imported

    errors = Errors(page)
    page.set_default_timeout(30_000)
    yield page, errors
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        UI_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", request.node.nodeid.split("::", 1)[-1])[:120]
        try:
            page.screenshot(path=str(UI_ARTIFACTS / f"{name}.png"), full_page=True)
        except Exception:  # noqa: BLE001 - the page may be gone
            pass
