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

import re
from typing import Any

LANGUAGES = ("procsql", "fedsql", "cas")
RESULT_TABLE = "_S2RES"
SAS_STEP = re.compile(
    r"^(proc|data|%|libname|filename|options|title\d?|footnote\d?|ods|run|quit|caslib|cas|endsas)\b|^%", re.I)
_LEADING = re.compile(r"^(\s+|/\*.*?\*/|--[^\n]*(\n|$))*", re.S)
_NAME = r'(?:"(?:[^"]|"")+"n?|\'(?:[^\']|\'\')+\'n|[A-Za-z_][\w$]*(?:\([^)]*\))?)'
_QUALIFIED = re.compile(rf"({_NAME})(?:\s*\.\s*({_NAME}))?(?:\s*\.\s*({_NAME}))?", re.S)
SAFE_COMPUTE = {"WORK"}
SAS_ENGINES_SINGLESTORE = {"S2", "SSTORE", "SINGLESTORE"}
CAS_TYPES_SINGLESTORE = {"singlestore", "singlestore_standard", "sstore"}


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
         max_rows: int = 1000, pushdown: bool = False) -> dict[str, Any]:
    """The SAS code (or CAS action) that runs one statement.

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
            lines.append(f"  create table WORK.{RESULT_TABLE} as")
            lines.append(_indent(body) + ";")
        else:
            lines.append(_indent(body) + ";")
        lines.append("quit;")
        if lib and lib != "WORK":
            lines.append("options user=work;")
        if pushdown:
            lines.append(TRACE_OFF)
        return {"code": "\n".join(lines), "kind": "select" if select and not noexec else "statement",
                "result": ("WORK", RESULT_TABLE) if select and not noexec else None, "capped": capped}
    if language == "fedsql":
        head = "proc fedsql _method;" if pushdown else "proc fedsql;"
        lines = [head]
        if select:
            lines.append(f'  drop table WORK."{RESULT_TABLE}" force;')
            lines.append(f'  create table WORK."{RESULT_TABLE}" as')
        lines.append(_indent(body) + ";")
        lines.append("quit;")
        return {"code": "\n".join(lines), "kind": "select" if select else "statement",
                "result": ("WORK", RESULT_TABLE) if select else None, "capped": False}
    if language == "cas":
        query = (f"create table CASUSER.{_quote_fedsql(RESULT_TABLE)} {{options replace=true}} as {body}"
                 if select else body)
        params: dict[str, Any] = {"query": query, "method": bool(pushdown)}
        actions = [("fedSql.execDirect", params)]
        code = ("proc cas;\n"
                + f"  sessionProp.setSessOpt / caslib={_quote_sas_string(library or 'CASUSER')};\n"
                + f"  fedSql.execDirect / method={'true' if pushdown else 'false'}\n"
                + f"    query={_quote_sas_string(query)};\nquit;")
        return {"code": code, "actions": actions, "kind": "select" if select else "statement",
                "result": ("CASUSER", RESULT_TABLE) if select else None, "capped": False}
    raise ValueError(f"Run as must be one of {', '.join(LANGUAGES)}")


def _indent(sql: str) -> str:
    return "\n".join("  " + line if line.strip() else line for line in sql.splitlines())


TRACE_ON = "options sastrace=',,,d' sastraceloc=saslog nostsuffix sql_ip_trace=(note, source);"
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


def pushdown_verdict(language: str, log_text: str, *, singlestore: bool | None = None) -> dict[str, Any]:
    """What reached SingleStore and how much of the work it did.

    ``verdict``: ``full`` (pushed down fully), ``partly``, ``pulled`` (rows pulled into SAS / CAS and processed
    there) or ``none`` (no SingleStore table involved / nothing traced). ``sent``: the SQL SingleStore got."""
    notes: list[str] = []
    if language == "cas":
        return _cas_verdict(log_text)
    if language == "fedsql":
        return _fedsql_verdict(log_text)
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


def _fedsql_verdict(log_text: str) -> dict[str, Any]:
    scans = [x.strip() for x in re.findall(r"SeqScan from ([^\n]+)", log_text)]
    pushed = [s for s in scans if "{Push Down}" in s]
    other = [s for s in scans if "{Push Down}" not in s and s.split(".")[0].upper() not in _SAS_LIBS]
    sent = [{"id": f"plan_{i + 1}", "engine": "FEDSQL", "sql": f"(FedSQL pushed a subquery to {s.split('.')[0]})"}
            for i, s in enumerate(pushed)]
    notes = ["From the FedSQL query plan (_method): the SQL text FedSQL sends isn't written to the SAS log."]
    if not pushed and not other:
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
