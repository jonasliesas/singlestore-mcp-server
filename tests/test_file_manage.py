"""Rename and delete in the file browser on this computer (files_rename / files_delete), offline: only one item in
its own folder, only the browser's file kinds, empty folders only, never a root or shortcut folder, the listed
modified time, and the SQL Editor tabs (and Recent) that follow a rename or keep a deleted file's text unsaved."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import sql_editor_state as st
from singlestore_mcp.apps import file_manage as fm
from singlestore_mcp.apps import sql_editor


@pytest.fixture
def roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SINGLESTORE_MCP_SQL_DIR", str(tmp_path / "sql"))
    monkeypatch.setenv("SINGLESTORE_MCP_FILE_ROOTS", str(tmp_path / "root"))
    work = tmp_path / "root" / "work"
    (work / "sub").mkdir(parents=True)
    (tmp_path / "sql").mkdir()
    return {"root": (tmp_path / "root").resolve(), "work": work.resolve(), "sql": (tmp_path / "sql").resolve()}


def _data(result: Any) -> dict[str, Any]:
    return result.structured_content


def _open_in_tab(path: Path) -> str:
    st.resolve_tab(None)
    tab = st.create_tab()["tab"]["id"]
    sql_editor.sql_editor_save_draft("select 1;", file_name=path.name, file_path=str(path), saved_hash="h", tab_id=tab)
    st.remember_file(path, path.name)
    return tab


def test_rename_a_file_and_the_tab_follows(roots: dict[str, Path]) -> None:
    a = roots["work"] / "a.sql"
    a.write_text("select 1;", encoding="utf-8")
    (roots["work"] / "b.sql").write_text("x", encoding="utf-8")
    tab = _open_in_tab(a)
    listed = _data(sql_editor.files_browse(str(roots["work"])))
    item = next(f for f in listed["files"] if f["name"] == "a.sql")

    assert _data(fm.files_rename(str(a), "b.sql"))["exists"]
    assert _data(fm.files_rename(str(a), "c", expected_modified=item["modified"] + 60))["conflict"]
    with pytest.raises(ValueError, match="extension"):
        fm.files_rename(str(a), "c.txt")
    for bad in ("x/y.sql", "..", "a?.sql", ""):
        with pytest.raises(ValueError):
            fm.files_rename(str(a), bad)
    res = _data(fm.files_rename(str(a), "c", expected_modified=item["modified"]))
    c = roots["work"] / "c.sql"
    assert res["renamed"] and c.read_text(encoding="utf-8") == "select 1;" and not a.exists()
    assert res["from_ref"] == str(a) and res["to_ref"] == str(c) and res["tabs"] == [tab]
    draft = st.load_draft(tab)
    assert draft["file"]["path"] == str(c) and draft["file"]["name"] == str(c)  # outside the SQL folder: the full path
    assert [r["path"] for r in st.recent_files()] == [str(c)]


def test_rename_a_folder_moves_its_open_files(roots: dict[str, Path]) -> None:
    q = roots["work"] / "sub" / "q.sql"
    q.write_text("select 2;", encoding="utf-8")
    tab = _open_in_tab(q)
    res = _data(fm.files_rename(str(roots["work"] / "sub"), "sub2"))
    assert res["renamed"] and res["folder"] and (roots["work"] / "sub2" / "q.sql").is_file()
    assert st.load_draft(tab)["file"]["path"] == str(roots["work"] / "sub2" / "q.sql")
    assert _data(fm.files_delete(str(roots["work"] / "sub2")))["not_empty"]


def test_delete_keeps_the_tab_text_unsaved(roots: dict[str, Path]) -> None:
    q = roots["sql"] / "folder" / "q.sql"
    q.parent.mkdir()
    q.write_text("select 3;", encoding="utf-8")
    tab = _open_in_tab(q)
    assert _data(fm.files_delete(str(q), expected_modified=1))["conflict"] and q.exists()
    res = _data(fm.files_delete(str(q)))
    assert res["deleted"] and not q.exists() and res["tabs"] == [tab]
    draft = st.load_draft(tab)
    assert draft["file"] is None and draft["dirty"] and draft["sql"] == "select 1;"
    assert st.recent_files() == []
    assert _data(fm.files_delete(str(q.parent)))["deleted"] and not q.parent.exists()


def test_rename_inside_the_sql_folder_keeps_the_relative_name(roots: dict[str, Path]) -> None:
    q = roots["sql"] / "q.sql"
    q.write_text("x", encoding="utf-8")
    tab = _open_in_tab(q)
    fm.files_rename(str(q), "r.sql")
    assert st.load_draft(tab)["file"]["name"] == "r.sql"


def test_only_the_browsers_files_and_never_a_root(roots: dict[str, Path], tmp_path: Path) -> None:
    (roots["work"] / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Only"):
        fm.files_delete(str(roots["work"] / "notes.txt"))
    nb = roots["work"] / "n.ipynb"
    nb.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Only"):
        fm.files_delete(str(nb))  # kind "sql" by default
    assert _data(fm.files_rename(str(nb), "m", kind="notebook"))["new_path"].endswith("m.ipynb")
    for protected in (roots["root"], roots["sql"], Path.home(), roots["root"].parent):  # roots, shortcuts, above them
        with pytest.raises(ValueError):
            fm.files_delete(str(protected))
        with pytest.raises(ValueError):
            fm.files_rename(str(protected), "x")
    outside = tmp_path / "elsewhere.sql"
    outside.write_text("x", encoding="utf-8")
    if not outside.resolve().is_relative_to(Path.home().resolve()):
        with pytest.raises(ValueError, match="outside"):
            fm.files_delete(str(outside))
    with pytest.raises(LookupError):
        fm.files_delete(str(roots["work"] / "missing.sql"))
    hidden = roots["work"] / ".h.sql"
    hidden.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="hidden"):
        fm.files_delete(str(hidden))
    assert os.path.exists(hidden)
