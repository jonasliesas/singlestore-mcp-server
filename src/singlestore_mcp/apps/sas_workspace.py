"""SAS in the Workspace: SAS libraries and caslibs in the Schema Explorer and SQL Editor, and "Run as"
PROC SQL / FedSQL / FedSQL in CAS with the SAS log and the pushdown to SingleStore.

App-only tools (the SQL Editor and Schema Explorer call them; Claude uses the SAS Viya MCP server itself).
Runs go through one SAS Compute session and one CAS session (``singlestore_mcp.sas_rest``); the SQL is
wrapped by ``singlestore_mcp.sas_sql``. Like the SingleStore runs, statements that write anywhere but WORK
(Compute) or CASUSER (CAS) are answered with ``needs_confirmation`` and run only through ``sas_execute``.
"""

from __future__ import annotations

import re
import time
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import sas_rest, sas_sql, sas_viya
from .. import sql_editor_state as editor_state
from ..sas_rest import cas, compute
from ..sas_sql import SasError
from ._core import APP_ONLY, apps, jsonable_rows, tool_result
from .schema_explorer import URI as SCHEMA_URI
from .sql_editor import URI as EDITOR_URI

READ_ONLY = ToolAnnotations(readOnlyHint=True)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
STATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
MAX_ROWS_LIMIT = 10_000
MAX_LOG_CHARS = 400_000


# ------------------------------------------------------------------ status and sources


def _status() -> dict[str, Any]:
    st = sas_viya.status()
    out = {"connection": st.get("name"), "configured": bool(st.get("url")), "signed_in": bool(st.get("signed_in")), "user": st.get("user"),
           "problem": st.get("problem"), "compute_context": st.get("compute_context"),
           "cas_server": st.get("cas_server"), "has_startup_code": bool((st.get("startup_code") or "").strip()),
           "compute_session": bool(compute.sid), "cas_session": bool(cas.sid)}
    if compute.startup:
        out["startup"] = compute.startup
    return out


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_sources() -> CallToolResult:
    """Whether SAS Viya is set up and signed in (for the SAS libraries / caslibs sources); starts no session."""
    data = _status()
    text = "SAS Viya ready" if data["signed_in"] else (data["problem"] or "SAS Viya isn't set up")
    return tool_result(text, data)


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_libraries(refresh: bool = False) -> CallToolResult:
    """SAS libraries in the Workspace's SAS Compute session (compute context + startup code): engine, path and,
    for SAS/ACCESS to SingleStore (engine S2 / SSTORE), the SingleStore database."""
    started = time.perf_counter()
    libs = sas_rest.libraries(force=refresh)
    s = sas_viya.settings()
    data = {"libraries": libs, "startup": compute.startup, "compute_context": s["compute_context"], "connection": s["name"],
            "ms": round((time.perf_counter() - started) * 1000)}
    return tool_result(f"{len(libs)} SAS librar{'y' if len(libs) == 1 else 'ies'}", data)


def _check_name(name: str, what: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 256 or any(c in name for c in "/\\\n\r"):
        raise ValueError(f"Not a valid {what}: {name!r}")
    return name


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_tables(library: str) -> CallToolResult:
    """Tables (data sets and views) in one SAS library, with rows and columns where SAS knows them."""
    lib = _check_name(library, "libref").upper()
    tables = sorted(compute.tables(lib), key=lambda t: str(t["name"]).lower())
    meta = next((x for x in compute.libraries or [] if x["name"] == lib), None)
    return tool_result(f"{len(tables)} table(s) in {lib}", {"library": lib, "tables": tables, "meta": meta})


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_columns(library: str, table: str) -> CallToolResult:
    """Columns of one SAS table: name, type, length, format, label."""
    lib, tbl = _check_name(library, "libref").upper(), _check_name(table, "table name")
    cols = compute.columns(lib, tbl)
    return tool_result(f"{len(cols)} column(s) in {lib}.{tbl}", {"library": lib, "table": tbl, "columns": cols})


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cas_caslibs() -> CallToolResult:
    """Caslibs on the CAS server: type, description, path and, for SingleStore caslibs, the database."""
    started = time.perf_counter()
    res = cas.action("table.caslibInfo", {"verbose": True})
    _, rows = sas_rest.cas_table(sas_rest.first_table(res["results"], "CASLibInfo"))
    libs = caslib_rows(rows)
    s = sas_viya.settings()
    return tool_result(f"{len(libs)} caslib(s)", {"caslibs": libs, "server": s["cas_server"], "connection": s["name"],
                                                 "ms": round((time.perf_counter() - started) * 1000)})


_DEF = re.compile(r"^\s*(\w+)\s*=\s*'?(.*?)'?\s*$")
_HIDDEN_OPTIONS = {"pwd", "password", "authtoken", "token", "secret", "credentials", "authdomain"}


def caslib_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """caslibInfo(verbose) rows → one entry per caslib. Verbose output adds one row per data source option
    ("database = 'SASDP'"); password-like options are never passed on."""
    libs: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = str(r.get("Name") or "")
        if not name:
            continue
        lib = libs.get(name)
        if lib is None or r.get("Type"):
            kind = str(r.get("Type") or "")
            lib = libs.setdefault(name, {"name": name, "options": {}})
            lib.update({"type": kind, "description": r.get("Description") or None, "path": r.get("Path") or None,
                        "personal": _flag(r.get("Personal")), "active": _flag(r.get("Active")),
                        "singlestore": kind.lower() in sas_sql.CAS_TYPES_SINGLESTORE})
        m = _DEF.match(str(r.get("Definition") or ""))
        if m and m.group(1).lower() not in _HIDDEN_OPTIONS:
            lib["options"][m.group(1).lower()] = m.group(2)
    out = []
    for lib in libs.values():
        opts = lib.pop("options")
        lib["database"] = opts.get("database") or opts.get("schema") or None
        lib["server"] = opts.get("host") or opts.get("server") or None
        out.append(lib)
    out.sort(key=lambda x: (not x.get("personal"), str(x["name"]).lower()))
    return out


def _flag(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "1.0", "true", "yes")


def cas_table_list(loaded: list[dict[str, Any]], files: list[dict[str, Any]], database: str | None) -> list[dict[str, Any]]:
    """tableInfo (loaded in memory) + fileInfo (source tables / files) → one list. For a database caslib only the
    tables of its own database (fileInfo lists every schema the login can see)."""
    tables: dict[str, dict[str, Any]] = {}
    for t in loaded:
        name = str(t.get("Name") or "")
        tables[name.upper()] = {"name": name, "loaded": True, "rows": t.get("Rows"), "columns": t.get("Columns"),
                                "label": t.get("Label") or None, "source": t.get("SourceName") or None,
                                "global": _flag(t.get("Global"))}
    for f in files:
        name = str(f.get("Name") or "")
        schema = f.get("Schema")
        if not name or (database and schema and str(schema).lower() != database.lower()):
            continue
        base = re.sub(r"\.(sashdat|sas7bdat|csv|parquet|xlsx?|txt)$", "", name, flags=re.I)
        hit = tables.get(base.upper())
        if hit:
            hit["source"] = hit.get("source") or name
        else:
            tables[base.upper()] = {"name": base, "loaded": False, "source": name, "rows": None, "columns": None,
                                    "label": f.get("Description") or None, "global": False}
    return sorted(tables.values(), key=lambda t: t["name"].lower())


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cas_tables(caslib: str, database: str | None = None) -> CallToolResult:
    """Tables in one caslib: the ones loaded in memory (rows, columns) and the source tables / files not loaded.
    ``database``: for a SingleStore caslib, its database (only its tables are listed)."""
    lib = _check_name(caslib, "caslib")
    loaded_res = cas.action("table.tableInfo", {"caslib": lib}, check=False)
    files_res = cas.action("table.fileInfo", {"caslib": lib}, check=False)
    _, loaded = sas_rest.cas_table(sas_rest.first_table(loaded_res["results"], "TableInfo"))
    _, files = sas_rest.cas_table(sas_rest.first_table(files_res["results"], "FileInfo"))
    out = cas_table_list(loaded, files, database)
    problem = None
    if str(files_res["severity"]).lower() == "error" and str(loaded_res["severity"]).lower() == "error":
        problem = files_res["status"] or loaded_res["status"] or "CAS couldn't list this caslib"
    return tool_result(f"{len(out)} table(s) in {lib}", {"caslib": lib, "tables": out, "problem": problem})


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cas_columns(caslib: str, table: str) -> CallToolResult:
    """Columns of a CAS table, loaded or not (for a database caslib CAS asks the database)."""
    lib, tbl = _check_name(caslib, "caslib"), _check_name(table, "table name")
    res = cas.action("table.columnInfo", {"table": {"caslib": lib, "name": tbl}}, check=False)
    if str(res["severity"]).lower() == "error":
        msg = " ".join(x for x in res["log"].splitlines() if x.startswith("ERROR"))[:300] or res["status"]
        raise LookupError(f"CAS couldn't describe {lib}.{tbl}: {msg}")
    _, rows = sas_rest.cas_table(sas_rest.first_table(res["results"], "ColumnInfo"))
    cols = [_cas_column(r) for r in rows]
    return tool_result(f"{len(cols)} column(s) in {lib}.{tbl}", {"caslib": lib, "table": tbl, "columns": cols})


MAX_LOOKUP = 8  # tables per sas_lookup_columns call


def _lookup_pairs(tables: list[dict[str, Any]], language: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for t in tables or []:
        if not isinstance(t, dict):
            raise ValueError("tables: a list of {library, table}")
        lib = _check_name(str(t.get("library") or ""), "caslib" if language == "cas" else "libref")
        tbl = _check_name(str(t.get("table") or ""), "table name")
        if language != "cas":
            lib = lib.upper()
        key = (lib.upper(), tbl.upper())
        if key not in seen:
            seen.add(key)
            pairs.append((lib, tbl))
    if len(pairs) > MAX_LOOKUP:
        raise ValueError(f"Look up at most {MAX_LOOKUP} tables at a time.")
    return pairs


def _cas_describe(lib: str, tbl: str) -> list[dict[str, Any]] | None:
    """Columns of a CAS table, or None when CAS can't find (or describe) it."""
    res = cas.action("table.columnInfo", {"table": {"caslib": lib, "name": tbl}}, check=False)
    if str(res["severity"]).lower() == "error":
        return None
    _, rows = sas_rest.cas_table(sas_rest.first_table(res["results"], "ColumnInfo"))
    return [_cas_column(r) for r in rows]


def _cas_column(r: dict[str, Any]) -> dict[str, Any]:
    return {"name": r.get("Column"), "type": "char" if "char" in str(r.get("Type", "")).lower() else "num",
            "cas_type": r.get("Type"), "length": r.get("RawLength"),
            "format": (f"{r.get('Format')}{r.get('FormattedLength') or ''}." if r.get("Format") else None),
            "label": r.get("Label") or None, "position": r.get("ID")}


def lookup_columns(language: str, pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Columns for the SQL Editor's autocomplete: {session, tables: [{library, table, name, columns | None}]}.
    ``session`` names the Viya connection and SAS session the answer came from (the editor forgets its cache
    when it changes). Compute: one data request per table, in parallel; a miss is retried once with the
    library's own spelling of the name (SAS/ACCESS tables can be case-sensitive). CAS: one columnInfo each."""
    conn = sas_viya.settings()["name"]
    found: dict[tuple[str, str], tuple[str, list[dict[str, Any]] | None]] = {}
    if language == "cas":
        with cas.lock:
            sid = cas.session()
            for lib, tbl in pairs:
                found[(lib, tbl)] = (tbl, _cas_describe(lib, tbl))
    else:
        sid, got = compute.columns_many(pairs)
        for pair, cols in got.items():
            found[pair] = (pair[1], cols)
        misses = [p for p, (_, cols) in found.items() if cols is None]
        retry: list[tuple[str, str]] = []
        names: dict[tuple[str, str], str] = {}
        for lib in {p[0] for p in misses}:
            try:
                listed = [str(t["name"]) for t in compute.tables(lib)]
            except LookupError:
                continue  # no such library
            for p in misses:
                if p[0] != lib:
                    continue
                real = next((n for n in listed if n.upper() == p[1].upper()), None)
                if real and real != p[1]:
                    names[(lib, real)] = p[1]
                    retry.append((lib, real))
        if retry:
            sid2, again = compute.columns_many(retry)
            if sid2 == sid:
                for (lib, real), cols in again.items():
                    found[(lib, names[(lib, real)])] = (real, cols)
    tables = [{"library": lib, "table": tbl, "name": name, "columns": cols, "missing": cols is None}
              for (lib, tbl), (name, cols) in found.items()]
    return {"language": language, "connection": conn, "session": f"{conn}|{'cas' if language == 'cas' else 'compute'}|{sid}",
            "tables": tables}


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_lookup_columns(language: str, tables: list[dict[str, Any]]) -> CallToolResult:
    """Columns of up to 8 SAS tables (``[{library, table}]``) for the SQL Editor's autocomplete, in one call:
    SAS libraries for procsql / fedsql, caslibs for cas. A table that doesn't exist comes back with
    ``missing: true`` (not an error), so the editor can remember it briefly and not ask again."""
    if language not in sas_sql.LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(sas_sql.LANGUAGES)}")
    pairs = _lookup_pairs(tables, language)
    if not pairs:
        return tool_result("no tables", {"language": language, "session": None, "tables": []})
    data = lookup_columns(language, pairs)
    hit = sum(1 for t in data["tables"] if not t["missing"])
    return tool_result(f"columns of {hit} of {len(pairs)} table(s)", data)


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=STATE)
def sas_session_reset() -> CallToolResult:
    """End the Workspace's SAS Compute and CAS sessions; the next run starts new ones (startup code runs again)."""
    sas_rest.reset()
    return tool_result("SAS sessions ended", _status())


# ------------------------------------------------------------------ running


def _limit(max_rows: int) -> int:
    return max(1, min(int(max_rows), MAX_ROWS_LIMIT))


_AFFECTED = re.compile(r"NOTE: (?:Table \S+ created, with (\d+) rows|(\d+) rows? (?:was|were) (?:inserted|updated|deleted))", re.I)


def _affected(log_text: str) -> int | None:
    total = None
    for m in _AFFECTED.finditer(log_text):
        total = (total or 0) + int(m.group(1) or m.group(2))
    return total


def run_sas(language: str, sql: str, library: str | None, options: dict[str, Any] | None, max_rows: int,
            pushdown: bool) -> dict[str, Any]:
    """Run one statement (or plain SAS code) and gather rows, log and the pushdown verdict."""
    if language not in sas_sql.LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(sas_sql.LANGUAGES)}")
    max_rows = _limit(max_rows)
    wrapped = sas_sql.wrap(language, sql, library=library, options=options, max_rows=max_rows, pushdown=pushdown)
    plain = wrapped["kind"] == "sas"
    started = time.perf_counter()
    if language == "cas" and not plain:
        data = _run_cas(wrapped, library, max_rows, pushdown)
    else:
        data = _run_compute(wrapped, language if not plain else "procsql", max_rows, pushdown)
    data.update(language=language, plain_sas=plain, sql=sql, library=library, code=wrapped["code"], max_rows=max_rows,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
    return data


def _run_compute(w: dict[str, Any], language: str, max_rows: int, pushdown: bool) -> dict[str, Any]:
    # One run at a time, through reading its result table (WORK._S2RES is shared by every run).
    with compute.lock:
        return _run_compute_locked(w, language, max_rows, pushdown)


def _run_compute_locked(w: dict[str, Any], language: str, max_rows: int, pushdown: bool) -> dict[str, Any]:
    res = compute.run(w["code"], listing=w["kind"] == "sas")
    lines = res["log"]
    counts = sas_sql.log_counts(lines)
    log_text = sas_rest.sas_code_log(lines)
    failed = res["state"] in ("error", "failed", "canceled") or counts["errors"] > 0
    out: dict[str, Any] = {"state": res["state"], "log": log_text[-MAX_LOG_CHARS:], "log_types": _types(lines),
                           "errors": counts["errors"], "warnings": counts["warnings"],
                           "error": sas_sql.mask_secrets(counts["first_error"]) if failed and counts["first_error"] else
                           ("SAS reported an error" if failed else None),
                           "columns": [], "rows": [], "has_result_set": False, "row_count": 0, "truncated": False,
                           "affected_rows": _affected(log_text), "listing": res.get("listing") or None}
    if w["result"] and not failed:
        lib, table = w["result"]
        got = compute.rows(lib, table, max_rows)
        out.update(columns=got["columns"], column_meta=got["column_meta"], rows=jsonable_rows(got["rows"]),
                   has_result_set=True, row_count=len(got["rows"]), total_rows=got["total_rows"])
        early = "terminated early due to OUTOBS" in log_text
        out["truncated"] = bool((w["capped"] and early) or (got["total_rows"] or 0) > len(got["rows"]))
        out["affected_rows"] = None
    if pushdown:
        out["pushdown"] = _pushdown(language, log_text)
    return out


def _run_cas(w: dict[str, Any], library: str | None, max_rows: int, pushdown: bool) -> dict[str, Any]:
    action, params = w["actions"][0]
    with cas.lock:
        cas.ensure_actionset("fedSql")
        cas.set_caslib(library or "CASUSER")  # one-level names: the tab's caslib, else CASUSER
        res = cas.action(action, params, check=False)
        failed = str(res["severity"]).lower() == "error"
        log_text = sas_sql.mask_secrets(res["log"])
        errs = [x for x in log_text.splitlines() if x.startswith("ERROR")]
        out: dict[str, Any] = {"state": "error" if failed else str(res["severity"]).lower(), "log": log_text[-MAX_LOG_CHARS:],
                               "log_types": None, "errors": len(errs) or int(failed),
                               "warnings": sum(1 for x in log_text.splitlines() if x.startswith("WARNING")),
                               "error": (errs[0] if errs else res["status"] or "CAS reported an error") if failed else None,
                               "columns": [], "rows": [], "has_result_set": False, "row_count": 0, "truncated": False,
                               "affected_rows": None, "listing": None}
        if w["result"] and not failed:
            caslib, table = w["result"]
            fetch = cas.action("table.fetch", {"table": {"caslib": caslib, "name": table}, "to": max_rows,
                                               "maxRows": max_rows, "index": False})
            cols, rows = sas_rest.cas_table(sas_rest.first_table(fetch["results"], "Fetch"))
            info = cas.action("table.tableInfo", {"caslib": caslib, "name": table}, check=False)
            _, irows = sas_rest.cas_table(sas_rest.first_table(info["results"], "TableInfo"))
            total = irows[0].get("Rows") if irows else None
            fmts = {c["name"]: c.get("format") for c in cols}
            rows = [{k: sas_sql.sas_value(v, fmts.get(k)) for k, v in r.items()} for r in rows]
            names = [c["name"] for c in cols]
            out.update(columns=names, column_meta=[{"name": c["name"], "type": c.get("type"), "format": c.get("format"),
                                                    "label": c.get("label"), "length": c.get("length")} for c in cols],
                       rows=jsonable_rows(rows), has_result_set=True, row_count=len(rows), total_rows=total,
                       truncated=bool(total and int(total) > len(rows)))
    if pushdown:
        out["pushdown"] = _pushdown("cas", log_text)
    return out


def _types(lines: list[dict[str, str]]) -> list[str]:
    """One letter per log line for colouring: e(rror) w(arning) n(ote) s(ource) or "." (other)."""
    out = []
    for x in lines:
        t, line = x.get("type", ""), x.get("line", "")
        out.append("e" if t == "error" or line.startswith("ERROR") else "w" if t == "warning" or line.startswith("WARNING")
                   else "n" if t == "note" or line.startswith("NOTE") else "s" if t == "source" else ".")
    return out


def _pushdown(language: str, log_text: str) -> dict[str, Any]:
    v = sas_sql.pushdown_verdict(language, log_text)
    for s in v["sent"]:
        first = s["sql"].split(" ", 1)
        s["search"] = re.sub(r"\s+", " ", s["sql"])[:80] if first else s["sql"][:80]
    return v


def _record(sql: str, library: str | None, language: str, data: dict[str, Any] | None, source: dict[str, Any],
            kind: str, started: float, exc: Exception | None = None) -> None:
    lang = "sas" if sas_sql.is_plain_sas(sql) else language
    ms = round((time.perf_counter() - started) * 1000, 1)
    if exc is not None or (data and data.get("error")):
        err = str(exc) if exc is not None else data.get("error")
        editor_state.add_history(sql, library, ok=False, kind=kind, error=err, source=source, elapsed_ms=ms, language=lang)
    else:
        editor_state.add_history(sql, library, ok=True, kind=kind, elapsed_ms=ms, source=source, language=lang,
                                 rows=data["row_count"] if data and data.get("has_result_set") else None,
                                 affected=data.get("affected_rows") if data else None)


def _summary(data: dict[str, Any]) -> str:
    if data.get("error"):
        return f"SAS error: {data['error']}"
    if data.get("has_result_set"):
        return f"{data['row_count']} row(s) in {data['elapsed_ms']} ms"
    return f"Ran in {data['elapsed_ms']} ms"


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_query(language: str, sql: str, library: str | None = None, options: dict[str, Any] | None = None,
              max_rows: int = 1000, pushdown: bool = True, tab_id: str | None = None, tab: str | None = None,
              file: str | None = None, history: bool = True) -> CallToolResult:
    """Run one statement from the SQL Editor as PROC SQL / FedSQL (SAS libraries) or FedSQL in CAS (caslibs).

    ``language``: procsql, fedsql or cas. The SQL is wrapped (proc sql; … quit; etc.); text starting with
    proc / data / % runs as plain SAS code. Statements that write anywhere but WORK / CASUSER are answered with
    ``needs_confirmation`` (the app asks, then calls sas_execute). ``pushdown`` adds sastrace / the FedSQL plan
    and returns what was sent to SingleStore with a verdict. ``history`` false: not recorded in the editor's
    run history (the Schema Explorer's previews).
    """
    check = sas_sql.needs_confirmation(language, sql, library)
    if check:
        data = {"needs_confirmation": True, **check, "sql": sql, "library": library, "language": language,
                "plain_sas": sas_sql.is_plain_sas(sql)}
        return tool_result(check["reason"] + " and needs confirmation.", data)
    return _run_tool(language, sql, library, options, max_rows, pushdown, {"tab_id": tab_id, "tab": tab, "file": file}, "read",
                     record=history)


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=WRITES)
def sas_execute(language: str, sql: str, library: str | None = None, options: dict[str, Any] | None = None,
                max_rows: int = 1000, pushdown: bool = True, tab_id: str | None = None, tab: str | None = None,
                file: str | None = None) -> CallToolResult:
    """Run one SAS statement the user confirmed in the SQL Editor (writes outside WORK / CASUSER included)."""
    return _run_tool(language, sql, library, options, max_rows, pushdown, {"tab_id": tab_id, "tab": tab, "file": file}, "write")


def _run_tool(language: str, sql: str, library: str | None, options: dict[str, Any] | None, max_rows: int,
              pushdown: bool, source: dict[str, Any], kind: str, record: bool = True) -> CallToolResult:
    started = time.perf_counter()
    try:
        data = run_sas(language, sql, library, options, max_rows, pushdown)
    except Exception as exc:
        if record:
            _record(sql, library, language, None, source, kind, started, exc)
        if isinstance(exc, (SasError, LookupError, ValueError)):
            raise
        raise SasError(str(exc) or type(exc).__name__) from exc
    data["kind"] = kind
    if record:
        _record(sql, library, language, data, source, kind, started)
    return tool_result(_summary(data), data)


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_classify(language: str, statements: list[str], library: str | None = None) -> CallToolResult:
    """For "Run all" in a SAS language: which statements need confirmation (write outside WORK / CASUSER)."""
    if len(statements) > 2000:
        raise ValueError("Run all handles up to 2000 statements at a time.")
    writes = []
    for i, sql in enumerate(statements):
        check = sas_sql.needs_confirmation(language, sql, library)
        if check:
            writes.append({"index": i, "reason": check["reason"], "shared": check["shared"]})
    return tool_result(f"{len(writes)} of {len(statements)} statement(s) need confirmation", {"writes": writes})


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_history_lookup(sql: str, database: str | None = None) -> CallToolResult:
    """Is this SQL (sent to SingleStore by SAS) in the cluster's plan cache or the local query history?"""
    from .. import history_store

    found: dict[str, Any] = {"plan_cache": None, "history": 0}
    key = history_store.statement_key(sql, database)
    key_any = history_store.statement_key(sql, None)
    try:
        for p in history_store.read_plancache():
            if p["stmt"] in (key, key_any) or history_store.statement_key(p["text"], None, parameterized=True) == key_any:
                found["plan_cache"] = {"runs": p["runs"], "avg_ms": p["avg_ms"], "last_executed": p["last_executed"],
                                       "database": p["database"]}
                break
    except Exception as exc:  # noqa: BLE001 - cluster unreachable: say so, the history may still know it
        found["plan_cache_error"] = str(exc)[:200]
    try:
        conn = history_store.connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE stmt IN (?, ?)", (key, key_any)).fetchone()
            found["history"] = int(row["n"] or 0)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    found["found"] = bool(found["plan_cache"] or found["history"])
    return tool_result("found" if found["found"] else "not found", found)
