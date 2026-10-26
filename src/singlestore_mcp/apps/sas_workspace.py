"""SAS in the Workspace: SAS libraries and caslibs in the Schema Explorer and SQL Editor, and "Run as"
PROC SQL / FedSQL / FedSQL in CAS with the SAS log and the pushdown to SingleStore.

App-only tools (the SQL Editor and Schema Explorer call them; Claude uses the SAS Viya MCP server itself).
Runs go through one SAS Compute session and one CAS session per SAS Viya connection (``singlestore_mcp.sas_rest``;
``connection`` picks one, default the active one); the SQL is wrapped by ``singlestore_mcp.sas_sql``. Runs go to
the background (``singlestore_mcp.sas_runs``): they can be stopped, wait in line for a busy session and their
result tables are paged. Like the SingleStore runs, statements that write anywhere but WORK (Compute) or CASUSER
(CAS) are answered with ``needs_confirmation`` and run only through ``sas_execute``.
"""

from __future__ import annotations

import re
import time
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import result_stream, sas_rest, sas_runs, sas_sql, sas_viya
from .. import sas_prewarm as prewarm_mod
from .. import sql_editor_state as editor_state
from ..sas_sql import SasError
from ._core import APP_ONLY, apps, jsonable_rows, tool_result
from .schema_explorer import URI as SCHEMA_URI
from .sql_editor import URI as EDITOR_URI
from .sql_editor import WORKSPACE_URI

READ_ONLY = ToolAnnotations(readOnlyHint=True)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
STATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
MAX_ROWS_LIMIT = 10_000          # rows in one answer (the first page)
MAX_TABLE_ROWS = result_stream.MAX_ROWS  # rows a PROC SQL result table keeps for paging (OUTOBS)
MAX_LOG_CHARS = 400_000
FIRST_WAIT_MS = 20_000           # sas_query waits this long for the result before answering "running"


def _sessions(connection: str | None) -> tuple[sas_rest.Compute, sas_rest.Cas]:
    """The Compute and CAS sessions of a SAS Viya connection (None: the active one); an unknown name fails."""
    if connection:
        sas_viya.settings(connection)  # LookupError for a connection that doesn't exist (any more)
    return sas_rest.sessions(connection)


def _conn_name(connection: str | None) -> str | None:
    return connection or sas_viya.active_name()


# ------------------------------------------------------------------ status and sources


def _status(connection: str | None = None) -> dict[str, Any]:
    st = sas_viya.status(connection)
    compute, cas = _sessions(connection)
    out = {"connection": st.get("name"), "configured": bool(st.get("url")), "signed_in": bool(st.get("signed_in")), "user": st.get("user"),
           "problem": st.get("problem"), "compute_context": st.get("compute_context"),
           "cas_server": st.get("cas_server"), "has_startup_code": bool((st.get("startup_code") or "").strip()),
           "compute_session": bool(compute.sid), "cas_session": bool(cas.sid)}
    if compute.startup:
        out["startup"] = compute.startup
    out["auto_libname"] = compute.auto_lib
    out["sas9_configured"] = bool((st.get("sas9_signon") or "").strip() and st.get("sas9_server"))
    out["sas9_server"] = st.get("sas9_server") or None
    if compute.sas9:
        out["sas9"] = {k: v for k, v in compute.sas9.items() if k != "remote"}
    return out


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_sources(connection: str | None = None) -> CallToolResult:
    """Whether SAS Viya is set up and signed in (for the SAS libraries / caslibs sources); starts no session.
    ``connection``: a saved SAS Viya connection (default: the active one)."""
    data = _status(connection)
    text = "SAS Viya ready" if data["signed_in"] else (data["problem"] or "SAS Viya isn't set up")
    return tool_result(text, data)


_SESSION_TEXT = {"starting": "SAS session: starting…", "ready": "SAS session: ready", "idle": "SAS session: not started",
                 "off": "SAS session: starts with the first run", "not_signed_in": "SAS session: not signed in",
                 "unreachable": "SAS session: SAS Viya unreachable", "error": "SAS session: couldn't start"}


@apps.tool(resource_uri=WORKSPACE_URI, visibility=APP_ONLY, annotations=STATE)
def sas_prewarm(connection: str | None = None, start: bool = True) -> CallToolResult:
    """The Workspace's SAS session for a SAS Viya connection (default: the active one): with ``start``, starts it
    in the background (Compute session with the automatic SingleStore library and the startup code, then CAS) so
    the first run doesn't wait, or checks a running one now and then (keep-alive); answers at once. Never asks to
    sign in: ``state`` is starting / ready / idle / off / not_signed_in / unreachable / error. Turned off per
    connection (Connections → SAS Viya → Edit) or with SINGLESTORE_MCP_SAS_PREWARM=0."""
    if connection:
        sas_viya.settings(connection)  # LookupError for an unknown connection
    data = prewarm_mod.prewarm(connection) if start else prewarm_mod.status(connection)
    return tool_result(_SESSION_TEXT.get(data.get("state"), "SAS session"), data)


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_libraries(refresh: bool = False, connection: str | None = None) -> CallToolResult:
    """SAS libraries in the Workspace's SAS Compute session (compute context + startup code): engine, path and,
    for SAS/ACCESS to SingleStore (engine S2 / SSTORE), the SingleStore database. ``connection``: a saved SAS
    Viya connection (default: the active one)."""
    started = time.perf_counter()
    compute, _ = _sessions(connection)
    libs = sas_rest.libraries(force=refresh, conn=connection)
    s = sas_viya.settings(connection)
    data = {"libraries": libs, "startup": compute.startup, "compute_context": s["compute_context"], "connection": s["name"],
            "auto_libname": compute.auto_lib, "ms": round((time.perf_counter() - started) * 1000)}
    return tool_result(f"{len(libs)} SAS librar{'y' if len(libs) == 1 else 'ies'}", data)


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=STATE)
def sas9_libraries(refresh: bool = False, connection: str | None = None) -> CallToolResult:
    """SAS 9 libraries through SAS/CONNECT: signs on to the SAS 9 server from the Workspace's SAS session (the
    active Viya connection's "SAS 9 sign-on code", its log never shown) once, assigns the remote libraries
    (``libname X server=… slibref=X``) and lists them (engine REMOTE). ``refresh``: sign on again."""
    started = time.perf_counter()
    res = sas_rest.sas9_connect(force=refresh, conn=connection)
    libs = [x for x in sas_rest.libraries(force=True, conn=connection) if x.get("sas9")]
    data = {"libraries": libs, "server": res["server"], "skipped": res.get("skipped") or [], "failed": res.get("failed") or [],
            "missing": res.get("missing") or [], "connection": sas_viya.settings(connection)["name"],
            "ms": round((time.perf_counter() - started) * 1000)}
    return tool_result(f"{len(libs)} SAS 9 librar{'y' if len(libs) == 1 else 'ies'} on {res['server']}", data)


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_library_code(library: str, connection: str | None = None) -> CallToolResult:
    """The LIBNAME statement for one SAS library, reconstructed from the session's dictionary.libnames (engine,
    path, database, user); passwords as placeholders (``password="<set password>"``), never real values."""
    lib = _check_name(library, "libref").upper()
    meta = next((x for x in sas_rest.libraries(conn=connection) if x["name"] == lib), None)
    if meta is None:
        meta = next((x for x in sas_rest.libraries(force=True, conn=connection) if x["name"] == lib), None)
    if meta is None:
        raise LookupError(f"No library {lib} in the Workspace's SAS session.")
    code = sas_sql.libname_code(meta)
    return tool_result(code, {"library": lib, "code": code, "note": sas_sql.RECONSTRUCTED, "language": "procsql"})


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cas_caslib_code(caslib: str, connection: str | None = None) -> CallToolResult:
    """The CASLIB statement and the ``table.addCaslib`` action (PROC CAS) for one caslib, reconstructed from
    ``caslibInfo`` (verbose); passwords / tokens / authentication domains as placeholders. Nothing is run."""
    lib = _check_name(caslib, "caslib")
    res = _sessions(connection)[1].action("table.caslibInfo", {"caslib": lib, "verbose": True})
    _, rows = sas_rest.cas_table(sas_rest.first_table(res["results"], "CASLibInfo"))
    libs = caslib_rows(rows)
    meta = next((x for x in libs if x["name"].upper() == lib.upper()), None)
    if meta is None:
        raise LookupError(f"No caslib {lib} on the CAS server.")
    code = sas_sql.caslib_code(meta)
    return tool_result(code["statement"], {"caslib": meta["name"], **code, "note": sas_sql.RECONSTRUCTED,
                                           "type": meta["type"], "singlestore_kind": meta.get("singlestore_kind")})


def _check_name(name: str, what: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 256 or any(c in name for c in "/\\\n\r"):
        raise ValueError(f"Not a valid {what}: {name!r}")
    return name


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_tables(library: str, connection: str | None = None) -> CallToolResult:
    """Tables (data sets and views) in one SAS library, with rows and columns where SAS knows them."""
    compute, _ = _sessions(connection)
    lib = _check_name(library, "libref").upper()
    tables = sorted(compute.tables(lib), key=lambda t: str(t["name"]).lower())
    meta = next((x for x in compute.libraries or [] if x["name"] == lib), None)
    return tool_result(f"{len(tables)} table(s) in {lib}", {"library": lib, "tables": tables, "meta": meta})


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_columns(library: str, table: str, connection: str | None = None) -> CallToolResult:
    """Columns of one SAS table: name, type, length, format, label."""
    compute, _ = _sessions(connection)
    lib, tbl = _check_name(library, "libref").upper(), _check_name(table, "table name")
    cols = compute.columns(lib, tbl)
    return tool_result(f"{len(cols)} column(s) in {lib}.{tbl}", {"library": lib, "table": tbl, "columns": cols})


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def cas_caslibs(connection: str | None = None) -> CallToolResult:
    """Caslibs on the CAS server: type, description, path and, for SingleStore caslibs, the database."""
    started = time.perf_counter()
    res = _sessions(connection)[1].action("table.caslibInfo", {"verbose": True})
    _, rows = sas_rest.cas_table(sas_rest.first_table(res["results"], "CASLibInfo"))
    libs = caslib_rows(rows)
    s = sas_viya.settings(connection)
    return tool_result(f"{len(libs)} caslib(s)", {"caslibs": libs, "server": s["cas_server"], "connection": s["name"],
                                                 "ms": round((time.perf_counter() - started) * 1000)})


_DEF = re.compile(r"^\s*(\w+)\s*=\s*(?:'(.*)'|(.*?))\s*$", re.S)


def caslib_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """caslibInfo(verbose) rows → one entry per caslib. Verbose output adds one row per data source option
    ("database = 'SASDP'"), kept in ``datasource`` (for "Show code"); the values of password-like options are
    never passed on, only their names (``secret_options``). SingleStore caslibs get their kind (SpeedyStore /
    standard connector) and badge."""
    libs: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = str(r.get("Name") or "")
        if not name:
            continue
        lib = libs.get(name)
        if lib is None or r.get("Type"):
            kind = str(r.get("Type") or "")
            lib = libs.setdefault(name, {"name": name, "datasource": [], "secret_options": []})
            lib.update({"type": kind, "description": r.get("Description") or None, "path": r.get("Path") or None,
                        "personal": _flag(r.get("Personal")), "active": _flag(r.get("Active")),
                        "subdirs": _flag(r.get("Subdirs")),
                        "singlestore": kind.lower() in sas_sql.CAS_TYPES_SINGLESTORE,
                        "singlestore_kind": sas_sql.cas_singlestore_kind(kind)})
        m = _DEF.match(str(r.get("Definition") or ""))
        if not m:
            continue
        opt = m.group(1)
        if sas_sql.is_secret_option(opt):
            if opt not in lib["secret_options"]:
                lib["secret_options"].append(opt)
        elif not any(o["name"].lower() == opt.lower() for o in lib["datasource"]):
            quoted = m.group(2) is not None
            lib["datasource"].append({"name": opt, "value": m.group(2) if quoted else m.group(3), "quoted": quoted})
    out = []
    for lib in libs.values():
        opts = {o["name"].lower(): o["value"] for o in lib["datasource"]}
        lib["database"] = opts.get("database") or opts.get("schema") or None
        lib["server"] = opts.get("host") or opts.get("server") or None
        lib.update(sas_sql.singlestore_badge(lib["singlestore_kind"], lib["database"], lib["server"]))
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
def cas_tables(caslib: str, database: str | None = None, connection: str | None = None) -> CallToolResult:
    """Tables in one caslib: the ones loaded in memory (rows, columns) and the source tables / files not loaded.
    ``database``: for a SingleStore caslib, its database (only its tables are listed)."""
    lib = _check_name(caslib, "caslib")
    cas = _sessions(connection)[1]
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
def cas_columns(caslib: str, table: str, connection: str | None = None) -> CallToolResult:
    """Columns of a CAS table, loaded or not (for a database caslib CAS asks the database)."""
    lib, tbl = _check_name(caslib, "caslib"), _check_name(table, "table name")
    res = _sessions(connection)[1].action("table.columnInfo", {"table": {"caslib": lib, "name": tbl}}, check=False)
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


def _cas_describe(cas: sas_rest.Cas, lib: str, tbl: str) -> list[dict[str, Any]] | None:
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


def lookup_columns(language: str, pairs: list[tuple[str, str]], connection: str | None = None) -> dict[str, Any]:
    """Columns for the SQL Editor's autocomplete: {session, tables: [{library, table, name, columns | None}]}.
    ``session`` names the Viya connection and SAS session the answer came from (the editor forgets its cache
    when it changes). Compute: one data request per table, in parallel; a miss is retried once with the
    library's own spelling of the name (SAS/ACCESS tables can be case-sensitive). CAS: one columnInfo each."""
    conn = sas_viya.settings(connection)["name"]
    compute, cas = _sessions(connection)
    found: dict[tuple[str, str], tuple[str, list[dict[str, Any]] | None]] = {}
    if language == "cas":
        with cas.lock:
            sid = cas.session()
            for lib, tbl in pairs:
                found[(lib, tbl)] = (tbl, _cas_describe(cas, lib, tbl))
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
def sas_lookup_columns(language: str, tables: list[dict[str, Any]], connection: str | None = None) -> CallToolResult:
    """Columns of up to 8 SAS tables (``[{library, table}]``) for the SQL Editor's autocomplete, in one call:
    SAS libraries for procsql / fedsql, caslibs for cas. A table that doesn't exist comes back with
    ``missing: true`` (not an error), so the editor can remember it briefly and not ask again."""
    if language not in sas_sql.LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(sas_sql.LANGUAGES)}")
    pairs = _lookup_pairs(tables, language)
    if not pairs:
        return tool_result("no tables", {"language": language, "session": None, "tables": []})
    data = lookup_columns(language, pairs, connection)
    hit = sum(1 for t in data["tables"] if not t["missing"])
    return tool_result(f"columns of {hit} of {len(pairs)} table(s)", data)


@apps.tool(resource_uri=SCHEMA_URI, visibility=APP_ONLY, annotations=STATE)
def sas_session_reset(connection: str | None = None) -> CallToolResult:
    """End the Workspace's SAS Compute and CAS sessions of a SAS Viya connection (default: the active one); the
    next run starts new ones (startup code runs again). Runs on it (also one a Stop couldn't end) are stopped."""
    stopped = sas_runs.abort_connection(connection)
    sas_rest.reset(connection)
    return tool_result("SAS sessions ended", {**_status(connection), "stopped_runs": stopped})


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
            pushdown: bool, *, connection: str | None = None, run: sas_runs.Run | None = None,
            tab_id: str | None = None, table_rows: int | None = None) -> dict[str, Any]:
    """Run one statement (or plain SAS code) and gather rows, log and the pushdown verdict.

    ``max_rows``: rows in the answer (the first page); ``table_rows``: rows the result table keeps for paging
    (PROC SQL OUTOBS; default ``max_rows``). ``tab_id``: the tab's own result table. ``run``: Stop / paging."""
    if language not in sas_sql.LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(sas_sql.LANGUAGES)}")
    max_rows = _limit(max_rows)
    cap = max(max_rows, min(int(table_rows), MAX_TABLE_ROWS)) if table_rows else max_rows
    wrapped = sas_sql.wrap(language, sql, library=library, options=options, max_rows=cap, pushdown=pushdown,
                           table=sas_sql.result_table(tab_id))
    plain = wrapped["kind"] == "sas"
    compute, cas = _sessions(connection)
    started = time.perf_counter()
    if language == "cas" and not plain:
        data = _run_cas(cas, wrapped, library, max_rows, pushdown, run)
    else:
        data = _run_compute(compute, wrapped, language if not plain else "procsql", max_rows, pushdown, run)
    data.update(language=language, plain_sas=plain, sql=sql, library=library, code=wrapped["code"], max_rows=max_rows,
                table_rows=cap, connection=_conn_name(connection),
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
    return data


def _stopped(run: sas_runs.Run | None) -> bool:
    return run is not None and run.cancel.is_set()


def _run_compute(compute: sas_rest.Compute, w: dict[str, Any], language: str, max_rows: int, pushdown: bool,
                 run: sas_runs.Run | None = None) -> dict[str, Any]:
    # One run at a time per session, through reading its result table.
    with compute.lock:
        res = compute.run(w["code"], listing=w["kind"] == "sas", ctl=run)
        lines = res["log"]
        counts = sas_sql.log_counts(lines)
        log_text = sas_rest.sas_code_log(lines)
        stopped = res["state"] == "canceled" or _stopped(run)
        failed = not stopped and (res["state"] in ("error", "failed") or counts["errors"] > 0)
        out: dict[str, Any] = {"state": res["state"], "log": log_text[-MAX_LOG_CHARS:], "log_types": _types(lines),
                               "errors": counts["errors"], "warnings": counts["warnings"], "stopped": stopped,
                               "error": sas_sql.mask_secrets(counts["first_error"]) if failed and counts["first_error"] else
                               ("SAS reported an error" if failed else None),
                               "columns": [], "rows": [], "has_result_set": False, "row_count": 0, "truncated": False,
                               "capped": False, "affected_rows": _affected(log_text), "listing": res.get("listing") or None}
        if w["result"] and not failed and not stopped:
            lib, table = w["result"]
            got = compute.rows(lib, table, max_rows)
            total = got["total_rows"]
            capped = bool(w["capped"] and "terminated early due to OUTOBS" in log_text)
            out.update(columns=got["columns"], column_meta=got["column_meta"], rows=jsonable_rows(got["rows"]),
                       has_result_set=True, row_count=len(got["rows"]), total_rows=total, capped=capped,
                       truncated=bool(capped or (total or 0) > len(got["rows"])), affected_rows=None)
            if run is not None:
                sas_runs.remember_table(run, lib, table, {"columns": got["columns"], "column_meta": got["column_meta"],
                                                          "total": total})
        traced, note = None, None
        if language == "fedsql" and not stopped:
            traced, note = _fedsql_trace(compute, pushdown)
        if pushdown:
            out["pushdown"] = _pushdown(language, log_text, traced, note)
    return out


def _fedsql_trace(compute: sas_rest.Compute, read: bool) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The SQL PROC FEDSQL sent through the automatic SingleStore library (its DRIVER_TRACE=SQL file, read into
    WORK and emptied after every FedSQL run). ``read``: return the statements (else only empty the file)."""
    auto = compute.auto_lib or {}
    if not auto.get("assigned"):
        return None, ("FedSQL doesn't write the SQL it sends to the SAS log; the SAS/ACCESS driver trace that does "
                      "is on the automatic SingleStore library, which isn't assigned in this session.")
    try:
        res = compute.run(sas_sql.TRACE_READ)
        if not read or res["state"] in ("error", "failed"):
            return None, None
        rows = compute.rows("WORK", sas_sql.TRACE_TABLE, 2000)["rows"]
    except (SasError, LookupError) as exc:
        return None, f"Couldn't read the driver trace: {str(exc)[:200]}"
    traced = sas_sql.driver_trace_statements([str(r.get("line") or "") for r in rows])
    if not traced:
        return None, (f"The driver trace of the automatic library {auto.get('libref')} has no SQL for this run: the "
                      "query read other SAS/ACCESS libraries (only the automatic one is traced) or no database table.")
    return traced, None


def _run_cas(cas: sas_rest.Cas, w: dict[str, Any], library: str | None, max_rows: int, pushdown: bool,
             run: sas_runs.Run | None = None) -> dict[str, Any]:
    action, params = w["actions"][0]
    with cas.lock:
        cas.ensure_actionset("fedSql")
        cas.set_caslib(library or "CASUSER")  # one-level names: the tab's caslib, else CASUSER
        if _stopped(run):
            raise sas_rest.SasStopped("Stopped before it started.")
        res = cas.action(action, params, check=False)
        stopped = _stopped(run)
        failed = not stopped and str(res["severity"]).lower() == "error"
        log_text = sas_sql.mask_secrets(res["log"])
        errs = [x for x in log_text.splitlines() if x.startswith("ERROR")]
        out: dict[str, Any] = {"state": "canceled" if stopped else "error" if failed else str(res["severity"]).lower(),
                               "log": log_text[-MAX_LOG_CHARS:], "log_types": None, "stopped": stopped,
                               "errors": 0 if stopped else len(errs) or int(failed),
                               "warnings": sum(1 for x in log_text.splitlines() if x.startswith("WARNING")),
                               "error": (errs[0] if errs else res["status"] or "CAS reported an error") if failed else None,
                               "columns": [], "rows": [], "has_result_set": False, "row_count": 0, "truncated": False,
                               "capped": False, "affected_rows": None, "listing": None}
        if w["result"] and not failed and not stopped:
            caslib, table = w["result"]
            page = _cas_page(cas, caslib, table, 0, max_rows)
            info = cas.action("table.tableInfo", {"caslib": caslib, "name": table}, check=False)
            _, irows = sas_rest.cas_table(sas_rest.first_table(info["results"], "TableInfo"))
            total = irows[0].get("Rows") if irows else None
            out.update(columns=page["columns"], column_meta=page["column_meta"], rows=page["rows"], has_result_set=True,
                       row_count=len(page["rows"]), total_rows=total, truncated=bool(total and int(total) > len(page["rows"])))
            if run is not None:
                sas_runs.remember_table(run, caslib, table, {"columns": page["columns"], "column_meta": page["column_meta"],
                                                             "total": total})
    if pushdown:
        out["pushdown"] = _pushdown("cas", log_text)
    return out


def _cas_page(cas: sas_rest.Cas, caslib: str, table: str, offset: int, limit: int) -> dict[str, Any]:
    """Rows ``offset`` … ``offset + limit - 1`` (0-based) of a CAS table (``table.fetch`` from / to are 1-based)."""
    fetch = cas.action("table.fetch", {"table": {"caslib": caslib, "name": table}, "from": int(offset) + 1,
                                       "to": int(offset) + int(limit), "maxRows": int(limit), "index": False})
    cols, rows = sas_rest.cas_table(sas_rest.first_table(fetch["results"], "Fetch"))
    fmts = {c["name"]: c.get("format") for c in cols}
    rows = [{k: sas_sql.sas_value(v, fmts.get(k)) for k, v in r.items()} for r in rows]
    meta = [{"name": c["name"], "type": c.get("type"), "format": c.get("format"), "label": c.get("label"),
             "length": c.get("length")} for c in cols]
    return {"columns": [c["name"] for c in cols], "column_meta": meta, "rows": jsonable_rows(rows)}


def _types(lines: list[dict[str, str]]) -> list[str]:
    """One letter per log line for colouring: e(rror) w(arning) n(ote) s(ource) or "." (other)."""
    out = []
    for x in lines:
        t, line = x.get("type", ""), x.get("line", "")
        out.append("e" if t == "error" or line.startswith("ERROR") else "w" if t == "warning" or line.startswith("WARNING")
                   else "n" if t == "note" or line.startswith("NOTE") else "s" if t == "source" else ".")
    return out


def _pushdown(language: str, log_text: str, traced: list[dict[str, Any]] | None = None,
              note: str | None = None) -> dict[str, Any]:
    v = sas_sql.pushdown_verdict(language, log_text, traced=traced, trace_note=note)
    for s in v["sent"]:
        s["search"] = re.sub(r"\s+", " ", s["sql"])[:80]
    return v


def _record(sql: str, library: str | None, language: str, data: dict[str, Any] | None, source: dict[str, Any],
            kind: str, ms: float, exc: BaseException | None = None, connection: str | None = None) -> None:
    lang = "sas" if sas_sql.is_plain_sas(sql) else language
    ms = round(ms, 1)
    conn = (data or {}).get("connection") or connection
    pushdown = ((data or {}).get("pushdown") or {}).get("verdict")
    if exc is not None or (data and (data.get("error") or data.get("stopped"))):
        err = str(exc) if exc is not None else data.get("error") or "Stopped"
        editor_state.add_history(sql, library, ok=False, kind=kind, error=err, source=source, elapsed_ms=ms, language=lang,
                                 connection=conn)
    else:
        editor_state.add_history(sql, library, ok=True, kind=kind, elapsed_ms=ms, source=source, language=lang,
                                 connection=conn, pushdown=pushdown,
                                 rows=data["row_count"] if data and data.get("has_result_set") else None,
                                 affected=data.get("affected_rows") if data else None)


def _summary(data: dict[str, Any]) -> str:
    if data.get("stopped"):
        return f"Stopped after {data.get('elapsed_ms', 0)} ms"
    if data.get("error"):
        return f"SAS error: {data['error']}"
    if data.get("has_result_set"):
        return f"{data['row_count']} row(s) in {data['elapsed_ms']} ms"
    return f"Ran in {data['elapsed_ms']} ms"


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_query(language: str, sql: str, library: str | None = None, options: dict[str, Any] | None = None,
              max_rows: int = 1000, pushdown: bool = True, tab_id: str | None = None, tab: str | None = None,
              file: str | None = None, history: bool = True, connection: str | None = None, run_id: str | None = None,
              wait_ms: int | None = None, table_rows: int | None = None) -> CallToolResult:
    """Run one statement from the SQL Editor as PROC SQL / FedSQL (SAS libraries) or FedSQL in CAS (caslibs).

    ``language``: procsql, fedsql or cas. The SQL is wrapped (proc sql; … quit; etc.); text starting with
    proc / data / % runs as plain SAS code. Statements that write anywhere but WORK / CASUSER are answered with
    ``needs_confirmation`` (the app asks, then calls sas_execute). ``pushdown`` adds sastrace / the FedSQL plan
    and returns what was sent to SingleStore with a verdict. ``history`` false: not recorded in the editor's
    run history (the Schema Explorer's previews).

    The run goes to the background: ``run_id`` (chosen by the app, so Stop works at once) names it,
    ``wait_ms`` is how long to wait for the result (default: until it's done); a run still going is answered
    with ``running`` and its status (``waiting_for``: the run in another tab it waits for), and the app asks
    again with sas_run_status. ``connection``: the SAS Viya connection (default: the active one).
    ``max_rows``: rows in the answer; ``table_rows``: rows PROC SQL keeps in the result table for paging
    (sas_rows; default ``max_rows``).
    """
    check = sas_sql.needs_confirmation(language, sql, library)
    if check:
        data = {"needs_confirmation": True, **check, "sql": sql, "library": library, "language": language,
                "plain_sas": sas_sql.is_plain_sas(sql)}
        return tool_result(check["reason"] + " and needs confirmation.", data)
    return _run_tool(language, sql, library, options, max_rows, pushdown, {"tab_id": tab_id, "tab": tab, "file": file}, "read",
                     record=history, connection=connection, run_id=run_id, wait_ms=wait_ms, table_rows=table_rows)


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=WRITES)
def sas_execute(language: str, sql: str, library: str | None = None, options: dict[str, Any] | None = None,
                max_rows: int = 1000, pushdown: bool = True, tab_id: str | None = None, tab: str | None = None,
                file: str | None = None, connection: str | None = None, run_id: str | None = None,
                wait_ms: int | None = None, table_rows: int | None = None) -> CallToolResult:
    """Run one SAS statement the user confirmed in the SQL Editor (writes outside WORK / CASUSER included).
    Same background run, Stop and paging as sas_query."""
    return _run_tool(language, sql, library, options, max_rows, pushdown, {"tab_id": tab_id, "tab": tab, "file": file}, "write",
                     connection=connection, run_id=run_id, wait_ms=wait_ms, table_rows=table_rows)


def _run_tool(language: str, sql: str, library: str | None, options: dict[str, Any] | None, max_rows: int,
              pushdown: bool, source: dict[str, Any], kind: str, record: bool = True, connection: str | None = None,
              run_id: str | None = None, wait_ms: int | None = None, table_rows: int | None = None) -> CallToolResult:
    if language not in sas_sql.LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(sas_sql.LANGUAGES)}")
    _sessions(connection)  # an unknown connection fails here, not in the background
    engine = "cas" if language == "cas" and not sas_sql.is_plain_sas(sql) else "compute"

    def work(run: sas_runs.Run) -> dict[str, Any]:
        data = run_sas(language, sql, library, options, max_rows, pushdown, connection=connection, run=run,
                       tab_id=source.get("tab_id"), table_rows=table_rows)
        data["kind"] = kind
        return data

    def finish(run: sas_runs.Run) -> None:
        if record:
            _record(sql, library, language, run.result, source, kind, ((run.ended or time.time()) - run.created) * 1000,
                    run.error, connection=getattr(run, "conn", None))

    run = sas_runs.start(run_id, connection, engine, source, sql, work, finish)
    return _answer(run, wait_ms)


def _answer(run: sas_runs.Run, wait_ms: int | None) -> CallToolResult:
    """The run's result when it's done within ``wait_ms`` (None: wait for it), else its status."""
    run.done.wait(None if wait_ms is None else max(0, int(wait_ms)) / 1000)
    if not run.done.is_set():
        info = run.info()
        w = info.get("waiting_for")
        text = (f"Waiting for the SAS run in {w['tab']}" if w and w.get("tab") else
                "Waiting for the SAS session to start" if w and w.get("warming") else
                "Waiting for the SAS session" if run.state == "queued" else "Running")
        return tool_result(text, info)
    if run.error is not None:
        exc = run.error
        if isinstance(exc, (SasError, LookupError, ValueError)):
            raise exc
        raise SasError(str(exc) or type(exc).__name__) from exc
    data = {**(run.result or {}), "run_id": run.id, "running": False, "run_state": run.state}
    data.setdefault("elapsed_ms", round(((run.ended or time.time()) - run.created) * 1000, 1))
    data.setdefault("connection", run.conn)
    return tool_result(_summary(data), data)


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_run_status(run_id: str, wait_ms: int = 0) -> CallToolResult:
    """A background SAS run (sas_query / sas_execute): its result once it's done (waits up to ``wait_ms``, at
    most 25 s), else ``running`` with its state, how long it has run and, while it waits for the session,
    ``waiting_for`` (the run in another tab). After Stop: ``stopping``, and ``cancel_failed`` when Viya didn't
    end it (offer Restart SAS session)."""
    return _answer(sas_runs.get(run_id), min(max(0, int(wait_ms)), 25_000))


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_stop(run_id: str) -> CallToolResult:
    """Stop a SAS run from the SQL Editor: a waiting run doesn't start; a running one is cancelled on Viya (Compute:
    the job's state set to canceled; CAS: the session's running action cancelled). The SAS session keeps running.
    The run then ends "stopped" with the log so far (sas_run_status)."""
    data = sas_runs.stop(run_id)
    return tool_result("Stopping." if data.get("stopped") else data.get("note", "Nothing to stop."), data)


@apps.tool(resource_uri=EDITOR_URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_rows(run_id: str, offset: int = 0, limit: int = 5000) -> CallToolResult:
    """More rows of a SAS run's result (its result table in the SAS / CAS session), from ``offset``. Works until the
    same tab runs something else (its result table is then replaced). Waits up to 15 s for a busy session."""
    run = sas_runs.get(run_id)
    tbl = sas_runs.table_of(run)
    offset, limit = max(0, int(offset)), max(1, min(int(limit), result_stream.MAX_PAGE_ROWS))
    compute, cas = sas_rest.sessions(run.conn)
    sess = cas if run.engine == "cas" else compute
    if not sess.lock.acquire(timeout=15):
        cur = getattr(sess, "current_run", None)
        who = f" with the run in {cur.label}" if isinstance(cur, sas_runs.Run) else ""
        raise SasError(f"The SAS session is busy{who}; more rows load when it has finished.")
    try:
        if run.engine == "cas":
            page = _cas_page(cas, tbl["lib"], tbl["table"], offset, limit)
            rows = page["rows"]
        else:
            got = compute.rows(tbl["lib"], tbl["table"], limit, start=offset)
            rows = jsonable_rows(got["rows"])
    finally:
        sess.lock.release()
    total = tbl.get("total")
    data = {"run_id": run.id, "offset": offset, "rows": rows, "total_rows": total, "columns": tbl.get("columns"),
            "done": True, "more": bool(total is not None and offset + len(rows) < int(total))}
    return tool_result(f"{len(rows)} row(s) from {offset}", data)


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
    """Is this SQL (sent to SingleStore by SAS) in the cluster's plan cache or the local query history?

    ``plan``: the plan cache entry as a Query History plan-cache row (``key`` "plan:…", totals, parameterized
    text), so the editor can open Query History on it when the statement isn't in its list (runs under the
    trace threshold of 1 s only show in the plan cache)."""
    from .. import history_store

    found: dict[str, Any] = {"plan_cache": None, "history": 0, "plan": None}
    key = history_store.statement_key(sql, database)
    key_any = history_store.statement_key(sql, None)
    try:
        hits = [p for p in history_store.read_plancache()
                if p["stmt"] in (key, key_any) or history_store.statement_key(p["text"], None, parameterized=True) == key_any]
        if hits:
            p = max(hits, key=lambda x: x["last_executed"] or "")
            found["plan_cache"] = {"runs": sum(x["runs"] for x in hits), "avg_ms": p["avg_ms"],
                                   "last_executed": p["last_executed"], "database": p["database"]}
            found["plan"] = plan_row(hits)
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


def plan_row(plans: list[dict[str, Any]]) -> dict[str, Any]:
    """Plan cache entries of one statement (``history_store.read_plancache``, one per aggregator) as one Query
    History plan-cache row (same keys as its list rows)."""
    p0 = plans[0]
    g: dict[str, Any] = {"key": f"plan:{p0['stmt']}", "stmt": p0["stmt"], "database": p0.get("database"),
                         "sql": " ".join((p0.get("text") or "").split())[:2000],
                         "sql_truncated": (p0.get("text_len") or 0) > 2000, "origin": p0.get("origin"),
                         "tables": p0.get("tables") or [], "plan_keys": [p["plan_key"] for p in plans],
                         "nodes": sorted({p["node_id"] for p in plans}), "live": True, "pinned": True,
                         "warnings": next((p.get("warnings") for p in plans if p.get("warnings")), None)}
    for k in ("runs", "failed", "rows", "exec_ms", "cpu_ms", "blob_wait_ms", "queued_ms"):
        g[k] = sum(p.get(k) or 0 for p in plans)
    for k in ("avg_max_mem", "avg_spill", "avg_blob_miss"):
        g[k] = max(p.get(k) or 0 for p in plans)
    g["first_load"] = min((p["first_load"] for p in plans if p.get("first_load")), default=None)
    g["last_executed"] = max((p["last_executed"] for p in plans if p.get("last_executed")), default=None)
    g["avg_ms"] = round(g["exec_ms"] / g["runs"], 1) if g["runs"] else 0.0
    return g
