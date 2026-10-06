"""Notebook app: SQL and Python cells on a Jupyter kernel, saved as .ipynb.

SQL cells run in the notebook's kernel (``_s2_sql`` in notebook_bridge's
startup code), so each result becomes a pandas DataFrame (``df`` or a named
variable) that Python cells can use. Python cells run as-is. Statements that
change data or schema ask for confirmation first, like the SQL Editor.

Files are standard Jupyter notebooks in the SQL folder; SQL cells are stored
as ``%%sql`` cells (``%%sql name <<`` when the result is named), the
convention SingleStore Notebooks and Jupyter use.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import assistant, notebook_kernel
from ..notebook_bridge import split_statements
from ._core import APP_ONLY, apps, register_app, tool_result, with_browser_link
from .query_grid import ReadOnlyViolation, check_read_only
from .sql_editor import _relative, _sql_path, deliver_reply, sql_dir

URI = "ui://singlestore/notebook.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
RUNS_CODE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
_SUFFIX = ".ipynb"
_MAX_FILE_BYTES = 20_000_000
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

register_app(URI, "notebook.html", name="Notebook", description="SQL and Python notebook on a Jupyter kernel", external_images=True)


@apps.tool(resource_uri=URI, title="SingleStore Notebook", annotations=READ_ONLY)
def notebook(name: str | None = None, database: str | None = None) -> CallToolResult:
    """Open a SingleStore notebook: SQL and Python cells on a Jupyter kernel.

    SQL cell results become pandas DataFrames for the Python cells; Python has
    pandas, matplotlib and `conn` (a SingleStore connection). Notebooks are
    .ipynb files in the user's SQL folder. It's also the Notebook view of the
    SingleStore Workspace (sql_editor with view="notebook").

    The result includes ``browser_url``: post it as a clickable link under the app.

    Args:
        name: Notebook file to open (e.g. "sales.ipynb"); omit for a new notebook.
        database: Database for SQL cells (case-sensitive).
    """
    data: dict[str, Any] = {"name": name, "database": database, "environment": notebook_kernel.environment_status()}
    summary = f"Notebook opened{f' ({name})' if name else ''}{f' on database {database}' if database else ''}."
    if data["environment"]["state"] != "ready":
        summary += " The notebook Python environment isn't installed yet; the app offers to install it."
    summary = with_browser_link(summary, data, "notebook", {"name": name, "database": database})
    return tool_result(summary, data)


# ------------------------------------------------------------------ environment and kernel


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_environment() -> CallToolResult:
    """Status of the notebook Python environment (and install progress)."""
    st = notebook_kernel.environment_status()
    return tool_result(st["state"], st)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def notebook_environment_install() -> CallToolResult:
    """Create the notebook Python environment (ipykernel, pandas, matplotlib, singlestoredb) with uv."""
    st = notebook_kernel.setup()
    return tool_result(st["state"], st)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_packages() -> CallToolResult:
    """Packages installed in the notebook Python environment (name, version, summary, core)."""
    pkgs = notebook_kernel.list_packages()
    return tool_result(f"{len(pkgs)} package(s)", {"packages": pkgs, "python": str(notebook_kernel.kernel_python())})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def notebook_packages_install(packages: str) -> CallToolResult:
    """Install packages (space-separated names with optional version pins) into the notebook environment."""
    st = notebook_kernel.install_packages(packages.split())
    return tool_result(st["state"], st)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_kernel_start(notebook_id: str) -> CallToolResult:
    """Start the notebook's Python kernel ahead of the first cell (no code runs)."""
    st = notebook_kernel.start(notebook_id)
    return tool_result(st["state"], st)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_kernel_state(notebook_id: str) -> CallToolResult:
    """The notebook kernel's state: stopped, starting, idle, busy, error or dead."""
    st = notebook_kernel.kernel_state(notebook_id)
    return tool_result(st["state"], st)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def notebook_kernel_control(notebook_id: str, action: str) -> CallToolResult:
    """Interrupt, restart or shut down the notebook's kernel (action: interrupt | restart | shutdown)."""
    if action == "interrupt":
        data = {"interrupted": notebook_kernel.interrupt(notebook_id)}
    elif action == "restart":
        data = notebook_kernel.restart(notebook_id)
    elif action == "shutdown":
        data = {"stopped": notebook_kernel.shutdown(notebook_id)}
    else:
        raise ValueError("action must be interrupt, restart or shutdown")
    return tool_result(action, data)


# ------------------------------------------------------------------ running cells


def sql_cell_code(sql: str, database: str | None, name: str | None) -> str:
    return f"_s2_sql({sql!r}, database={database!r}, name={name!r})"


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=RUNS_CODE)
def notebook_run(
    notebook_id: str,
    cell_type: str,
    source: str,
    database: str | None = None,
    name: str | None = None,
    confirmed: bool = False,
) -> CallToolResult:
    """Run one notebook cell in its kernel; returns a run id to poll with notebook_poll.

    cell_type "sql": a SingleStore statement whose result becomes a DataFrame
    (``df``, and ``name`` if given). Statements that change data or schema
    come back with ``needs_confirmation`` unless ``confirmed``.
    cell_type "python": Python code, run as-is.
    """
    if cell_type == "sql":
        sql = source.strip()
        if not sql:
            raise ValueError("The SQL cell is empty.")
        if name and not _NAME_RE.match(name):
            raise ValueError(f"{name!r} isn't a valid Python variable name.")
        if not confirmed:
            statements = split_statements(sql)
            for stmt in statements:
                try:
                    check_read_only(stmt)
                except ReadOnlyViolation as exc:
                    reason = str(exc).split(": ", 1)[-1].split(". Use run_sql")[0]
                    if len(statements) > 1:
                        reason = f"{len(statements)} statements, including {stmt.split(None, 1)[0].upper()}"
                    return tool_result("Needs confirmation", {"needs_confirmation": True, "reason": reason})
        code = sql_cell_code(sql, database, name)
    elif cell_type == "python":
        code = source
    else:
        raise ValueError("cell_type must be sql or python")
    run_id = notebook_kernel.execute(notebook_id, code, database)
    return tool_result(f"Running ({run_id})", {"run_id": run_id})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_poll(run_id: str, after: int = 0, cleared: int = 0) -> CallToolResult:
    """New output of a running (or finished) cell since output index ``after``."""
    data = notebook_kernel.poll(run_id, after, cleared)
    return tool_result("done" if data["done"] else "running", data)


# ------------------------------------------------------------------ files


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_files() -> CallToolResult:
    """The .ipynb notebooks in the SQL folder, newest first."""
    root = sql_dir()
    files = []
    if root.is_dir():
        for path in root.rglob(f"*{_SUFFIX}"):
            if path.is_file() and ".ipynb_checkpoints" not in path.parts:
                st = path.stat()
                files.append({"name": _relative(path.resolve()), "size": st.st_size, "modified": round(st.st_mtime)})
    files.sort(key=lambda f: f["modified"], reverse=True)
    return tool_result(f"{len(files)} notebook(s)", {"folder": str(root), "files": files[:500]})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_open(name: str) -> CallToolResult:
    """Read one .ipynb notebook from the SQL folder."""
    path = _sql_path(name, _SUFFIX)
    if not path.is_file():
        raise LookupError(f"{_relative(path)} doesn't exist in {sql_dir()}.")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"{_relative(path)} is larger than {_MAX_FILE_BYTES // 1_000_000} MB.")
    try:
        nb = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"{_relative(path)} isn't a valid notebook: {exc}") from exc
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise ValueError(f"{_relative(path)} isn't a Jupyter notebook.")
    return tool_result(f"Opened {_relative(path)}", {"name": _relative(path), "path": str(path), "notebook": nb, "folder": str(sql_dir())})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def notebook_save(name: str, notebook: dict[str, Any], overwrite: bool = False) -> CallToolResult:
    """Save a notebook (nbformat 4 JSON) as a .ipynb file in the SQL folder."""
    path = _sql_path(name, _SUFFIX)
    if not isinstance(notebook.get("cells"), list):
        raise ValueError("Not a notebook: missing cells.")
    text = json.dumps(notebook, indent=1, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > _MAX_FILE_BYTES:
        raise ValueError(f"The notebook is larger than {_MAX_FILE_BYTES // 1_000_000} MB (large outputs or images?).")
    if path.exists() and not overwrite:
        return tool_result(f"{_relative(path)} already exists.", {"name": _relative(path), "exists": True, "saved": False})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return tool_result(f"Saved {_relative(path)}", {"name": _relative(path), "path": str(path), "saved": True, "exists": False, "folder": str(sql_dir())})


# ------------------------------------------------------------------ assistant


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_ask(notebook_id: str, question: str, context: str = "", profile: str | None = None) -> CallToolResult:
    """Ask the in-app assistant about the notebook; the reply arrives in the notebook's inbox (sql_editor_inbox)."""
    if not notebook_id.strip() or not question.strip():
        raise ValueError("notebook_id and question are required")
    prompt = f"{context.strip()[:12000]}\n\nQuestion: {question.strip()}" if context.strip() else f"Question: {question.strip()}"
    assistant.ask(notebook_id, prompt, deliver_reply, profile, kind="notebook")
    return tool_result("Claude is answering in the notebook.", {"started": True})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def notebook_assistant_warm(notebook_id: str, profile: str | None = None) -> CallToolResult:
    """Start the notebook's assistant process ahead of the first question (no model call)."""
    return tool_result("ok", {"warm": assistant.warm(notebook_id, profile, kind="notebook")})
