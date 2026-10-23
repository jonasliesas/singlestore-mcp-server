"""SAS in the SQL Editor: wrapping SQL as SAS code, telling writes from reads, and reading pushdown traces.

Pure functions (no Viya access), so they're unit-tested offline. ``sas_rest`` runs what they produce.

Languages ("Run as" in the SQL Editor):

- ``procsql``  PROC SQL on SAS libraries (Viya Compute): ``proc sql; <sql>; quit;``
- ``fedsql``   PROC FEDSQL on SAS libraries (Viya Compute): ``proc fedsql; <sql>; quit;``
- ``cas``      FedSQL in CAS on caslibs: the ``fedSql.execDirect`` action
- text that starts with ``proc`` / ``data`` / ``%`` / ``libname`` … runs as plain SAS code, unwrapped.

A SELECT is wrapped into ``CREATE TABLE <work>._S2RES AS <select>`` so its rows are fetched from that table
(few rows over the wire, SAS formats kept); everything else runs as written.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

LANGUAGES = ("procsql", "fedsql", "cas")
RESULT_TABLE = "_S2RES"


def result_table(tab_id: str | None) -> str:
    """The result table of one SQL Editor tab (``_S2R_`` + 8 hex of its id): a tab's result can still be paged
    while another tab runs; the tab's next run replaces it. Without a tab: ``_S2RES``."""
    if not tab_id:
        return RESULT_TABLE
    return "_S2R_" + hashlib.sha1(str(tab_id).encode()).hexdigest()[:8].upper()


SAS_STEP = re.compile(
    r"^(proc|data|%|libname|filename|options|title\d?|footnote\d?|ods|run|quit|caslib|cas|endsas)\b|^%", re.I)
_LEADING = re.compile(r"^(\s+|/\*.*?\*/|--[^\n]*(\n|$))*", re.S)
_NAME = r'(?:"(?:[^"]|"")+"n?|\'(?:[^\']|\'\')+\'n|[A-Za-z_][\w$]*(?:\([^)]*\))?)'
_QUALIFIED = re.compile(rf"({_NAME})(?:\s*\.\s*({_NAME}))?(?:\s*\.\s*({_NAME}))?", re.S)
SAFE_COMPUTE = {"WORK"}
SAS_ENGINES_SINGLESTORE = {"S2", "SSTORE", "SINGLESTORE"}
# Caslib types that read SingleStore ("is SingleStore"); which kind they are: ``cas_singlestore_kind``.
CAS_TYPES_SINGLESTORE = {"singlestore", "singlestore_standard", "sstore"}
SAS_ENGINES_REMOTE = {"REMOTE", "REMOTE9"}  # SAS/CONNECT remote library services (libname … server=)


class SasError(ValueError):
    """A SAS-side failure shown to the user as is (SAS errors, Viya answers)."""


# ------------------------------------------------------------------ text helpers


def strip_leading_comments(sql: str) -> str:
    return _LEADING.sub("", sql or "")


def first_word(sql: str) -> str:
    m = re.match(r"%?[A-Za-z_]+", strip_leading_comments(sql))
    return m.group(0).lower() if m else ""


def is_plain_sas(sql: str) -> bool:
    """Text that starts with ``proc`` / ``data`` / ``%`` / ``libname`` … is SAS code, not SQL."""
    return bool(SAS_STEP.match(strip_leading_comments(sql)))


def split_statements(text: str) -> list[str]:
    """Split SAS-flavoured SQL on ``;`` outside quotes and ``/* */`` comments (no ``#`` or backtick quoting)."""
    out, start, i, quote = [], 0, 0, None
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:  # '' or "" inside a quoted string
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 1
        elif ch == "-" and text.startswith("--", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif ch == ";":
            out.append(text[start:i])
            start = i + 1
        i += 1
    out.append(text[start:])
    return [s.strip() for s in out if strip_leading_comments(s).strip()]


def _unquote(name: str) -> str:
    name = name.strip()
    if name.endswith("n") and name[:1] in "'\"" and len(name) > 2:
        name = name[:-1]
    if name[:1] in "'\"" and name.endswith(name[0]):
        return name[1:-1].replace(name[0] * 2, name[0])
    return name


def _quote_sas_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_fedsql(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ------------------------------------------------------------------ writes


_SQL_WRITE = re.compile(
    r"^\s*(?:create\s+(?:or\s+replace\s+)?(?:unique\s+)?(?:table|view|index)\s+(?:if\s+not\s+exists\s+)?"
    r"(?:\w+\s+on\s+)?|insert\s+into\s+|update\s+|delete\s+from\s+|drop\s+(?:table|view|index)\s+(?:if\s+exists\s+)?"
    r"|alter\s+table\s+)", re.I)
_SQL_READ = {"select", "describe", "validate", "explain", "with", "reset", "show"}


def _target_lib(rest: str, default_lib: str) -> str:
    m = _QUALIFIED.match(rest)
    if not m:
        return "?"
    parts = [p for p in m.groups() if p]
    return _unquote(parts[0]).upper() if len(parts) >= 2 else default_lib.upper()


def sql_targets(sql: str, default_lib: str) -> list[str] | None:
    """Libraries / caslibs one SQL statement writes to (None: it only reads; ["?"]: can't tell)."""
    text = strip_leading_comments(sql)
    word = first_word(text)
    if word in _SQL_READ:
        return None
    m = _SQL_WRITE.match(text)
    if not m:
        return ["?"]
    rest = text[m.end():]
    if text.lower().startswith("drop"):
        libs = []
        for part in re.split(r",", rest.split(";")[0]):
            if part.strip():
                libs.append(_target_lib(part.strip(), default_lib))
        return libs or ["?"]
    if re.match(r"create\s+(?:unique\s+)?index", text, re.I):
        on = re.search(r"\bon\s+(.*)$", rest, re.I | re.S)
        return [_target_lib(on.group(1), default_lib)] if on else ["?"]
    return [_target_lib(rest, default_lib)]


_DATA_STEP = re.compile(r"(?:^|;)\s*data\s+([^;]*);", re.I)
_OUT_OPT = re.compile(r"\b(?:out|base|outlib|outdata|outtable|outstat|outest|outfile)\s*=\s*(" + _NAME + r"(?:\s*\.\s*" + _NAME + r")?)", re.I)
_PROC_SQL = re.compile(r"\bproc\s+(?:sql|fedsql)\b([^;]*);(.*?)(?:\bquit\s*;|\Z)", re.I | re.S)
_DANGER = re.compile(
    r"(?:^|;)\s*(?:x\s|systask\b|caslib\b|cas\s+\w+\s+(?:terminate|drop)|libname\s+\w+\s+clear)|\bcall\s+system\b|%sysexec\b"
    r"|\bfilename\s+\w+\s+pipe\b|\bproc\s+(?:datasets|delete|casutil|cas|append|copy|upload|download|s3|hadoop)\b"
    r"|\bdropTable\b|\bdeleteSource\b|\bpromote\b|\bsave\b\s*/", re.I)


def _strip_sas_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    return re.sub(r"(?m)(^|;)\s*%?\*[^;]*;", r"\1", code)


def sas_targets(code: str) -> list[str]:
    """Libraries plain SAS code writes to, "?" for anything that can't be told (or could change more than data)."""
    code = _strip_sas_comments(code)
    libs: list[str] = []
    if _DANGER.search(code):
        libs.append("?")
    for m in _DATA_STEP.finditer(code):
        spec = re.sub(r"\([^)]*\)", " ", m.group(1))
        for name in spec.split():
            if name.lower() in ("_null_", "_last_", "_data_"):
                continue
            libs.append(name.split(".", 1)[0].upper() if "." in name else "WORK")
    for m in _OUT_OPT.finditer(code):
        name = m.group(1)
        libs.append(_unquote(name.split(".", 1)[0]).upper() if "." in name else "WORK")
    for m in _PROC_SQL.finditer(code):
        for stmt in split_statements(m.group(2)):
            t = sql_targets(stmt, "WORK")
            libs.extend(t or [])
    return sorted(set(libs))


def needs_confirmation(language: str, sql: str, library: str | None, user: str | None = None) -> dict[str, Any] | None:
    """None when the statement may run straight away (reads, writes to WORK / CASUSER), else why it needs a yes.

    ``shared``: a write to a caslib other than CASUSER (others' data: the app shows an extra warning)."""
    if is_plain_sas(sql):
        libs = sas_targets(sql)
        safe = SAFE_COMPUTE
    elif language == "cas":
        libs = sql_targets(sql, library or "CASUSER")
        safe = None
    else:
        default = (library or "WORK") if language == "procsql" else "WORK"
        libs = sql_targets(sql, default)
        safe = SAFE_COMPUTE
    if not libs:
        return None
    if safe is None:
        risky = [lib for lib in libs if not is_casuser(lib, user)]
    else:
        risky = [lib for lib in libs if lib.upper() not in safe]
    if not risky:
        return None
    unknown = "?" in risky
    named = [lib for lib in risky if lib != "?"]
    if unknown and not named:
        reason = ("This SAS code can change data or settings beyond WORK" if is_plain_sas(sql)
                  else "This statement can change data or schema")
    else:
        reason = f"This writes to {', '.join(named)}"
    return {"libraries": named, "unknown": unknown,
            "shared": language == "cas" and not is_plain_sas(sql) and bool(named), "reason": reason}


def is_casuser(caslib: str, user: str | None = None) -> bool:
    c = caslib.strip().upper()
    return c == "CASUSER" or c.startswith("CASUSER(")


# ------------------------------------------------------------------ wrapping


def proc_sql_options(options: dict[str, Any] | None, max_rows: int) -> tuple[str, bool]:
    """PROC SQL statement options and whether OUTOBS came from the row cap (so hitting it means "truncated")."""
    o = options or {}
    parts = []
    outobs = o.get("outobs")
    capped = False
    try:
        outobs = int(outobs) if outobs not in (None, "") else None
    except (TypeError, ValueError):
        outobs = None
    if outobs is None or outobs > max_rows:
        outobs, capped = max_rows, True
    parts.append(f"outobs={outobs}")
    if o.get("noexec"):
        parts.append("noexec")
    if o.get("feedback"):
        parts.append("feedback")
    return " ".join(parts), capped


def wrap(language: str, sql: str, *, library: str | None = None, options: dict[str, Any] | None = None,
         max_rows: int = 1000, pushdown: bool = False, table: str = RESULT_TABLE) -> dict[str, Any]:
    """The SAS code (or CAS action) that runs one statement.

    ``max_rows``: PROC SQL's row cap (OUTOBS) for the result table; ``table``: the result table's name.
    Returns ``{"code", "kind": "select" | "statement" | "sas", "result": (lib, table) | None, "capped": bool}``.
    For ``cas``: ``{"actions": [(action, params), …], "code": <the same as PROC CAS, for the log tab>}``."""
    body = sql.strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    if not body:
        raise ValueError("There's no SQL to run.")
    if is_plain_sas(sql):
        code = sql.strip()
        if pushdown:
            code = f"{TRACE_ON}\n{code}\n{TRACE_OFF}"
        return {"code": code, "kind": "sas", "result": None, "capped": False}
    select = first_word(body) == "select" and not re.search(r"\binto\s*:", body, re.I)
    noexec = bool((options or {}).get("noexec"))
    if language == "procsql":
        opts, capped = proc_sql_options(options, max_rows)
        lines = []
        if pushdown:
            lines.append(TRACE_ON)
        lib = (library or "").strip().upper()
        if lib and lib != "WORK":
            lines.append(f"options user={lib};")
        lines.append(f"proc sql {opts};")
        if select and not noexec:
            lines.append(f"  create table WORK.{table} as")
            lines.append(_indent(body) + ";")
        else:
            lines.append(_indent(body) + ";")
        lines.append("quit;")
        if lib and lib != "WORK":
            lines.append("options user=work;")
        if pushdown:
            lines.append(TRACE_OFF)
        return {"code": "\n".join(lines), "kind": "select" if select and not noexec else "statement",
                "result": ("WORK", table) if select and not noexec else None, "capped": capped}
    if language == "fedsql":
        head = "proc fedsql _method;" if pushdown else "proc fedsql;"
        lines = [head]
        if select:
            lines.append(f'  drop table WORK."{table}" force;')
            lines.append(f'  create table WORK."{table}" as')
        lines.append(_indent(body) + ";")
        lines.append("quit;")
        return {"code": "\n".join(lines), "kind": "select" if select else "statement",
                "result": ("WORK", table) if select else None, "capped": False}
    if language == "cas":
        query = (f"create table CASUSER.{_quote_fedsql(table)} {{options replace=true}} as {body}"
                 if select else body)
        params: dict[str, Any] = {"query": query, "method": bool(pushdown)}
        actions = [("fedSql.execDirect", params)]
        code = ("proc cas;\n"
                + f"  sessionProp.setSessOpt / caslib={_quote_sas_string(library or 'CASUSER')};\n"
                + f"  fedSql.execDirect / method={'true' if pushdown else 'false'}\n"
                + f"    query={_quote_sas_string(query)};\nquit;")
        return {"code": code, "actions": actions, "kind": "select" if select else "statement",
                "result": ("CASUSER", table) if select else None, "capped": False}
    raise ValueError(f"Run as must be one of {', '.join(LANGUAGES)}")


def _indent(sql: str) -> str:
    return "\n".join("  " + line if line.strip() else line for line in sql.splitlines())


TRACE_ON = "options sastrace=',,,d' sastraceloc=saslog nostsuffix sql_ip_trace=(note, source);"

# FedSQL doesn't write SASTRACE output: the SQL it sends to SingleStore comes from the SAS/ACCESS driver trace
# (DRIVER_TRACE=SQL, a LIBNAME option for PROC FEDSQL / PROC DS2), which only the automatic library has. The file
# is read into a WORK table after a FedSQL run (not through the log: long lines would wrap) and then deleted.
TRACE_FILE = "%sysfunc(pathname(work))/_s2trace.log"
TRACE_TABLE = "_S2TRC"
TRACE_READ = f"""%macro _s2trc;
%local _p; %let _p={TRACE_FILE};
%if %sysfunc(fileexist(&_p)) %then %do;
data work.{TRACE_TABLE}; infile "&_p" lrecl=32767 truncover; input line $char32767.; if line =: 'DEFAULT:'; run;
data _null_; rc=filename('_s2tf', "&_p"); rc=fdelete('_s2tf'); rc=filename('_s2tf'); run;
%end;
%else %do; data work.{TRACE_TABLE}; length line $1; stop; run; %end;
%mend _s2trc;
options nonotes nosource;
%_s2trc
%sysmacdelete _s2trc;
options notes source;"""


def driver_trace_statements(lines: list[str]) -> list[dict[str, Any]]:
    """The statements in a DRIVER_TRACE=SQL file (``DEFAULT: <sql>`` lines: the SQL as sent, names in their own
    case), without the ``WHERE 1=0`` metadata probes. One entry per distinct statement, in order."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        m = re.match(r"^\s*DEFAULT:\s*(.*\S)\s*$", str(line or ""))
        if not m:
            continue
        sql = m.group(1)
        if re.search(r"\bwhere\s+(1\s*=\s*0|0\s*=\s*1)\s*$", sql, re.I) or any(p.search(sql) for p in _PROBES):
            continue
        if sql in seen:
            continue
        seen.add(sql)
        out.append({"id": f"fedsql_{len(out) + 1}", "engine": "SSTORE", "sql": sql})
    return out
TRACE_OFF = "options sastrace=off sql_ip_trace=none;"


# ------------------------------------------------------------------ log


def log_counts(lines: list[dict[str, str]]) -> dict[str, Any]:
    errors = [x["line"] for x in lines if x.get("type") == "error" or x.get("line", "").startswith("ERROR")]
    warnings = [x["line"] for x in lines if x.get("type") == "warning" or x.get("line", "").startswith("WARNING")]
    return {"errors": len(errors), "warnings": len(warnings), "first_error": errors[0] if errors else None}


_SECRETS = re.compile(r"((?:password|passwd|pwd|pw|authtoken|token|secret)\s*=\s*)(\"[^\"]*\"|'[^']*'|\{[^}]*\}|[^\s;)]+)", re.I)


def mask_secrets(text: str) -> str:
    """Hide password= / pwd= / token= values (SAS masks most itself; this is for anything that slips through)."""
    return _SECRETS.sub(lambda m: m.group(1) + ("{*}" if m.group(2).startswith("{") else "********"), text)


# ------------------------------------------------------------------ pushdown


_TRACE_HEAD = re.compile(r"^\s*([A-Z][A-Z0-9]*)_(\d+):\s+(Prepared|Executed):\s+on\s+(connection|thread)\s+(\d+)\s*$")
_PROBES = (re.compile(r"\bwhere\s+0\s*=\s*1\b", re.I), re.compile(r"\binformation_schema\b", re.I),
           re.compile(r"^\s*DROP\s+RESULT\s+TABLE\b", re.I), re.compile(r"^\s*(COMMIT|ROLLBACK|SET\s)", re.I))
_STMT_REF = re.compile(r"^\s*SELECT statement ([A-Z][A-Z0-9]*_\d+)\s*$", re.I)
_PARTITION_READ = re.compile(r"select \* from :: (\S+) where \(partition_id\(\) = \d+\)", re.I)


def trace_statements(log_text: str) -> list[dict[str, Any]]:
    """The statements SAS/ACCESS sent to the database, from a ``sastrace=',,,d'`` log.

    Metadata probes (``WHERE 0=1``, information_schema, DROP RESULT TABLE) and statements that were only
    prepared to describe a table are left out; the parallel reads of a result table are counted on it."""
    blocks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in log_text.splitlines():
        m = _TRACE_HEAD.match(raw)
        if m:
            cur = {"id": f"{m.group(1)}_{m.group(2)}", "engine": m.group(1), "action": m.group(3).lower(),
                   "on": f"{m.group(4)} {m.group(5)}", "lines": []}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        if not raw.strip():
            if cur["lines"]:
                cur = None
            continue
        if re.match(r"^(NOTE|WARNING|ERROR|SQL_IP_TRACE|ACCESS ENGINE)\b|^\d+\s|^\s*\d+ !", raw):
            cur = None
            continue
        cur["lines"].append(raw.rstrip())
    executed_refs = set()
    for b in blocks:
        b["sql"] = " ".join(x.strip() for x in b["lines"]).strip()
        ref = _STMT_REF.match(b["sql"])
        if ref and b["action"] == "executed":
            executed_refs.add(ref.group(1).upper())
    out: list[dict[str, Any]] = []
    parallel: dict[str, int] = {}
    for b in blocks:
        sql = b["sql"]
        if not sql or _STMT_REF.match(sql):
            continue
        pm = _PARTITION_READ.search(sql)
        if pm:
            parallel[pm.group(1)] = parallel.get(pm.group(1), 0) + 1
            continue
        if any(p.search(sql) for p in _PROBES):
            continue
        if b["action"] == "prepared" and b["id"].upper() not in executed_refs:
            continue
        out.append({"id": b["id"], "engine": b["engine"], "sql": sql})
    for s in out:
        m = re.match(r"CREATE RESULT TABLE (\S+) AS", s["sql"], re.I)
        if m and m.group(1) in parallel:
            s["parallel_reads"] = parallel[m.group(1)]
    return out


def _is_pass_all(sql: str) -> bool:
    """A plain ``SELECT cols FROM t`` (no WHERE / GROUP BY / JOIN …): the whole table goes to SAS."""
    s = re.sub(r"^CREATE RESULT TABLE \S+ AS\s+", "", sql.strip(), flags=re.I)
    return bool(re.match(r"(?is)^select\s+.*?\s+from\s+[`\w.]+\s*;?\s*$", s)) and not re.search(
        r"(?i)\b(where|group\s+by|having|join|limit|order\s+by|count|sum|avg|min|max|distinct)\b", s)


def pushdown_verdict(language: str, log_text: str, *, singlestore: bool | None = None,
                     traced: list[dict[str, Any]] | None = None, trace_note: str | None = None) -> dict[str, Any]:
    """What reached SingleStore and how much of the work it did.

    ``verdict``: ``full`` (pushed down fully), ``partly``, ``pulled`` (rows pulled into SAS / CAS and processed
    there) or ``none`` (no SingleStore table involved / nothing traced). ``sent``: the SQL SingleStore got."""
    notes: list[str] = []
    if language == "cas":
        return _cas_verdict(log_text)
    if language == "fedsql":
        return _fedsql_verdict(log_text, traced, trace_note)
    sent = trace_statements(log_text)
    ip = re.findall(r"SQL_IP_TRACE:\s*(.*)", log_text)
    passed = re.search(r"ACCESS ENGINE:\s+SQL statement was passed to the DBMS", log_text)
    verdict = None
    for msg in ip:
        low = msg.lower()
        if "none of the sql" in low:
            verdict = verdict or "pulled"
            notes.append(msg.strip())
        elif "some of the sql" in low:
            verdict = "partly"
            notes.append(msg.strip())
        elif "passed to the dbms" in low or "all of the sql" in low:
            verdict = verdict or "full"
            notes.append(msg.strip())
    if passed:
        notes.append("SAS/ACCESS passed the whole query to the database.")
        verdict = verdict or "full"
    if verdict is None:
        if not sent:
            return {"verdict": "none", "sent": [], "notes": ["No SQL was sent to SingleStore (no SingleStore library in the query, or nothing ran)."]}
        verdict = "pulled" if all(_is_pass_all(s["sql"]) for s in sent) else "partly"
    if verdict == "pulled" and sent and not all(_is_pass_all(s["sql"]) for s in sent):
        verdict = "partly"  # WHERE clauses / column lists went down, the rest ran in SAS
    return {"verdict": verdict, "sent": sent, "notes": notes}


_SAS_LIBS = {"WORK", "SASHELP", "SASUSER", "MAPS", "MAPSSAS", "MAPSGFK", "CASUSER"}


def _fedsql_verdict(log_text: str, traced: list[dict[str, Any]] | None = None,
                    trace_note: str | None = None) -> dict[str, Any]:
    """The verdict from the FedSQL plan (_method); the SQL text from the driver trace (``traced``) when there is
    one, else a placeholder per pushed subquery."""
    scans = [x.strip() for x in re.findall(r"SeqScan from ([^\n]+)", log_text)]
    pushed = [s for s in scans if "{Push Down}" in s]
    other = [s for s in scans if "{Push Down}" not in s and s.split(".")[0].upper() not in _SAS_LIBS]
    if traced:
        sent = traced
        notes = ["SQL text from the SAS/ACCESS driver trace (DRIVER_TRACE=SQL) of the automatic SingleStore library; "
                 "the verdict from the FedSQL query plan (_method)."]
    else:
        sent = [{"id": f"plan_{i + 1}", "engine": "FEDSQL", "sql": f"(FedSQL pushed a subquery to {s.split('.')[0]})"}
                for i, s in enumerate(pushed)]
        notes = [trace_note or ("From the FedSQL query plan (_method): FedSQL doesn't write the SQL it sends to the SAS "
                                "log; the driver trace that does is only on the automatic SingleStore library.")]
    if not pushed and not other:
        if traced:
            verdict = "pulled" if all(_is_pass_all(s["sql"]) for s in traced) else "full"
            return {"verdict": verdict, "sent": sent, "notes": notes}
        return {"verdict": "none", "sent": [], "notes": ["No database table in the FedSQL query plan (or nothing ran)."]}
    if pushed and not other:
        verdict = "full"
    elif pushed:
        verdict = "partly"
    else:
        verdict = "pulled"
        notes.append("Each table was read into SAS: " + ", ".join(other))
    return {"verdict": verdict, "sent": sent, "notes": notes}


def _cas_verdict(log_text: str) -> dict[str, Any]:
    sent = []
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("Offloaded SQL statement"):
            buf = []
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if set(s) <= {"-"} and not buf:
                    continue
                if not s:
                    if buf:
                        break
                    continue
                if s.startswith("CAS output table") or s.startswith("NOTE"):
                    break
                buf.append(s)
            if buf:
                sent.append({"id": f"offload_{len(sent) + 1}", "engine": "CAS", "sql": " ".join(buf)})
    full = re.search(r"fully offloaded", log_text, re.I)
    partial = re.search(r"partially offloaded|was partially", log_text, re.I)
    loaded = re.findall(r"LoadTable action using SAS Data Connector to (\w+)", log_text)
    notes = [m.strip() for m in re.findall(r"NOTE:\s*(The SQL statement was[^\n]*)", log_text)]
    if full:
        verdict = "full"
    elif sent or partial:
        verdict = "partly"
    elif loaded:
        verdict = "pulled"
        notes.append("CAS loaded the table(s) from the data source and ran the query itself.")
    else:
        return {"verdict": "none", "sent": [], "notes": ["Nothing was offloaded or loaded from a database (CAS in-memory tables only?)."]}
    if loaded and verdict != "pulled":
        notes.append(f"CAS also loaded {len(loaded)} table(s) from the data source itself.")
        verdict = "partly" if verdict == "full" else verdict
    return {"verdict": verdict, "sent": sent, "notes": notes}


# ------------------------------------------------------------------ values


_DATE_FMT = re.compile(r"^(DATE|YYMMDD|MMDDYY|DDMMYY|E8601DA|YYMMDDN|NLDATE|WEEKDATE|WORDDATE|MONYY|YYMON|YYQ|YEAR|DAY|MONTH|JULIAN|MINGUO|NENGO|B8601DA)", re.I)
_DATETIME_FMT = re.compile(r"^(DATETIME|E8601DT|B8601DT|NLDATM|DATEAMPM|MDYAMPM|IS8601DT|E8601DZ)", re.I)
_TIME_FMT = re.compile(r"^(TIME|HHMM|TOD|E8601TM|B8601TM|NLTIME|HOUR|MMSS)", re.I)


def sas_value(value: Any, fmt: str | None) -> Any:
    """A SAS numeric with a date / datetime / time format as ISO text; everything else unchanged."""
    if value is None or not fmt or not isinstance(value, (int, float)):
        return value
    import datetime as dt

    name = re.sub(r"[\d.]+$", "", fmt)
    try:
        if _DATETIME_FMT.match(name):
            return (dt.datetime(1960, 1, 1) + dt.timedelta(seconds=float(value))).isoformat(sep=" ")
        if _DATE_FMT.match(name):
            return (dt.date(1960, 1, 1) + dt.timedelta(days=int(value))).isoformat()
        if _TIME_FMT.match(name):
            secs = int(value)
            return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
    except (OverflowError, ValueError):
        return value
    return value


# ------------------------------------------------------------------ SingleStore kinds and badges


def cas_singlestore_kind(cas_type: str | None) -> str | None:
    """Which SingleStore integration a caslib type is: ``speedystore`` (type ``SingleStore``: SAS SpeedyStore,
    queries pushed down live, no in-memory copy), ``standard`` (``singlestore_standard``: the standard data
    connector, tables loaded into CAS memory) or None (not SingleStore)."""
    t = str(cas_type or "").strip().lower()
    if t == "singlestore":
        return "speedystore"
    if t in CAS_TYPES_SINGLESTORE:
        return "standard"
    return None


def singlestore_badge(kind: str | None, database: str | None = None, server: str | None = None,
                      auto: str | None = None) -> dict[str, Any]:
    """The tree badge for a SingleStore library / caslib: ``SS`` (SpeedyStore) or ``S2`` (standard caslib,
    SAS/ACCESS libname) with a tooltip naming the kind and the SingleStore database. ``auto``: the SingleStore
    connection an automatic libname came from."""
    if not kind:
        return {"badge": None, "badge_style": None, "badge_title": None}
    where = f"SingleStore database {database or '?'}" + (f" on {server}" if server else "")
    if kind == "speedystore":
        return {"badge": "SS", "badge_style": "ss",
                "badge_title": f"SAS SpeedyStore integration: CAS pushes queries down to SingleStore live (no in-memory copy). {where}"}
    if kind == "standard":
        return {"badge": "S2", "badge_style": "s2-std",
                "badge_title": f"Standard SingleStore data connector: CAS loads the tables into its memory. {where}"}
    title = f"SAS/ACCESS to SingleStore (engine SSTORE). {where}"
    if auto:
        title += f". Assigned automatically from the active SingleStore connection {auto}"
    return {"badge": "S2", "badge_style": "s2", "badge_title": title}


# ------------------------------------------------------------------ code reconstruction ("Show code")

_SECRET_OPTION = re.compile(r"pass|pwd|^pw$|token|secret|cred|authdomain|^key$|apikey|accesskey", re.I)
RECONSTRUCTED = "Reconstructed from the library's current settings; passwords and tokens are placeholders."


def is_secret_option(name: str) -> bool:
    """A data-source / libname option whose value is never shown (passwords, tokens, authentication domains)."""
    return bool(_SECRET_OPTION.search(str(name or "")))


def placeholder(option: str) -> str:
    low = str(option).lower()
    if "authdomain" in low:
        return "<authentication domain>"
    if "token" in low:
        return "<set token>"
    return "<set password>"


def sas_string(value: Any) -> str:
    """A double-quoted SAS string literal (no macro resolution issues for typical values)."""
    return '"' + str(value).replace('"', '""') + '"'


def _engine_word(engine: str) -> str:
    e = engine.upper()
    return "" if e in ("V9", "BASE", "V8", "V7") else engine.lower()


def libname_code(lib: dict[str, Any]) -> str:
    """The LIBNAME statement that would assign library ``lib`` (a ``sas_rest.library_rows`` entry) again.
    Passwords are never known: ``password="<set password>"``."""
    name = str(lib.get("name") or "LIB")
    engine = str(lib.get("engine") or "").strip()
    eng = engine.upper()
    path = lib.get("path")
    head = f"/* {RECONSTRUCTED} */\n"
    if eng in SAS_ENGINES_SINGLESTORE:
        opts = [f"server={sas_string(lib.get('server') or path or '<host>')}"]
        if lib.get("port"):
            opts.append(f"port={int(lib['port'])}")
        opts.append(f"user={sas_string(lib.get('user') or '<user>')}")
        opts.append(f'password="{placeholder("password")}"')
        if lib.get("database"):
            opts.append(f"database={sas_string(lib['database'])}")
        note = ""
        if lib.get("auto"):
            note = (f"/* Assigned automatically from the active SingleStore connection {lib.get('connection') or ''}"
                    " (Connections → SAS Viya → Automatic SingleStore library). */\n")
        elif not lib.get("port"):
            note = "/* SAS doesn't report the port: add port= if it isn't 3306. */\n"
        return head + note + f"libname {name} sstore\n  " + "\n  ".join(opts) + ";"
    if eng in SAS_ENGINES_REMOTE:
        server = lib.get("remote_server") or "<server>"
        slib = lib.get("remote_libref") or name
        return (head + f"/* SAS/CONNECT remote library services: needs a signon to {server} first. */\n"
                + f"libname {name} server={server} slibref={slib};")
    if not engine:
        return head + f"/* SAS reports no engine for {name}. */\nlibname {name} list;"
    word = _engine_word(engine)
    target = ""
    if path:
        target = str(path) if str(path).startswith("(") else sas_string(path)
    stmt = f"libname {name}{' ' + word if word else ''}{' ' + target if target else ''};"
    extra = "" if word == "" else "/* Engine options other than the path aren't reported by SAS and aren't shown. */\n"
    return head + extra + stmt


def _caslib_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_]\w*", name) else sas_string(name)


def _cas_value(opt: dict[str, Any]) -> str:
    if is_secret_option(opt["name"]):
        return f'"{placeholder(opt["name"])}"'
    v = str(opt.get("value") or "")
    if opt.get("quoted") or not re.fullmatch(r"-?\d+(\.\d+)?|true|false", v, re.I):
        return sas_string(v)
    return v.lower() if v.lower() in ("true", "false") else v


def caslib_code(lib: dict[str, Any]) -> dict[str, str]:
    """The CASLIB statement and the ``table.addCaslib`` action (PROC CAS) for caslib ``lib`` (a ``caslib_rows``
    entry with ``datasource`` options and ``secret_options``). Secret values are placeholders."""
    name = str(lib.get("name") or "MYCASLIB")
    srctype = str(lib.get("type") or "path").strip()
    opts = list(lib.get("datasource") or []) + [{"name": n, "value": None} for n in lib.get("secret_options") or []]
    desc = lib.get("description")
    path = lib.get("path")
    head = f"/* {RECONSTRUCTED} */\n"
    if lib.get("personal"):
        head += "/* A personal caslib: CAS creates it for each user. */\n"
    if srctype.lower() in ("path", "dnfs", "hdfs") or (path and not opts):
        stmt = f"caslib {_caslib_name(name)}"
        if srctype.lower() != "path":
            stmt += f" datasource=(srctype={sas_string(srctype.lower())})"
        if path:
            stmt += f" path={sas_string(path)}"
        if lib.get("subdirs"):
            stmt += " subdirs"
        if desc:
            stmt += f" desc={sas_string(desc)}"
        act = [f"    name={sas_string(name)}", f"    dataSource={{srcType={sas_string(srctype.lower())}}}"]
        if path:
            act.append(f"    path={sas_string(path)}")
        if lib.get("subdirs"):
            act.append("    subDirectories=true")
    else:
        ds = " ".join([f"srctype={sas_string(srctype.lower())}"] + [f"{o['name']}={_cas_value(o)}" for o in opts])
        stmt = f"caslib {_caslib_name(name)}\n  datasource=({ds})"
        if desc:
            stmt += f"\n  desc={sas_string(desc)}"
        inner = ", ".join([f"srcType={sas_string(srctype.lower())}"] + [f"{o['name']}={_cas_value(o)}" for o in opts])
        act = [f"    name={sas_string(name)}", f"    dataSource={{{inner}}}"]
    if desc:
        act.append(f"    description={sas_string(desc)}")
    statement = head + stmt + ";\n/* Add GLOBAL to define it for everyone (needs the permission to add global caslibs). */"
    action = (head + "proc cas;\n  table.addCaslib /\n" + "\n".join(act)
              + "\n    session=true;  /* false: a global caslib (needs permission) */\nquit;")
    return {"statement": statement, "action": action}


# ------------------------------------------------------------------ automatic SingleStore library


def auto_libname_code(libref: str, host: str, port: int, user: str, password: str, database: str | None,
                      trace: bool = False) -> str:
    """The LIBNAME for the Workspace's automatic SingleStore library, submitted with NOSOURCE / NONOTES so the
    statement (and its password) is never echoed to the SAS log. Same engine and options as notebook kernels.
    ``trace``: DRIVER_TRACE=SQL into WORK (the SQL PROC FEDSQL sends; see TRACE_READ)."""
    opts = f"server={sas_string(host)} port={int(port)} user={sas_string(user)} password={sas_string(password)}"
    if database:
        opts += f" database={sas_string(database)}"
    if trace:
        opts += " driver_trace=SQL driver_traceoptions=APPEND driver_tracefile=" + sas_string(TRACE_FILE)
    # The markers fence the libname's own messages (a new session's first job log also holds the compute
    # context's autoexec, errors included); the libref's return code tells whether it was assigned.
    return ("options nosource nosource2 nonotes nomprint nosymbolgen;\n"
            f"%put {AUTO_MARK}BEGIN;\n"
            f"libname {libref} sstore {opts};\n"
            f"%put {AUTO_MARK}RC=%sysfunc(libref({libref}));\n"
            "options source notes;")


AUTO_MARK = "S2AUTOLIB_"


def auto_libname_result(lines: list[str]) -> tuple[bool, list[str]]:
    """(assigned, its ERROR lines) from the log of ``auto_libname_code``."""
    errors: list[str] = []
    inside, rc = False, None
    for line in lines:
        if line.startswith(AUTO_MARK + "BEGIN"):
            inside, errors = True, []
        elif line.startswith(AUTO_MARK + "RC="):
            rc = line[len(AUTO_MARK) + 3:].strip()
            inside = False
        elif inside and line.startswith("ERROR"):
            errors.append(line)
    return rc == "0" and not errors, errors


def mask_value(text: str | None, secret: str | None) -> str:
    """``text`` with every occurrence of ``secret`` (and password=/token= values) masked."""
    out = str(text or "")
    if secret:
        out = out.replace(secret, "********")
        quoted = secret.replace('"', '""')
        if quoted != secret:
            out = out.replace(quoted, "********")
    return mask_secrets(out)


def parse_sas9_libraries(text: str | None) -> list[tuple[str, str]]:
    """The "SAS 9 libraries" setting: remote librefs, optionally ``local=REMOTE``, separated by spaces, commas or
    lines → [(local, remote)]. Invalid names raise ValueError."""
    out: list[tuple[str, str]] = []
    for part in re.split(r"[\s,;]+", str(text or "").strip()):
        if not part:
            continue
        local, _, remote = part.partition("=")
        remote = remote or local
        for n in (local, remote):
            if not re.fullmatch(r"[A-Za-z_]\w{0,7}", n):
                raise ValueError(f"Not a SAS libref (1–8 letters, digits, _): {n!r}")
        out.append((local.upper(), remote.upper()))
    return out


SAS9_SKIP = {"WORK", "SASHELP", "SASUSER", "MAPS", "MAPSSAS", "MAPSGFK", "SASMSG", "SASCFG"}
