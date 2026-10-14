"""UI test helpers: frame lookup across the apps' nested sandboxed iframes, error collection, and a
screenshot in tests/ui/_artifacts/ when a test fails."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from uihelpers import Errors

ARTIFACTS = Path(__file__).parent / "_artifacts"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: Any):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture
def ui(page: Any, request: pytest.FixtureRequest):
    """The Playwright page plus an error collector; screenshots the page if the test fails."""
    errors = Errors(page)
    page.set_default_timeout(30_000)
    yield page, errors
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", request.node.nodeid.split("::", 1)[-1])[:120]
        try:
            page.screenshot(path=str(ARTIFACTS / f"{name}.png"), full_page=True)
        except Exception:  # noqa: BLE001 - the page may be gone
            pass
