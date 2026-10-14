"""UI: the SAS Embedded Process overview in Query History's Usage tab and the EP card's time breakdown and tips.
Read-only on the cluster (EP calls are never profiled); skipped when the history has no SAS EP calls.

Every step waits for its own condition with a generous timeout: under the load of the full suite the cluster calls
behind each step (history, the full SQL of a query, EXPLAIN for the tuning) can each take tens of seconds."""

from __future__ import annotations

from typing import Any

import pytest
from uihelpers import banner, banners, find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]

# The list is idle (no spinner while the history loads) and shows rows, the first one a SAS EP call.
EP_ROWS = """(() => {
  if (document.querySelector('.qh-list .s2-spinner, .s2-empty .s2-spinner')) return false;
  const rows = [...document.querySelectorAll('.qh-table tbody tr')];
  return rows.length > 0 && /SAS EP/.test(rows[0].children[6]?.textContent ?? '');
})()"""
TUNE_READY = """[...document.querySelectorAll('section[aria-label="Selected query"] button')]
  .some((b) => b.textContent.includes('Get tuning recommendations') && !b.disabled)"""


def step(frame: Any, expr: str, timeout: float, what: str) -> None:
    assert wait_js(frame, expr, timeout), f"{what} (waited {timeout:.0f} s): " + text(frame)[-700:]


def test_ep_overview_and_card(ui: Any, app_url: Any) -> None:
    page, errors = ui
    page.goto(app_url("query_history", {"tab": "usage"}), wait_until="domcontentloaded")
    qh = find_frame(page, "Query History", 60)
    step(qh, "!document.querySelector('.s2-spinner') && /Most used tables/.test(document.body.innerText)", 120,
         "the Usage tab didn't load")
    ov = qh.locator('section[aria-label="SAS Embedded Process overview"]')
    if ov.count() == 0:
        pytest.skip("no SAS EP calls in the history")
    assert "traced EP calls" in ov.inner_text()
    ov.locator("tbody tr").first.click()       # → Queries, Source = SAS EP
    step(qh, EP_ROWS, 120, "the Queries tab didn't list the SAS EP calls")
    qh.locator(".qh-table tbody tr").first.click()   # a group opens and selects its latest run
    # The button is enabled once the run's full SQL has loaded.
    step(qh, TUNE_READY, 120, "the selected EP call's details didn't load")
    qh.get_by_role("button", name="Get tuning recommendations").first.click()
    card = qh.locator('section[aria-label="SAS Embedded Process analysis"]')
    step(qh, "!!document.querySelector('section[aria-label=\"SAS Embedded Process analysis\"]')"
             " || !!document.querySelector('section[aria-label=\"Selected query\"] .s2-error')", 180,
         "no EP card after Get tuning recommendations")
    assert card.count() == 1, "tuning failed: " + qh.locator('section[aria-label="Selected query"]').inner_text()[-700:]
    step(qh, "/Where the time went/.test(document.body.innerText) && /Tuning tips/.test(document.body.innerText)", 60,
         "the EP card has no time breakdown / tips")
    assert card.locator(".qh-ep-split").count() == 1, "no time breakdown bar"
    assert card.locator(".qh-ep-tips li").count() >= 2
    assert not qh.get_by_role("button", name="Profile (runs it)").count(), "EP calls must never be profiled"
    assert not banner(qh) and not banners(page), banners(page)
    assert not errors.drain()
