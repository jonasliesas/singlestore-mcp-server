"""UI: the Load file wizard (vendor/s2-load-file.js) from the Schema Explorer – pick a CSV in the test SQL folder on
This computer, see the detected options and the first rows, change the table name, review (size, rows, where it runs),
load into a new test.zz_test_load_ui_* table, the row-count check; then append the same file from the SQL Editor's
tree with the mapping shown. The table is dropped afterwards."""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect
from uihelpers import app_frame, banners, find_frame

pytestmark = [pytest.mark.ui, pytest.mark.cluster]

CSV = "id;navn;beløp;dato\n1;Ås;12,50;01.02.2024\n2;Øre;.;15.03.2024\n3;Blå;7,00;31.12.2023\n"


def _no_problems(page: Any, errors: Any, what: str) -> None:
    assert not banners(page), f"{what}: {banners(page)}"
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, f"{what}: console errors: {errs}"


def _pick_file(f: Any, name: str) -> Any:
    dlg = f.locator(".s2-lf")
    expect(dlg).to_be_visible()
    dlg.get_by_role("button", name="SQL folder", exact=True).click()
    dlg.locator(".s2-fb .item", has_text=name).click()
    expect(dlg.get_by_text("How to read the file")).to_be_visible(timeout=30_000)
    return dlg


def test_load_a_file_from_the_schema_explorer(ui, app_url, mcp, sql_dir: Path) -> None:
    page, errors = ui
    tag = secrets.token_hex(3)
    name = f"zz_load_ui_{tag}.csv"
    table = f"zz_test_load_ui_{tag}"
    (sql_dir / name).write_bytes(CSV.encode("cp1252"))
    try:
        page.goto(app_url("schema_explorer", {"database": "test"}), wait_until="domcontentloaded")
        f = app_frame(page, 30)
        btn = f.get_by_role("button", name="Load file into table…")
        expect(btn).to_be_visible(timeout=30_000)
        btn.click()
        dlg = _pick_file(f, name)
        # Detected: Windows-1252, semicolon, header, decimal comma; the first rows are read with the right letters.
        expect(dlg.get_by_label("Encoding")).to_have_value("cp1252")
        expect(dlg.get_by_label("Delimiter", exact=True)).to_have_value(";")
        expect(dlg.get_by_label("Decimal comma")).to_be_checked()
        expect(dlg.locator(".s2-table-wrap").first).to_contain_text("Øre")
        expect(dlg.get_by_label("Format of dato")).to_have_value("DD.MM.YYYY")
        # A new table: the CREATE TABLE follows the name typed (and keeps the focus while typing).
        box = dlg.get_by_label("Table name")
        box.fill(table)
        expect(box).to_be_focused()
        ddl = dlg.get_by_label("CREATE TABLE script")
        expect(ddl).to_have_value(re.compile(rf"CREATE TABLE `test`\.`{table}`"), timeout=15_000)
        assert "SHARD KEY (`id`)" in ddl.input_value() and "SORT KEY (`dato`)" in ddl.input_value()
        dlg.get_by_role("button", name="Next: review…").click()
        # The review: where it runs, the size and the target; nothing written before Load.
        expect(dlg.get_by_text("The data goes from this computer to SingleStore.")).to_be_visible()
        expect(dlg.locator(".s2-engine")).to_have_count(2)  # the title's chip and the review's
        expect(dlg).to_contain_text(f"test.{table}")
        assert not mcp.ok("run_sql", {"sql": f"SHOW TABLES IN test LIKE '{table}'"})["rows"]
        dlg.get_by_role("button", name=re.compile(rf"^Load .* into {table}$")).click()
        expect(dlg.get_by_text("row count checked")).to_be_visible(timeout=60_000)
        expect(dlg).to_contain_text("Loaded 3 row(s)")
        dlg.locator(".s2-lf-foot").get_by_role("button", name="Close", exact=True).click()
        expect(f.locator(".s2-lf")).to_have_count(0)
        rows = mcp.ok("run_sql", {"sql": f"SELECT id, navn, `beløp` AS b, dato FROM test.{table} ORDER BY id"})["rows"]
        assert [r["navn"] for r in rows] == ["Ås", "Øre", "Blå"] and rows[1]["b"] is None
        _no_problems(page, errors, "schema explorer load")

        # Append from the SQL Editor's tree: the table exists, so the mapping is shown.
        page.goto(app_url("sql_editor", {"database": "test"}), wait_until="domcontentloaded")
        sql = find_frame(page, "SQL Editor", 30, min_text=40)
        row = sql.locator(".sq-row.tbl", has_text=table)
        expect(row).to_be_visible(timeout=30_000)
        row.locator("span.name").click()  # expand: "Load file…" sits under the table's columns
        sql.locator(".sq-row.sq-pick").get_by_role("button", name="Load file…").click()
        dlg = _pick_file(sql, name)
        expect(dlg.get_by_text("existing table: rows are added")).to_be_visible(timeout=15_000)
        expect(dlg.get_by_label("File column for beløp")).to_have_value("beløp")
        dlg.get_by_role("button", name="Next: review…").click()
        expect(dlg.get_by_text("add to the existing table")).to_be_visible()
        dlg.get_by_role("button", name=re.compile(r"^Load ")).click()
        expect(dlg.get_by_text("row count checked")).to_be_visible(timeout=60_000)
        expect(dlg).to_contain_text("was 3")
        dlg.locator(".s2-lf-foot").get_by_role("button", name="Close", exact=True).click()
        assert mcp.ok("run_sql", {"sql": f"SELECT COUNT(*) AS n FROM test.{table}"})["rows"][0]["n"] == 6
        time.sleep(0.5)
        _no_problems(page, errors, "sql editor append")
    finally:
        mcp.call("run_sql", {"sql": f"DROP TABLE IF EXISTS test.{table}"})
