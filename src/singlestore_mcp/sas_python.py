"""Python in SAS: notebook cells that run as ``PROC PYTHON`` in the Workspace's SAS Compute session.

A cell's code goes into ``proc python; submit; … endsubmit; run;`` and runs in the same Compute session (and
queue, pre-warm, Stop and "Restart SAS session") as the SQL Editor's SAS runs of that SAS Viya connection. SAS
keeps the Python interpreter between PROC PYTHON steps of one session ("Resuming Python state from previous
PROC PYTHON invocation"), so variables persist from cell to cell and WORK tables are the session's. The first
step of a session starts Python ("NOTE: Python initialized.", ~8 s on Zeus). Nothing runs on this computer, so
these cells also work where local notebook kernels are off (server mode): each user's code runs in their own
SAS session, which Viya isolates.

What comes back is the SAS log; ``parse`` turns it into what a notebook shows: the Python output (the ``>>>``
echo, the start-up banner, SAS's NOTE / source lines and page titles removed, lines SAS wrapped at the line size
joined again), a Python traceback (SAS's own ``<stdin>`` frames dropped, ``<string>`` frames named ``<cell>``
with the cell's line), and SAS's own ERROR / WARNING lines (from ``SAS.submit`` code). ODS output (``SAS.show``,
``SAS.pyplot``, PROC output from ``SAS.submit``) is written to an ODS HTML5 file in WORK with inline images and
read back through the Compute file API.

The ``SAS`` object PROC PYTHON provides (checked on Zeus 2026-10-30 with ``dir(SAS)`` / ``inspect.signature``):
``sd2df(dataset)`` (alias ``sasdata2dataframe``), ``df2sd(df, dataset)`` (alias ``dataframe2sasdata``),
``symget(name)``, ``symput(name, val)``, ``submit(code)``, ``sasfnc(name, *args)``, ``show(obj, title=…,
count=…)``, ``pyplot(plot, filename=…, filetype="svg")``, ``renderImage(filename)``, ``logMessage(message,
messageType="NOTE")``, ``hideLOG(tf)`` / ``printLOG()`` / ``clearLOG()``, ``showMLA(results)``, ``workpath``.
Macro triggers (``&``, ``%``) inside SUBMIT are not resolved (checked); the word ``endsubmit`` would end the
block early, so a cell that contains it is refused.
"""

from __future__ import annotations

import html
import re
import textwrap
import threading
from typing import Any

from . import sas_rest, sas_sql

LINESIZE = 256                 # options ls=max: SAS wraps longer log lines at this width
MAX_LINE = 32_000              # SAS code lines are at most 32,767 characters
MAX_HTML_BYTES = 8_000_000     # ODS output read back (inline images included)
MAX_LOG_CHARS = 200_000
MIME = "application/vnd.s2.sas+json"   # the notebook's SAS output (log, ODS) – shared with SAS cells
HTML_FILE = "_s2py.html"
_HTML_NOTE = re.compile(r"^NOTE: S2PY_HTML=(.+)$")
_ENDSUBMIT = re.compile(r"endsubmit", re.I)
_BANNER = re.compile(r'^(Python \d+\.\d+\S* \(.*\) \[.*\] on \S+|Type "help", "copyright", "credits" or "license" '
                     r"for more information\.)$")
_FRAME = re.compile(r'^(\s*)File "<(string|stdin)>", line (\d+)(, in (.+))?$')
_EXC = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")
_WARN_AT = re.compile(r"^<string>:(\d+):")
_LISTING_TITLE = re.compile(r"^\s*The SAS System\s{2,}.*$")

_started: set[tuple[str, str]] = set()   # (connection, session id) where Python has run already
_started_lock = threading.Lock()


class CellRefused(ValueError):
    """The cell can't run as PROC PYTHON (empty, contains endsubmit, a line too long)."""


# ------------------------------------------------------------------ code


def prepare(source: str) -> tuple[str, int]:
    """The cell's code as it goes into SUBMIT: newlines normalised, common indentation removed, leading blank lines
    and trailing whitespace dropped. Returns (code, offset): line N of the code is line N + offset of the cell.
    Raises CellRefused for an empty cell, the word ``endsubmit`` (it would end the SUBMIT block early) or a line
    longer than SAS accepts."""
    text = str(source or "").replace("\r\n", "\n").replace("\r", "\n")
    if _mixed_indent(text):
        text = text.expandtabs(8)
    lines = [x.rstrip() for x in text.split("\n")]
    offset = 0
    while lines and not lines[0].strip():
        lines.pop(0)
        offset += 1
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise CellRefused("The Python in SAS cell is empty.")
    code = textwrap.dedent("\n".join(lines))
    for i, line in enumerate(code.split("\n"), 1):
        if _ENDSUBMIT.search(line):
            raise CellRefused(
                f"Line {i + offset} contains \"endsubmit\": SAS ends PROC PYTHON's SUBMIT block at that word, even in "
                "a string or a comment, so the rest of the cell would run as SAS code. Split the word, e.g. "
                "'end' + 'submit', or move that text into a SAS cell.")
        if len(line) > MAX_LINE:
            raise CellRefused(f"Line {i + offset} is {len(line):,} characters long; SAS takes at most {MAX_LINE:,} "
                              "per line. Break it up (or read the data from a file or table instead).")
    return code, offset


def _mixed_indent(text: str) -> bool:
    """True when some lines indent with tabs and others with spaces (dedent can't line those up)."""
    tabs = spaces = False
    for line in text.splitlines():
        lead = line[: len(line) - len(line.lstrip())]
        tabs |= "\t" in lead
        spaces |= " " in lead
    return tabs and spaces


def wrap(code: str) -> str:
    """The SAS code that runs the (prepared) cell: session reset, a wide log (SAS wraps at the line size), an ODS
    HTML5 file in WORK for SAS.show / SAS.pyplot / PROC output, the PROC PYTHON step, then the options put back."""
    return "\n".join([
        sas_sql.SESSION_RESET,
        "%let _s2pyls=%sysfunc(getoption(ls)); %let _s2pyps=%sysfunc(getoption(ps));",
        "options ls=max ps=max;",
        f"%put NOTE: S2PY_HTML=%sysfunc(pathname(work))/{HTML_FILE};",
        f'ods html5 (id=_s2py) file="%sysfunc(pathname(work))/{HTML_FILE}" '
        'options(svg_mode="inline" bitmap_mode="inline");',
        "proc python;",
        "submit;",
        code,
        "endsubmit;",
        "run;",
        "ods html5 (id=_s2py) close;",
        "options ls=&_s2pyls ps=&_s2pyps;",
    ])


# ------------------------------------------------------------------ log


def _rejoin(lines: list[str]) -> list[str]:
    """Join the lines SAS wrapped at the line size back into one."""
    out: list[str] = []
    carry = ""
    for line in lines:
        if len(line) == LINESIZE:
            carry += line
            continue
        out.append(carry + line)
        carry = ""
    if carry:
        out.append(carry)
    return out


def _trim(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines


def clean_traceback(lines: list[str], code: str, offset: int = 0) -> list[str]:
    """A traceback as the user wants it: SAS's ``<stdin>`` frames (its own wrapper) out, ``<string>`` frames named
    ``<cell>`` with the cell's line number and source line."""
    src = code.split("\n")
    out: list[str] = []
    for line in lines:
        m = _FRAME.match(line)
        if not m:
            out.append(line)
            continue
        if m.group(2) == "stdin":
            continue
        n = int(m.group(3))
        out.append(f'{m.group(1)}File "<cell>", line {n + offset}{m.group(4) or ""}')
        if m.group(5) and 1 <= n <= len(src) and src[n - 1].strip():
            out.append(f"{m.group(1)}  {src[n - 1].strip()}")
    return out


def parse(log: list[dict[str, str]], code: str = "", offset: int = 0) -> dict[str, Any]:
    """The cell's result from the SAS log lines ({line, type}): ``stdout`` (Python's output), ``error`` ({ename,
    evalue, traceback} for an unhandled Python exception), ``sas_errors`` / ``warnings`` (SAS's own lines, e.g.
    from SAS.submit code), ``python`` ("initialized" / "resumed" / None) and ``html_path`` (the ODS file)."""
    out: list[str] = []
    sas_errors: list[str] = []
    warnings: list[str] = []
    py_error = False
    python = None
    html_path = None
    for x in log:
        t, line = str(x.get("type") or "normal").lower(), str(x.get("line") or "")
        if t in ("source", "title", "footnote", "byline", "page"):
            continue
        if t == "note":
            if "NOTE: Python initialized" in line:
                python = "initialized"
            elif "Resuming Python state" in line:
                python = python or "resumed"
            m = _HTML_NOTE.match(line.strip())
            if m:
                html_path = m.group(1).strip()
            continue
        if t == "error":
            if line.startswith("ERROR: Unhandled Python exception"):
                py_error = True
            elif line.strip():
                sas_errors.append(line)
            continue
        if t == "warning":
            if line.strip():
                warnings.append(line)
            continue
        if line.rstrip() == ">>>" or _BANNER.match(line.strip()):
            continue
        out.append(line)
    out = [_WARN_AT.sub(lambda w: f"<cell>:{int(w.group(1)) + offset}:", x) for x in _rejoin(out)]
    error = None
    if py_error:
        start = max((i for i, x in enumerate(out) if x.strip() == "Traceback (most recent call last):"), default=None)
        tb = out[start:] if start is not None else []
        out = out[:start] if start is not None else out
        tb = clean_traceback(_trim(tb), code, offset)
        last = next((x.strip() for x in reversed(tb) if x.strip()), "")
        m = _EXC.match(last)
        ename, evalue = (m.group(1), m.group(2) or "") if m and tb else ("PythonError", "Unhandled Python exception")
        error = {"ename": ename, "evalue": evalue, "traceback": tb}
    stdout = "\n".join(_trim(out))
    return {"stdout": sas_sql.mask_secrets(stdout + "\n") if stdout else "", "error": error,
            "sas_errors": [sas_sql.mask_secrets(x) for x in sas_errors],
            "warnings": [sas_sql.mask_secrets(x) for x in warnings], "python": python, "html_path": html_path}


def has_output(page: str) -> bool:
    """Whether an ODS HTML5 file has any output in its body (it's written even when nothing was shown)."""
    body = page.split("<body", 1)[-1] if "<body" in page else ""
    return bool(re.search(r"<(table|svg|img|pre|figure)\b", body, re.I))


def listing_text(listing: str) -> str:
    """The listing without SAS's page header lines and blank edges."""
    lines = [x for x in str(listing or "").split("\n") if not _LISTING_TITLE.match(x)]
    return "\n".join(_trim(lines))


# ------------------------------------------------------------------ running


def python_started(compute: sas_rest.Compute) -> bool:
    """Has Python run in this Compute session already (so the next cell doesn't wait ~8 s for it)?"""
    sid = compute.sid
    with _started_lock:
        return bool(sid) and (compute.conn or "", sid) in _started


def _read_html(compute: sas_rest.Compute, path: str) -> str | None:
    from .viya_files import server_segment

    sid = compute.sid
    if not sid:
        return None
    status, raw, _ = sas_rest.request("GET", f"{sas_rest._base(compute.conn)}/compute/sessions/{sid}/files/"
                                      f"{server_segment(path)}/content", accept="*/*", raw=True, conn=compute.conn,
                                      timeout=60)
    if status != 200 or not isinstance(raw, bytes | bytearray) or len(raw) > MAX_HTML_BYTES:
        return None
    page = bytes(raw).decode("utf-8", "replace")
    return page if has_output(page) else None


def run(compute: sas_rest.Compute, source: str, ctl: sas_rest.RunControl | None = None) -> dict[str, Any]:
    """Run one cell in ``compute`` (holding its lock): the parsed result plus ``html`` (ODS output), ``listing``
    (text, when there's no ODS HTML), ``log`` (masked), ``state`` and ``stopped``."""
    code, offset = prepare(source)
    with compute.lock:
        res = compute.run(wrap(code), listing=True, ctl=ctl)
        parsed = parse(res["log"], code, offset)
        stopped = res["state"] == "canceled" or bool(ctl is not None and ctl.cancel.is_set())
        page = None
        if parsed["html_path"] and not stopped:
            try:
                page = _read_html(compute, parsed["html_path"])
            except (sas_rest.SasError, OSError):
                page = None
        if compute.sid and parsed["python"]:
            with _started_lock:
                _started.add((compute.conn or "", compute.sid))
    listing = None if page else (listing_text(res.get("listing") or "") or None)
    failed = not stopped and bool(parsed["error"] or parsed["sas_errors"] or res["state"] in ("error", "failed"))
    log_text = sas_rest.sas_code_log(res["log"])
    return {**parsed, "html": page, "listing": listing, "state": res["state"], "stopped": stopped, "ok": not failed
            and not stopped, "log": log_text[-MAX_LOG_CHARS:], "ms": res.get("ms")}


def outputs(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The cell's outputs in nbformat 4 (saved in the .ipynb): stdout as a stream, SAS's part (ODS output, SAS
    errors / warnings, the log) as the notebook's SAS output, a Python exception as an error."""
    outs: list[dict[str, Any]] = []
    if result.get("stdout"):
        outs.append({"output_type": "stream", "name": "stdout", "text": result["stdout"]})
    listing = result.get("html") or (f"<pre>{html.escape(result['listing'])}</pre>" if result.get("listing") else None)
    errs = result.get("sas_errors") or []
    outs.append({"output_type": "display_data", "metadata": {}, "data": {MIME: {
        "python": True, "errors": len(errs), "warnings": len(result.get("warnings") or []),
        "first_error": errs[0] if errs else None, "warning_lines": (result.get("warnings") or [])[:20],
        "listing": listing, "log": result.get("log") or ""},
        "text/plain": "SAS log (Python in SAS)"}})
    if result.get("stopped"):
        outs.append({"output_type": "stream", "name": "stderr", "text": "Stopped: the SAS job was cancelled.\n"})
    err = result.get("error")
    if err:
        outs.append({"output_type": "error", "ename": err["ename"], "evalue": err["evalue"],
                     "traceback": err["traceback"]})
    return outs
