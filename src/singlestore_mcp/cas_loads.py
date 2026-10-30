"""CAS: "Load into this session" – a caslib's source table loaded into the Workspace's own CAS session.

A FedSQL-in-CAS tab only sees tables loaded in CAS; a ``PROC CASUTIL load`` in plain SAS code runs in Compute's
own CAS session, not the Workspace's. So the Schema Explorer's CAS source and the SQL Editor's tree offer
``table.loadTable`` into the Workspace's CAS session of the SAS Viya connection – **session scope only**: never
``promote``, the output goes to the same caslib under the same name (session tables are only visible to this
session), the caslib itself isn't changed. "Unload" is ``table.dropTable`` of that session table; a global table
(promoted by someone else) is never dropped – CAS would drop the global one when the session has none.

What a load does depends on the caslib (checked on Zeus 2026-10-30 with CARS, ``table.tableDetails`` after the
load):

- **SpeedyStore caslibs (type SingleStore, SS badge)**: the table stays in SingleStore – ``TableLocation``
  SINGLESTORE, no blocks, 0 bytes of CAS memory; queries read SingleStore live. A ``where=`` on loadTable is
  ignored there (all 428 rows were visible), a column list (``vars``) is applied.
- **Standard SingleStore connector caslibs (singlestore_standard, S2 badge)** and every other caslib: the rows are
  copied into CAS memory (``TableLocation`` CAS, AllocatedMemory ≈ the data size); ``where=`` and ``vars`` apply.

Before a load the source's size comes from ``table.fileInfo`` (rows, bytes – SpeedyStore and path caslibs tell
it, the standard connector doesn't); a copy above ``SINGLESTORE_MCP_CAS_LOAD_CONFIRM_MB`` (default 100 MB) or of
unknown size needs a second confirmation. The loads of a session are remembered (per SAS Viya connection, with
the CAS session id): after the session restarted (Restart SAS session, Viya's idle timeout, a network break) the
views offer to load them again.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

CONFIRM_MB_DEFAULT = 100
_EXT = re.compile(r"\.(sashdat|sas7bdat|csv|parquet|xlsx?|txt)$", re.I)
_COLUMN = re.compile(r"^[^\x00-\x1f]{1,256}$")


def confirm_bytes() -> int:
    try:
        mb = float(os.environ.get("SINGLESTORE_MCP_CAS_LOAD_CONFIRM_MB") or CONFIRM_MB_DEFAULT)
    except ValueError:
        mb = CONFIRM_MB_DEFAULT
    return int(max(0.0, mb) * 1_000_000)


def expected(kind: str | None) -> str:
    """What loading a table of a caslib of this SingleStore kind does: ``live`` (SpeedyStore: stays in SingleStore)
    or ``copy`` (into CAS memory)."""
    return "live" if kind == "speedystore" else "copy"


def table_name(path: str) -> str:
    """The CAS table name for a source file / table (``cars.sashdat`` -> ``cars``)."""
    return _EXT.sub("", str(path or "").strip())


def parse_columns(columns: Any) -> list[str]:
    """A column list from the UI: a list or a comma / newline separated string; names as given (case kept)."""
    if columns is None:
        return []
    items = columns if isinstance(columns, list) else re.split(r"[,\n]", str(columns))
    out: list[str] = []
    for c in items:
        name = str(c).strip()
        if not name:
            continue
        if not _COLUMN.match(name):
            raise ValueError(f"Not a column name: {name!r}")
        if name.upper() not in (x.upper() for x in out):
            out.append(name)
    return out


def load_params(caslib: str, path: str, *, name: str | None = None, where: str | None = None,
                columns: list[str] | None = None, replace: bool = False) -> dict[str, Any]:
    """The ``table.loadTable`` parameters: the source ``path`` of ``caslib`` into a session-scope table of the same
    caslib (``promote`` False – never global)."""
    params: dict[str, Any] = {"caslib": caslib, "path": path,
                              "casOut": {"caslib": caslib, "name": name or table_name(path), "promote": False,
                                         "replace": bool(replace)}}
    w = str(where or "").strip()
    if w:
        if len(w) > 10_000:
            raise ValueError("The WHERE expression is too long.")
        params["where"] = w
    if columns:
        params["vars"] = [{"name": c} for c in columns]
    return params


def drop_params(caslib: str, name: str) -> dict[str, Any]:
    """``table.dropTable`` of a session table (``quiet``: no error when it's already gone)."""
    return {"caslib": caslib, "name": name, "quiet": True}


def file_info(rows: list[dict[str, Any]], path: str, database: str | None = None) -> dict[str, Any] | None:
    """The source table's entry from ``table.fileInfo`` rows: {name, rows, columns, bytes} (None where CAS doesn't
    say; for a database caslib the row of its own schema when it lists several)."""
    want = str(path or "").upper()
    hits = [r for r in rows if str(r.get("Name") or "").upper() == want]
    hits = hits or [r for r in rows if table_name(str(r.get("Name") or "")).upper() == table_name(want)]
    if database:
        own = [r for r in hits if not r.get("Schema") or str(r.get("Schema")).lower() == database.lower()]
        hits = own or hits
    if not hits:
        return None
    r = hits[0]

    def num(k: str) -> int | None:
        v = r.get(k)
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return {"name": r.get("Name"), "rows": num("Rows"), "columns": num("Columns"),
            "bytes": num("Size") if num("Size") is not None else num("UncompressedSize"), "schema": r.get("Schema")}


def needs_confirmation(mode: str, size: int | None) -> str | None:
    """Why a load needs a second yes (a copy into CAS memory above the limit, or of unknown size), else None."""
    if mode != "copy":
        return None
    limit = confirm_bytes()
    if size is None:
        return "CAS can't tell how big this table is; it is copied into CAS memory"
    if size > limit:
        return f"This copies about {fmt_bytes(size)} into CAS memory (more than {fmt_bytes(limit)})"
    return None


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1000
    return f"{n} bytes"


def details(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``table.tableDetails`` (node ALL) -> where the table is and the memory it holds in CAS."""
    r = next((x for x in rows if str(x.get("Node") or "").upper() == "ALL"), rows[0] if rows else {})
    loc = str(r.get("TableLocation") or "").upper() or None

    def num(k: str) -> int | None:
        try:
            return int(r[k]) if r.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            return None

    memory = num("AllocatedMemory")
    live = loc == "SINGLESTORE" or (loc not in (None, "CAS") and not memory)
    return {"location": loc, "memory": memory, "rows": num("Rows"), "live": bool(live)}


def describe(d: dict[str, Any]) -> str:
    if d.get("live"):
        return "Live – queries run in SingleStore (no copy in CAS memory)"
    mem = d.get("memory")
    return f"Copied into CAS memory{f' ({fmt_bytes(mem)})' if mem else ''}"


# ------------------------------------------------------------------ what this Workspace loaded


_lock = threading.Lock()
# connection -> {(CASLIB, TABLE): {caslib, table, path, where, columns, sid, at, mode, location, memory, live}}
_loads: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}


def remember(conn: str, entry: dict[str, Any]) -> None:
    key = (str(entry["caslib"]).upper(), str(entry["table"]).upper())
    with _lock:
        _loads.setdefault(conn or "", {})[key] = {**entry, "at": time.time()}


def forget(conn: str, caslib: str, table: str) -> None:
    with _lock:
        _loads.get(conn or "", {}).pop((caslib.upper(), table.upper()), None)


def state(conn: str, sid: str | None) -> dict[str, list[dict[str, Any]]]:
    """This connection's loads: ``loaded`` (in the current CAS session ``sid``) and ``lost`` (loaded in an earlier
    session that has ended since: offer to load them again)."""
    with _lock:
        items = list(_loads.get(conn or "", {}).values())
    loaded = [x for x in items if sid and x.get("sid") == sid]
    lost = [x for x in items if not sid or x.get("sid") != sid]
    return {"loaded": sorted(loaded, key=lambda x: x["at"]), "lost": sorted(lost, key=lambda x: x["at"])}


def clear(conn: str, entries: list[dict[str, Any]] | None = None) -> None:
    """Forget ``entries`` (default: all of the connection's)."""
    with _lock:
        if entries is None:
            _loads.pop(conn or "", None)
            return
        d = _loads.get(conn or "", {})
        for e in entries:
            d.pop((str(e["caslib"]).upper(), str(e["table"]).upper()), None)
