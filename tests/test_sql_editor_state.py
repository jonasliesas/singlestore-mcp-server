"""The SQL Editor's saved state (singlestore_mcp.sql_editor_state): tabs, one draft per tab, the migration of
the single draft from before tabs, and the run history's tab/file info. No server, no cluster: each test runs
against its own empty SINGLESTORE_MCP_HOME."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from singlestore_mcp import sql_editor_state as st


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    return tmp_path / "sql-editor"


def _ids(data: dict) -> list[str]:
    return [t["id"] for t in data["tabs"]]


def test_first_run_has_one_empty_tab(home: Path) -> None:
    data = st.list_tabs()
    assert len(data["tabs"]) == 1 and data["active"] == data["tabs"][0]["id"]
    assert data["tabs"][0]["n"] == 1 and not data["tabs"][0]["dirty"] and data["tabs"][0]["file"] is None
    assert st.load_draft() is None
    assert (home / "tabs.json").is_file()


def test_migrates_the_old_single_draft_into_the_first_tab(home: Path) -> None:
    home.mkdir(parents=True)
    old = {"sql": "SELECT 1;\nSELECT 'æøå';", "database": "SASDP", "cursor": {"anchor": 3, "head": 5},
           "file": {"name": "q.sql", "path": "C:\\x\\q.sql", "external": False, "saved_hash": "abc"},
           "continue_on_error": True, "updated": 1.0}
    (home / "draft.json").write_text(json.dumps(old), encoding="utf-8")
    data = st.list_tabs()
    assert len(data["tabs"]) == 1
    first = data["tabs"][0]
    assert first["file"] == "q.sql" and first["path"] == "C:\\x\\q.sql" and first["database"] == "SASDP"
    d = st.load_draft(first["id"])
    assert d["sql"] == old["sql"] and d["cursor"] == old["cursor"] and d["continue_on_error"] is True
    assert st.load_draft() == d  # the active tab
    # The old file stays as a backup, and migrating happens once: a second tab doesn't get it again.
    assert (home / "draft.json").is_file()
    st.create_tab()
    assert st.list_tabs()["tabs"][1]["file"] is None
    assert len(list((home / "drafts").glob("*.json"))) == 1


def test_untitled_draft_from_before_tabs_counts_as_unsaved(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "draft.json").write_text(json.dumps({"sql": "SELECT 2", "file": None}), encoding="utf-8")
    assert st.list_tabs()["tabs"][0]["dirty"] is True


def test_broken_old_draft_is_ignored(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "draft.json").write_text("{not json", encoding="utf-8")
    assert len(st.list_tabs()["tabs"]) == 1 and st.load_draft() is None


def test_create_places_after_and_activates(home: Path) -> None:
    a = st.list_tabs()["tabs"][0]["id"]
    b = st.create_tab()["tab"]["id"]
    c = st.create_tab(after=a)
    assert _ids(c) == [a, c["tab"]["id"], b]
    assert c["active"] == c["tab"]["id"]
    assert [t["n"] for t in c["tabs"]] == [1, 3, 2]
    d = st.create_tab(activate=False)
    assert d["active"] == c["tab"]["id"]


def test_untitled_numbers_reuse_gaps(home: Path) -> None:
    st.list_tabs()
    two = st.create_tab()["tab"]["id"]
    st.create_tab()
    st.close_tab(two)
    assert st.create_tab()["tab"]["n"] == 2


def test_each_tab_keeps_its_own_draft(home: Path) -> None:
    a = st.list_tabs()["tabs"][0]["id"]
    b = st.create_tab()["tab"]["id"]
    st.save_draft({"sql": "SELECT 'a'", "database": "SASDP", "dirty": True}, a)
    st.save_draft({"sql": "SELECT 'b'", "database": "other", "file": {"name": "b.sql", "path": "C:\\b.sql"}}, b)
    assert st.load_draft(a)["sql"] == "SELECT 'a'" and st.load_draft(b)["sql"] == "SELECT 'b'"
    assert st.load_draft(a)["language"] == "singlestore"
    tabs = {t["id"]: t for t in st.list_tabs()["tabs"]}
    assert tabs[a]["dirty"] is True and tabs[a]["database"] == "SASDP"
    assert tabs[b]["file"] == "b.sql" and tabs[b]["dirty"] is False
    # Without a tab id (a page from before tabs) a save goes to the first tab, a read to the active one.
    st.save_draft({"sql": "legacy"})
    assert st.load_draft(a)["sql"] == "legacy"
    assert st.load_draft()["sql"] == "SELECT 'b'"


def test_close_removes_draft_and_picks_neighbour(home: Path) -> None:
    a = st.list_tabs()["tabs"][0]["id"]
    b = st.create_tab()["tab"]["id"]
    c = st.create_tab()["tab"]["id"]
    st.activate_tab(b)
    st.save_draft({"sql": "x"}, b)
    res = st.close_tab(b)
    assert _ids(res) == [a, c] and res["active"] == c  # the tab after it
    assert not (home / "drafts" / f"{b}.json").exists()
    # A late autosave for the closed tab is dropped instead of bringing it back.
    late = st.save_draft({"sql": "late"}, b)
    assert late["closed"] is True and not (home / "drafts" / f"{b}.json").exists()
    assert _ids(st.list_tabs()) == [a, c]
    res = st.close_tab(c)
    assert res["active"] == a  # last one closed: the one before it


def test_closing_the_last_tab_leaves_an_empty_one(home: Path) -> None:
    a = st.list_tabs()["tabs"][0]["id"]
    st.save_draft({"sql": "SELECT 1"}, a)
    res = st.close_tab(a)
    assert len(res["tabs"]) == 1 and res["tabs"][0]["id"] != a and res["active"] == res["tabs"][0]["id"]
    assert st.load_draft() is None


def test_rename_and_clear(home: Path) -> None:
    a = st.list_tabs()["tabs"][0]["id"]
    assert st.rename_tab(a, "  Monthly   report ")["tabs"][0]["title"] == "Monthly report"
    assert st.rename_tab(a, "x" * 500)["tabs"][0]["title"] == "x" * st.MAX_TITLE_CHARS
    assert st.rename_tab(a, "")["tabs"][0]["title"] is None
    with pytest.raises(LookupError):
        st.rename_tab("t0000dead", "nope")


def test_reorder(home: Path) -> None:
    a = st.list_tabs()["tabs"][0]["id"]
    b = st.create_tab()["tab"]["id"]
    c = st.create_tab()["tab"]["id"]
    assert _ids(st.reorder_tabs([c, a, b])) == [c, a, b]
    # Unknown ids are ignored; tabs left out keep their order at the end.
    assert _ids(st.reorder_tabs([b, "t0000dead", b])) == [b, c, a]
    assert _ids(st.list_tabs()) == [b, c, a]


def test_activate(home: Path) -> None:
    st.list_tabs()
    b = st.create_tab(activate=False)["tab"]["id"]
    st.activate_tab(b)
    assert st.list_tabs()["active"] == b and st.resolve_tab(None) == b
    with pytest.raises(LookupError):
        st.activate_tab("t0000dead")


@pytest.mark.parametrize("bad", ["../../x", "t", "draft", "T1234", "t12345678901234567890", "t12/34"])
def test_tab_ids_are_checked(home: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        st.load_draft(bad)
    with pytest.raises(ValueError):
        st.save_draft({"sql": "x"}, bad)
    with pytest.raises(ValueError):
        st.close_tab(bad)


def test_limits(home: Path) -> None:
    a = st.list_tabs()["tabs"][0]["id"]
    with pytest.raises(ValueError, match="larger than"):
        st.save_draft({"sql": "x" * (st.MAX_DRAFT_CHARS + 1)}, a)
    with pytest.raises(ValueError, match="language"):
        st.save_draft({"sql": "x", "language": "cobol"}, a)
    for _ in range(st.MAX_TABS - 1):
        st.create_tab()
    with pytest.raises(ValueError, match="tabs open"):
        st.create_tab()
    assert len(st.list_tabs()["tabs"]) == st.MAX_TABS


def test_damaged_tabs_file_is_repaired(home: Path) -> None:
    st.list_tabs()
    (home / "tabs.json").write_text(json.dumps({"tabs": [{"id": "t1234abcd"}, {"id": "../evil"}, {"id": "t1234abcd"}, 5],
                                                "active": "t9999"}), encoding="utf-8")
    data = st.list_tabs()
    assert _ids(data) == ["t1234abcd"] and data["active"] == "t1234abcd" and data["tabs"][0]["n"] == 1
    (home / "tabs.json").write_text(json.dumps({"tabs": []}), encoding="utf-8")
    assert len(st.list_tabs()["tabs"]) == 1


def test_history_records_tab_and_file(home: Path) -> None:
    st.add_history("SELECT 1", "SASDP", ok=True, source={"tab_id": "t1234abcd", "tab": "untitled 2", "file": None})
    st.add_history("SELECT 2", "SASDP", ok=True, source={"tab_id": "t1234abcd", "tab": "report", "file": "sub/report.sql"})
    st.add_history("SELECT 3", None, ok=False, error="boom")
    newest, middle, oldest = st.list_history()["entries"]
    assert oldest["tab"] == "untitled 2" and "file" not in oldest
    assert middle["file"] == "sub/report.sql" and middle["tab_id"] == "t1234abcd"
    assert "tab" not in newest
    assert [e["sql"] for e in st.list_history("report.sql")["entries"]] == ["SELECT 2"]
    assert [e["sql"] for e in st.list_history("untitled")["entries"]] == ["SELECT 1"]


# ----------------------------------------------------------------------------- files in use (Windows)
# On Windows os.replace / open fail with PermissionError while another reader, the second server process or
# antivirus has the file open (see singlestore_mcp.fileio). These were the cause of the flaky UI tests: a failed
# draft save, and a failed read taken for "no tabs yet", which started the tab list over.

def _flaky(monkeypatch: pytest.MonkeyPatch, target: object, name: str, fails: int, match: str) -> list[int]:
    real = getattr(target, name)
    calls = [0]

    def wrapper(*args: object, **kw: object) -> object:
        if match in str(args[-1] if name == "replace" else args[0]):
            calls[0] += 1
            if calls[0] <= fails:
                raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real(*args, **kw)

    monkeypatch.setattr(target, name, wrapper)
    return calls


def test_save_retries_while_the_file_is_in_use(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp import fileio

    tab = st.list_tabs()["active"]
    calls = _flaky(monkeypatch, fileio.os, "replace", 3, f"{tab}.json")
    st.save_draft({"sql": "SELECT 1"}, tab)
    assert calls[0] == 4 and st.load_draft(tab)["sql"] == "SELECT 1"
    assert not list(home.glob("drafts/*.tmp")), "temp files left behind"


def test_unreadable_tab_list_is_not_taken_for_missing(home: Path) -> None:
    from singlestore_mcp import fileio

    created = st.create_tab()["tab"]["id"]
    before = _ids(st.list_tabs())
    # Briefly in use: the read is retried and the tabs are all there.
    with pytest.MonkeyPatch.context() as mp:
        _flaky(mp, Path, "read_text", 2, "tabs.json")
        assert _ids(st.list_tabs()) == before
    # Still in use after the retries: an error, never a fresh tab list (that lost every tab).
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fileio, "RETRIES", 2)
        _flaky(mp, Path, "read_text", 99, "tabs.json")
        with pytest.raises(OSError):
            st.list_tabs()
    assert _ids(st.list_tabs()) == before and created in before


def test_concurrent_reads_and_saves(home: Path) -> None:
    """A reader polling a draft while it's autosaved (what the UI test and a second process do): no save fails
    and no read comes back empty."""
    import threading

    tab = st.list_tabs()["active"]
    st.save_draft({"sql": "SELECT 0"}, tab)
    stop, bad = threading.Event(), []

    def reader() -> None:
        while not stop.is_set():
            d = st.load_draft(tab)
            if not d or not d["sql"].startswith("SELECT"):
                bad.append(d)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        for i in range(150):
            st.save_draft({"sql": f"SELECT {i}"}, tab)
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert not bad
    assert st.load_draft(tab)["sql"] == "SELECT 149"
