"""SQL Editor Open / Save as with SAS Content, in a browser: Save as into a SAS Content folder, Save back with the
version check (a newer version on Viya asks before overwriting), Open a .sas file from SAS Content into a new tab
(Run as PROC SQL) and Recent. Needs SAS Viya (``sas``); writes only to its own test folder in My Folder, deleted
at the end."""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
from support import DB, VIYA_TEST_PREFIX, McpSession, delete_viya_test_folder
from uihelpers import find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster, pytest.mark.sas]
TAG = VIYA_TEST_PREFIX + secrets.token_hex(3)


def _visible_sql(page: Any, timeout: float = 30) -> Any:
    """The SQL Editor frame of the tab that is shown, once it has restored its draft."""
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


@pytest.fixture(scope="module")
def content_dir(sas: McpSession, test_home: Path):
    my = sas.ok("viya_files_browse", {"location": "content"}, timeout=300)["path"]
    path = sas.ok("viya_files_mkdir", {"location": "content", "parent": my, "name": TAG})["path"]
    try:
        yield path
    finally:
        delete_viya_test_folder(test_home, path)


def test_sql_editor_open_save_as_sas_content(ui, app_url, sas: McpSession, content_dir: str) -> None:
    page, errors = ui
    marker = f"ui_{secrets.token_hex(3)}"
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, "sql": f"select '{marker}' as x;"}),
              wait_until="domcontentloaded")
    ws = find_frame(page, "SingleStore Workspace", 30)
    sql = _visible_sql(page)
    conn = sas.ok("viya_files_browse", {"location": "content", "path": content_dir})["connection"]
    ref = f"sas-content://{conn}{content_dir}/ui.sql"

    # Save as → SAS Content → a name with the full path of the test folder.
    sql.locator(".sq-filebtns").get_by_role("button", name="Save as", exact=True).click()
    sql.get_by_role("tab", name="SAS Content").click()
    assert wait_js(sql, "document.querySelector('.sq-files')?.innerText.includes('My Folder')", 120), \
        "SAS Content didn't list: " + text(sql)[-600:]
    sql.get_by_label("File name").fill(f"{content_dir}/ui.sql")
    sql.locator(".sq-files").get_by_role("button", name="Save", exact=True).click()
    assert wait_js(sql, "(() => { const l = document.querySelector('.sq-file')?.innerText ?? ''; return l.startsWith('ui.sql') && !l.includes('modified'); })()", 60), \
        "not saved to SAS Content: " + text(sql)[-600:]
    assert marker in sas.ok("sql_editor_open_file", {"name": ref, "remember": False})["sql"]
    assert "SAS Content" in sql.locator(".sq-file").get_attribute("title")

    # Someone saves a newer version (e.g. in SAS Studio): Save asks before overwriting it.
    sas.ok("sql_editor_save_file", {"name": ref, "sql": "-- changed elsewhere\n", "overwrite": True})
    sql.locator(".cm-content").click()
    page.keyboard.press("Control+End")
    page.keyboard.type("\n-- more")
    page.keyboard.press("Escape")
    sql.locator(".sq-filebtns").get_by_role("button", name="Save", exact=True).click()
    assert wait_js(sql, "document.querySelector('.sq-files')?.innerText.includes('changed on SAS Content since you opened it')", 60), \
        "no conflict question: " + text(sql)[-600:]
    assert sas.ok("sql_editor_open_file", {"name": ref, "remember": False})["sql"] == "-- changed elsewhere\n"
    sql.locator(".sq-files").get_by_role("button", name="Overwrite", exact=True).click()
    assert wait_js(sql, "!(document.querySelector('.sq-file')?.innerText ?? 'modified').includes('modified')", 60), text(sql)[-600:]
    saved = sas.ok("sql_editor_open_file", {"name": ref, "remember": False})["sql"]
    assert marker in saved and "-- more" in saved

    # Open a .sas program from SAS Content: this tab has text, so it opens in a new tab, as PROC SQL.
    sas.ok("sql_editor_save_file", {"name": f"sas-content://{conn}{content_dir}/other.sas",
                                     "sql": "proc sql; select name from sashelp.class; quit;\n"})
    sql.locator(".sq-filebtns").get_by_role("button", name="Open", exact=True).click()
    assert wait_js(sql, "document.querySelector('.sq-files [role=tab][aria-selected=true]')?.textContent === 'SAS Content'", 10), \
        "Open should start in the open file's location"
    item = sql.locator(".sq-files .list:not(.recent) .item", has_text="other.sas")
    item.wait_for(timeout=60_000)
    item.click()
    assert wait_js(ws, "[...document.querySelectorAll('.ws-tabs .ws-tab')].some((t) => t.innerText.includes('other.sas') && t.classList.contains('on'))", 60), \
        "other.sas didn't open in a tab"
    other = _visible_sql(page)
    assert wait_js(other, "document.querySelector('.cm-content')?.innerText.includes('sashelp.class')", 30)
    assert wait_js(other, "document.querySelector('.sq-runas')?.value === 'procsql'", 10), "a .sas file should run as PROC SQL"
    assert wait_js(other, "[...document.querySelectorAll('select[aria-label=\"Recent files\"] option')].some((o) => o.textContent === 'other.sas (SAS Content)')", 10), \
        "Recent should list the SAS Content file"

    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


def _content_folder(frame: Any, panel: str, folder: str) -> Any:
    """In an Open / Save as panel: SAS Content → My Folder → the test folder; returns the browser part."""
    frame.locator(panel).get_by_role("tab", name="SAS Content").click()
    browser = frame.locator(f"{panel} .s2-fb").last
    browser.get_by_role("button", name="My Folder", exact=True).click(timeout=120_000)
    browser.locator(".item", has_text=folder).first.click(timeout=60_000)
    assert wait_js(frame, f"[...document.querySelectorAll('{panel} .s2-fb .s2-muted')].some((e) => e.textContent.endsWith({folder!r}))", 60), \
        "didn't open the test folder: " + text(frame)[-500:]
    return browser


def test_rename_delete_in_sas_content_and_the_tab_follows(ui, app_url, sas: McpSession, content_dir: str) -> None:
    page, errors = ui
    folder = content_dir.rsplit("/", 1)[-1]
    conn = sas.ok("viya_files_browse", {"location": "content", "path": content_dir})["connection"]
    sas.ok("sql_editor_save_file", {"name": f"sas-content://{conn}{content_dir}/ren.sql", "sql": "select 'ren' as x;\n"})
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True}), wait_until="domcontentloaded")
    ws = find_frame(page, "SingleStore Workspace", 30)
    sql = _visible_sql(page)
    sql.locator(".sq-filebtns").get_by_role("button", name="Open", exact=True).click()
    _content_folder(sql, ".sq-files", folder).locator(".item", has_text="ren.sql").click()
    assert wait_js(sql, "(document.querySelector('.sq-file')?.innerText ?? '').startsWith('ren.sql')", 60), text(sql)[-400:]
    sql = _visible_sql(page)

    # Rename in the browser (Open starts in the file's folder): the tab follows, and Save still works afterwards
    # (the rename's new version is handed to the editor).
    sql.locator(".sq-filebtns").get_by_role("button", name="Open", exact=True).click()
    browser = sql.locator(".sq-files .sq-browser")
    browser.get_by_role("button", name="Rename ren.sql").click(timeout=60_000)
    box = browser.get_by_label("New name for ren.sql")
    box.fill("ren2")
    box.press("Enter")
    assert wait_js(sql, "[...document.querySelectorAll('.sq-browser .item')].some((i) => i.innerText.includes('ren2.sql'))", 60), text(sql)[-500:]
    assert wait_js(ws, "(document.querySelector('.ws-tabs .ws-tab.on')?.innerText ?? '').includes('ren2.sql')", 20), "the tab didn't follow"
    browser.get_by_role("button", name="Close", exact=True).click()
    sql.locator(".cm-content").click()
    page.keyboard.press("Control+End")
    page.keyboard.type("\n-- after rename")
    page.keyboard.press("Escape")
    sql.locator(".sq-filebtns").get_by_role("button", name="Save", exact=True).click()
    assert wait_js(sql, "!(document.querySelector('.sq-file')?.innerText ?? 'modified').includes('modified')", 60), \
        "Save after the rename didn't save: " + text(sql)[-500:]
    ref2 = f"sas-content://{conn}{content_dir}/ren2.sql"
    assert "-- after rename" in sas.ok("sql_editor_open_file", {"name": ref2, "remember": False})["sql"]

    # Delete it: the tab keeps its text, unsaved.
    sql.locator(".sq-filebtns").get_by_role("button", name="Open", exact=True).click()
    browser = sql.locator(".sq-files .sq-browser")
    browser.get_by_role("button", name="Delete ren2.sql").click(timeout=60_000)
    browser.get_by_role("button", name="Delete", exact=True).click()
    assert wait_js(sql, "(document.querySelector('.sq-file')?.innerText ?? '').startsWith('untitled')", 60), text(sql)[-400:]
    assert sas.call("sql_editor_open_file", {"name": ref2, "remember": False}, timeout=120).is_error

    # The Notebook saves to SAS Content too.
    ws.locator('nav.ws-rail button[aria-label^="Notebook"]').click()
    nb = find_frame(page, re.compile(r"^Notebook"), 30)
    nb.locator(".nb-head").get_by_role("button", name="Save as", exact=True).click()
    _content_folder(nb, ".nb-files", folder)
    nb.get_by_label("File name", exact=True).fill("nb_ui")
    nb.locator(".nb-files").get_by_role("button", name="Save", exact=True).click()
    assert wait_js(nb, "(document.querySelector('.nb-file, .nb-head')?.innerText ?? '').includes('nb_ui.ipynb')", 60), text(nb)[-500:]
    opened = sas.ok("notebook_open", {"name": f"sas-content://{conn}{content_dir}/nb_ui.ipynb"})
    assert opened["notebook"]["cells"]

    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs
