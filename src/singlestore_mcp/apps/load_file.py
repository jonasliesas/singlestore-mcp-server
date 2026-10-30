"""Load a file from this computer into a SingleStore table ("Load file into table…" in the Schema Explorer and the
SQL Editor's tree; the wizard is ``vendor/s2-load-file.js``).

The MCP server runs on the user's PC, so it reads the file itself (no browser upload). Steps:

1. ``load_file_browse`` lists data files (.csv / .tsv / .txt / …) for the large Open dialog.
2. ``load_file_preview`` reads the start of the file: encoding (UTF-8 or Windows-1252 / Latin-1, common for SAS and
   Excel exports), delimiter, quote, header, null markers, decimal comma, date / time formats; the first rows; column
   types suggested from a sample (conservative: VARCHARs widened, BIGINT / DECIMAL / DOUBLE, DATE / DATETIME(6)); a
   CREATE TABLE (columnstore: SORT KEY, plus a SHARD KEY) the user can edit; or, for an existing table, the column
   mapping by name.
3. ``load_file_start`` (after the user confirmed: file size, rows, target, where it runs) creates the table when it's
   new, checks every row on this computer (bad rows: line and reason, nothing written yet), then loads: ``LOAD DATA
   LOCAL INFILE`` streamed from the parsed rows (re-written as UTF-8 tab-separated text, so the cluster gets one
   format whatever the file's) or, when the cluster or driver refuses local infile, batched multi-row INSERTs in one
   transaction. Both are all-or-nothing; Cancel aborts the statement / rolls back. A load cut off by a network break
   is never sent again: its result says it may or may not have run, with the row count read afterwards.
   ``load_file_status`` / ``load_file_cancel`` follow and stop it.
4. ``load_file_pipeline`` writes a CREATE PIPELINE template (S3 / Azure blob, credential placeholders only) for
   files above the size limit (SINGLESTORE_MCP_LOAD_LIMIT_MB, default 100): the server route instead of the PC.

Server mode (``server_mode``): the user's PC disk isn't reachable from the server, so every tool but
``load_file_info`` refuses with ``UNAVAILABLE``.
"""

from __future__ import annotations

import array
import codecs
import csv
import datetime as dt
import io
import itertools
import os
import re
import socket
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import singlestoredb as s2
from mcp.types import CallToolResult, ToolAnnotations

from .. import netguard, server_mode
from .. import sql_editor_state as editor_state
from ..db import _is_connection_error, db, open_connection, quote_identifier
from ._core import APP_ONLY, apps, tool_result
from .sql_editor import URI, _check_allowed, _hidden, _known_folders, file_roots, sql_dir

READ_ONLY = ToolAnnotations(readOnlyHint=True)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

SUFFIXES = (".csv", ".tsv", ".txt", ".tab", ".psv", ".dat")
PREVIEW_ROWS = 200
INFER_ROWS = 5000
SAMPLE_BYTES = 4 * 1024 * 1024
MAX_BAD_LISTED = 200
CHUNK_BYTES = 64 * 1024
INSERT_BATCH_ROWS = 1000
INSERT_BATCH_BYTES = 1_000_000
_MAX_LISTED = 500

UNAVAILABLE = ("Loading a file from this computer isn't available on the server: Studio runs on a shared server and "
               "can't read your PC's disk. Uploading a file is a later step; for now use a pipeline (S3 / Azure blob).")

ENCODINGS = {"utf-8": "UTF-8", "utf-8-sig": "UTF-8 with BOM", "cp1252": "Windows-1252", "latin-1": "ISO-8859-1 (Latin-1)",
             "iso-8859-15": "ISO-8859-15 (Latin-9)", "utf-16": "UTF-16"}
DELIMITERS = {",": "Comma", ";": "Semicolon", "\t": "Tab", "|": "Pipe"}
NULL_CANDIDATES = (".", "NULL", "null", "NA", "N/A", "\\N", "nan", "NaN")
# MySQL client / server codes for "local infile is off" (client flag, server setting, client restriction).
_INFILE_OFF = {1148, 3948, 2068, 1235}

csv.field_size_limit(64 * 1024 * 1024)


def limit_mb() -> float:
    try:
        return max(1.0, float(os.environ.get("SINGLESTORE_MCP_LOAD_LIMIT_MB", "100")))
    except ValueError:
        return 100.0


def _require_local() -> None:
    if server_mode.enabled():
        raise ValueError(UNAVAILABLE)


def _q(name: str) -> str:
    return quote_identifier(name)


def _lit(s: str) -> str:
    """A SQL string literal (backslash and quote escaped; tab / newline / CR as their escapes)."""
    s = s.replace("\\", "\\\\").replace("'", "''").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    return f"'{s}'"


# ------------------------------------------------------------------------------------------------ detection

def detect_encoding(raw: bytes) -> tuple[str, str]:
    """(encoding, why) for the start of a file."""
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", "UTF-8 with a byte order mark"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", "UTF-16 byte order mark"
    try:
        raw.decode("ascii")
        return "utf-8", "plain ASCII in the sample (read as UTF-8)"
    except UnicodeDecodeError:
        pass
    try:
        # Not final: a sample that ends inside a character is still UTF-8.
        codecs.getincrementaldecoder("utf-8")().decode(raw, final=False)
        return "utf-8", "valid UTF-8"
    except UnicodeDecodeError:
        pass
    if any(b in raw for b in (b"\x81", b"\x8d", b"\x8f", b"\x90", b"\x9d")):
        return "latin-1", "not UTF-8, with bytes Windows-1252 doesn't define: ISO-8859-1"
    return "cp1252", "not UTF-8: Windows-1252 (usual for SAS and Excel exports on Windows)"


def _reader(f: Any, delimiter: str, quote: str | None) -> Any:
    if quote:
        return csv.reader(f, delimiter=delimiter, quotechar=quote, doublequote=True, strict=False)
    return csv.reader(f, delimiter=delimiter, quoting=csv.QUOTE_NONE, strict=False)


def detect_delimiter(text: str, quote: str | None = '"') -> str:
    """The candidate that splits the sample's records into the same number (> 1) of fields most consistently."""
    best, best_score = ",", (-1.0, 0)
    for d in DELIMITERS:
        counts = [len(r) for r in itertools.islice(_reader(io.StringIO(text), d, quote), 200) if r]
        if len(counts) > 2:
            counts = counts[:-1]  # the sample's last record may be cut off
        if not counts:
            continue
        width, n = Counter(counts).most_common(1)[0]
        if width < 2:
            continue
        score = (round(n / len(counts), 2), width)
        if score > best_score:
            best, best_score = d, score
    return best


def detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text[:100_000] else "\n"


# ------------------------------------------------------------------------------------------------ value formats

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_TIME = r"(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2})(?:[.,](?P<f>\d{1,9}))?)?"
# name, regex, MySQL STR_TO_DATE format (for the pipeline template), datetime separators
_DATES = [
    ("YYYY-MM-DD", r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})", "%Y-%m-%d", (" ", "T")),
    ("YYYY/MM/DD", r"(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})", "%Y/%m/%d", (" ",)),
    ("DD.MM.YYYY", r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})", "%d.%m.%Y", (" ",)),
    ("DD/MM/YYYY", r"(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})", "%d/%m/%Y", (" ",)),
    ("MM/DD/YYYY", r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})", "%m/%d/%Y", (" ",)),
    ("DD-MM-YYYY", r"(?P<d>\d{1,2})-(?P<m>\d{1,2})-(?P<y>\d{4})", "%d-%m-%Y", (" ",)),
    ("DDMONYYYY", r"(?P<d>\d{1,2})(?P<b>[A-Za-z]{3})(?P<y>\d{4})", "%d%b%Y", (":", " ")),
    ("DD-MON-YYYY", r"(?P<d>\d{1,2})-(?P<b>[A-Za-z]{3})-(?P<y>\d{4})", "%d-%b-%Y", (" ",)),
]


class Fmt:
    def __init__(self, name: str, kind: str, regex: str, mysql: str) -> None:
        self.name, self.kind, self.mysql = name, kind, mysql
        self.rx = re.compile(rf"^{regex}$")

    def parse(self, v: str) -> str | None:
        """The value as ISO text (YYYY-MM-DD / YYYY-MM-DD HH:MM:SS[.ffffff] / HH:MM:SS), or None."""
        m = self.rx.match(v)
        if not m:
            return None
        g = m.groupdict()
        try:
            date = None
            if "y" in g:
                month = _MONTHS.get(g["b"].lower()) if g.get("b") else int(g["m"])
                if not month:
                    return None
                date = dt.date(int(g["y"]), month, int(g["d"])).isoformat()
            clock = None
            if g.get("H") is not None:
                h, mi, s = int(g["H"]), int(g["M"]), int(g.get("S") or 0)
                if h > 23 or mi > 59 or s > 59:
                    return None
                clock = f"{h:02d}:{mi:02d}:{s:02d}"
                if g.get("f"):
                    clock += "." + g["f"][:6].ljust(6, "0")
        except ValueError:
            return None
        return " ".join(x for x in (date, clock) if x)


def _build_formats() -> list[Fmt]:
    out = [Fmt(n, "date", rx, my) for n, rx, my, _ in _DATES]
    for n, rx, my, seps in _DATES:
        for sep in seps:
            out.append(Fmt(f"{n}{sep}HH:MM:SS", "datetime", rx + re.escape(sep) + _TIME, f"{my}{sep}%H:%i:%s"))
    out.append(Fmt("HH:MM:SS", "time", _TIME, "%H:%i:%s"))
    return out


FORMATS = _build_formats()
FORMATS_BY_NAME = {f.name: f for f in FORMATS}

_INT = re.compile(r"^[+-]?(0|[1-9]\d*)$")
_INT_ANY = re.compile(r"^[+-]?\d+$")
_DEC = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)$")
_FLOAT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)[eE][+-]?\d+$")
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_STEPS = (32, 64, 128, 255, 512, 1024, 2048, 4096, 8192, 16384)


def widen(max_len: int) -> str:
    """A VARCHAR with room: at least twice the longest value seen, rounded up; LONGTEXT for very long text."""
    want = max(32, 2 * max_len)
    for n in _STEPS:
        if want <= n:
            return f"VARCHAR({n})"
    return "LONGTEXT"


def _num_text(v: str, decimal_comma: bool) -> str:
    v = v.strip()
    if decimal_comma:
        v = v.replace(" ", "").replace("\u00a0", "").replace(".", "").replace(",", ".") if "," in v else v
    return v


def infer_column(values: list[str], nulls: set[str], decimal_comma: bool = False) -> dict[str, Any]:
    """Kind, suggested SingleStore type and (for dates / times) the format of one column's sample values."""
    present = [v for v in values if not _is_null(v, nulls, typed=True)]
    max_len = max((len(v) for v in values if not _is_null(v, nulls, typed=False)), default=0)
    out: dict[str, Any] = {"nulls": len(values) - len(present), "max_len": max_len, "format": None, "format_choices": []}
    if not present:
        return {**out, "kind": "text", "sql_type": "VARCHAR(255)", "note": "no values in the sample"}
    stripped = [v.strip() for v in present]
    if all(_INT.match(v) for v in stripped):
        digits = max(len(v.lstrip("+-")) for v in stripped)
        if digits <= 18:
            return {**out, "kind": "int", "sql_type": "BIGINT"}
        if digits <= 60:
            return {**out, "kind": "num", "sql_type": f"DECIMAL({min(65, digits + 2)},0)"}
    nums = [_num_text(v, decimal_comma) for v in stripped]
    if all(_DEC.match(v) or _INT.match(v) for v in nums) and any(_DEC.match(v) for v in nums):
        ints = max(len(v.lstrip("+-").split(".")[0]) for v in nums)
        scale = max(len(v.split(".")[1]) if "." in v else 0 for v in nums)
        if scale <= 12 and ints + scale <= 55:
            return {**out, "kind": "num", "sql_type": f"DECIMAL({min(65, ints + scale + 6)},{scale})"}
        return {**out, "kind": "num", "sql_type": "DOUBLE"}
    if all(_NUM.match(v) for v in nums) and any(_FLOAT.match(v) for v in nums):
        return {**out, "kind": "num", "sql_type": "DOUBLE"}
    matches = [f for f in FORMATS if all(f.parse(v) is not None for v in stripped)]
    if matches:
        best = matches[0]
        choices = [f.name for f in matches if f.kind == best.kind]
        if best.kind == "date":
            sql = "DATE"
        elif best.kind == "datetime":
            sql = "DATETIME(6)"
        else:
            sql = "TIME(6)" if any("." in (best.parse(v) or "") for v in stripped) else "TIME"
        return {**out, "kind": best.kind, "sql_type": sql, "format": best.name, "format_choices": choices}
    return {**out, "kind": "text", "sql_type": widen(max_len)}


def _is_null(v: str, nulls: set[str], typed: bool) -> bool:
    """A null marker; for a typed (not text) column an empty or blank value is always NULL."""
    if v in nulls:
        return True
    s = v.strip()
    return (typed and not s) or (bool(s) and s in nulls)


def clean_names(header: list[str]) -> list[str]:
    """Column names from a header row: word characters only (letters of any language), unique, at most 64 long."""
    out: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(header):
        name = re.sub(r"\W+", "_", raw.strip()).strip("_")[:60] or f"col{i + 1}"
        base, n = name, 2
        while name.lower() in seen:
            name, n = f"{base}_{n}", n + 1
        seen.add(name.lower())
        out.append(name)
    return out


def table_name_from(path: str) -> str:
    stem = Path(path).stem
    name = re.sub(r"\W+", "_", stem).strip("_").lower()[:60]
    return name or "loaded_file"


_KEY_NAME = re.compile(r"^(id|key|nr|no|.*_id|.*id|.*_key|.*_nr|.*_no)$", re.I)


def suggest_keys(columns: list[dict[str, Any]], sample: list[list[str]], nulls: set[str]) -> tuple[str | None, str | None]:
    """(shard key column, sort key column): an id-like column unique and present in every sampled row shards evenly;
    the first date / time column is a good sort key (time filters skip segments). None: keyless / empty."""
    shard = None
    for i, c in enumerate(columns):
        if c["kind"] not in ("int", "text") or not _KEY_NAME.match(c["name"]) or c.get("max_len", 0) > 128:
            continue
        vals = [r[i] for r in sample if i < len(r)]
        if vals and not any(_is_null(v, nulls, typed=True) for v in vals) and len(set(vals)) == len(vals):
            shard = c["name"]
            break
    sort = next((c["name"] for c in columns if c["kind"] in ("date", "datetime")), None)
    return shard, sort


def create_table_sql(database: str, table: str, columns: list[dict[str, Any]], shard: str | None, sort: str | None) -> str:
    lines = [f"  {_q(c['name'])} {c['sql_type']}" for c in columns]
    lines.append(f"  SORT KEY ({_q(sort)})" if sort else "  SORT KEY ()")
    lines.append(f"  SHARD KEY ({_q(shard)})" if shard else "  SHARD KEY ()")
    return f"CREATE TABLE {_q(database)}.{_q(table)} (\n" + ",\n".join(lines) + "\n);"


# ------------------------------------------------------------------------------------------------ options

class Options:
    """How to read the file (detected, or as the user set it in the wizard)."""

    def __init__(self, encoding: str, delimiter: str, quote: str | None, header: bool, nulls: list[str],
                 decimal_comma: bool, newline: str = "\n") -> None:
        if encoding not in ENCODINGS:
            raise ValueError(f"encoding must be one of {', '.join(ENCODINGS)}")
        if not delimiter or len(delimiter) != 1:
            raise ValueError("The delimiter is one character.")
        if quote is not None and len(quote) != 1:
            raise ValueError("The quote is one character (or none).")
        self.encoding, self.delimiter, self.quote, self.header = encoding, delimiter, quote, header
        self.nulls = list(dict.fromkeys(nulls))
        self.null_set = set(self.nulls)
        self.decimal_comma, self.newline = decimal_comma, newline

    def as_dict(self) -> dict[str, Any]:
        return {"encoding": self.encoding, "delimiter": self.delimiter, "quote": self.quote, "header": self.header,
                "null_markers": self.nulls, "decimal_comma": self.decimal_comma, "newline": self.newline}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Options:
        quote = d.get("quote", '"')
        return cls(str(d.get("encoding") or "utf-8"), str(d.get("delimiter") or ","), quote or None, bool(d.get("header", True)),
                   [str(x) for x in (d.get("null_markers") if d.get("null_markers") is not None else [""])],
                   bool(d.get("decimal_comma", False)), str(d.get("newline") or "\n"))


def _data_path(path: str) -> Path:
    if not path or not str(path).strip():
        raise ValueError("Pick a file.")
    p = _check_allowed(Path(path).expanduser())
    if not p.is_file():
        raise LookupError(f"{p} isn't a file (or doesn't exist any more).")
    return p


def _records(text: str, opts: Options, whole: bool) -> list[list[str]]:
    rows = [r for r in _reader(io.StringIO(text, newline=""), opts.delimiter, opts.quote) if r]
    if not whole and len(rows) > 1:
        rows = rows[:-1]
    return rows


def _looks_like_header(rows: list[list[str]]) -> bool:
    if len(rows) < 2:
        return bool(rows) and all(re.match(r"^[^\d\W]", v.strip()) for v in rows[0])
    first, rest = rows[0], rows[1:INFER_ROWS]
    if any(not v.strip() for v in first) or len({v.strip().lower() for v in first}) != len(first):
        return False
    if any(_NUM.match(v.strip()) for v in first):
        return False
    for i in range(len(first)):
        vals = [r[i] for r in rest if i < len(r)]
        info = infer_column(vals, {""})
        if info["kind"] != "text":
            return True  # a typed column under a text first row
    return all(re.match(r"^[^\d\W][\w .()/%#-]*$", v.strip()) for v in first)


def preview(path: str, *, encoding: str | None = None, delimiter: str | None = None, quote: str | None = '"',
            header: bool | None = None, null_markers: list[str] | None = None, decimal_comma: bool | None = None,
            formats: dict[str, str] | None = None, database: str | None = None, table: str | None = None,
            connection: bool = True) -> dict[str, Any]:
    """What the wizard shows for a file: detected options, first rows, suggested columns / CREATE TABLE, target."""
    p = _data_path(path)
    st = p.stat()
    with p.open("rb") as f:
        head = f.read(SAMPLE_BYTES + 1)
    detected_enc, why = detect_encoding(head[:SAMPLE_BYTES])
    enc = encoding or detected_enc
    if enc not in ENCODINGS:
        raise ValueError(f"encoding must be one of {', '.join(ENCODINGS)}")
    whole = len(head) <= SAMPLE_BYTES
    text = head[:SAMPLE_BYTES].decode(enc, errors="replace")
    if enc == "utf-8-sig" or text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    replaced = text.count("\ufffd")
    q = quote if quote != "" else None
    delim = delimiter or detect_delimiter(text, q)
    newline = detect_newline(text)
    probe = Options(enc, delim, q, True, [""], False, newline)
    rows = _records(text, probe, whole)
    hdr = _looks_like_header(rows) if header is None else bool(header)
    width = Counter(len(r) for r in rows[:INFER_ROWS]).most_common(1)[0][0] if rows else 0
    names = clean_names(rows[0]) if hdr and rows else [f"col{i + 1}" for i in range(width)]
    raw_header = rows[0] if hdr and rows else None
    data = rows[1:] if hdr else rows
    if len(names) < width:
        names += [f"col{i + 1}" for i in range(len(names), width)]
    # Null markers: "" plus the usual ones (SAS writes "." for a missing number) when the sample has them.
    seen_markers = [m for m in NULL_CANDIDATES if any(m in r for r in data[:INFER_ROWS])]
    nulls = list(null_markers) if null_markers is not None else ["", *seen_markers]
    # Decimal comma: a semicolon file with numbers like 12,5 (Excel in Norway, Germany, …).
    if decimal_comma is None:
        decimal_comma = delim != "," and any(re.match(r"^-?\d+,\d+$", v.strip()) for r in data[:INFER_ROWS] for v in r)
    opts = Options(enc, delim, q, hdr, nulls, bool(decimal_comma), newline)
    sample = data[:INFER_ROWS]
    columns = []
    for i, name in enumerate(names):
        vals = [r[i] for r in sample if i < len(r)]
        info = infer_column(vals, opts.null_set, opts.decimal_comma)
        chosen = (formats or {}).get(name)
        if chosen and chosen in FORMATS_BY_NAME and info["kind"] in ("date", "datetime", "time"):
            info["format"] = chosen
        columns.append({"name": name, "source": raw_header[i] if raw_header and i < len(raw_header) else name, "index": i, **info})
    shard, sort = suggest_keys(columns, sample, opts.null_set)
    bad_width = sum(1 for r in sample if len(r) != width)
    sample_bytes = len(text.encode("utf-8", errors="replace"))
    est_rows = len(data) if whole else int(st.st_size / max(1.0, sample_bytes / max(1, len(rows))))
    if not whole and hdr:
        est_rows = max(0, est_rows - 1)
    out: dict[str, Any] = {
        "path": str(p), "name": p.name, "size": st.st_size, "modified": round(st.st_mtime),
        "options": opts.as_dict(), "detected": {"encoding": detected_enc, "encoding_note": why, "delimiter": delim,
                                                 "header": hdr, "null_markers": seen_markers},
        "encodings": ENCODINGS, "delimiters": DELIMITERS, "replaced_chars": replaced,
        "columns": columns, "header_row": raw_header,
        "rows": [r for r in data[:PREVIEW_ROWS]], "sample_rows": len(sample), "whole_file_in_sample": whole,
        "estimated_rows": est_rows, "bad_width_in_sample": bad_width, "width": width,
        "limit_mb": limit_mb(), "over_limit": st.st_size > limit_mb() * 1024 * 1024,
        "shard_key": shard, "sort_key": sort, "formats": [{"name": f.name, "kind": f.kind} for f in FORMATS],
        "suggested_table": table_name_from(str(p)),
    }
    if database:
        tname = table or out["suggested_table"]
        out["create_sql"] = create_table_sql(database, tname, columns, shard, sort)
        if connection:
            out["target"] = target_info(database, tname, columns)
    return out


# ------------------------------------------------------------------------------------------------ target table

def _kind_of(data_type: str) -> str:
    t = (data_type or "").lower()
    if t in ("tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year"):
        return "int"
    if t in ("decimal", "numeric", "float", "double", "real"):
        return "num"
    if t == "date":
        return "date"
    if t in ("datetime", "timestamp"):
        return "datetime"
    if t == "time":
        return "time"
    return "text"


def _norm(name: str) -> str:
    return re.sub(r"[\W_]+", "", name).lower()


def map_columns(source: list[dict[str, Any]], target: list[dict[str, Any]],
                mapping: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Target column -> source column by name (exact, then any case, then letters and digits only), or as ``mapping``
    ({target: source name or None}) says. Unmatched source columns are skipped; unmatched target columns get their
    default (``required``: NOT NULL without a default, so every row would fail)."""
    by_name = {c["name"]: c for c in source}
    used: set[str] = set()
    pairs = []
    for t in target:
        src = None
        if mapping is not None and t["name"] in mapping:
            want = mapping[t["name"]]
            src = by_name.get(want) if want else None
        else:
            for test in (lambda s: s["name"] == t["name"], lambda s: s["name"].lower() == t["name"].lower(),
                         lambda s: _norm(s["name"]) == _norm(t["name"]) or _norm(s.get("source") or "") == _norm(t["name"])):
                src = next((s for s in source if s["name"] not in used and test(s)), None)
                if src:
                    break
        if src:
            used.add(src["name"])
        pairs.append({"target": t["name"], "target_type": t.get("type"), "kind": _kind_of(t.get("data_type", "")),
                      "source": src["name"] if src else None, "source_index": src["index"] if src else None,
                      "source_kind": src.get("kind") if src else None, "format": src.get("format") if src else None,
                      "required": src is None and not t.get("nullable", True) and t.get("default") is None and not t.get("auto")})
    skipped = [s["name"] for s in source if s["name"] not in used]
    return {"pairs": pairs, "skipped_source": skipped, "missing_target": [p["target"] for p in pairs if p["source"] is None],
            "required_missing": [p["target"] for p in pairs if p["required"]]}


def _target_columns(execute: Callable[..., Any], database: str, table: str) -> tuple[str | None, list[dict[str, Any]]]:
    rows = execute("SELECT TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                   (database, table))
    if not rows:
        return None, []
    cols = execute("SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA FROM information_schema.COLUMNS "
                   "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION", (database, table))
    return rows[0]["TABLE_TYPE"], [{"name": c["COLUMN_NAME"], "data_type": c["DATA_TYPE"], "type": c["COLUMN_TYPE"],
                                    "nullable": c["IS_NULLABLE"] == "YES", "default": c["COLUMN_DEFAULT"],
                                    "auto": "auto_increment" in (c.get("EXTRA") or "").lower()} for c in cols]


def target_info(database: str, table: str, source: list[dict[str, Any]],
                mapping: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Whether ``database.table`` exists and, if so, its columns and the mapping of the file's columns onto them."""
    kind, cols = _target_columns(lambda sql, args: db.execute(sql, args)[1], database, table)
    if kind is None:
        return {"database": database, "table": table, "exists": False}
    out = {"database": database, "table": table, "exists": True, "table_type": kind, "columns": cols}
    if kind != "BASE TABLE":
        out["error"] = f"{database}.{table} is a {kind.lower()}, not a table: pick another name."
        return out
    return {**out, "mapping": map_columns(source, cols, mapping)}


# ------------------------------------------------------------------------------------------------ CREATE TABLE check

_IDENT = r"(?:`(?:[^`]|``)+`|[\w$]+)"
_CREATE_RE = re.compile(rf"^CREATE\s+(?:(?:ROWSTORE|REFERENCE|COLUMNSTORE|TEMPORARY|GLOBAL)\s+)*TABLE\s+(?P<a>{_IDENT})(?:\s*\.\s*(?P<b>{_IDENT}))?\s*\(",
                        re.I)


def _unquote(ident: str) -> str:
    return ident[1:-1].replace("``", "`") if ident.startswith("`") else ident


def _strip_comments_and_check(sql: str) -> str:
    """The statement without leading comments or a trailing ';'; ValueError if it holds more than one statement."""
    out, i, n = [], 0, len(sql)
    quote = None
    while i < n:
        c = sql[i]
        if quote:
            out.append(c)
            if c == "\\" and quote != "`" and i + 1 < n:
                out.append(sql[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
            out.append(c)
        elif sql.startswith("--", i) or c == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        elif c == ";":
            if sql[i + 1:].strip() and _strip_comments_and_check(sql[i + 1:]).strip():
                raise ValueError("The CREATE TABLE script must be one statement.")
            break
        else:
            out.append(c)
        i += 1
    return "".join(out).strip()


def check_create_sql(sql: str, database: str, table: str) -> str:
    """The editable CREATE TABLE, checked: one CREATE TABLE statement for ``database.table`` (or ``table``)."""
    body = _strip_comments_and_check(sql or "")
    m = _CREATE_RE.match(body)
    if not m:
        raise ValueError("The script must be a CREATE TABLE statement.")
    if re.match(r"^CREATE\s+(?:\w+\s+)*TEMPORARY\b", body, re.I):
        raise ValueError("A temporary table would be gone after the load: remove TEMPORARY.")
    a, b = m.group("a"), m.group("b")
    db_name, tname = (_unquote(a), _unquote(b)) if b else (database, _unquote(a))
    if db_name != database or tname != table:
        raise ValueError(f"The CREATE TABLE names {db_name}.{tname}, but the target is {database}.{table}: "
                         "change the script or the target.")
    return body


# ------------------------------------------------------------------------------------------------ converting rows

class BadValue(ValueError):
    pass


def make_converter(kind: str, fmt: str | None, decimal_comma: bool) -> Callable[[str], str]:
    """A function from the file's text to the text the cluster gets for a column of ``kind`` (BadValue if it can't)."""
    if kind == "int":
        def conv(v: str) -> str:
            s = v.strip()
            if not _INT_ANY.match(s):
                raise BadValue(f"{_short(v)!r} isn't a whole number")
            return s
        return conv
    if kind == "num":
        def conv(v: str) -> str:
            s = _num_text(v, decimal_comma)
            if not _NUM.match(s):
                raise BadValue(f"{_short(v)!r} isn't a number")
            return s
        return conv
    if kind in ("date", "datetime", "time"):
        first = FORMATS_BY_NAME.get(fmt or "")
        tries = [f for f in ([first] if first else []) + [FORMATS_BY_NAME["YYYY-MM-DD"], FORMATS_BY_NAME["YYYY-MM-DD HH:MM:SS"],
                                                            FORMATS_BY_NAME["YYYY-MM-DDTHH:MM:SS"], FORMATS_BY_NAME["HH:MM:SS"]] if f]
        expect = fmt or {"date": "YYYY-MM-DD", "datetime": "YYYY-MM-DD HH:MM:SS", "time": "HH:MM:SS"}[kind]

        def conv(v: str) -> str:
            s = v.strip()
            for f in tries:
                iso = f.parse(s)
                if iso is None:
                    continue
                has_date, has_time = f.kind != "time", f.kind != "date"
                if kind == "date":
                    if has_time and not iso.endswith(" 00:00:00") and " 00:00:00." not in iso:
                        raise BadValue(f"{_short(v)!r} has a time of day, the column is a DATE")
                    return iso[:10]
                if kind == "time":
                    return iso.split(" ")[-1] if has_time else "00:00:00"
                return iso if has_time else (iso + " 00:00:00" if has_date else iso)
            raise BadValue(f"{_short(v)!r} isn't a {kind} in {expect}")
        return conv
    return lambda v: v


def _short(v: str, n: int = 40) -> str:
    return v if len(v) <= n else v[: n - 1] + "…"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r").replace("\x00", "\\0")


class Plan:
    """Which file column goes into which table column, and how each is converted."""

    def __init__(self, pairs: list[dict[str, Any]], opts: Options, width: int, formats: dict[str, str] | None = None) -> None:
        self.width = width
        self.opts = opts
        self.cols = [p for p in pairs if p.get("source_index") is not None]
        if not self.cols:
            raise ValueError("No column of the file matches a column of the table.")
        self.targets = [p["target"] for p in self.cols]
        self.idx = [int(p["source_index"]) for p in self.cols]
        self.typed = [p["kind"] != "text" for p in self.cols]
        self.convs = [make_converter(p["kind"], (formats or {}).get(p["source"]) or p.get("format"), opts.decimal_comma)
                      for p in self.cols]

    def row(self, fields: list[str]) -> list[str | None]:
        if len(fields) != self.width:
            raise BadValue(f"{len(fields)} field(s), expected {self.width}")
        out: list[str | None] = []
        nulls = self.opts.null_set
        for i, typed, conv, name in zip(self.idx, self.typed, self.convs, self.targets):
            v = fields[i]
            if _is_null(v, nulls, typed):
                out.append(None)
                continue
            try:
                out.append(conv(v))
            except BadValue as exc:
                raise BadValue(f"{name}: {exc}") from None
        return out

    @staticmethod
    def tsv(values: list[str | None]) -> str:
        return "\t".join("\\N" if v is None else _escape(v) for v in values) + "\n"


def iter_file(p: Path, opts: Options, progress: Callable[[int], None] | None = None) -> Iterator[tuple[int, list[str]]]:
    """(first line number of the record, fields) for every data record of the file; blank lines are skipped.
    ``progress(bytes read so far)`` is called now and then. UnicodeDecodeError says where the encoding is wrong."""
    raw = p.open("rb")
    try:
        text = io.TextIOWrapper(raw, encoding=opts.encoding, newline="", errors="strict")
        reader = _reader(text, opts.delimiter, opts.quote)
        line = 1
        first = True
        n = 0
        while True:
            try:
                fields = next(reader)
            except StopIteration:
                return
            except UnicodeDecodeError as exc:
                raise ValueError(f"Near line {reader.line_num + 1} the file can't be read as {ENCODINGS[opts.encoding]} "
                                 f"(byte 0x{exc.object[exc.start]:02X}): pick another encoding.") from None
            except csv.Error as exc:
                raise ValueError(f"Line {reader.line_num}: {exc}") from None
            start, line = line, reader.line_num + 1
            if first:
                first = False
                if fields and fields[0].startswith("\ufeff"):
                    fields[0] = fields[0][1:]
                if opts.header:
                    continue
            if not fields or fields == [""]:
                continue
            n += 1
            if progress and n % 2000 == 0:
                progress(raw.tell())
            yield start, fields
    finally:
        raw.close()


# ------------------------------------------------------------------------------------------------ pipeline template

def pipeline_sql(database: str, table: str, columns: list[dict[str, Any]], opts: Options, file_name: str,
                 create_sql: str | None = None) -> dict[str, str]:
    """CREATE PIPELINE templates (S3, Azure blob) that load the original file on the cluster's side. Credentials are
    placeholders: the user fills them in (never real ones here)."""
    col_list, sets = [], []
    for c in columns:
        name = _q(c["name"])
        fmt = FORMATS_BY_NAME.get(c.get("format") or "")
        if c.get("kind") in ("date", "datetime") and fmt and fmt.name not in ("YYYY-MM-DD", "YYYY-MM-DD HH:MM:SS"):
            var = "@" + re.sub(r"\W", "_", c["name"])
            col_list.append(var)
            sets.append(f"{name} = STR_TO_DATE(NULLIF({var}, ''), {_lit(fmt.mysql)})")
        elif c.get("kind") == "num" and opts.decimal_comma:
            var = "@" + re.sub(r"\W", "_", c["name"])
            col_list.append(var)
            sets.append(f"{name} = REPLACE(REPLACE(NULLIF({var}, ''), '.', ''), ',', '.')")
        else:
            col_list.append(name)
    marker = next((m for m in opts.nulls if m), None)
    fields = f"FIELDS TERMINATED BY {_lit(opts.delimiter)}" + (f" OPTIONALLY ENCLOSED BY {_lit(opts.quote)}" if opts.quote else "")
    body = [f"INTO TABLE {_q(table)}", fields, f"LINES TERMINATED BY {_lit(opts.newline)}"]
    if marker:
        body.append(f"NULL DEFINED BY {_lit(marker)}")
    if opts.header:
        body.append("IGNORE 1 LINES")
    body.append("(" + ", ".join(col_list) + ")")
    if sets:
        body.append("SET " + ",\n    ".join(sets))
    pname = _q("load_" + re.sub(r"\W+", "_", table).strip("_").lower()[:50])
    note = []
    if opts.encoding not in ("utf-8", "utf-8-sig"):
        note.append(f"-- The file is {ENCODINGS[opts.encoding]}: pipelines read UTF-8, so save it as UTF-8 before uploading.")
    head = [f"-- Load {file_name} on the cluster's side instead of through this computer:",
            "-- 1. Upload the file to the bucket / container below.  2. Fill in the <placeholders> (never commit real keys).",
            "-- 3. Run this, then TEST PIPELINE / START PIPELINE (Pipeline Monitor).", *note, ""]
    pre = [create_sql.rstrip().rstrip(";") + ";", ""] if create_sql else []
    use = [f"USE {_q(database)};", ""]
    tail = "\n".join(body) + ";"
    s3 = "\n".join([*head, *use, *pre, f"CREATE PIPELINE {pname} AS",
                    f"LOAD DATA S3 '<bucket>/<path>/{file_name}'",
                    "CONFIG '{\"region\": \"<region>\"}'",
                    "CREDENTIALS '{\"aws_access_key_id\": \"<access key id>\", \"aws_secret_access_key\": \"<secret access key>\"}'",
                    tail])
    azure = "\n".join([*head, *use, *pre, f"CREATE PIPELINE {pname} AS",
                       f"LOAD DATA AZURE '<container>/<path>/{file_name}'",
                       "CREDENTIALS '{\"account_name\": \"<storage account>\", \"account_key\": \"<account key>\"}'",
                       tail])
    return {"s3": s3, "azure": azure, "pipeline": pname.strip("`")}


# ------------------------------------------------------------------------------------------------ load jobs

class Cancelled(Exception):
    pass


class RolledBack(Exception):
    pass


class Job:
    def __init__(self, path: Path, database: str, table: str, mode: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.path, self.database, self.table, self.mode = path, database, table, mode
        self.phase = "starting"
        self.total_bytes = path.stat().st_size
        self.bytes_done = 0
        self.rows_total: int | None = None
        self.rows_read = 0
        self.rows_sent = 0
        self.rows_loaded: int | None = None
        self.bad: list[dict[str, Any]] = []
        self.bad_count = 0
        self.server_bad = 0
        self.method: str | None = None
        self.message: str | None = None
        self.error: str | None = None
        self.result: dict[str, Any] = {}
        self.cancel = threading.Event()
        self.started = time.monotonic()
        self.ended: float | None = None
        self.conn: Any = None
        self.connection: str | None = None
        self.created = False
        self.lock = threading.Lock()

    @property
    def done(self) -> bool:
        return self.ended is not None

    def add_bad(self, line: int, reason: str, text: str | None = None, where: str = "file") -> None:
        self.bad_count += 1
        if len(self.bad) < MAX_BAD_LISTED:
            self.bad.append({"line": line, "reason": reason, "text": _short(text or "", 200) if text else None, "where": where})

    def status(self) -> dict[str, Any]:
        elapsed = (self.ended or time.monotonic()) - self.started
        if self.phase == "checking":
            pct = 100 * self.bytes_done / max(1, self.total_bytes)
        elif self.phase == "loading" and self.rows_total:
            pct = 100 * self.rows_sent / self.rows_total
        else:
            pct = 100.0 if self.done and self.phase == "done" else None
        return {"job_id": self.id, "phase": self.phase, "done": self.done, "path": str(self.path), "name": self.path.name,
                "database": self.database, "table": self.table, "mode": self.mode, "method": self.method,
                "total_bytes": self.total_bytes, "bytes_done": self.bytes_done, "pct": None if pct is None else round(min(100.0, pct), 1),
                "rows_total": self.rows_total, "rows_read": self.rows_read, "rows_sent": self.rows_sent,
                "rows_loaded": self.rows_loaded, "bad_count": self.bad_count, "bad": self.bad, "server_bad": self.server_bad,
                "message": self.message, "error": self.error, "elapsed_s": round(elapsed, 1), "result": self.result,
                "connection": self.connection, "created_table": self.created}


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def _remember(job: Job) -> None:
    with _JOBS_LOCK:
        running = [j for j in _JOBS.values() if not j.done]
        if running:
            raise ValueError(f"Another file is being loaded ({running[0].path.name} into {running[0].table}): wait for it or cancel it.")
        _JOBS[job.id] = job
        for old in sorted((j for j in _JOBS.values() if j.done), key=lambda j: j.started)[:-10]:
            _JOBS.pop(old.id, None)


def get_job(job_id: str) -> Job:
    job = _JOBS.get(job_id)
    if job is None:
        raise LookupError("No such load (the server was restarted?).")
    return job


def _active_profile() -> Any:
    from .. import connections

    p = connections.active()
    if p is None:
        raise ValueError("No active SingleStore connection.")
    return p


def _open(database: str) -> Any:
    from .. import connections

    conn = open_connection(connections.connect_kwargs(_active_profile()), autocommit=True, local_infile=True)
    cur = conn.cursor()
    cur.execute(f"USE {_q(database)}")
    cur.close()
    return conn


def _fetch(conn: Any, sql: str, args: Any = None) -> list[dict[str, Any]]:
    cur = conn.cursor()
    try:
        cur.execute(sql, args or ())
        return list(cur.fetchall() or [])
    finally:
        cur.close()


def _count(conn: Any, database: str, table: str) -> int:
    rows = _fetch(conn, f"SELECT COUNT(*) AS n FROM {_q(database)}.{_q(table)}")
    return int(next(iter(rows[0].values())))


def _count_fresh(database: str, table: str) -> int | None:
    try:
        rows = db.execute(f"SELECT COUNT(*) AS n FROM {_q(database)}.{_q(table)}")[1]
        return int(rows[0]["n"])
    except Exception:  # noqa: BLE001 - the network may still be away
        return None


def _error_text(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 2 and isinstance(args[0], int):
        return f"SingleStore error {args[0]}: {args[1]}"
    return str(exc).replace("\n", " ") or type(exc).__name__


def _errno(exc: BaseException) -> int | None:
    code = getattr(exc, "errno", None)
    if isinstance(code, int):
        return code
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def _hard_close(conn: Any) -> None:
    """Cut the connection mid-statement (Cancel during LOAD DATA): the cluster never gets the end of the data, so the
    statement is aborted and nothing is committed."""
    raw = getattr(conn, "_conn", conn)
    sock = getattr(raw, "_sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _history(job: Job, sql: str, ok: bool, *, error: str | None = None, affected: int | None = None) -> None:
    try:
        editor_state.add_history(sql, job.database, ok=ok, kind="write", error=error, affected=affected,
                                 elapsed_ms=round(1000 * ((job.ended or time.monotonic()) - job.started)),
                                 source={"tab": "Load file", "file": str(job.path)}, connection=job.connection)
    except Exception:  # noqa: BLE001 - history is a convenience
        pass


def _load_statement(table: str, targets: list[str], skip: bool, handle: str | None) -> str:
    sql = ("LOAD DATA LOCAL INFILE ':stream:' " + ("SKIP ALL ERRORS " if skip else "") + f"INTO TABLE {_q(table)} CHARACTER SET utf8 "
           "FIELDS TERMINATED BY '\\t' ENCLOSED BY '' ESCAPED BY '\\\\' LINES TERMINATED BY '\\n' "
           "(" + ", ".join(_q(t) for t in targets) + ")")
    if skip and handle:
        sql += f" ERRORS HANDLE {_lit(handle)}"
    return sql


def run_job(job: Job, spec: dict[str, Any]) -> None:
    opts: Options = spec["options"]
    created_sql = spec.get("create_sql")
    try:
        job.connection = getattr(_active_profile(), "name", None)
        job.phase = "connecting"
        job.conn = _open(job.database)
        if job.cancel.is_set():
            raise Cancelled
        before = None
        if job.mode == "append":
            before = _count(job.conn, job.database, job.table)
        else:
            job.phase = "creating"
            cur = job.conn.cursor()
            try:
                cur.execute(created_sql)
            except Exception as exc:
                _history(job, created_sql, False, error=_error_text(exc))
                raise
            finally:
                cur.close()
            job.created = True
            _history(job, created_sql, True)
            before = 0
        kind, cols = _target_columns(lambda sql, args: _fetch(job.conn, sql, args), job.database, job.table)
        if kind != "BASE TABLE":
            raise ValueError(f"{job.database}.{job.table} isn't a table.")
        mapping = map_columns(spec["source_columns"], cols, spec.get("mapping"))
        if mapping["required_missing"]:
            raise ValueError("No file column for the required column(s) " + ", ".join(mapping["required_missing"])
                             + " (NOT NULL without a default): map one, or change the table.")
        plan = Plan(mapping["pairs"], opts, spec["width"], spec.get("formats"))
        job.result["mapping"] = mapping

        # Pass 1: check every row on this computer; nothing is sent yet.
        job.phase = "checking"

        def progress(n: int) -> None:
            job.bytes_done = n
            if job.cancel.is_set():
                raise Cancelled

        good = 0
        for line, fields in iter_file(job.path, opts, progress):
            job.rows_read += 1
            try:
                plan.row(fields)
                good += 1
            except BadValue as exc:
                job.add_bad(line, str(exc), opts.delimiter.join(fields))
        job.bytes_done = job.total_bytes
        job.rows_total = good
        if job.bad_count and not spec.get("skip_bad"):
            job.phase = "bad_rows"
            job.message = (f"{job.bad_count:,} of {job.rows_read:,} row(s) can't be loaded as they are. Nothing was written"
                           + (" (the new table was removed again)" if job.created else "") + ": skip them, or fix the file or the table.")
            _drop_if_ours(job)
            return
        if good == 0:
            raise ValueError("The file has no rows to load.")

        # Pass 2: load.
        job.phase = "loading"
        loaded, server_errors = _load(job, plan, opts, spec)
        job.phase = "counting"
        after = _count(job.conn, job.database, job.table)
        job.rows_loaded = loaded
        added = after - (before or 0)
        job.result.update({"count_before": before, "count_after": after, "rows_added": added, "check_ok": added == loaded,
                           "server_errors": server_errors, "skipped_rows": job.bad_count,
                           "sql": f"SELECT * FROM {_q(job.database)}.{_q(job.table)} LIMIT 100;"})
        job.phase = "done"
        job.message = (f"Loaded {loaded:,} row(s) into {job.database}.{job.table}"
                       + (f"; {job.bad_count:,} bad row(s) skipped" if job.bad_count else "")
                       + ("" if added == loaded else f". Check: the table grew by {added:,} row(s), not {loaded:,} (others writing to it?)"))
    except Cancelled:
        job.phase = "cancelled"
        _after_abort(job, "Cancelled")
    except Exception as exc:  # noqa: BLE001 - shown in the wizard
        if job.cancel.is_set():
            job.phase = "cancelled"
            _after_abort(job, "Cancelled")
        elif job.phase in ("loading", "counting") and _is_connection_error(exc):
            job.phase = "uncertain"
            netguard.failure("singlestore", exc)
            now = _count_fresh(job.database, job.table)
            job.result["count_after"] = now
            job.error = (f"The connection to SingleStore broke while loading ({_error_text(exc)}). The load may or may not have "
                         "finished, and it is not sent again by itself: "
                         + (f"the table now has {now:,} row(s)" if now is not None else "the table's row count can't be read yet")
                         + " – check it before loading again.")
            _history(job, job.result.get("history_sql") or f"LOAD DATA LOCAL INFILE ({job.path.name})", False, error=job.error)
        else:
            job.phase = "error"
            job.error = _error_text(exc) if isinstance(exc, s2.Error) else str(exc)
            if job.result.get("history_sql"):
                _history(job, job.result["history_sql"], False, error=job.error)
            _drop_if_ours(job)
    finally:
        if job.conn is not None:
            try:
                job.conn.close()
            except Exception:  # noqa: BLE001
                pass
            job.conn = None
        job.ended = time.monotonic()


def _drop_if_ours(job: Job) -> None:
    """Remove a table this load created when nothing was loaded into it (a failed or cancelled first load)."""
    if not job.created:
        return
    # The load's own connection, unless it was cut (Cancel) or broke: then a fresh one.
    for fresh in (False, True):
        conn = None
        try:
            conn = _open(job.database) if fresh else job.conn
            if conn is None:
                continue
            if _count(conn, job.database, job.table) == 0:
                _fetch(conn, f"DROP TABLE {_q(job.database)}.{_q(job.table)}")
                job.created = False
                job.result["table_removed"] = True
            else:
                job.result["table_removed"] = False
            return
        except Exception:  # noqa: BLE001 - try a fresh connection; else the table stays and the message says so
            continue
        finally:
            if fresh and conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    job.result["table_removed"] = False


def _after_abort(job: Job, what: str) -> None:
    job.rows_loaded = 0
    _drop_if_ours(job)
    now = _count_fresh(job.database, job.table) if not job.result.get("table_removed") else None
    job.result["count_after"] = now
    job.message = f"{what}: nothing was loaded" + (" (the new table was removed again)" if job.result.get("table_removed") else "") + "."
    if job.result.get("history_sql"):
        _history(job, job.result["history_sql"], False, error=what)


def _load(job: Job, plan: Plan, opts: Options, spec: dict[str, Any]) -> tuple[int, int]:
    """Send the rows; (rows loaded, rows the cluster refused and skipped)."""
    method = spec.get("method") or "auto"
    skip = bool(spec.get("skip_bad"))
    line_map = array.array("q")
    started = {"sent": False}

    def chunks() -> Iterator[bytes]:
        buf: list[str] = []
        size = 0
        for line, fields in iter_file(job.path, opts):
            if job.cancel.is_set():
                _hard_close(job.conn)
                raise Cancelled
            try:
                values = plan.row(fields)
            except BadValue:
                continue  # listed in pass 1
            s = Plan.tsv(values)
            buf.append(s)
            size += len(s)
            line_map.append(line)
            job.rows_sent += 1
            if size >= CHUNK_BYTES:
                started["sent"] = True
                yield "".join(buf).encode("utf-8")
                buf, size = [], 0
        if buf:
            started["sent"] = True
            yield "".join(buf).encode("utf-8")

    if method in ("auto", "local_infile"):
        handle = f"s2load_{job.id}" if skip else None
        sql = _load_statement(job.table, plan.targets, skip, handle)
        job.method = "local_infile"
        job.result["history_sql"] = f"{sql} /* Load file: {job.path.name} */"
        cur = job.conn.cursor()
        try:
            cur.execute(sql, infile_stream=chunks())
            loaded = int(cur.rowcount)
        except s2.Error as exc:
            if method == "auto" and not started["sent"] and _errno(exc) in _INFILE_OFF and not job.cancel.is_set():
                job.message = f"Local infile isn't allowed here ({_error_text(exc)}): loading with INSERTs instead."
                job.rows_sent = 0
                return _insert(job, plan, opts), 0
            raise
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass
        server_bad = 0
        if skip and handle:
            errs = _fetch(job.conn, "SELECT LOAD_DATA_LINE_NUMBER AS n, ERROR_MESSAGE AS m, LOAD_DATA_LINE AS t "
                                    "FROM information_schema.LOAD_DATA_ERRORS WHERE HANDLE = %s ORDER BY LOAD_DATA_LINE_NUMBER", (handle,))
            for e in errs:
                n = int(e.get("n") or 0)
                src = line_map[n - 1] if 0 < n <= len(line_map) else n
                t = e.get("t")
                job.add_bad(src, str(e.get("m") or "refused by SingleStore"),
                            t.decode("utf-8", "replace").rstrip("\n").replace("\t", opts.delimiter) if isinstance(t, (bytes, bytearray)) else t,
                            where="cluster")
            server_bad = len(errs)
            job.server_bad = server_bad
        _history(job, job.result["history_sql"], True, affected=loaded)
        return loaded, server_bad
    return _insert(job, plan, opts), 0


def _insert(job: Job, plan: Plan, opts: Options) -> int:
    """Batched multi-row INSERTs in one transaction (when local infile isn't allowed): Cancel or an error rolls back."""
    job.method = "insert"
    cols = ", ".join(_q(t) for t in plan.targets)
    one = "(" + ", ".join(["%s"] * len(plan.targets)) + ")"
    head = f"INSERT INTO {_q(job.table)} ({cols}) VALUES ".replace("%", "%%")
    job.result["history_sql"] = f"{head}… /* Load file: {job.path.name}, batched INSERTs in one transaction */"
    cur = job.conn.cursor()
    loaded = 0
    committed = False
    try:
        cur.execute("BEGIN")
        batch: list[Any] = []
        rows = 0
        size = 0

        def flush() -> None:
            nonlocal batch, rows, size, loaded
            if rows:
                cur.execute(head + ", ".join([one] * rows), batch)
                loaded += rows
            batch, rows, size = [], 0, 0

        for _line, fields in iter_file(job.path, opts):
            if job.cancel.is_set():
                raise Cancelled
            try:
                values = plan.row(fields)
            except BadValue:
                continue
            batch.extend(values)
            rows += 1
            size += sum(len(v) for v in values if v)
            job.rows_sent += 1
            if rows >= INSERT_BATCH_ROWS or size >= INSERT_BATCH_BYTES:
                flush()
        flush()
        if job.cancel.is_set():
            raise Cancelled
        cur.execute("COMMIT")
        committed = True
    except BaseException as exc:
        if not committed:
            if _is_connection_error(exc) and not isinstance(exc, Cancelled):
                # The transaction never committed before the break: the cluster rolls it back with the session.
                raise RolledBack(f"The connection broke before COMMIT ({_error_text(exc)}): the cluster rolls the "
                                 "transaction back, so nothing was loaded.") from exc
            try:
                cur.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
    _history(job, job.result["history_sql"], True, affected=loaded)
    return loaded


def start_job(spec: dict[str, Any]) -> Job:
    job = Job(spec["path"], spec["database"], spec["table"], spec["mode"])
    _remember(job)
    threading.Thread(target=run_job, args=(job, spec), name="load-file", daemon=True).start()
    return job


# ------------------------------------------------------------------------------------------------ tools

@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def load_file_info() -> CallToolResult:
    """Whether "Load file into table" works here (not in server mode: the PC's disk isn't reachable), the size above
    which the server route (a pipeline) is recommended, and the file types listed."""
    available = not server_mode.enabled()
    data = {"available": available, "reason": None if available else UNAVAILABLE, "limit_mb": limit_mb(),
            "suffixes": list(SUFFIXES), "encodings": ENCODINGS, "delimiters": DELIMITERS}
    return tool_result("Load file: available" if available else UNAVAILABLE, data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def load_file_browse(path: str | None = None) -> CallToolResult:
    """List a folder on this computer for the Load file dialog: subfolders and data files (.csv .tsv .txt .tab .psv
    .dat). Same folders as the SQL Editor's file browser (user folder, SQL folder, SINGLESTORE_MCP_FILE_ROOTS)."""
    _require_local()
    folder = _check_allowed(Path(path).expanduser() if path else sql_dir())
    if not folder.is_dir():
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
            elif entry.suffix.lower() in SUFFIXES:
                st = entry.stat()
                files.append({"name": entry.name, "path": str(entry), "size": st.st_size, "modified": round(st.st_mtime)})
        except OSError:
            continue
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: f["modified"], reverse=True)
    parent = folder.parent
    data = {"path": str(folder),
            "parent": str(parent) if parent != folder and any(parent.is_relative_to(r) for r in file_roots()) else None,
            "folders": folders[:_MAX_LISTED], "files": files[:_MAX_LISTED], "shortcuts": _known_folders(), "manage": False}
    return tool_result(f"{len(folders)} folder(s), {len(files)} data file(s) in {folder}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def load_file_preview(path: str, database: str | None = None, table: str | None = None, encoding: str | None = None,
                      delimiter: str | None = None, quote: str | None = '"', header: bool | None = None,
                      null_markers: list[str] | None = None, decimal_comma: bool | None = None,
                      formats: dict[str, str] | None = None) -> CallToolResult:
    """Read the start of a data file on this computer for the Load file wizard (nothing is written).

    Detects (or takes) the encoding, delimiter, quote ("" = none), header, null markers and decimal comma; returns the
    first rows, the columns with suggested SingleStore types and date / time formats (``formats``: {column: format
    name} overrides), the estimated row count, whether the file is over the size limit, and for ``database`` a CREATE
    TABLE for ``table`` (default: from the file name) plus ``target`` (does the table exist; its column mapping)."""
    _require_local()
    data = preview(path, encoding=encoding, delimiter=delimiter, quote=quote, header=header, null_markers=null_markers,
                   decimal_comma=decimal_comma, formats=formats, database=database, table=table)
    summary = (f"{data['name']}: {len(data['columns'])} column(s), about {data['estimated_rows']:,} row(s), "
               f"{ENCODINGS[data['options']['encoding']]}, delimiter {DELIMITERS.get(data['options']['delimiter'], repr(data['options']['delimiter']))}")
    return tool_result(summary, data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def load_file_target(database: str, table: str, columns: list[dict[str, Any]],
                     mapping: dict[str, str | None] | None = None) -> CallToolResult:
    """Does ``database.table`` exist, and how do the file's ``columns`` ([{name, index, kind, format, source}], from
    load_file_preview) map onto it by name (``mapping`` {table column: file column or null} overrides)."""
    _require_local()
    data = target_info(database, table, columns, mapping)
    return tool_result(f"{database}.{table} {'exists' if data['exists'] else 'is new'}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def load_file_pipeline(database: str, table: str, columns: list[dict[str, Any]], options: dict[str, Any], file_name: str,
                       create_sql: str | None = None) -> CallToolResult:
    """CREATE PIPELINE templates (S3 and Azure blob, credential placeholders) that load the file on the cluster's side
    instead of through this computer – for files above the size limit. Nothing is run."""
    _require_local()
    data = pipeline_sql(database, table, columns, Options.from_dict(options), file_name, create_sql)
    return tool_result(f"Pipeline templates for {database}.{table}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=WRITES)
def load_file_start(path: str, database: str, table: str, mode: str, options: dict[str, Any], columns: list[dict[str, Any]],
                    width: int, expected_size: int, confirmed: bool = False, confirmed_large: bool = False,
                    create_sql: str | None = None, mapping: dict[str, str | None] | None = None,
                    formats: dict[str, str] | None = None, skip_bad: bool = False, method: str = "auto") -> CallToolResult:
    """Load a file from this computer into a table – only after the user confirmed in the wizard (``confirmed``; above
    the size limit also ``confirmed_large``, the second confirmation).

    ``mode`` "new" runs ``create_sql`` (one CREATE TABLE for ``database.table``) first, "append" adds to the existing
    table. ``columns`` / ``width`` / ``options`` come from load_file_preview; ``expected_size`` is the file size the user
    saw (a file that changed since is refused). Every row is checked first; bad rows stop the load before anything is
    written unless ``skip_bad``. ``method``: "auto" (LOAD DATA LOCAL INFILE, else INSERTs), "local_infile" or "insert".
    Runs in the background: follow it with load_file_status, stop it with load_file_cancel."""
    _require_local()
    if not confirmed:
        raise ValueError("Loading a file needs the user's confirmation in the wizard.")
    if mode not in ("new", "append"):
        raise ValueError('mode must be "new" or "append"')
    if method not in ("auto", "local_infile", "insert"):
        raise ValueError('method must be "auto", "local_infile" or "insert"')
    p = _data_path(path)
    size = p.stat().st_size
    if int(expected_size) != size:
        raise ValueError(f"{p.name} changed since the preview ({size:,} bytes now, {int(expected_size):,} then): preview it again.")
    if size > limit_mb() * 1024 * 1024 and not confirmed_large:
        raise ValueError(f"{p.name} is over {limit_mb():g} MB: the server route (a pipeline) is recommended; loading it through "
                         "this computer needs a second confirmation.")
    if not database or not table:
        raise ValueError("Pick the target database and table.")
    checked = check_create_sql(create_sql or "", database, table) if mode == "new" else None
    if mode == "new":
        exists = db.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (database, table))[1]
        if exists:
            raise ValueError(f"{database}.{table} exists already: append to it, or pick another name.")
    spec = {"path": p, "database": database, "table": table, "mode": mode, "options": Options.from_dict(options),
            "source_columns": columns, "width": int(width), "create_sql": checked, "mapping": mapping, "formats": formats,
            "skip_bad": bool(skip_bad), "method": method}
    job = start_job(spec)
    return tool_result(f"Loading {p.name} into {database}.{table}", job.status())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def load_file_status(job_id: str, wait_ms: int = 0) -> CallToolResult:
    """Progress of a file load (phase, rows, bytes, %, bad rows, result). ``wait_ms``: wait up to this long (max 5 s)
    for it to finish before answering."""
    job = get_job(job_id)
    deadline = time.monotonic() + min(max(0, int(wait_ms)), 5000) / 1000
    while not job.done and time.monotonic() < deadline:
        time.sleep(0.1)
    data = job.status()
    return tool_result(f"{data['phase']}: {data['rows_sent'] or data['rows_read']:,} row(s)", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def load_file_cancel(job_id: str) -> CallToolResult:
    """Stop a file load: while checking nothing has been written; while loading the statement is aborted (LOAD DATA)
    or rolled back (INSERTs); a table the load created is removed again when it stayed empty."""
    job = get_job(job_id)
    # Only a flag: the load's own thread cuts its connection at the next row (sending from here could interleave packets).
    job.cancel.set()
    return tool_result("Cancelling…" if not job.done else "Already finished.", {"job_id": job_id, "done": job.done})
