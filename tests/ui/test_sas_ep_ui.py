"""UI: the SAS Embedded Process overview in Query History's Usage tab and the EP card's time breakdown and tips.
Read-only on the cluster (EP calls are never profiled); skipped when the history has no SAS EP calls."""

from __future__ import annotations

from typing import Any

import pytest
from uihelpers import banner, banners, find_frame, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]


def test_ep_overview_and_card(ui: Any, app_url: Any) -> None:
    page, errors = ui
    page.goto(app_url("query_history", {"tab": "usage"}), wait_until="domcontentloaded")
    qh = find_frame(page, "Query History", 30)
    assert wait_js(qh, "!document.querySelector('.s2-spinner') && /Most used tables/.test(document.body.innerText)", 60), \
        "the Usage tab didn't load"
    ov = qh.locator('section[aria-label="SAS Embedded Process overview"]')
    if ov.count() == 0:
        pytest.skip("no SAS EP calls in the history")
    assert "traced EP calls" in ov.inner_text()
    ov.locator("tbody tr").first.click()       # → Queries, Source = SAS EP
    rows = qh.locator(".qh-table tbody tr")
    rows.first.wait_for(timeout=30_000)
    rows.first.click()
    qh.get_by_role("button", name="Get tuning recommendations").first.click()
    card = qh.locator('section[aria-label="SAS Embedded Process analysis"]')
    card.wait_for(timeout=90_000)
    assert wait_js(qh, "/Where the time went/.test(document.body.innerText) && /Tuning tips/.test(document.body.innerText)", 60), \
        card.inner_text()[:800]
    assert card.locator(".qh-ep-split").count() == 1, "no time breakdown bar"
    assert card.locator(".qh-ep-tips li").count() >= 2
    assert not qh.get_by_role("button", name="Profile (runs it)").count(), "EP calls must never be profiled"
    assert not banner(qh) and not banners(page), banners(page)
    assert not errors.drain()
