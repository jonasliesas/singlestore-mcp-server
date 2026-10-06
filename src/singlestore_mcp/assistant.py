"""In-app Claude for the SQL Editor's chat panel, via headless Claude Code.

The SQL Editor can answer questions without going through the Claude chat:
this module keeps one Claude Code process per editor running in print mode
with streaming input (``claude -p --input-format stream-json``), using the
user's own Claude login, and drops each answer into the editor's inbox (the
same one ``sql_editor_reply`` uses).

The headless session gets no built-in tools (no shell, no file access) and
one MCP server: this package started with ``--serve-readonly``, which offers
only schema lookups and read-only queries (the Query Grid's read-only guard).
Each editor keeps one Claude session, so follow-up questions have context.

Settings (environment variables, all optional):
  SINGLESTORE_MCP_CLAUDE            path to the claude executable
  SINGLESTORE_MCP_ASSISTANT_MODEL   model for the "balanced" profile (default sonnet)
  SINGLESTORE_MCP_ASSISTANT_EFFORT  effort for the "balanced" profile (default medium)
  SINGLESTORE_MCP_ASSISTANT_TIMEOUT seconds before an answer is abandoned (default 300)
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from . import connections as _connections
from .paths import data_dir
from typing import Any, Callable

READONLY_SERVER = "singlestore_ro"
SKILL_FILE = Path(__file__).parent / "skills" / "singlestore-sql" / "SKILL.md"
# Speed/quality profiles the editor offers: (model, effort).
PROFILES = {
    "fast": ("haiku", None),
    "balanced": (None, None),  # filled from the environment below
    "thorough": ("opus", "high"),
}
DEFAULT_PROFILE = "balanced"
_MAX_CONCURRENT = 3
_SYSTEM_PROMPT = """\
You are the assistant inside the SQL Editor of a SingleStore MCP app. The user \
types questions in the editor's chat panel; your reply is shown there as plain \
text, and every ```sql fenced block gets "Replace editor", "Insert at cursor" \
and "Copy" buttons. The user is working in a SQL editor, so the SQL is the \
main deliverable:
- Every answer must include the SQL behind it in a ```sql block, ready to run \
in the editor - also when you computed the answer yourself with read_query: \
then give the query (combined into one statement where reasonable) that \
produces what you report, so the user can run it and see the result.
- Put each SQL statement in its own ```sql block (fence tag exactly `sql`); \
write SQL keywords in UPPERCASE. Don't fence anything that isn't SQL.
- Keep the prose short: a few sentences or "- " bullets. The panel only \
renders plain text, **bold**, `inline code` and ```sql blocks, so never use \
Markdown tables or headings.
- Write SingleStore SQL (MySQL-compatible, with SingleStore extensions). \
Database and table names are case-sensitive.
- You can inspect the database with the singlestore_ro tools: list_databases, \
list_tables, describe_table and read_query (read-only statements only). Check \
table and column names before using them, and test the SQL you suggest when \
that is cheap. Tables can be very large: prefer aggregates, LIMIT and \
information_schema metadata over scanning full tables more than once.
- You cannot change data or schema yourself. If the user wants a change, give \
the statement in a ```sql block for them to review and run."""


# ------------------------------------------------------------------ claude CLI


def find_claude() -> str | None:
    """Path of the Claude Code executable, or None if it isn't installed."""
    configured = os.environ.get("SINGLESTORE_MCP_CLAUDE")
    if configured:
        return configured if Path(configured).exists() else None
    found = shutil.which("claude")
    if not found:
        return None
    # The npm install puts a claude.cmd wrapper on PATH; call the native
    # binary behind it so arguments don't go through cmd.exe quoting.
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".bat", ".ps1", ""):
        native = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.exists():
            return str(native)
    return found


def _workdir() -> Path:
    # A neutral folder, so the assistant doesn't pick up a project's CLAUDE.md;
    # Claude Code keeps the editor sessions under this folder's project entry.
    return data_dir("assistant")


def _mcp_config(workdir: Path) -> Path:
    config = {
        "mcpServers": {
            READONLY_SERVER: {
                "command": sys.executable,
                "args": ["-m", "singlestore_mcp.assistant", "--serve-readonly"],
            }
        }
    }
    path = workdir / "mcp-readonly.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def _profile(name: str | None) -> tuple[str | None, str | None]:
    if name == "balanced" or name not in PROFILES:
        return (
            os.environ.get("SINGLESTORE_MCP_ASSISTANT_MODEL", "sonnet"),
            os.environ.get("SINGLESTORE_MCP_ASSISTANT_EFFORT", "medium"),
        )
    return PROFILES[name]


_NOTEBOOK_PROMPT = """\
You are the assistant inside a SingleStore notebook app. The notebook has \
SQL cells (run against SingleStore; each result becomes the pandas DataFrame \
`df`, or a named variable if the cell sets one) and Python cells (an IPython \
kernel with pandas as pd, matplotlib (inline charts), and `conn`, a \
singlestoredb connection). Your reply is shown in the notebook's chat panel as \
plain text; every ```sql block gets an "Insert SQL cell" button and every \
```python block an "Insert Python cell" button. So:
- Answer with ready-to-run cells: SQL in ```sql blocks (no %%sql line), \
Python in ```python blocks that use `df` / named results, pandas and \
matplotlib. Keep each block to one cell's worth of code.
- Keep the prose short: a few sentences or "- " bullets. The panel only \
renders plain text, **bold**, `inline code` and fenced code blocks, so never \
use Markdown tables or headings.
- Write SingleStore SQL. Database and table names are case-sensitive.
- You can inspect the database with the singlestore_ro tools: list_databases, \
list_tables, describe_table and read_query (read-only statements only). Check \
names before using them and test SQL when that is cheap; tables can be very \
large. You can't run Python yourself; write it carefully.
- You cannot change data or schema yourself; give such statements as SQL \
cells for the user to review and run."""


def _system_prompt(kind: str = "sql_editor") -> str:
    base = _NOTEBOOK_PROMPT if kind == "notebook" else _SYSTEM_PROMPT
    # The SingleStore skill (key learnings) rides along with every question.
    try:
        skill = SKILL_FILE.read_text(encoding="utf-8").split("---", 2)[-1].strip()
    except OSError:
        return base
    return f"{base}\n\n{skill}"


def status() -> dict[str, Any]:
    claude = find_claude()
    if not claude:
        return {
            "available": False,
            "reason": "Claude Code (the `claude` command) isn't installed or isn't on PATH for the MCP server.",
        }
    return {"available": True, "claude": claude, "profiles": list(PROFILES), "default_profile": DEFAULT_PROFILE}


# ------------------------------------------------------------------ workers
# One long-running Claude Code process per editor ("worker"), fed questions
# as stream-json lines on stdin. Only the first question pays Claude Code's
# startup (and the read-only MCP server's); the editor warms its worker when
# the chat tab opens. Idle workers close after _IDLE_SECONDS; a profile
# change or Stop restarts the worker, resuming the same Claude session.

_IDLE_SECONDS = 600
_MAX_WORKERS = 4


class _Job:
    def __init__(self, editor_id: str, profile: str | None, kind: str = "sql_editor"):
        self.editor_id = editor_id
        self.profile = profile
        self.kind = kind
        self.started = time.time()
        self.activity = "Thinking…"
        self.queries = 0
        self.cancelled = False
        self.worker: _Worker | None = None


class _Worker:
    def __init__(self, claude: str, editor_id: str, profile: str | None, session: str | None, kind: str = "sql_editor"):
        workdir = _workdir()
        self.editor_id = editor_id
        self.profile = profile
        self.session = session or str(uuid.uuid4())
        args = [
            claude, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--tools", "",
            "--strict-mcp-config", "--mcp-config", str(_mcp_config(workdir)),
            "--allowedTools", f"mcp__{READONLY_SERVER}",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--append-system-prompt", _system_prompt(kind),
        ]
        args += ["--resume", session] if session else ["--session-id", self.session]
        model, effort = _profile(profile)
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        self.proc = subprocess.Popen(
            args, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=_connections.apply_env(dict(os.environ)),  # its read-only server uses the active connection
        )
        self.last_used = time.time()
        self.job: _Job | None = None
        self.result: dict[str, Any] | None = None
        self.done = threading.Event()
        self.stderr: deque[str] = deque(maxlen=40)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(self.proc.stderr or []), daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, prompt: str) -> None:
        assert self.proc.stdin is not None
        line = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "assistant" and self.job:
                _note_activity(self.job, event)
            elif kind == "result":
                self.result = event
                self.done.set()
        self.done.set()  # process ended: wake anyone waiting

    def close(self, kill: bool = False) -> None:
        # Closing stdin ends Claude Code (and its read-only MCP server) cleanly.
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        if kill:
            self.proc.kill()
            return

        def reap() -> None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        threading.Thread(target=reap, daemon=True).start()


_jobs: dict[str, _Job] = {}
_workers: dict[str, _Worker] = {}
_sessions: dict[str, str] = {}  # editor id -> Claude session id (survives worker restarts)
_lock = threading.Lock()
_slots = threading.Semaphore(_MAX_CONCURRENT)
_reaper_started = False


def _worker_for(claude: str, editor_id: str, profile: str | None, kind: str = "sql_editor") -> _Worker:
    """The editor's running worker for this profile, starting one if needed. Call with _lock held."""
    worker = _workers.pop(editor_id, None)
    if worker and worker.alive() and worker.profile == profile:
        _workers[editor_id] = worker
        return worker
    if worker:
        worker.close()
    idle = sorted((w for w in _workers.values() if w.job is None), key=lambda w: w.last_used)
    while len(_workers) >= _MAX_WORKERS and idle:
        oldest = idle.pop(0)
        oldest.close()
        _workers.pop(oldest.editor_id, None)
    worker = _Worker(claude, editor_id, profile, _sessions.get(editor_id), kind)
    _workers[editor_id] = worker
    _start_reaper()
    return worker


def _start_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def reap() -> None:
        while True:
            time.sleep(30)
            with _lock:
                for eid, w in list(_workers.items()):
                    if not w.alive() or (w.job is None and time.time() - w.last_used > _IDLE_SECONDS):
                        w.close()
                        _workers.pop(eid, None)

    threading.Thread(target=reap, daemon=True).start()


def _on_connection_change() -> None:
    """New active connection: idle assistant processes restart on the next question."""
    with _lock:
        for eid, w in list(_workers.items()):
            if w.job is None:
                w.close()
                _workers.pop(eid, None)


_connections.on_change(_on_connection_change)


@atexit.register
def _close_all() -> None:
    for w in list(_workers.values()):
        try:
            w.close(kill=True)
        except OSError:
            pass


def warm(editor_id: str, profile: str | None = None, kind: str = "sql_editor") -> bool:
    """Start the editor's worker ahead of its first question (no model call)."""
    claude = find_claude()
    if not claude:
        return False
    with _lock:
        if editor_id not in _jobs:
            _worker_for(claude, editor_id, profile, kind).last_used = time.time()
    return True


def job_state(editor_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(editor_id)
        if not job:
            return None
        return {
            "state": "running",
            "activity": job.activity,
            "queries": job.queries,
            "seconds": round(time.time() - job.started),
        }


def cancel(editor_id: str) -> bool:
    with _lock:
        job = _jobs.get(editor_id)
        if not job:
            return False
        job.cancelled = True
        worker = _workers.pop(editor_id, None)
    if worker:
        worker.close(kill=True)
    return True


def build_prompt(question: str, database: str | None, sql: str | None, selected: bool, last_result: str | None) -> str:
    parts = [f"Current database: {database or '(default)'}", f"Question: {question}"]
    if sql:
        label = "Selected SQL in the editor" if selected else "SQL in the editor"
        parts.append(f"{label}:\n```sql\n{sql[:8000]}\n```")
    if last_result:
        parts.append(f"Last run in the editor: {last_result[:600]}")
    return "\n\n".join(parts)


def ask(
    editor_id: str,
    prompt: str,
    deliver: Callable[[str, str, str], Any],
    profile: str | None = None,
    kind: str = "sql_editor",
) -> None:
    """Start answering ``prompt`` for one editor (or notebook) in the background.

    ``deliver(editor_id, text, kind)`` posts the answer ("claude") or a
    problem ("error"/"note") to the editor's inbox.
    """
    claude = find_claude()
    if not claude:
        raise LookupError(status()["reason"])
    with _lock:
        if editor_id in _jobs:
            raise ValueError("Claude is still answering the previous question in this editor.")
        _jobs[editor_id] = _Job(editor_id, profile, kind)
    threading.Thread(target=_run, args=(claude, editor_id, prompt, deliver), daemon=True).start()


def _run(claude: str, editor_id: str, prompt: str, deliver: Callable[[str, str, str], Any]) -> None:
    job = _jobs[editor_id]
    try:
        with _slots:
            if job.cancelled:
                text, kind = "Stopped.", "note"
            else:
                text, ok = _call_claude(claude, job, prompt)
                kind = "note" if job.cancelled else "claude" if ok else "error"
    except Exception as exc:  # noqa: BLE001 - always report back to the editor
        text, kind = f"The in-app assistant failed: {exc}", "error"
    # End the job before delivering, so the editor never sees the answer
    # while the job still looks like it's running.
    with _lock:
        _jobs.pop(editor_id, None)
        if job.worker:
            job.worker.job = None
            job.worker.last_used = time.time()
    deliver(editor_id, text, kind)
    if job.cancelled:
        # Stop ended the process; have a fresh one ready for the next question.
        warm(editor_id, job.profile, job.kind)


def _attach(claude: str, job: _Job) -> _Worker:
    with _lock:
        worker = _worker_for(claude, job.editor_id, job.profile, job.kind)
        job.worker = worker
        worker.job = job
        worker.result = None
        worker.done.clear()
        return worker


def _call_claude(claude: str, job: _Job, prompt: str) -> tuple[str, bool]:
    timeout = float(os.environ.get("SINGLESTORE_MCP_ASSISTANT_TIMEOUT", "300"))
    worker = _attach(claude, job)
    try:
        worker.send(prompt)
    except OSError:
        # The worker died while idle: start a fresh one and send again.
        with _lock:
            if _workers.get(job.editor_id) is worker:
                _workers.pop(job.editor_id)
        worker = _attach(claude, job)
        worker.send(prompt)
    finished = worker.done.wait(timeout)

    if job.cancelled:
        return "Stopped.", False
    result = worker.result
    if not finished or result is None:
        with _lock:
            if _workers.get(job.editor_id) is worker:
                _workers.pop(job.editor_id)
        worker.close(kill=True)
        if not finished:
            return f"No answer within {int(timeout)} seconds, so the assistant was stopped.", False
        detail = "".join(worker.stderr).strip()[-500:] or f"exit code {worker.proc.poll()}"
        return f"Claude Code stopped without an answer ({detail}).", False
    text = str(result.get("result") or "").strip()
    if result.get("is_error"):
        if "authenticate" in text.lower() or "login" in text.lower():
            text += "\n\nLog in once in a terminal: run `claude`, then `/login`."
        return text or "Claude Code reported an error.", False
    with _lock:
        _sessions[job.editor_id] = result.get("session_id") or worker.session
    return text or "(empty answer)", True


def _note_activity(job: _Job, event: dict[str, Any]) -> None:
    for block in event.get("message", {}).get("content", []) or []:
        if block.get("type") != "tool_use":
            continue
        name = str(block.get("name", "")).rsplit("__", 1)[-1]
        with _lock:
            if name == "read_query":
                job.queries += 1
                job.activity = f"Running query {job.queries}…"
            elif name in ("describe_table", "list_tables", "list_databases"):
                job.activity = "Looking at the schema…"


# ------------------------------------------------------------------ read-only MCP server


def serve_readonly() -> None:
    """Stdio MCP server with only schema lookups and read-only queries."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    from .apps._core import rows_as_text, surface_errors
    from .apps.query_grid import ReadOnlyViolation, run_query
    from .db import db

    server = MCPServer(READONLY_SERVER)
    read_only = ToolAnnotations(readOnlyHint=True)

    def tool(fn):
        server.tool(annotations=read_only)(surface_errors(fn))
        return fn

    @tool
    def list_databases() -> str:
        """List the databases on the SingleStore cluster."""
        rows = db.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")[1]
        return "\n".join(r["SCHEMA_NAME"] for r in rows)

    @tool
    def list_tables(database: str) -> str:
        """List tables and views in a database (names are case-sensitive)."""
        rows = db.execute(
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
            (database,),
        )[1]
        if not rows:
            return f"No tables in {database!r} (or the database doesn't exist; names are case-sensitive)."
        return "\n".join(f"{r['TABLE_NAME']}{' (view)' if r['TABLE_TYPE'] != 'BASE TABLE' else ''}" for r in rows)

    @tool
    def describe_table(table: str, database: str) -> str:
        """Columns, types and keys of one table."""
        rows = db.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (database, table),
        )[1]
        if not rows:
            raise LookupError(f"Table {database}.{table} not found (names are case-sensitive).")
        return "\n".join(
            f"{r['COLUMN_NAME']} {r['COLUMN_TYPE']}{' NOT NULL' if r['IS_NULLABLE'] == 'NO' else ''}"
            f"{' [' + r['COLUMN_KEY'] + ']' if r['COLUMN_KEY'] else ''}"
            for r in rows
        )

    @tool
    def read_query(sql: str, database: str | None = None, max_rows: int = 50) -> str:
        """Run one read-only statement (SELECT, WITH, SHOW, DESCRIBE, EXPLAIN) and return up to max_rows rows (max 200)."""
        try:
            data = run_query(sql, database, max(1, min(int(max_rows), 200)))
        except ReadOnlyViolation as exc:
            reason = str(exc).split(": ", 1)[-1].split(". Use run_sql", 1)[0]
            raise ValueError(
                f"Only read-only statements can run here ({reason}). Give the statement to the user in a ```sql block instead."
            ) from exc
        if not data["has_result_set"]:
            return f"Ran in {data['elapsed_ms']} ms; no result set."
        head = f"{data['row_count']} row(s) in {data['elapsed_ms']} ms"
        if data["truncated"]:
            head += f" (truncated at {data['max_rows']})"
        return "\n".join([head, *data["warnings"], rows_as_text(data["columns"], data["rows"], limit=data["max_rows"])])

    server.run(transport="stdio")


if __name__ == "__main__":
    if "--serve-readonly" in sys.argv:
        serve_readonly()
    else:
        print("Usage: python -m singlestore_mcp.assistant --serve-readonly", file=sys.stderr)
        sys.exit(2)
