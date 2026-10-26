"""The Connections window's SAS Viya connections in a browser (Edge by default), served by the test server's
browser view. Needs neither the cluster nor SAS Viya: the connections are made up (``*.invalid``), signing in
is stubbed, and they're deleted afterwards (the previously active connection is active again)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from playwright.sync_api import expect
from support import McpSession
from uihelpers import app_frame, banners, text, wait_js

pytestmark = pytest.mark.ui

_VIYA_A, _VIYA_B, _VIYA_NEW = "UI test Viya A", "UI test Viya B", "UI test Viya new"


@pytest.fixture
def viya_connections(mcp: McpSession):
    """Two made-up SAS Viya connections (nothing reaches them); afterwards they're deleted and the previously
    active connection is active again."""
    before = mcp.ok("sas_connections_state")
    mcp.ok("sas_connection_save", {"connection": {"name": _VIYA_A, "url": "https://viya-a.invalid", "startup_code": "%put a;"}})
    mcp.ok("sas_connection_save", {"connection": {"name": _VIYA_B, "url": "https://viya-b.invalid", "compute_context": "Other context"}})
    try:
        yield mcp
    finally:
        names = {c["name"] for c in mcp.ok("sas_connections_state")["connections"]}
        for name in (_VIYA_A, _VIYA_B, _VIYA_NEW):
            if name in names:
                mcp.call("sas_connection_delete", {"name": name})
        if before["active"]:
            mcp.call("sas_connection_activate", {"name": before["active"]})


def _call_name(route: Any) -> str:
    try:
        return json.loads(route.request.post_data or "{}").get("name") or ""
    except ValueError:
        return ""


def test_connections_sas_viya_list_and_no_save_on_sign_in(ui, app_url, viya_connections: McpSession) -> None:
    """Regression: the SAS Viya form was usable before its state had loaded (Viya can be slow to answer), and
    Sign in saved the form first, so an early click wrote an empty address to sas.json and deleted the sign-in.
    Now the list only renders once loaded, Sign in only signs in, and connections are saved, used and deleted
    from the list like the SingleStore ones."""
    page, errors = ui
    calls: list[str] = []
    held: list[Any] = []
    hold = {"on": True}

    def route(r: Any) -> None:
        name = _call_name(r)
        calls.append(name)
        if name == "sas_connections_state" and hold["on"]:
            held.append(r)  # Viya (or the credential store) answering slowly
        elif name == "sas_viya_sign_in":  # stubbed: the real one opens SAS Logon in a browser window
            args = json.loads(r.request.post_data)["arguments"]
            data = {"authorize_url": "https://viya-b.invalid/SASLogon/oauth/authorize?client_id=vscode", "name": args.get("name")}
            r.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"content": [{"type": "text", "text": "Sign in in the browser"}], "structuredContent": data}))
        else:
            r.continue_()

    page.route("**/call", route)
    page.goto(app_url("connections_window", {}), wait_until="domcontentloaded")
    f = app_frame(page, 30)
    sas_section = f.locator('section[aria-label="SAS Viya"]')
    assert wait_js(f, "document.querySelector('section[aria-label=\"SAS Viya\"]')?.innerText.includes('Loading SAS Viya connections')", 20), \
        "no loading state: " + text(f)[-400:]
    # While loading there's nothing to type into or click (the old form was here, empty and usable).
    assert sas_section.locator("input, textarea").count() == 0
    assert sas_section.get_by_role("button", name=re.compile("^Sign in")).count() == 0
    expect(sas_section.get_by_role("button", name="+ New Viya connection")).to_be_disabled()
    hold["on"] = False
    for r in held:
        r.continue_()
    assert wait_js(f, f"document.querySelector('[data-sas-connection=\"{_VIYA_B}\"]')", 20), "no SAS Viya list: " + text(f)[-400:]
    card_b = sas_section.locator(f'[data-sas-connection="{_VIYA_B}"]')
    expect(card_b).to_contain_text("https://viya-b.invalid")
    expect(card_b).to_contain_text("not signed in")

    # Sign in: opens SAS Logon (stubbed) and asks for the code; saves nothing.
    card_b.get_by_role("button", name="Sign in", exact=True).click()
    expect(card_b.get_by_label("Sign-in code")).to_be_visible()
    assert not [c for c in calls if c in ("sas_viya_save", "sas_connection_save")], calls
    saved = {c["name"]: c for c in viya_connections.ok("sas_connections_state")["connections"]}
    assert saved[_VIYA_A]["url"] == "https://viya-a.invalid" and saved[_VIYA_A]["startup_code"] == "%put a;"
    assert saved[_VIYA_B]["url"] == "https://viya-b.invalid"
    card_b.get_by_role("button", name="Cancel").click()

    # Use: B becomes the active connection.
    card_b.get_by_role("button", name="Use", exact=True).click()
    expect(card_b).to_contain_text("Active")
    assert viya_connections.ok("sas_connections_state")["active"] == _VIYA_B

    # + New, Save: a third connection; then Delete it.
    sas_section.get_by_role("button", name="+ New Viya connection").click()
    sas_section.get_by_label("Viya connection name").fill(_VIYA_NEW)
    sas_section.get_by_label("Viya address").fill("viya-new.invalid")
    sas_section.get_by_role("button", name="Save", exact=True).click()
    card_new = sas_section.locator(f'[data-sas-connection="{_VIYA_NEW}"]')
    expect(card_new).to_contain_text("https://viya-new.invalid")
    assert viya_connections.ok("sas_connections_state")["active"] == _VIYA_B  # saving doesn't switch
    card_new.get_by_role("button", name="Delete").click()
    card_new.locator(".s2-confirm .danger").click()  # the inline confirmation
    expect(card_new).to_have_count(0)
    assert _VIYA_NEW not in {c["name"] for c in viya_connections.ok("sas_connections_state")["connections"]}
    assert not banners(page), banners(page)
    assert not errors.drain()


def test_connections_viya_automatic_library_and_sas9_settings(ui, app_url, viya_connections: McpSession) -> None:
    """Edit on a Viya connection: the automatic SingleStore library can be turned off and the SAS 9 sign-on
    (code, server, libraries) is saved with the connection and shown on its card."""
    page, errors = ui
    page.goto(app_url("connections_window", {}), wait_until="domcontentloaded")
    f = app_frame(page, 30)
    sas_section = f.locator('section[aria-label="SAS Viya"]')
    assert wait_js(f, f"document.querySelector('[data-sas-connection=\"{_VIYA_A}\"]')", 20), "no SAS Viya list: " + text(f)[-400:]
    card = sas_section.locator(f'[data-sas-connection="{_VIYA_A}"]')
    card.get_by_role("button", name="Edit").click()
    auto = sas_section.get_by_label("Automatic SingleStore library")
    expect(auto).to_be_checked()
    auto.uncheck()
    sas_section.get_by_label("SAS 9 sign-on code").fill("%include s9 / nosource2;")
    sas_section.get_by_label("SAS 9 server").fill("cauki")
    sas_section.get_by_label("SAS 9 libraries").fill("stg sasdata")
    sas_section.get_by_role("button", name="Save", exact=True).click()
    expect(card).to_contain_text("SAS 9 CAUKI")
    expect(card).to_contain_text("not automatic")
    saved = {c["name"]: c for c in viya_connections.ok("sas_connections_state")["connections"]}[_VIYA_A]
    assert saved["auto_libname"] is False and saved["sas9_server"] == "CAUKI" and saved["sas9_libraries"] == "stg sasdata"
    assert saved["sas9_signon"] == "%include s9 / nosource2;" and saved["startup_code"] == "%put a;"
    assert not banners(page), banners(page)
    assert not errors.drain()
