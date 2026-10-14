"""UI: Security view v2 – a change is previewed, runs only after confirmation (tick, or the typed name for a DROP),
the password is masked and never shows on the page, and the Audit log tab loads.

Cluster writes are limited to zz_test_ objects created here (a role and a user), dropped through the app and, as a
fallback, with IF EXISTS statements in teardown; the test checks that nothing is left. The password is random and
never printed."""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from support import McpSession
from uihelpers import app_frame, banners, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]


def _leftovers(cluster: McpSession, tag: str) -> list[Any]:
    r = cluster.ok("run_sql", {"sql": "SELECT USER AS o FROM information_schema.USERS WHERE USER LIKE 'zz\\_test\\_%' UNION ALL "
                                      "SELECT ROLE FROM information_schema.ROLES WHERE ROLE LIKE 'zz\\_test\\_%'"})
    return [x for x in r.get("rows", []) if tag in str(x)]


@pytest.fixture
def names(cluster: McpSession):
    tag = secrets.token_hex(3)
    n = {"tag": tag, "role": f"zz_test_r{tag}", "user": f"zz_test_u{tag}"}
    try:
        yield n
    finally:
        cluster.call("run_sql", {"sql": f"DROP ROLE IF EXISTS '{n['role']}'"})
        cluster.call("run_sql", {"sql": f"DROP USER IF EXISTS '{n['user']}'@'%'"})
        assert not _leftovers(cluster, tag), "zz_test_ objects left behind"


def _panel(f: Any) -> Any:
    return f.locator("#chg .sc-chg")


def _preview(f: Any, expect_sql: str) -> None:
    _panel(f).get_by_role("button", name="Preview SQL").click()
    assert wait_js(f, f"(document.querySelector('#chg pre')?.textContent || '').includes({expect_sql!r})", 30), \
        _panel(f).inner_text()[:800]


def test_security_change_preview_confirm(ui: Any, app_url: Any, cluster: McpSession, names: dict[str, str]) -> None:
    page, errors = ui
    cap = cluster.ok("security_admin_capabilities")
    if not (cap["users"]["ok"] and cap["roles"]["ok"]):
        pytest.skip("The connected user can't create users and roles")
    page.goto(app_url("security", {"tab": "roles"}), wait_until="domcontentloaded")
    f = app_frame(page, 30)
    new_role = f.get_by_role("button", name="+ New role")
    new_role.wait_for(timeout=60_000)
    assert wait_js(f, "!document.querySelector('[data-act=\"create_role\"]').disabled", 30)

    # 1. Create a role: preview first, Run stays off until the tick.
    new_role.click()
    p = _panel(f)
    p.get_by_label("Role name").fill(names["role"])
    p.get_by_label("Role name").press("Tab")
    _preview(f, f"CREATE ROLE '{names['role']}';")
    run = p.get_by_role("button", name="Run")
    assert run.is_disabled(), "Run must wait for the confirmation"
    p.get_by_label("I have checked the statements").check()
    assert run.is_enabled()
    run.click()
    assert wait_js(f, "/ok: CREATE ROLE/.test(document.querySelector('#chg')?.innerText || '')", 30), p.inner_text()[:800]
    assert names["role"] in cluster.ok("security_admin_capabilities")["pick"]["roles"]
    p.get_by_role("button", name="Close").click()

    # 2. Drop it: needs the typed name; a wrong name keeps Run off.
    row = f.locator("tr", has=f.locator("td.mono", has_text=names["role"])).first
    assert wait_js(f, f"document.body.innerText.includes({names['role']!r})", 30), "the new role isn't listed after refresh"
    row.locator('[data-act="drop_role"]').click()
    _preview(f, f"DROP ROLE '{names['role']}';")
    typed = p.get_by_label("Type the name to confirm")
    typed.fill(names["role"][:-1])
    assert p.get_by_role("button", name="Run").is_disabled()
    typed.fill(names["role"])
    p.get_by_role("button", name="Run").click()
    assert wait_js(f, "/ok: DROP ROLE/.test(document.querySelector('#chg')?.innerText || '')", 30), p.inner_text()[:800]
    p.get_by_role("button", name="Close").click()

    # 3. Create a user: the password is masked in the preview and never appears on the page.
    pw = secrets.token_urlsafe(18)
    f.get_by_role("tab", name="Users").click()
    f.get_by_role("button", name="+ New user").click()
    p.get_by_label("User name").fill(names["user"])
    p.get_by_label("User name").press("Tab")
    pw_field = p.get_by_label("Password")
    assert pw_field.get_attribute("type") == "password"
    _preview(f, f"CREATE USER '{names['user']}'@'%' IDENTIFIED BY '********';")
    p.get_by_label("I have checked the statements").check()
    assert p.get_by_role("button", name="Run").is_disabled(), "Run needs the password"
    pw_field.fill(pw)
    p.get_by_role("button", name="Run").click()
    assert wait_js(f, "/ok: CREATE USER/.test(document.querySelector('#chg')?.innerText || '')", 30), p.inner_text()[:800]
    assert pw_field.input_value() == "", "the password field should be emptied after the run"
    assert pw not in text(f) and pw not in f.content(), "the password must never be on the page"

    # 4. Drop the user through the app (typed name).
    p.get_by_role("button", name="Close").click()
    cluster.ok("security_change_apply", {"action": {"action": "drop_user", "user": names["user"]}, "confirm": True,
                                         "typed_confirmation": names["user"]})

    # 5. The Audit log tab loads (read-only) and explains how to turn it on when it is off.
    f.get_by_role("tab", name="Audit log").click()
    assert wait_js(f, "/auditlog_level/.test(document.body.innerText)", 30), text(f)[:500]
    assert wait_js(f, "/Audit logging is (on|off)/.test(document.body.innerText)", 10)
    assert not banners(page), banners(page)
    assert not errors.drain()
