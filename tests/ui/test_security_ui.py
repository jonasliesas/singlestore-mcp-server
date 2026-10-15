"""UI: Security view v2 – a change is previewed, runs only after confirmation (tick, or the typed name for a DROP),
the password is masked and never shows on the page, and the Audit log tab loads.

Cluster writes are limited to zz_test_ objects created here (a role and a user), dropped through the app and, as a
fallback, with IF EXISTS statements in teardown; the test checks that nothing is left. The password is random and
never printed."""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
from support import DB, McpSession
from uihelpers import app_frame, banners, find_frame, text, wait_js

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


# ----------------------------------------------------------------------------- SQL script instead of Run (nothing runs)

def _visible_sql(page: Any, timeout: float = 30) -> Any:
    """The SQL Editor frame of the shown tab, once it has restored its draft."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for fr in page.frames:
            try:
                if fr.evaluate("document.title") == "SQL Editor" and fr.frame_element().get_attribute("hidden") is None \
                        and fr.evaluate("document.body.innerText.includes('opens a new tab')"):
                    return fr
            except Exception:  # noqa: BLE001 - detached while switching tabs
                continue
        time.sleep(0.3)
    raise AssertionError("no visible, restored SQL Editor tab")


def test_security_script_basket_opens_in_sql_editor(ui: Any, app_url: Any, cluster: McpSession, sql_dir: Path) -> None:
    """Preview → Add to script (create role, create group, grant the role to the group, which needs the role from
    the script so Run is off) → Open in SQL Editor: a new editor tab holds the commented script, and nothing ran on
    the cluster. Works without any change privilege; creates nothing."""
    page, errors = ui
    tag = secrets.token_hex(3)
    role, group = f"zz_test_r{tag}", f"zz_test_g{tag}"
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql"}), wait_until="domcontentloaded")
    ws = find_frame(page, "SingleStore Workspace", 30)
    find_frame(page, "SQL Editor", 30)
    tabs_before = ws.evaluate("[...document.querySelectorAll('.ws-tabs .ws-tab')].map((t) => t.dataset.tab)")
    ws.locator('nav.ws-rail button[aria-label^="Security"]').click()
    f = find_frame(page, "Security", 40, min_text=40)
    f.get_by_role("button", name="+ New user").wait_for(timeout=60_000)  # opened (on Users) and capabilities known
    f.get_by_role("tab", name="Roles & groups").click()
    new_role = f.get_by_role("button", name="+ New role")
    new_role.wait_for(timeout=60_000)
    p = _panel(f)
    basket = f.locator("#sc-basket")
    new_tab = None
    try:
        # 1. Create role → preview → Add to script.
        new_role.click()
        p.get_by_label("Role name").fill(role)
        p.get_by_label("Role name").press("Tab")
        _preview(f, f"CREATE ROLE '{role}';")
        p.get_by_role("button", name="Add to script").click()
        assert wait_js(f, "document.querySelectorAll('#sc-basket li').length === 1", 20), text(f)[-600:]
        p.get_by_role("button", name="Cancel").click()

        # 2. Create group → Add to script.
        f.get_by_role("button", name="+ New group").click()
        p.get_by_label("Group name").fill(group)
        p.get_by_label("Group name").press("Tab")
        _preview(f, f"CREATE GROUP '{group}';")
        p.get_by_role("button", name="Add to script (1)").click()
        assert wait_js(f, "document.querySelectorAll('#sc-basket li').length === 2", 20), text(f)[-600:]
        p.get_by_role("button", name="Cancel").click()

        # 3. Grant the script's role to the script's group: only possible as a script (Run is off).
        f.get_by_role("button", name="Grant role to group…").first.click()
        p.get_by_label("Role", exact=True).select_option(role)
        p.get_by_label("Group", exact=True).select_option(group)
        _preview(f, f"GRANT ROLE '{role}' TO '{group}';")
        assert p.get_by_label("Run is off").is_visible()
        assert p.get_by_role("button", name="Run").count() == 0
        p.get_by_role("button", name="Add to script (2)").click()
        assert wait_js(f, "document.querySelectorAll('#sc-basket li').length === 3", 20), text(f)[-600:]
        p.get_by_role("button", name="Cancel").click()

        # The basket's script: header, safe order.
        basket.get_by_role("button", name="Show script").click()
        assert wait_js(f, "(document.querySelector('#sc-basket pre')?.textContent || '').includes('Nothing has been run')", 20)
        script = basket.locator("pre").text_content()
        assert script.index(f"CREATE ROLE '{role}';") < script.index(f"CREATE GROUP '{group}';") < script.index(f"GRANT ROLE '{role}' TO '{group}';")

        # Save as .sql: into the (test) SQL folder, with the suggested name; no password anywhere.
        basket.get_by_role("button", name="Save as .sql…").click()
        name_field = basket.get_by_label("Script file name")
        assert re.match(r"^security-\d{8}-\d{4}-3-changes\.sql$", name_field.input_value()), name_field.input_value()
        saved_name = f"zz_test_script_{tag}.sql"
        name_field.fill(saved_name)
        basket.get_by_role("button", name="Save", exact=True).click()
        saved = sql_dir / saved_name
        deadline = time.monotonic() + 15
        while not saved.exists() and time.monotonic() < deadline:
            time.sleep(0.3)
        assert saved.exists(), "Save as .sql didn't write the file"
        body = saved.read_text(encoding="utf-8")
        assert f"GRANT ROLE '{role}' TO '{group}';" in body and "********" not in body

        # 4. Open in SQL Editor: a new tab with the script, not run.
        basket.get_by_role("button", name="Open in SQL Editor").click()
        assert wait_js(ws, f"document.querySelectorAll('.ws-tabs .ws-tab').length === {len(tabs_before) + 1}", 20), "no new SQL tab"
        new_tab = ws.evaluate("document.querySelector('.ws-tabs .ws-tab.on')?.dataset.tab ?? null")
        editor = _visible_sql(page)
        assert wait_js(editor, f"document.querySelector('.cm-content').innerText.includes(\"GRANT ROLE '{role}' TO '{group}';\")", 15), \
            editor.evaluate("document.querySelector('.cm-content')?.innerText ?? ''")[:800]
        assert "-- SingleStore security changes" in editor.evaluate("document.querySelector('.cm-content').innerText")
        time.sleep(1.0)
        pick = cluster.ok("security_admin_capabilities")["pick"]
        assert role not in pick["roles"] and group not in pick["groups"], "the script must not have run"
        assert not banners(page), banners(page)
        assert not errors.drain()
    finally:
        if new_tab:
            cluster.call("sql_editor_tab_close", {"tab_id": new_tab})
        cluster.call("run_sql", {"sql": f"DROP ROLE IF EXISTS '{role}'"})
        cluster.call("run_sql", {"sql": f"DROP GROUP IF EXISTS '{group}'"})
