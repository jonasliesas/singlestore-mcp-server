"""UI: the Schema Explorer's "Copy to a new table with this shard key…" (a reshard script next to a simulation result:
shown, saved as .sql, nothing runs) and the Security view's "Drop view…" for a column view (listed in the "Grant
columns (view)" form, previewed with the grants that go, run after the typed name).

Cluster writes: only a zz_test_ table and view in the `test` database for the Drop view test (dropped in teardown,
checked to be gone). The reshard script test only reads SASDP.CARS."""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect
from support import DB, McpSession
from uihelpers import app_frame, banners, find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]


def test_schema_explorer_reshard_script(ui: Any, app_url: Any, cluster: McpSession, sql_dir: Path) -> None:
    page, errors = ui
    page.goto(app_url("schema_explorer", {"database": DB, "table": "CARS"}), wait_until="domcontentloaded")
    se = find_frame(page, re.compile(r"Schema Explorer"), 30)
    se.get_by_role("tab", name="Distribution").click()
    region = se.get_by_role("region", name="Reshard simulation")
    expect(region).to_be_visible(timeout=30_000)
    cols = region.get_by_role("group", name="Candidate shard key columns")
    cols.get_by_label(re.compile(r"^\s*Make(\s|$)")).check()
    cols.get_by_label(re.compile(r"^\s*Model(\s|$)")).check()
    region.get_by_role("button", name="Simulate", exact=True).click()
    copy_btn = region.get_by_role("button", name="Copy to a new table with this shard key…")
    copy_btn.wait_for(timeout=60_000)  # offered next to the simulation result only
    copy_btn.click()
    box = se.get_by_role("region", name="Reshard script")
    expect(box).to_be_visible(timeout=60_000)
    script = box.get_by_label("Script", exact=True).text_content()
    assert "Nothing has been run" in script and "-- Shard key: " in script and "-> (Make, Model)" in script, script[:1500]
    assert f"CREATE TABLE `{DB}`.`CARS__reshard` (" in script and "SHARD KEY (`Make`, `Model`)" in script
    assert f"INSERT INTO `{DB}`.`CARS__reshard` SELECT * FROM `{DB}`.`CARS`;" in script
    assert f"ALTER TABLE `{DB}`.`CARS` RENAME TO `{DB}`.`CARS__old`;" in script
    assert f"-- DROP TABLE `{DB}`.`CARS__old`;" in script
    warn = box.get_by_label("Before you run it").inner_text()
    assert "needs as much again" in warn and "quiet window" in warn and "Duration" in warn, warn
    for name in ("Copy SQL", "Open in SQL Editor", "Save as .sql…"):
        assert box.get_by_role("button", name=name).is_visible()

    # Save as .sql (the shared file browser): into the test SQL folder with the suggested name.
    box.get_by_role("button", name="Save as .sql…").click()
    form = box.get_by_role("group", name="Save the script as a .sql file")
    expect(form.get_by_role("tab")).to_have_text(["This computer", "SAS Content", "SAS Server"], timeout=10_000)
    form.get_by_role("tab", name="This computer").click()
    form.get_by_role("button", name="SQL folder", exact=True).click()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (form.locator(".s2-muted").first.text_content() or "").endswith(sql_dir.name):
        time.sleep(0.2)
    name_field = form.get_by_label("File name")
    assert re.match(r"^reshard-\d{8}-\d{4}-cars-make-model\.sql$", name_field.input_value()), name_field.input_value()
    saved_name = f"zz_test_reshard_{secrets.token_hex(3)}.sql"
    name_field.fill(saved_name)
    form.get_by_role("button", name="Save", exact=True).click()
    saved = sql_dir / saved_name
    deadline = time.monotonic() + 15
    while not saved.exists() and time.monotonic() < deadline:
        time.sleep(0.3)
    assert saved.exists(), "Save as .sql didn't write the file"
    assert "SHARD KEY (`Make`, `Model`)" in saved.read_text(encoding="utf-8")

    # Nothing ran: no work tables.
    r = cluster.ok("run_sql", {"sql": f"SELECT TABLE_NAME AS t FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{DB}'"
                                      " AND TABLE_NAME IN ('CARS__reshard', 'CARS__old')"})
    assert not r.get("rows"), "making the script must not run it"
    box.get_by_role("button", name="Close the reshard script").click()
    expect(box).to_have_count(0)
    assert not banners(page), banners(page)
    assert not [e for e in errors.drain() if "favicon" not in e]


def test_security_drop_view(ui: Any, app_url: Any, cluster: McpSession) -> None:
    page, errors = ui
    cap = cluster.ok("security_admin_capabilities")
    tag = secrets.token_hex(3)
    table, view = f"zz_test_dvt_{tag}", f"zz_test_dvv_{tag}"
    cluster.ok("run_sql", {"sql": f"CREATE TABLE `test`.`{table}` (a INT, b INT, secret INT, SHARD KEY (a))"})
    cluster.ok("run_sql", {"sql": f"CREATE VIEW `test`.`{view}` AS SELECT a, b FROM `test`.`{table}`"})
    try:
        page.goto(app_url("security", {"tab": "roles"}), wait_until="domcontentloaded")
        f = app_frame(page, 30)
        f.get_by_role("button", name="+ New role").wait_for(timeout=60_000)
        f.get_by_role("button", name="Grant columns (view)…").first.click()
        p = f.locator("#chg .sc-chg")
        p.get_by_label("Database", exact=True).select_option("test")
        assert wait_js(f, f"[...document.querySelectorAll('#chg select[aria-label=\"Table\"] option')].some((o) => o.value === '{table}')", 30)
        p.get_by_label("Table", exact=True).select_option(table)
        listed = p.get_by_role("group", name="Existing column views")
        expect(listed).to_contain_text(view, timeout=30_000)
        assert "(a, b)" in listed.inner_text() and "no grants" in listed.inner_text()
        listed.get_by_role("button", name="Drop view…").click()

        # The panel is now the Drop view change: preview, the grants note, typed name, Run.
        expect(f.get_by_role("dialog", name="Drop view")).to_be_visible(timeout=10_000)
        assert f"test.{view}" in p.inner_text() and "revoked first" in p.get_by_label("Drop view note").inner_text()
        p.get_by_role("button", name="Preview SQL").click()
        assert wait_js(f, f"(document.querySelector('#chg pre')?.textContent || '').includes('DROP VIEW `test`.`{view}`;')", 30), \
            p.inner_text()[:800]
        assert f"No grants on test.{view}" in p.inner_text() and f"Reads test.{table}: that table stays" in p.inner_text()
        assert p.get_by_role("button", name="Copy SQL").is_visible()  # the script route is there too
        if p.get_by_label("Run is off").count():
            pytest.skip(f"{cap['me']['user']} can't drop the view here: {p.get_by_label('Run is off').inner_text()}")
        run = p.get_by_role("button", name="Run")
        assert run.is_disabled(), "a DROP needs the typed name"
        p.get_by_label("Type the name to confirm").fill(view)
        run.click()
        assert wait_js(f, "/ok: DROP VIEW/.test(document.querySelector('#chg')?.innerText || '')", 30), p.inner_text()[:800]
        r = cluster.ok("run_sql", {"sql": f"SELECT TABLE_NAME AS t FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'test' AND TABLE_NAME = '{view}'"})
        assert not r.get("rows"), "the view is gone"
        assert not banners(page), banners(page)
        assert not errors.drain(), text(f)[-400:]
    finally:
        cluster.call("run_sql", {"sql": f"DROP VIEW IF EXISTS `test`.`{view}`"})
        cluster.call("run_sql", {"sql": f"DROP TABLE IF EXISTS `test`.`{table}`"})
        r = cluster.ok("run_sql", {"sql": f"SELECT TABLE_NAME AS t FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'test' AND TABLE_NAME LIKE '%{tag}'"})
        assert not r.get("rows"), "zz_test_ objects left behind"
