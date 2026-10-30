"""Notebook "Python in SAS" cells in the browser: adding the cell kind, its helpers, saving it in the .ipynb
(``%%sas_python`` + cell metadata) and opening it again, the endsubmit refusal, and (``--sas``) running two cells
that share Python state in the SAS Compute session."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect
from support import DB
from uihelpers import find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]

NB_FILE = "zz_pytest_pysas.ipynb"


def _notebook(page: Any, app_url: Any, **args: Any) -> Any:
    page.goto(app_url("notebook", {"database": DB, **args}), wait_until="domcontentloaded")
    nb = find_frame(page, re.compile(r"^Notebook"), 30)
    assert wait_js(nb, "document.body.dataset.loaded === '1' && document.querySelectorAll('.cell').length > 0", 30), text(nb)[-400:]
    return nb


def _add_pysas(nb: Any) -> Any:
    before = nb.locator(".cell.sas_python").count()
    nb.locator(".nb-add").last.get_by_role("button", name="+ Python in SAS", exact=True).click()
    expect(nb.locator(".cell.sas_python")).to_have_count(before + 1)
    return nb.locator(".cell.sas_python").last


def _set_code(page: Any, cell: Any, code: str) -> None:
    cell.locator(".cm-content").click()
    page.keyboard.press("Control+A")
    page.keyboard.insert_text(code)  # no auto-closed brackets / auto-indent


def test_add_python_in_sas_cell_save_and_reopen(ui, app_url, sql_dir: Path) -> None:
    page, errors = ui
    nb = _notebook(page, app_url)
    cell = _add_pysas(nb)
    assert cell.locator(".gutter .badge").inner_text() == "PY·SAS"
    assert cell.locator(".bar select").input_value() == "sas_python"
    assert cell.locator(".bar select option:checked").inner_text() == "Python in SAS"
    assert cell.locator(".bar .s2-engine").get_attribute("data-engine") == "compute"
    assert "SAS Compute" in cell.locator(".bar .s2-engine").inner_text()
    # The helpers: the SAS object's methods and where the data stays.
    cell.get_by_role("button", name="SAS helpers").click()
    help_text = cell.locator(".pysas-help").inner_text()
    for name in ("SAS.sd2df", "SAS.df2sd", "SAS.symget", "SAS.submit", "Keep the data on the server", "Restart SAS session"):
        assert name in help_text, name
    # The header offers Restart SAS session once the notebook has such a cell.
    expect(nb.locator(".nb-head").get_by_role("button", name="Restart SAS session")).to_have_count(1)

    # endsubmit is refused before anything runs on SAS.
    _set_code(page, cell, "s = 'endsubmit'\nprint(s)")
    cell.locator(".gutter .run").click()
    expect(cell.locator(".outputs .err")).to_contain_text("endsubmit", timeout=20_000)

    # Saved as %%sas_python with the kind in the cell's metadata …
    _set_code(page, cell, "x = 1\nprint(x)")
    nb.locator(".nb-head").get_by_role("button", name="Save as", exact=True).click()
    name = nb.get_by_label("File name", exact=True)
    expect(name).to_be_focused(timeout=10_000)
    name.fill(NB_FILE)
    name.press("Enter")
    path = sql_dir / NB_FILE
    assert wait_js(nb, "document.querySelector('.nb-files').hidden", 15), text(nb)[-300:]
    saved = json.loads(path.read_text(encoding="utf-8"))
    cells = [c for c in saved["cells"] if c.get("metadata", {}).get("singlestore", {}).get("kind") == "sas_python"]
    assert len(cells) == 1 and "".join(cells[0]["source"]) == "%%sas_python\nx = 1\nprint(x)"

    # … and comes back as a Python in SAS cell.
    nb = _notebook(page, app_url, name=NB_FILE)
    assert wait_js(nb, "document.querySelectorAll('.cell.sas_python').length === 1", 20), text(nb)[-300:]
    assert "print(x)" in nb.locator(".cell.sas_python .cm-content").inner_text()
    assert not errors.drain()


@pytest.mark.sas
def test_python_in_sas_cells_share_state(ui, app_url) -> None:
    page, errors = ui
    nb = _notebook(page, app_url)
    first = _add_pysas(nb)
    _set_code(page, first, "n = 20\nprint('n =', n)")
    first.locator(".gutter .run").click()
    expect(first.locator(".outputs")).to_contain_text("n = 20", timeout=240_000)
    assert first.locator(".status .s2-engine").get_attribute("aria-label").startswith("Ran in SAS Compute")
    second = _add_pysas(nb)
    _set_code(page, second, "print('n + 1 =', n + 1)\nprint(1 / 0)")
    second.locator(".gutter .run").click()
    expect(second.locator(".outputs")).to_contain_text("n + 1 = 21", timeout=120_000)
    expect(second.locator(".outputs .err")).to_contain_text("ZeroDivisionError")
    second.locator(".outputs .err summary").click()  # the traceback names the cell's line
    expect(second.locator(".outputs .err pre")).to_contain_text('File "<cell>", line 2')
    assert "✕ error" in second.locator(".status").inner_text()
    assert not errors.drain()
