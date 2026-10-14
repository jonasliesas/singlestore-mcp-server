"""SQL Editor app: schema tree, SQL editor with autocomplete, and a results pane.

Read-only statements run through ``sql_editor_query`` (the Query Grid's
read-only guard and row cap). Anything else needs the user to confirm in the
editor and then runs through ``sql_editor_execute``, which is marked
destructive so hosts can ask for approval. Both are app-only: the model opens
the editor with ``sql_editor`` and uses run_sql / query_grid itself.

The editor's draft, run history and recent files are kept server-side (see
``singlestore_mcp.sql_editor_state``); both run tools record into the history.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ToolAnnotations

from .. import assistant, fileio, result_stream
from .. import sql_editor_state as editor_state
from ..db import db
from ._core import APP_ONLY, apps, head_hash, jsonable_rows, page_parts, register_app, tool_result, with_browser_link
from .query_grid import MAX_ROWS_LIMIT, ReadOnlyViolation, check_read_only

URI = "ui://singlestore/sql-editor.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
SYSTEM_DATABASES = ("information_schema", "cluster", "memsql")

register_app(
    URI,
    "sql_editor.html",
    name="SQL Editor",
    description="SQL editor with SingleStore autocomplete, schema tree and results",
)

# The workspace shell: SQL editor plus the Schema, Pipelines and Cluster apps
# as views behind a left-hand rail. sql_editor opens it.
WORKSPACE_URI = "ui://singlestore/workspace.html"
WORKSPACE_VIEWS = ("sql", "notebook", "schema", "pipelines", "cluster", "history", "security", "config", "alerts", "workload", "connections")
register_app(
    WORKSPACE_URI,
    "workspace.html",
    name="SingleStore Workspace",
    description="SQL editor with notebook, schema explorer, pipeline monitor and cluster monitor views",
    external_images=True,  # the Notebook view renders text cells with images
)


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def app_page(uri: str, head: str | None = None, build: str | None = None) -> CallToolResult:
    """HTML of one of this server's app pages (the workspace loads its views with this).

    Tool results aren't cached by hosts, unlike resources/read, so views pick
    up changes right after restart_server.

    ``head``: the caller's shared-head hash (its ``s2-head`` meta); when it matches,
    the page comes without the shared head (``head_omitted``; the caller puts its own
    copy in at SHARED_HEAD_MARKER). ``build``: the build the caller already has; when
    it matches, only ``unchanged`` comes back.
    """
    parts = page_parts(uri)
    if parts is None:
        raise LookupError(f"No app page {uri!r}")
    page_build, html, lite = parts
    if build and build == page_build:
        return tool_result(f"{uri} unchanged", {"uri": uri, "build": page_build, "unchanged": True})
    omit = bool(head) and head == head_hash()
    page = lite if omit else html
    return tool_result(f"{uri} ({len(page) // 1024} KB)",
                       {"uri": uri, "build": page_build, "html": page, "head_omitted": omit})


def _databases() -> list[str]:
    rows = db.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")[1]
    names = [r["SCHEMA_NAME"] for r in rows]
    return sorted(names, key=lambda n: (n in SYSTEM_DATABASES, n.lower(), n))


@apps.tool(resource_uri=WORKSPACE_URI, title="SingleStore Workspace", annotations=READ_ONLY)
def sql_editor(
    database: str | None = None,
    sql: str | None = None,
    view: str = "sql",
    table: str | None = None,
    new_tab: bool = False,
) -> CallToolResult:
    """Open the SingleStore workspace: an interactive SQL editor plus views.

    A slim rail on the left switches between the SQL Editor (schema tree,
    editor with autocomplete for keywords, SingleStore functions, databases,
    tables and columns, and a results pane), the Schema Explorer, the Pipeline
    Monitor and the Cluster Monitor. Use this when the user wants to write,
    edit or run SQL themselves, or wants the combined workspace. To run a
    query for your own reasoning use run_sql; to show results use query_grid.
    The editor has a chat panel: questions from it arrive in the conversation
    tagged "[SQL Editor <id>]"; answer those with sql_editor_reply.

    The editor has tabs; `sql` goes into the active tab, below the user's
    text (or into a new tab with new_tab=true).

    The result includes ``browser_url``, which opens this view full-window in
    the user's browser: post it as a clickable link right under the app.

    Args:
        database: Database to start in (case-sensitive).
        sql: SQL to put in the editor (not run automatically).
        view: View to show first: "sql" (default), "notebook", "schema", "pipelines", "cluster", "history" (Query History), "alerts", "workload" or "connections".
        table: For view="schema": table to pre-select in `database`.
        new_tab: With `sql`: open it in a new editor tab instead of the active one.
    """
    if view not in WORKSPACE_VIEWS:
        raise ValueError(f"view must be one of {', '.join(WORKSPACE_VIEWS)}")
    tab_id = None
    if new_tab and sql:
        try:
            tab_id = editor_state.create_tab(editor_state.resolve_tab(None))["tab"]["id"]
        except ValueError:  # too many tabs: the SQL goes into the active one
            pass
        view = "sql"
    # A failing connection must not lock the user out of the workspace: it then
    # opens on the Connections view with the error, to fix or switch connection.
    connection_error = None
    try:
        databases = _databases()
    except Exception as exc:  # noqa: BLE001 - shown in the app and to the model
        databases, connection_error = [], str(exc)
        view = "connections" if view in ("sql", "schema", "pipelines", "cluster") else view
    if database and databases and database not in databases:
        raise LookupError(f"Database {database!r} not found (names are case-sensitive).")
    data: dict[str, Any] = {"databases": databases, "database": database, "sql": sql, "view": view, "table": table,
                            "connection_error": connection_error, "tab_id": tab_id}
    summary = f"SingleStore Workspace opened on the {view} view{f', database {database}' if database else ''}."
    if connection_error:
        summary += f" The active connection fails ({connection_error[:200]}); the user can fix or switch it in Connections."
    if sql:
        summary += f" {'New editor tab' if tab_id else 'Editor'} prefilled with: {' '.join(sql.split())[:300]}"
    summary = with_browser_link(
        summary, data, "sql_editor", {"database": database, "sql": sql, "view": view if view != "sql" else None, "table": table}
    )
    return tool_result(summary, data)


def _record_failure(sql: str, database: str | None, kind: str, started: float, exc: Exception,
                    source: dict[str, Any]) -> None:
    message = str(exc).replace("\n", " ") or type(exc).__name__
    editor_state.add_history(sql, database, ok=False, kind=kind, error=message, source=source,
                             elapsed_ms=round((time.perf_counter() - started) * 1000, 1))


def _stream_columns(stream: result_stream.Stream, rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Column names and warnings: singlestoredb keys a repeated column name as "table.col", so the row
    keys are the reliable column list then (as in query_grid.run_query)."""
    columns = list(stream.columns or [])
    unique = list(dict.fromkeys(columns))
    if len(unique) == len(columns):
        return unique, []
    if rows and len(rows[0]) == len(columns):
        unique = list(rows[0].keys())
    dupes = sorted({c for c in columns if columns.count(c) > 1})
    return unique, [f"Duplicate column names ({', '.join(dupes)}); alias them for clearer headers."]


def _stream_data(stream: result_stream.Stream, rows: list[dict[str, Any]], offset: int = 0) -> dict[str, Any]:
    st = stream.status()
    columns, warnings = _stream_columns(stream, rows)
    return {
        **st,
        "sql": stream.sql, "database": stream.database, "kind": "read",
        "columns": columns, "has_result_set": bool(stream.columns),
        "rows": rows, "offset": offset,
        # Compatibility with the non-streamed result: row_count = rows read so far (the total once done).
        "row_count": st["loaded"], "truncated": st["capped"], "warnings": warnings,
    }


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_query(sql: str, database: str | None = None, max_rows: int = result_stream.FIRST_PAGE,
                     tab_id: str | None = None, tab: str | None = None, file: str | None = None,
                     stream_id: str | None = None, replaces: str | None = None) -> CallToolResult:
    """Run a read-only statement from the SQL Editor app, streaming its result.

    Returns as soon as the first ``max_rows`` rows (the first page) are in, or the result is complete; the
    rest keeps loading on the server (up to SINGLESTORE_MCP_EDITOR_MAX_ROWS, default 100,000 rows) and the
    app pages through it with sql_editor_rows (``done`` / ``loaded`` give the progress and the total).
    ``stream_id`` is chosen by the app, so its Stop (sql_editor_stop) works before this call returns;
    ``replaces``: the tab's previous result, dropped. Statements that change data or schema are answered
    with ``needs_confirmation`` instead of running; the app then asks the user and uses
    sql_editor_execute. Every run lands in the editor's history (when it has finished), with the tab
    (``tab_id``, its label ``tab``) and open ``file`` it ran from.
    """
    try:
        check_read_only(sql)
    except ReadOnlyViolation as exc:
        data = {"needs_confirmation": True, "reason": str(exc), "sql": sql, "database": database}
        return tool_result("This statement changes data or schema and needs confirmation.", data)
    source = {"tab_id": tab_id, "tab": tab, "file": file}
    first = max(1, min(int(max_rows), result_stream.MAX_PAGE_ROWS))

    def record(s: result_stream.Stream) -> None:
        if s.error is not None:
            editor_state.add_history(sql, database, ok=False, kind="read", error=_error_text(s.error), source=source,
                                     elapsed_ms=s.total_ms)
        else:
            editor_state.add_history(sql, database, ok=True, kind="read", elapsed_ms=s.total_ms, source=source,
                                     rows=len(s.rows) if s.columns else None)

    stream = result_stream.start(sql, database, stream_id=stream_id, replaces=replaces, on_finish=record)
    stream.wait_first(first)
    if stream.done:
        stream.finished.wait(5)  # its history entry is written before the app refreshes the history
    if stream.error is not None and not stream.rows:
        raise ToolError(_error_text(stream.error))
    data = _stream_data(stream, stream.page(0, first))
    more = "" if data["done"] else "; more loading"
    return tool_result(f"{data['loaded']} row(s) in {data['elapsed_ms']} ms{more}", data)


def _error_text(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 2 and isinstance(args[0], int):
        return f"SingleStore error {args[0]}: {args[1]}"
    return str(exc).replace("\n", " ") or type(exc).__name__


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_rows(stream_id: str, offset: int = 0, limit: int = 5000, wait_ms: int = 0) -> CallToolResult:
    """More rows of a streamed SQL Editor result (from sql_editor_query), from ``offset``, plus its progress.

    At most ``limit`` rows and about 2 MB per call. ``wait_ms``: wait up to this long for rows past
    ``offset`` (or the end) while the result is still loading, instead of polling.
    """
    stream = result_stream.get(stream_id)
    if wait_ms:
        stream.wait_first(int(offset) + 1, timeout=min(int(wait_ms), 10_000) / 1000)
    rows = stream.page(offset, limit)
    data = _stream_data(stream, rows, int(offset))
    return tool_result(f"{len(rows)} row(s) from {offset}; {data['loaded']} loaded{'' if data['done'] else ', loading'}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_stop(stream_id: str) -> CallToolResult:
    """Stop a SQL Editor query that is still running or loading rows (KILL QUERY on its own connection,
    after checking it still runs this statement). The rows read so far stay."""
    stream = result_stream.find(stream_id)
    stopped = bool(stream and stream.stop())
    return tool_result("Stopped." if stopped else "Nothing to stop.", {"stream_id": stream_id, "stopped": stopped})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_classify(statements: list[str]) -> CallToolResult:
    """Which of these statements need the user's confirmation (change data or schema), for "Run all".

    Same check as sql_editor_query, without running anything.
    """
    if len(statements) > 2000:
        raise ValueError("Run all handles up to 2000 statements at a time.")
    writes = []
    for i, sql in enumerate(statements):
        try:
            check_read_only(sql)
        except ReadOnlyViolation as exc:
            writes.append({"index": i, "reason": str(exc).split(": ", 1)[-1][:200]})
    return tool_result(f"{len(writes)} of {len(statements)} statement(s) need confirmation", {"writes": writes})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=WRITES)
def sql_editor_execute(sql: str, database: str | None = None, max_rows: int = 1000, tab_id: str | None = None,
                       tab: str | None = None, file: str | None = None) -> CallToolResult:
    """Run any single statement the user confirmed in the SQL Editor app (writes and DDL included)."""
    max_rows = max(1, min(int(max_rows), MAX_ROWS_LIMIT))
    source = {"tab_id": tab_id, "tab": tab, "file": file}
    started = time.perf_counter()
    try:
        columns, rows, rowcount = db.execute(sql, database=database, max_rows=max_rows + 1)
    except Exception as exc:
        _record_failure(sql, database, "write", started, exc, source)
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    data = {
        "kind": "write",
        "sql": sql,
        "database": database,
        "columns": list(dict.fromkeys(columns)),
        "has_result_set": bool(columns),
        "rows": jsonable_rows(rows[:max_rows]),
        "row_count": min(len(rows), max_rows),
        "truncated": len(rows) > max_rows,
        "affected_rows": rowcount if rowcount is not None and rowcount >= 0 else None,
        "elapsed_ms": elapsed_ms,
        "max_rows": max_rows,
        "warnings": [],
    }
    editor_state.add_history(sql, database, ok=True, kind="write", elapsed_ms=elapsed_ms, source=source,
                             rows=data["row_count"] if columns else None, affected=data["affected_rows"])
    return tool_result(f"Statement ran in {elapsed_ms} ms; {data['affected_rows']} row(s) affected", data)


# ---------------------------------------------------------------- SQL files
# Open/Save work on files on the machine running this server (the user's
# laptop), so they work inside Claude and in the browser view alike. Names
# are relative to the SQL folder (SINGLESTORE_MCP_SQL_DIR) or absolute paths;
# either way they must be inside an allowed root: the user's home folder, the
# SQL folder, and any SINGLESTORE_MCP_FILE_ROOTS (separated by os.pathsep).
_SQL_SUFFIX = ".sql"
_SAS_SUFFIX = ".sas"  # SAS programs open and save too (a .sas file opens as PROC SQL)
_MAX_FILE_BYTES = 2_000_000
_MAX_LISTED = 500


def sql_dir() -> Path:
    configured = os.environ.get("SINGLESTORE_MCP_SQL_DIR")
    if configured:
        return Path(configured).expanduser()
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / "SingleStore SQL"


def file_roots() -> list[Path]:
    roots = [Path.home(), sql_dir()]
    roots += [Path(r).expanduser() for r in os.environ.get("SINGLESTORE_MCP_FILE_ROOTS", "").split(os.pathsep) if r.strip()]
    out: list[Path] = []
    for r in roots:
        r = r.resolve()
        if not any(r.is_relative_to(o) for o in out):
            out = [o for o in out if not o.is_relative_to(r)] + [r]
    return out


def _check_allowed(path: Path) -> Path:
    path = path.resolve()
    if not any(path.is_relative_to(r) for r in file_roots()):
        raise ValueError(f"{path} is outside the folders the editor may use (your user folder"
                         " and SINGLESTORE_MCP_FILE_ROOTS).")
    return path


def _sql_path(name: str, suffix: str = _SQL_SUFFIX) -> Path:
    """Resolve a file name: relative to the SQL folder, or absolute inside an allowed root."""
    name = name.strip()
    if not name or name.endswith(("/", "\\")):
        raise ValueError("Give the file a name.")
    if not name.lower().endswith(suffix) and not (suffix == _SQL_SUFFIX and name.lower().endswith(_SAS_SUFFIX)):
        name += suffix
    raw = Path(name).expanduser()
    path = _check_allowed(raw if raw.is_absolute() else sql_dir() / name.replace("\\", "/"))
    if path in file_roots():
        raise ValueError("Give the file a name.")
    return path


def _relative(path: Path) -> str:
    """Display name: relative to the SQL folder when inside it, else the full path."""
    root = sql_dir().resolve()
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def _known_folders() -> list[dict[str, str]]:
    """Shortcuts for the file browser: the SQL folder plus Documents, Desktop, Downloads, Home."""
    found: dict[str, Path] = {"SQL folder": sql_dir()}
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
            for label, value in (("Documents", "Personal"), ("Desktop", "Desktop"),
                                 ("Downloads", "{374DE290-123F-4565-9164-39C4925E467B}")):
                try:
                    found[label] = Path(os.path.expandvars(winreg.QueryValueEx(key, value)[0]))
                except OSError:
                    pass
        except OSError:
            pass
    for label in ("Documents", "Desktop", "Downloads"):
        found.setdefault(label, Path.home() / label)
    found["Home"] = Path.home()
    return [{"label": k, "path": str(v)} for k, v in found.items() if v.is_dir()]


def _hidden(path: Path) -> bool:
    if path.name.startswith((".", "$")) or path.name in ("AppData", "node_modules", "__pycache__"):
        return True
    attrs = getattr(path.stat(), "st_file_attributes", 0)
    return bool(attrs & 0x2)  # FILE_ATTRIBUTE_HIDDEN


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def files_browse(path: str | None = None, kind: str = "sql") -> CallToolResult:
    """List a folder for the file browser: subfolders and .sql / .sas (kind="sql") or .ipynb (kind="notebook") files.

    ``path`` defaults to the SQL folder. Only folders inside the allowed roots
    (the user's home folder, the SQL folder, SINGLESTORE_MCP_FILE_ROOTS) can be browsed.
    """
    suffix = {"notebook": (".ipynb",), "pem": (".pem", ".crt", ".cer", ".key")}.get(kind, (_SQL_SUFFIX, _SAS_SUFFIX))
    folder = _check_allowed(Path(path).expanduser() if path else sql_dir())
    if not folder.is_dir():
        if folder == sql_dir().resolve():
            folder.mkdir(parents=True, exist_ok=True)
        else:
            raise LookupError(f"{folder} isn't a folder.")
    folders, files = [], []
    try:
        entries = list(folder.iterdir())
    except PermissionError as exc:
        raise ValueError(f"No access to {folder}.") from exc
    for entry in entries:
        try:
            if _hidden(entry):
                continue
            if entry.is_dir():
                folders.append({"name": entry.name, "path": str(entry)})
            elif entry.suffix.lower() in suffix:
                st = entry.stat()
                files.append({"name": entry.name, "path": str(entry), "size": st.st_size, "modified": round(st.st_mtime)})
        except OSError:
            continue
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: f["modified"], reverse=True)
    parent = folder.parent
    data = {
        "path": str(folder),
        "parent": str(parent) if parent != folder and any(parent.is_relative_to(r) for r in file_roots()) else None,
        "folders": folders[:_MAX_LISTED],
        "files": files[:_MAX_LISTED],
        "shortcuts": _known_folders(),
        "sql_folder": str(sql_dir()),
    }
    return tool_result(f"{len(folders)} folder(s), {len(files)} file(s) in {folder}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_files() -> CallToolResult:
    """The .sql files in the SQL Editor's folder, newest first."""
    root = sql_dir()
    files = []
    if root.is_dir():
        for path in root.rglob(f"*{_SQL_SUFFIX}"):
            if path.is_file():
                st = path.stat()
                files.append({"name": _relative(path.resolve()), "size": st.st_size, "modified": round(st.st_mtime)})
    files.sort(key=lambda f: f["modified"], reverse=True)
    data = {"folder": str(root), "files": files[:_MAX_LISTED], "truncated": len(files) > _MAX_LISTED}
    return tool_result(f"{len(files)} SQL file(s) in {root}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_open_file(name: str, remember: bool = True) -> CallToolResult:
    """Read one .sql file from the SQL Editor's folder.

    ``remember`` puts it on the Recent list (false when the editor only
    re-reads its open file to compare it with the restored draft).
    """
    path = _sql_path(name)
    if not path.is_file():
        raise LookupError(f"{_relative(path)} doesn't exist in {sql_dir()}.")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"{_relative(path)} is larger than {_MAX_FILE_BYTES // 1_000_000} MB.")
    raw = path.read_bytes()
    try:
        sql = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        sql = raw.decode("cp1252", errors="replace")
    if remember:
        editor_state.remember_file(path, _relative(path))
    return tool_result(f"Opened {_relative(path)}", {"name": _relative(path), "path": str(path), "sql": sql, "folder": str(sql_dir())})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def sql_editor_save_file(name: str, sql: str, overwrite: bool = False) -> CallToolResult:
    """Save the editor's SQL as a .sql file in the SQL Editor's folder.

    An existing file is only replaced with ``overwrite``; otherwise the result
    says ``exists`` so the app can ask first.
    """
    path = _sql_path(name)
    if len(sql.encode("utf-8")) > _MAX_FILE_BYTES:
        raise ValueError(f"The SQL is larger than {_MAX_FILE_BYTES // 1_000_000} MB.")
    if path.exists() and not overwrite:
        return tool_result(f"{_relative(path)} already exists.", {"name": _relative(path), "exists": True, "saved": False})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(sql, encoding="utf-8", newline="")
    fileio.replace(tmp, path)  # retried while Windows has the file open (an editor, antivirus)
    editor_state.remember_file(path, _relative(path))
    return tool_result(
        f"Saved {_relative(path)}",
        {"name": _relative(path), "saved": True, "exists": False, "folder": str(sql_dir()), "path": str(path)},
    )


# ---------------------------------------------------------------- editor state
# The editor has tabs, each autosaving its text (debounced) and restored when
# it opens again, also after a restart; see singlestore_mcp.sql_editor_state.
# Without a tab id (an editor outside the workspace, or a page from before
# tabs) the draft tools use the active tab.
@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_state(tab_id: str | None = None) -> CallToolResult:
    """One SQL Editor tab's saved draft (the active tab's by default), recent files and history size, for restoring it."""
    tab_id = editor_state.resolve_tab(tab_id)
    draft = editor_state.load_draft(tab_id)
    data = {"tab_id": tab_id, "draft": draft, "recent": editor_state.recent_files(), "history_count": editor_state.history_count()}
    return tool_result(f"Draft of {len(draft['sql']) if draft else 0} char(s); {len(data['recent'])} recent file(s)", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def sql_editor_save_draft(
    sql: str,
    database: str | None = None,
    anchor: int = 0,
    head: int = 0,
    file_name: str | None = None,
    file_path: str | None = None,
    file_external: bool = False,
    saved_hash: str | None = None,
    continue_on_error: bool = False,
    tab_id: str | None = None,
    dirty: bool = False,
    language: str = "singlestore",
    origin: dict[str, Any] | None = None,
    sas: dict[str, Any] | None = None,
) -> CallToolResult:
    """Autosave one SQL Editor tab's text, database and cursor (never writes the open .sql file itself).

    ``saved_hash`` is the editor's hash of the open file's last saved text, so a
    restored draft knows whether it has unsaved changes; ``dirty`` says so for
    the tab bar. ``origin``: the workload statement the tab was opened from
    (``{"workload", "statement_id", "statement_name"}``), for "Update in workload".
    ``language``: what the tab runs as (singlestore, procsql, fedsql, cas); ``sas``: its SAS library / caslib
    and PROC SQL options (outobs, noexec, feedback, pushdown).
    ``closed`` in the result: the tab was closed (elsewhere) and the text wasn't kept.
    """
    file = {"name": file_name, "path": file_path, "external": file_external, "saved_hash": saved_hash} if file_name else None
    draft = editor_state.save_draft({"sql": sql, "database": database, "cursor": {"anchor": anchor, "head": head},
                                     "file": file, "continue_on_error": continue_on_error, "dirty": dirty,
                                     "language": language, "origin": editor_state.clean_origin(origin),
                                     "sas": editor_state.clean_sas(sas)}, tab_id)
    closed = bool(draft.get("closed"))
    return tool_result("Tab is closed; draft not saved" if closed else "Draft saved",
                       {"updated": draft["updated"], "tab_id": draft["tab_id"], "closed": closed})


# Tab bar (the workspace shell calls these).
_WRITES_STATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


def _tabs_result(data: dict[str, Any]) -> CallToolResult:
    return tool_result(f"{len(data['tabs'])} SQL Editor tab(s)", data)


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_tabs() -> CallToolResult:
    """The SQL Editor's tabs in order, each with its open file and unsaved-changes flag, and the active tab."""
    return _tabs_result(editor_state.list_tabs())


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=_WRITES_STATE)
def sql_editor_tab_new(after: str | None = None) -> CallToolResult:
    """Open a new, empty SQL Editor tab right after the tab ``after`` (else at the end) and make it active."""
    return _tabs_result(editor_state.create_tab(after))


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=_WRITES_STATE)
def sql_editor_tab_close(tab_id: str) -> CallToolResult:
    """Close a SQL Editor tab and drop its draft (the open .sql file is untouched); the last tab is replaced by an empty one."""
    return _tabs_result(editor_state.close_tab(tab_id))


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=_WRITES_STATE)
def sql_editor_tab_activate(tab_id: str) -> CallToolResult:
    """Remember which SQL Editor tab is active (it's the one that opens next time)."""
    return tool_result("Active tab set", editor_state.activate_tab(tab_id))


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=_WRITES_STATE)
def sql_editor_tab_rename(tab_id: str, title: str | None = None) -> CallToolResult:
    """Give a SQL Editor tab its own title; an empty title goes back to the file name or "untitled N"."""
    return _tabs_result(editor_state.rename_tab(tab_id, title))


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=_WRITES_STATE)
def sql_editor_tab_reorder(order: list[str]) -> CallToolResult:
    """Put the SQL Editor tabs in this order (tab ids)."""
    return _tabs_result(editor_state.reorder_tabs(order))


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_history(search: str = "", limit: int = 100, before_id: int | None = None) -> CallToolResult:
    """The SQL Editor's run history, newest first (SQL cut to a preview; full text with sql_editor_history_entry)."""
    data = editor_state.list_history(search, limit, before_id)
    return tool_result(f"{len(data['entries'])} of {data['matches']} statement(s)", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_history_entry(entry_id: int) -> CallToolResult:
    """One statement from the SQL Editor's run history, with its full SQL."""
    entry = editor_state.history_entry(entry_id)
    return tool_result(f"History entry {entry_id}", entry)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def sql_editor_history_clear() -> CallToolResult:
    """Empty the SQL Editor's run history."""
    removed = editor_state.clear_history()
    return tool_result(f"Removed {removed} statement(s) from the history", {"removed": removed})


# ---------------------------------------------------------------- chat relay
# Questions travel editor -> chat as a user message; answers travel back via
# sql_editor_reply into this per-editor inbox, which the open editor polls.
_INBOX_MAX_MESSAGES = 50
_INBOX_MAX_EDITORS = 50
_inbox: dict[str, list[dict[str, Any]]] = {}
_inbox_lock = threading.Lock()
_inbox_seq = 0


def deliver_reply(editor_id: str, message: str, kind: str = "claude") -> int:
    """Queue a reply ("claude") or a problem ("error"/"note") for an editor's chat panel; returns its sequence number."""
    global _inbox_seq
    if not editor_id.strip() or not message.strip():
        raise ValueError("editor_id and message are required")
    with _inbox_lock:
        _inbox_seq += 1
        box = _inbox.setdefault(editor_id, [])
        box.append({"seq": _inbox_seq, "t": round(time.time(), 1), "text": message, "kind": kind})
        del box[:-_INBOX_MAX_MESSAGES]
        while len(_inbox) > _INBOX_MAX_EDITORS:
            _inbox.pop(next(iter(_inbox)))
        return _inbox_seq


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_inbox(editor_id: str, after: int = 0) -> CallToolResult:
    """Replies sent to one SQL Editor's chat panel, newer than ``after``."""
    with _inbox_lock:
        messages = [m for m in _inbox.get(editor_id, []) if m["seq"] > after]
    data = {"editor_id": editor_id, "messages": messages, "job": assistant.job_state(editor_id)}
    return tool_result(f"{len(messages)} new message(s)", data)


# In-app answers: headless Claude Code with read-only database tools (see
# singlestore_mcp.assistant). Works in the browser view too, and needs no
# Send click in the Claude chat.
@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_assistant() -> CallToolResult:
    """Whether the SQL Editor can answer questions in the app (Claude Code installed)."""
    info = assistant.status()
    return tool_result("available" if info["available"] else info["reason"], info)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_ask(
    editor_id: str,
    question: str,
    database: str | None = None,
    sql: str | None = None,
    selected: bool = False,
    last_result: str | None = None,
    profile: str | None = None,
) -> CallToolResult:
    """Answer a SQL Editor chat question in the app; the reply arrives in the editor's inbox.

    ``profile`` picks speed vs depth: "fast", "balanced" (default) or "thorough".
    """
    if not editor_id.strip() or not question.strip():
        raise ValueError("editor_id and question are required")
    prompt = assistant.build_prompt(question, database, sql, selected, last_result)
    assistant.ask(editor_id, prompt, deliver_reply, profile)
    return tool_result("Claude is answering in the editor.", {"editor_id": editor_id, "started": True})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_assistant_warm(editor_id: str, profile: str | None = None) -> CallToolResult:
    """Start the editor's assistant process ahead of the first question (no model call)."""
    started = assistant.warm(editor_id, profile)
    return tool_result("Assistant ready." if started else "Assistant unavailable.", {"warm": started})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_ask_cancel(editor_id: str) -> CallToolResult:
    """Stop the in-app answer that is running for one SQL Editor."""
    stopped = assistant.cancel(editor_id)
    return tool_result("Stopped." if stopped else "Nothing to stop.", {"stopped": stopped})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_schema(database: str) -> CallToolResult:
    """Tables, columns and routines of one database, for the SQL Editor's tree and autocomplete."""
    tables, columns, routines = db.parallel(
        lambda: db.execute(
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
            (database,),
        )[1],
        lambda: db.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (database,),
        )[1],
        lambda: db.execute(
            "SELECT ROUTINE_NAME, ROUTINE_TYPE, DATA_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s",
            (database,),
        )[1],
    )
    if not tables and database not in _databases():
        raise LookupError(f"Database {database!r} not found (names are case-sensitive).")
    by_table: dict[str, list[dict[str, Any]]] = {}
    for c in columns:
        by_table.setdefault(c["TABLE_NAME"], []).append(
            {"name": c["COLUMN_NAME"], "type": c["COLUMN_TYPE"], "key": c["COLUMN_KEY"] or None}
        )
    data = {
        "database": database,
        "tables": sorted(
            (
                {"name": t["TABLE_NAME"], "view": t["TABLE_TYPE"] != "BASE TABLE", "columns": by_table.get(t["TABLE_NAME"], [])}
                for t in tables
            ),
            key=lambda t: (t["name"].lower(), t["name"]),
        ),
        "routines": [
            {"name": r["ROUTINE_NAME"], "type": r["ROUTINE_TYPE"], "returns": r["DATA_TYPE"] or None} for r in routines
        ],
    }
    return tool_result(f"{len(data['tables'])} table(s) in {database}", data)
