"""SQL Editor files on SAS Viya, against SAS Viya (``sas``): browse SAS Content and the SAS server, save, reopen,
the version conflict, and cleanup.

Writes only to a test folder of its own: ``<My Folder>/zz_test_sqleditor_<random>`` in SAS Content (deleted at
the end) and ``/tmp/zz_test_sqleditor_<random>`` on the SAS server, inside the test server's own Compute session
(that session's /tmp; the session is ended at the end, which removes it). Never opens a file it didn't write:
My Folder's listing (e.g. the ``mcp`` folder of connection files) is only listed, never read.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from support import VIYA_TEST_PREFIX, McpSession, delete_viya_test_folder

pytestmark = pytest.mark.sas
TAG = VIYA_TEST_PREFIX + secrets.token_hex(3)


def _ok(mcp: McpSession, name: str, args: dict | None = None, timeout: float = 300) -> dict:
    r = mcp.call(name, args or {}, timeout=timeout)
    assert not r.is_error, f"{name}: {r.text[:800]}"
    return r.data


@pytest.fixture(scope="module")
def content_dir(sas: McpSession, test_home: Path) -> Iterator[str]:
    my = _ok(sas, "viya_files_browse", {"location": "content"})["path"]
    assert my.endswith("/My Folder"), my
    path = _ok(sas, "viya_files_mkdir", {"location": "content", "parent": my, "name": TAG})["path"]
    try:
        yield path
    finally:
        delete_viya_test_folder(test_home, path)


def test_content_browse_save_reopen_conflict(sas: McpSession, content_dir: str) -> None:
    listing = _ok(sas, "viya_files_browse", {"location": "content", "path": content_dir})
    assert listing["folders"] == [] and listing["files"] == [] and listing["connection"]
    conn = listing["connection"]
    ref = f"sas-content://{conn}{content_dir}/query æ.sql"

    first = _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "select 'æøå' as x;\n"})
    assert first["saved"] and first["created"] and first["version"] and first["path"] == ref
    again = _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "select 2;"})
    assert again["exists"] and not again["saved"]  # Save as onto it: the app asks first

    opened = _ok(sas, "sql_editor_open_file", {"name": ref})
    assert opened["sql"] == "select 'æøå' as x;\n" and opened["version"] == first["version"]
    assert opened["location"] == "content" and opened["connection"] == conn

    second = _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "select 2;\n", "expected_version": opened["version"]})
    assert second["saved"] and second["version"] != opened["version"]
    # A save based on the old version: the newer one on Viya isn't overwritten.
    stale = _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "select 3;\n", "expected_version": opened["version"]})
    assert stale["conflict"] and not stale["saved"]
    assert _ok(sas, "sql_editor_open_file", {"name": ref, "remember": False})["sql"] == "select 2;\n"
    forced = _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "select 3;\n", "overwrite": True})
    assert forced["saved"]
    assert _ok(sas, "sql_editor_open_file", {"name": ref, "remember": False})["sql"] == "select 3;\n"

    # A SAS program keeps SAS Studio's media type when it's saved again.
    prog = f"sas-content://{conn}{content_dir}/prog.sas"
    p1 = _ok(sas, "sql_editor_save_file", {"name": prog, "sql": "proc sql; select 1 from sashelp.class(obs=1); quit;\n"})
    _ok(sas, "sql_editor_save_file", {"name": prog, "sql": "proc sql; select 2; quit;\n", "expected_version": p1["version"]})
    names = {f["name"]: f for f in _ok(sas, "viya_files_browse", {"location": "content", "path": content_dir})["files"]}
    assert set(names) == {"query æ.sql", "prog.sas"}
    assert names["prog.sas"]["media_type"].startswith("application/vnd.sas.studio.programfile")
    assert names["query æ.sql"]["size"] == len(b"select 3;\n") and names["query æ.sql"]["modified"]

    recent = _ok(sas, "sql_editor_state", {})["recent"]
    assert any(r["path"] == ref and r["location"] == "content" for r in recent)


def test_content_errors_are_readable(sas: McpSession, content_dir: str) -> None:
    conn = _ok(sas, "viya_files_browse", {"location": "content", "path": content_dir})["connection"]
    r = sas.call("sql_editor_open_file", {"name": f"sas-content://{conn}{content_dir}/missing.sql"}, timeout=120)
    assert r.is_error and "doesn't exist" in r.text
    r = sas.call("viya_files_browse", {"location": "content", "path": f"{content_dir}/nope"}, timeout=120)
    assert r.is_error and "No SAS Content folder" in r.text
    other = _ok(sas, "sql_editor_save_file", {"name": f"sas-content://No Such Viya{content_dir}/x.sql", "sql": "x"})
    assert other["connection_mismatch"] and other["active"] == conn


def test_sas_server_browse_save_reopen_conflict(sas: McpSession) -> None:
    root = _ok(sas, "viya_files_browse", {"location": "server", "path": "/"})
    assert root["path"] == "/" and any(f["name"] == "tmp" for f in root["folders"]), root["folders"]
    assert {s["path"] for s in root["shortcuts"]} >= {"/"}
    tmp = f"/tmp/{TAG}"
    assert _ok(sas, "viya_files_mkdir", {"location": "server", "parent": "/tmp", "name": TAG})["path"] == tmp
    try:
        conn = root["connection"]
        ref = f"sas-server://{conn}{tmp}/prog.sas"
        first = _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "proc sql; select 1; quit;\n"})
        assert first["saved"] and first["created"] and first["version"]
        assert _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "x"})["exists"]
        listing = _ok(sas, "viya_files_browse", {"location": "server", "path": tmp})
        assert [f["name"] for f in listing["files"]] == ["prog.sas"] and listing["files"][0]["ref"] == ref
        opened = _ok(sas, "sql_editor_open_file", {"name": ref})
        assert opened["sql"] == "proc sql; select 1; quit;\n" and opened["location"] == "server"
        second = _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "proc sql; select 2; quit;\n",
                                                   "expected_version": opened["version"]})
        assert second["saved"]
        stale = _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "lost", "expected_version": opened["version"]})
        assert stale["conflict"]
        assert _ok(sas, "sql_editor_open_file", {"name": ref, "remember": False})["sql"] == "proc sql; select 2; quit;\n"
        assert _ok(sas, "sql_editor_save_file", {"name": ref, "sql": "proc sql; select 3; quit;\n", "overwrite": True})["saved"]
        r = sas.call("sql_editor_save_file", {"name": f"sas-server://{conn}/tmp/{TAG}_missing/x.sql", "sql": "x"}, timeout=120)
        assert r.is_error and "doesn't exist" in r.text
    finally:
        # The files are in this test server's own SAS session (its /tmp): ending the session removes them.
        _ok(sas, "sas_session_reset")
    gone = sas.call("viya_files_browse", {"location": "server", "path": tmp}, timeout=300)
    assert gone.is_error and "isn't a folder" in gone.text, gone.text[:300]
