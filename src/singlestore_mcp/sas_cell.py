"""Notebook SAS cells in the Workspace's SAS session.

A SAS cell's code (DATA steps, PROCs, macro code) runs in the same SAS Compute session – and queue, pre-warm, Stop
and "Restart SAS session" – as the SQL Editor's SAS runs and the Python in SAS cells (``sas_python``) of the SAS
Viya connection. So WORK tables and macro variables are shared between them, and SAS cells need no local kernel
(they work in server mode too). The automatic SingleStore library (``S2`` by default, the active SingleStore
connection's database) is the session's; saspy is only used where a Python cell calls ``sas_session()`` itself.

``wrap`` puts the cell between a quiet preamble (session reset, an ODS HTML5 file in WORK with inline images) and a
guard that ends an open quote / step, then closes the ODS file; ``parse`` keeps the log from the cell's own first
line on (the preamble runs with NOSOURCE) and counts its ERROR / WARNING lines; the ODS output is read back
through the Compute file API (``sas_python._read_html``).
"""

from __future__ import annotations

import html
import re
from typing import Any

from . import sas_python, sas_rest, sas_sql

HTML_FILE = "_s2sas.html"
MIME = sas_python.MIME
MAX_LOG_CHARS = 400_000
GUARD = ";*';*\";*/;quit;run;"
_HTML_NOTE = re.compile(r"^S2SAS_HTML=(.+)$")
_ODS_NOTE = re.compile(r"^NOTE: Writing HTML5\(_S2SAS\) Body file: ", re.I)
_BEGIN = "S2SAS_BEGIN"


class CellRefused(ValueError):
    """The cell can't run (empty)."""


def prepare(source: str) -> str:
    text = str(source or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [x.rstrip() for x in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise CellRefused("The SAS cell is empty.")
    return "\n".join(lines)


def wrap(code: str) -> str:
    """The SAS code that runs one (prepared) cell."""
    return "\n".join([
        "options nosource nonotes;",
        sas_sql.SESSION_RESET,
        f"%put S2SAS_HTML=%sysfunc(pathname(work))/{HTML_FILE};",  # (a %put NOTE: line isn't written with NONOTES)
        f'ods html5 (id=_s2sas) file="%sysfunc(pathname(work))/{HTML_FILE}" '
        'options(svg_mode="inline" bitmap_mode="inline");',
        f"%put {_BEGIN};",
        "options source notes;",
        code,
        f"{GUARD}options nosource nonotes;",
        "ods html5 (id=_s2sas) close;",
        "options source notes;",
    ])


def parse(log: list[dict[str, str]]) -> dict[str, Any]:
    """The cell's part of the log ({line, type} items): from the marker on, without the guard / closing lines and
    the ODS file note. ``errors`` / ``warnings`` (lines), ``first_error``, ``html_path``."""
    html_path = None
    start = 0
    path_at = None
    for i, x in enumerate(log):
        line = str(x.get("line") or "")
        m = _HTML_NOTE.match(line.strip())
        if m:
            html_path, path_at = m.group(1).strip(), i
        if line.strip() == _BEGIN:
            start = i + 1
            if path_at is not None and html_path:
                # SAS wraps a long WORK path at the line size: the pieces up to the marker belong to it
                # (a page break's title lines can come in between)
                html_path += "".join(str(y.get("line") or "").strip() for y in log[path_at + 1: i]
                                     if str(y.get("type") or "normal").lower() == "normal")
            path_at = None
    kept: list[dict[str, str]] = []
    for x in log[start:]:
        line = str(x.get("line") or "")
        if GUARD in line or _ODS_NOTE.match(line) or str(x.get("type") or "").lower() in ("title", "footnote", "page"):
            continue
        if line.strip() in ("options nosource nonotes;", "ods html5 (id=_s2sas) close;"):
            continue
        kept.append({"line": line, "type": str(x.get("type") or "normal").lower()})
    while kept and not kept[-1]["line"].strip():
        kept.pop()
    errors = [x["line"] for x in kept if x["type"] == "error" or x["line"].startswith("ERROR")]
    warnings = [x["line"] for x in kept if x["type"] == "warning" or x["line"].startswith("WARNING")]
    errors = [x for x in errors if x.strip()]
    return {"log_lines": kept, "errors": errors, "warnings": [x for x in warnings if x.strip()],
            "first_error": sas_sql.mask_secrets(errors[0]) if errors else None, "html_path": html_path}


def library_note(compute: sas_rest.Compute, database: str | None = None) -> str | None:
    """What the automatic SingleStore library points at (a line under the cell's output)."""
    auto = compute.auto_lib or {}
    lib = auto.get("libref") or "S2"
    if not auto.get("assigned"):
        note = auto.get("note") or auto.get("error")
        return f"No {lib} library: {note}" if note else None
    where = f"{auto.get('connection') or 'SingleStore'}{' / ' + auto['database'] if auto.get('database') else ''}"
    text = f"{lib} = SingleStore {where} (the Workspace's automatic library, shared with the SQL Editor)"
    if database and auto.get("database") and str(database).lower() != str(auto["database"]).lower():
        text += f"; this notebook's database {database} isn't {lib}'s"
    return text


def run(compute: sas_rest.Compute, source: str, ctl: sas_rest.RunControl | None = None,
        database: str | None = None) -> dict[str, Any]:
    """Run one SAS cell in ``compute`` (holding its lock): errors / warnings, the ODS output (``html``) or the
    listing text, the log (masked), ``state``, ``stopped``, ``ok``."""
    code = prepare(source)
    with compute.lock:
        res = compute.run(wrap(code), listing=True, ctl=ctl)
        parsed = parse(res["log"])
        stopped = res["state"] == "canceled" or bool(ctl is not None and ctl.cancel.is_set())
        page = None
        if parsed["html_path"] and not stopped:
            try:
                page = sas_python._read_html(compute, parsed["html_path"])
            except (sas_rest.SasError, OSError):
                page = None
        note = library_note(compute, database)
    listing = None if page else (sas_python.listing_text(res.get("listing") or "") or None)
    failed = not stopped and bool(parsed["errors"] or res["state"] in ("error", "failed"))
    log_text = sas_rest.sas_code_log(parsed["log_lines"])
    return {"errors": [sas_sql.mask_secrets(x) for x in parsed["errors"]],
            "warnings": [sas_sql.mask_secrets(x) for x in parsed["warnings"]], "first_error": parsed["first_error"],
            "html": page, "listing": listing, "state": res["state"], "stopped": stopped, "ok": not failed and not stopped,
            "log": log_text[-MAX_LOG_CHARS:], "library": note, "ms": res.get("ms")}


def outputs(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The cell's outputs in nbformat 4: the notebook's SAS output (counts, first error, ODS output, the log), as
    the kernel's SAS cells wrote it (notebooks saved before show the same way)."""
    listing = result.get("html") or (f"<pre>{html.escape(result['listing'])}</pre>" if result.get("listing") else "")
    errs = result.get("errors") or []
    warns = result.get("warnings") or []
    first = result.get("first_error")
    if not first and not result.get("ok") and not result.get("stopped") and result.get("state") in ("error", "failed"):
        first = f"The SAS job ended in state {result.get('state')}."
    outs: list[dict[str, Any]] = [{"output_type": "display_data", "metadata": {}, "data": {MIME: {
        "log": result.get("log") or "", "listing": listing, "errors": len(errs) or (1 if first and not errs else 0),
        "warnings": len(warns), "first_error": first, "library": result.get("library")},
        "text/plain": f"SAS: {len(errs)} error(s), {len(warns)} warning(s)"}}]
    if result.get("stopped"):
        outs.append({"output_type": "stream", "name": "stderr", "text": "Stopped: the SAS job was cancelled.\n"})
    return outs
