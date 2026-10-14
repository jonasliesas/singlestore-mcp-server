"""The SQL Editor's saved state: its tabs with one autosaved draft each, the run history and recent files.

Kept as JSON in ``<state folder>/sql-editor/`` (``paths.data_dir``), not in the
browser: the editor runs in sandboxed frames where localStorage is unreliable,
and the Claude-hosted server and the standalone workspace (two processes)
share this folder. Each call re-reads its file, so either process sees the
other's writes; writes are atomic (temp file + replace).

- ``tabs.json``: the editor tabs in order ({id, n, title}), the active tab and
  the ids of recently closed tabs (late autosaves for those are dropped).
- ``drafts/<tab id>.json``: one tab's text, database, cursor, the open file
  (name, path, hash of its saved text), whether it has unsaved changes and
  editor options (continue on error, the language it runs as) and, for a tab
  opened from the Workload view, the workload statement it came from
  (``origin``). Never the user's file itself.
- ``draft.json``: the single draft of the editor before it had tabs; moved
  into the first tab once (and left in place, unused, as a backup).
- ``history.json``: statements the user ran (newest last), capped.
- ``recent.json``: SQL files recently opened or saved, newest first.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import fileio
from .paths import data_dir

MAX_DRAFT_CHARS = 2_000_000      # same limit as a .sql file
MAX_TABS = 30
MAX_TITLE_CHARS = 60
MAX_CLOSED = 100                 # closed tab ids remembered (to drop their late autosaves)
MAX_HISTORY = 500
MAX_HISTORY_SQL = 20_000         # longer statements are kept cut off (and can't be re-run)
MAX_ERROR_CHARS = 500
MAX_RECENT = 10
PREVIEW_CHARS = 400
LANGUAGES = ("singlestore", "procsql", "fedsql", "cas")  # what a tab's SQL runs as ("Run as")
HISTORY_LANGUAGES = (*LANGUAGES, "sas")  # "sas": plain SAS code (proc / data / %…) run from a SAS tab

_TAB_ID = re.compile(r"^t[0-9a-f]{4,16}$")
_lock = threading.RLock()


def _file(name: str) -> Path:
    return data_dir("sql-editor") / name


def _read(name: str, default: Any) -> Any:
    """The file's JSON, or ``default`` when it's missing or not JSON. A file that exists but stays
    unreadable (in use on Windows, see ``fileio``) raises OSError: taking it for missing would, for
    tabs.json, start the tab list over and lose the user's tabs."""
    with _lock:  # in this process, never read a file while it's being replaced (Windows refuses the replace)
        return fileio.read_json(_file(name), default)


def _write(name: str, value: Any) -> None:
    with _lock:
        fileio.write_json(_file(name), value)


def _valid_draft(draft: Any) -> dict[str, Any] | None:
    return draft if isinstance(draft, dict) and isinstance(draft.get("sql"), str) else None


# ---------------------------------------------------------------- tabs
# Each tab is one editor: its own text, database, open file, cursor and
# options, autosaved to drafts/<id>.json. tabs.json only holds the order,
# the "untitled N" number, an optional title the user gave the tab and
# which tab is active.
def _check_id(tab_id: str) -> str:
    if not isinstance(tab_id, str) or not _TAB_ID.match(tab_id):
        raise ValueError(f"Not a SQL Editor tab id: {tab_id!r}")
    return tab_id


def _draft_name(tab_id: str) -> str:
    return f"drafts/{_check_id(tab_id)}.json"


def _new_id(taken: set[str]) -> str:
    while True:
        tab_id = "t" + secrets.token_hex(4)
        if tab_id not in taken:
            return tab_id


def _free_number(tabs: list[dict[str, Any]]) -> int:
    used = {t.get("n") for t in tabs}
    return next(n for n in range(1, len(tabs) + 2) if n not in used)


def _load_tabs() -> dict[str, Any]:
    """tabs.json, cleaned up; on first run, a first tab holding the old single draft (draft.json)."""
    data = _read("tabs.json", None)
    if not isinstance(data, dict) or not isinstance(data.get("tabs"), list):
        return _migrate()
    tabs, seen = [], set()
    for t in data["tabs"]:
        if isinstance(t, dict) and isinstance(t.get("id"), str) and _TAB_ID.match(t["id"]) and t["id"] not in seen:
            seen.add(t["id"])
            title = t.get("title") if isinstance(t.get("title"), str) and t["title"].strip() else None
            n = t.get("n") if isinstance(t.get("n"), int) and t["n"] > 0 else None
            tabs.append({"id": t["id"], "n": n, "title": title})
    for t in tabs:
        if t["n"] is None:
            t["n"] = _free_number([x for x in tabs if x["n"] is not None])
    closed = [c for c in data.get("closed") or [] if isinstance(c, str)][-MAX_CLOSED:]
    out = {"tabs": tabs, "active": data.get("active"), "closed": closed}
    if not tabs:
        out["tabs"] = [{"id": _new_id(set(closed)), "n": 1, "title": None}]
    if out["active"] not in {t["id"] for t in out["tabs"]}:
        out["active"] = out["tabs"][0]["id"]
    if out != data:
        _write("tabs.json", out)
    return out


def _migrate() -> dict[str, Any]:
    """First run with tabs: the old single draft becomes the first tab (draft.json itself is left as it is)."""
    tab_id = _new_id(set())
    old = _valid_draft(_read("draft.json", None))
    if old is not None:
        _write(_draft_name(tab_id), {**old, "migrated": True})
    data = {"tabs": [{"id": tab_id, "n": 1, "title": None}], "active": tab_id, "closed": []}
    _write("tabs.json", data)  # last: if anything above failed, the next call migrates again
    return data


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    """The tab list for the workspace's tab bar: each tab with its open file and unsaved-changes flag."""
    tabs = []
    for t in data["tabs"]:
        d = _valid_draft(_read(_draft_name(t["id"]), None)) or {}
        f = d.get("file") if isinstance(d.get("file"), dict) else None
        dirty = d.get("dirty")
        if dirty is None:  # drafts written before tabs: unsaved if there's text and no file
            dirty = bool((d.get("sql") or "").strip()) and not f
        tabs.append({**t, "file": (f or {}).get("name"), "path": (f or {}).get("path"), "dirty": bool(dirty),
                     "database": d.get("database"), "language": d.get("language") or LANGUAGES[0]})
    return {"tabs": tabs, "active": data["active"]}


def list_tabs() -> dict[str, Any]:
    with _lock:
        return _summary(_load_tabs())


def resolve_tab(tab_id: str | None) -> str:
    """``tab_id``, or the active tab when None (an editor outside the workspace, or an older page)."""
    if tab_id:
        return _check_id(tab_id)
    with _lock:
        return _load_tabs()["active"]


def create_tab(after: str | None = None, *, activate: bool = True) -> dict[str, Any]:
    """A new, empty tab right after ``after`` (else at the end). Returns the tab list plus ``tab``, the new one."""
    with _lock:
        data = _load_tabs()
        if len(data["tabs"]) >= MAX_TABS:
            raise ValueError(f"The SQL Editor has {MAX_TABS} tabs open; close some first.")
        tab = {"id": _new_id({t["id"] for t in data["tabs"]} | set(data["closed"])), "n": _free_number(data["tabs"]), "title": None}
        ids = [t["id"] for t in data["tabs"]]
        at = ids.index(after) + 1 if after in ids else len(ids)
        data["tabs"].insert(at, tab)
        if activate:
            data["active"] = tab["id"]
        _write("tabs.json", data)
        return {**_summary(data), "tab": tab}


def close_tab(tab_id: str) -> dict[str, Any]:
    """Remove a tab and its draft. The tab after it (else before it) becomes active if it was;
    closing the last tab leaves one new, empty tab."""
    _check_id(tab_id)
    with _lock:
        data = _load_tabs()
        ids = [t["id"] for t in data["tabs"]]
        if tab_id in ids:
            i = ids.index(tab_id)
            data["tabs"].pop(i)
            data["closed"] = [*[c for c in data["closed"] if c != tab_id], tab_id][-MAX_CLOSED:]
            if not data["tabs"]:
                data["tabs"] = [{"id": _new_id(set(data["closed"])), "n": 1, "title": None}]
            if data["active"] == tab_id:
                data["active"] = data["tabs"][min(i, len(data["tabs"]) - 1)]["id"]
            _write("tabs.json", data)
        try:
            _file(_draft_name(tab_id)).unlink(missing_ok=True)
        except OSError:
            pass
        return _summary(data)


def activate_tab(tab_id: str) -> dict[str, Any]:
    _check_id(tab_id)
    with _lock:
        data = _load_tabs()
        if tab_id not in {t["id"] for t in data["tabs"]}:
            raise LookupError("That SQL Editor tab is closed.")
        if data["active"] != tab_id:
            data["active"] = tab_id
            _write("tabs.json", data)
        return {"active": tab_id}


def rename_tab(tab_id: str, title: str | None) -> dict[str, Any]:
    """Give a tab its own title (shown instead of the file name / "untitled N"); empty clears it."""
    _check_id(tab_id)
    title = " ".join((title or "").split())[:MAX_TITLE_CHARS] or None
    with _lock:
        data = _load_tabs()
        tab = next((t for t in data["tabs"] if t["id"] == tab_id), None)
        if tab is None:
            raise LookupError("That SQL Editor tab is closed.")
        tab["title"] = title
        _write("tabs.json", data)
        return _summary(data)


def reorder_tabs(order: list[str]) -> dict[str, Any]:
    """Put the tabs in this order. Tabs missing from ``order`` (opened elsewhere meanwhile) keep their place at the end."""
    with _lock:
        data = _load_tabs()
        by_id = {t["id"]: t for t in data["tabs"]}
        wanted = [by_id[i] for i in dict.fromkeys(order) if i in by_id]
        data["tabs"] = wanted + [t for t in data["tabs"] if t not in wanted]
        _write("tabs.json", data)
        return _summary(data)


# ---------------------------------------------------------------- drafts (one per tab)
def clean_origin(origin: Any) -> dict[str, str] | None:
    """A tab's origin, the workload statement it was opened from: {"workload", "statement_id", "statement_name"}."""
    if not isinstance(origin, dict) or not isinstance(origin.get("workload"), str) or not isinstance(origin.get("statement_id"), str):
        return None
    out = {k: str(origin.get(k) or "")[:200] for k in ("workload", "statement_id", "statement_name")}
    return out if out["workload"].strip() and out["statement_id"].strip() else None


def clean_sas(sas: Any) -> dict[str, Any] | None:
    """A tab's SAS settings: SAS library / caslib it runs in, PROC SQL options and Show pushdown."""
    if not isinstance(sas, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("library", "caslib"):
        v = sas.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()[:128]
    try:
        if sas.get("outobs") not in (None, ""):
            out["outobs"] = max(1, min(int(sas["outobs"]), 10_000_000))
    except (TypeError, ValueError):
        pass
    for key in ("noexec", "feedback"):
        if sas.get(key):
            out[key] = True
    out["pushdown"] = sas.get("pushdown") is not False
    return out


def load_draft(tab_id: str | None = None) -> dict[str, Any] | None:
    """A tab's draft (the active tab's when ``tab_id`` is None)."""
    return _valid_draft(_read(_draft_name(resolve_tab(tab_id)), None))


def save_draft(draft: dict[str, Any], tab_id: str | None = None) -> dict[str, Any]:
    """Autosave one tab. A save for a tab that was closed meanwhile is dropped (``closed`` in the result).

    Without ``tab_id`` (only an editor page from before tabs, still open while the
    server was updated) it goes to the first tab, where its draft was migrated to.
    """
    if len(draft.get("sql") or "") > MAX_DRAFT_CHARS:
        raise ValueError(f"The editor text is larger than {MAX_DRAFT_CHARS // 1_000_000} MB; the draft isn't autosaved.")
    language = draft.get("language") or LANGUAGES[0]
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
    draft = {**draft, "sql": draft.get("sql") or "", "language": language, "updated": round(time.time(), 1)}
    with _lock:
        data = _load_tabs()
        tab_id = _check_id(tab_id) if tab_id else data["tabs"][0]["id"]
        if tab_id not in {t["id"] for t in data["tabs"]}:
            return {**draft, "tab_id": tab_id, "closed": True}
        _write(_draft_name(tab_id), draft)
    return {**draft, "tab_id": tab_id}


# ---------------------------------------------------------------- run history
def _history() -> dict[str, Any]:
    data = _read("history.json", None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        data = {"next_id": 1, "entries": []}
    return data


def add_history(sql: str, database: str | None, *, ok: bool, elapsed_ms: float | None = None,
                rows: int | None = None, affected: int | None = None, error: str | None = None,
                kind: str = "read", source: dict[str, Any] | None = None, language: str | None = None) -> None:
    """Record one statement the user ran. Never fails the run itself.

    ``source``: the editor tab it ran from, ``{"tab_id", "tab", "file"}`` (tab
    label and open file name), when the editor says.
    """
    sql = sql.strip()
    if not sql:
        return
    entry = {
        "t": round(time.time(), 1), "database": database, "kind": kind, "ok": ok,
        "ms": elapsed_ms, "rows": rows, "affected": affected,
        "error": (error or "")[:MAX_ERROR_CHARS] or None,
        "sql": sql[:MAX_HISTORY_SQL], "sql_truncated": len(sql) > MAX_HISTORY_SQL,
    }
    if language and language != "singlestore" and language in HISTORY_LANGUAGES:
        entry["language"] = language
    for key in ("tab_id", "tab", "file"):
        value = (source or {}).get(key)
        if isinstance(value, str) and value.strip():
            entry[key] = value.strip()[:200]
    try:
        with _lock:
            data = _history()
            entry["id"] = int(data.get("next_id") or 1)
            data["next_id"] = entry["id"] + 1
            data["entries"] = [*data["entries"], entry][-MAX_HISTORY:]
            _write("history.json", data)
    except OSError:
        pass


def list_history(search: str = "", limit: int = 100, before_id: int | None = None) -> dict[str, Any]:
    """Newest first, with SQL previews (full text via history_entry)."""
    entries = _history()["entries"]
    q = search.strip().lower()
    hits = [e for e in reversed(entries)
            if (before_id is None or e.get("id", 0) < before_id)
            and (not q or any(q in (e.get(k) or "").lower() for k in ("sql", "database", "file", "tab")))]
    limit = max(1, min(int(limit), 200))
    page = []
    for e in hits[:limit]:
        sql = e.get("sql", "")
        page.append({**e, "sql": sql[:PREVIEW_CHARS], "preview": len(sql) > PREVIEW_CHARS})
    return {"entries": page, "total": len(entries), "matches": len(hits), "more": len(hits) > limit}


def history_entry(entry_id: int) -> dict[str, Any]:
    for e in _history()["entries"]:
        if e.get("id") == entry_id:
            return e
    raise LookupError("That history entry is gone (the history keeps the last 500 statements).")


def history_count() -> int:
    return len(_history()["entries"])


def clear_history() -> int:
    with _lock:
        data = _history()
        removed = len(data["entries"])
        data["entries"] = []
        _write("history.json", data)
    return removed


# ---------------------------------------------------------------- recent files
def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def remember_file(path: str | Path, name: str) -> None:
    """Put a file at the top of the recent list."""
    path = str(path)
    try:
        with _lock:
            recent = [r for r in _read("recent.json", []) if isinstance(r, dict) and r.get("path") and not _same(r["path"], path)]
            _write("recent.json", [{"path": path, "name": name, "t": round(time.time(), 1)}, *recent][:MAX_RECENT])
    except OSError:
        pass


def recent_files() -> list[dict[str, Any]]:
    """Recent files that still exist, newest first."""
    out = []
    try:
        recent = _read("recent.json", [])
    except OSError:  # in use right now: the list is optional
        recent = []
    for r in recent if isinstance(recent, list) else []:
        if isinstance(r, dict) and r.get("path"):
            try:
                if Path(r["path"]).is_file():
                    out.append({"path": r["path"], "name": r.get("name") or Path(r["path"]).name, "t": r.get("t")})
            except OSError:
                continue
    return out
