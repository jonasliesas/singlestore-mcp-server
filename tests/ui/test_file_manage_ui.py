"""UI: rename / delete in the file browser (This computer, in the test SQL folder) with the SQL Editor tab that has
the file open following it; the Notebook's Open / Save as location bar; the Schema Explorer's reshard simulation
(read-only, only on a click). Files are created in the test server's own SQL folder only."""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect
from support import DB
from uihelpers import banners, find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]


def _visible_sql(page: Any, timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for f in page.frames:
            try:
                if f.evaluate("document.title") != "SQL Editor" or f.frame_element().get_attribute("hidden") is not None:
                    continue
                if f.evaluate("document.body.innerText.includes('opens a new tab')"):
                    return f
            except Exception:  # noqa: BLE001 - detached while switching tabs
                continue
        time.sleep(0.3)
    raise AssertionError("no visible SQL Editor tab")


def _no_problems(page: Any, errors: Any, what: str) -> None:
    assert not banners(page), f"{what}: {banners(page)}"
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, f"{what}: console errors: {errs}"


def _browse_to(sql: Any, folder: str) -> Any:
    """The open file panel's browser (not its Recent list), on This computer, in the SQL folder's sub folder."""
    sql.locator(".sq-files").get_by_role("tab", name="This computer").click()
    files = sql.locator(".sq-files .sq-browser")
    files.get_by_role("button", name="SQL folder", exact=True).click()
    files.locator(".item", has_text=folder).first.click()
    assert wait_js(sql, f"(document.querySelector('.sq-browser .s2-muted')?.textContent ?? '').endsWith({folder!r})", 20), \
        "didn't open the folder: " + text(sql)[-400:]
    return files


def test_rename_and_delete_follow_the_open_tab(ui, app_url, sql_dir: Path) -> None:
    page, errors = ui
    folder = f"zz_test_fm_{secrets.token_hex(3)}"
    (sql_dir / folder).mkdir()
    (sql_dir / folder / "a.sql").write_text("select 'renamed' as x;\n", encoding="utf-8")
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True}), wait_until="domcontentloaded")
    ws = find_frame(page, "SingleStore Workspace", 30)
    sql = _visible_sql(page)

    # Open a.sql from the folder (this tab is empty, so it opens here).
    sql.locator(".sq-filebtns").get_by_role("button", name="Open", exact=True).click()
    files = _browse_to(sql, folder)
    files.locator(".item", has_text="a.sql").click()
    # It opens in this tab, or in a new one when this tab has text (left by an earlier test).
    assert wait_js(ws, "(document.querySelector('.ws-tabs .ws-tab.on')?.innerText ?? '').includes('a.sql')", 20), "a.sql didn't open"
    sql = _visible_sql(page)
    assert wait_js(sql, "(document.querySelector('.sq-file')?.innerText ?? '').includes('a.sql')", 20), text(sql)[-400:]

    # Rename it in the browser: the file moves, the tab and its label follow.
    sql.locator(".sq-filebtns").get_by_role("button", name="Open", exact=True).click()
    files = _browse_to(sql, folder)
    files.get_by_role("button", name="Rename a.sql").click()
    box = files.get_by_label("New name for a.sql")
    expect(box).to_be_focused()
    box.fill("b")
    box.press("Enter")
    assert wait_js(sql, "!!document.querySelector('.sq-browser .item') && [...document.querySelectorAll('.sq-browser .item')].some((i) => i.innerText.includes('b.sql'))", 20), \
        "the list didn't show b.sql: " + text(sql)[-400:]
    assert (sql_dir / folder / "b.sql").is_file() and not (sql_dir / folder / "a.sql").exists()
    assert wait_js(sql, "(document.querySelector('.sq-file')?.innerText ?? '').includes('b.sql')", 10), "the editor's file label didn't follow"
    assert wait_js(ws, "(document.querySelector('.ws-tabs .ws-tab.on')?.innerText ?? '').includes('b.sql')", 10), "the tab didn't follow"
    assert "modified" not in sql.locator(".sq-file").inner_text()

    # Delete it: asked first; the tab keeps its text, now unsaved.
    files.get_by_role("button", name="Delete b.sql").click()
    files.get_by_role("button", name="Delete", exact=True).click()
    assert wait_js(sql, "![...document.querySelectorAll('.sq-browser .item')].some((i) => i.innerText.includes('b.sql'))", 20)
    assert not (sql_dir / folder / "b.sql").exists()
    assert wait_js(sql, "(() => { const l = document.querySelector('.sq-file')?.innerText ?? ''; return l.startsWith('untitled') && l.includes('●'); })()", 10), \
        "a deleted file's tab should be unsaved: " + sql.locator(".sq-file").inner_text()
    assert "select 'renamed'" in sql.evaluate("document.querySelector('.cm-content').innerText")

    # The empty folder: its name must be typed first.
    files.locator(".item", has_text="..").first.click()
    files.get_by_role("button", name=f"Delete {folder}").click()
    confirm = files.get_by_role("button", name="Delete folder", exact=True)
    expect(confirm).to_be_disabled()
    files.get_by_label(f"Type {folder} to confirm").fill(folder)
    confirm.click()
    assert wait_js(sql, f"![...document.querySelectorAll('.sq-browser .item')].some((i) => i.innerText.includes({folder!r}))", 20)
    assert not (sql_dir / folder).exists()
    _no_problems(page, errors, "file browser rename / delete")


def test_notebook_save_as_has_the_locations(ui, app_url) -> None:
    page, errors = ui
    page.goto(app_url("sql_editor", {"database": DB, "view": "notebook"}), wait_until="domcontentloaded")
    nb = find_frame(page, re.compile(r"^Notebook"), 30)
    nb.locator(".nb-head").get_by_role("button", name="Save as", exact=True).click()
    tabs = nb.locator(".nb-files").get_by_role("tab")
    expect(tabs).to_have_text(["This computer", "SAS Content", "SAS Server"], timeout=10_000)
    nb.locator(".nb-files").get_by_role("tab", name="This computer").click()
    expect(nb.get_by_label("File name", exact=True)).to_have_value(re.compile(r"\.ipynb$"))
    nb.locator(".nb-files").get_by_role("button", name="Cancel", exact=True).click()
    assert wait_js(nb, "document.querySelector('.nb-files').hidden", 5)
    _no_problems(page, errors, "Notebook locations")


def test_schema_explorer_reshard_simulation(ui, app_url) -> None:
    page, errors = ui
    page.goto(app_url("schema_explorer", {"database": DB, "table": "CARS"}), wait_until="domcontentloaded")
    se = find_frame(page, re.compile(r"Schema Explorer"), 30)
    se.get_by_role("tab", name="Distribution").click()
    region = se.get_by_role("region", name="Reshard simulation")
    expect(region).to_be_visible(timeout=30_000)
    simulate = region.get_by_role("button", name="Simulate", exact=True)
    expect(simulate).to_be_disabled()  # nothing runs until columns are picked and Simulate is clicked
    assert "Predicted" not in region.inner_text() and "reads all" in region.inner_text()
    cols = region.get_by_role("group", name="Candidate shard key columns")
    cols.get_by_label(re.compile(r"^\s*Make(\s|$)")).check()
    cols.get_by_label(re.compile(r"^\s*Model(\s|$)")).check()
    assert "(Make, Model)" in region.inner_text()
    simulate.click()
    assert wait_js(se, "/Predicted/.test(document.querySelector('[aria-label=\"Reshard simulation\"]')?.innerText ?? '')", 60), \
        "no simulation result: " + text(se)[-500:]
    out = region.inner_text()
    assert re.search("distinct keys", out, re.I) and re.search(r"against [\d.]+ % now", out), out[-600:]
    assert region.get_by_role("button", name="Query in SQL Editor").is_visible()
    _no_problems(page, errors, "reshard simulation")


def test_a_slow_folder_doesnt_replace_a_later_one(ui, app_url, sql_dir: Path) -> None:
    """Clicking a second folder while the first is still loading: the first's (slower) answer must not replace the
    list afterwards (it detached the items under a click - seen with SAS Content's My Folder)."""
    page, errors = ui
    tag = secrets.token_hex(3)
    slow, fast = f"zz_test_slow_{tag}", f"zz_test_fast_{tag}"
    for d in (slow, fast):
        (sql_dir / d).mkdir()
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql"}), wait_until="domcontentloaded")
    find_frame(page, "SingleStore Workspace", 30)
    sql = _visible_sql(page)
    sql.locator(".sq-filebtns").get_by_role("button", name="Open", exact=True).click()
    sql.locator(".sq-files").get_by_role("tab", name="This computer").click()
    files = sql.locator(".sq-files .sq-browser")
    files.get_by_role("button", name="SQL folder", exact=True).click()
    files.locator(".item", has_text=slow).wait_for()
    sql.evaluate("""(slow) => {
      const orig = S2.callTool;
      S2.callTool = async (name, args) => {
        const res = await orig(name, args);
        if (name === 'files_browse' && String(args.path ?? '').endsWith(slow)) await new Promise((r) => setTimeout(r, 2500));
        return res;
      };
    }""", slow)
    files.locator(".item", has_text=slow).click()
    files.locator(".item", has_text=fast).click()
    assert wait_js(sql, f"(document.querySelector('.sq-browser .s2-muted')?.textContent ?? '').endsWith({fast!r})", 10)
    time.sleep(3.5)  # the slow answer has come back meanwhile
    assert sql.evaluate("document.querySelector('.sq-browser .s2-muted').textContent").endswith(fast), \
        "the slower answer for the folder clicked first replaced the list"
    _no_problems(page, errors, "file browser navigation race")
