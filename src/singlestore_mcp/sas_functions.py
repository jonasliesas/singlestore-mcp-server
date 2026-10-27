"""The SAS function matrix (data/sas_functions.json): which functions PROC SQL, FedSQL and FedSQL in CAS support
and which of them are pushed down to SingleStore. Used by the in-app assistant's SAS guide and the SQL Editor's
pre-run check (the same rules run in the browser: apps/vendor/sas-funcs.js). Update the data with
scripts/sas_functions.py (--verify runs the checks in SAS, --table rewrites the README table)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import sas_sql

DATA = Path(__file__).parent / "data" / "sas_functions.json"
DIALECTS = ("procsql", "fedsql", "cas")
LABELS = {"procsql": "PROC SQL", "fedsql": "FedSQL", "cas": "FedSQL in CAS"}


@lru_cache(maxsize=1)
def matrix() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def token(name: str) -> str:
    """The name a function is called by in SQL: ``PUT (number)`` -> ``PUT``; ``||`` and ``COUNT(DISTINCT)`` as is."""
    return name if name in ("||", "COUNT(DISTINCT)") else name.split(" ")[0].split("(")[0].upper()


def compact() -> dict[str, dict[str, Any]]:
    """{token: {"name", "category", "singlestore", "alternative", "access_map", <dialect>: {"s": supported, "p": pushdown}}}
    - what the editor's check needs (functions sharing a token, e.g. the two PUT rows, are merged: the worst wins)."""
    out: dict[str, dict[str, Any]] = {}
    for fn in matrix()["functions"]:
        key = token(fn["name"])
        cur = out.setdefault(key, {"name": key, "category": fn["category"], "singlestore": fn.get("singlestore"),
                                   "alternative": fn.get("alternative") or "", "access_map": fn.get("access_map")})
        for d in DIALECTS:
            e = fn.get(d) or {}
            new = {"s": e.get("supported"), "p": e.get("pushdown")}
            old = cur.get(d)
            if old is None:
                cur[d] = new
            else:  # merged rows: unsupported / not pushed down wins
                if new["s"] is False:
                    old["s"] = False
                if new["p"] in ("no", "n/a"):
                    old["p"] = new["p"]
            if fn.get("alternative") and not cur["alternative"]:
                cur["alternative"] = fn["alternative"]
    return out


_STRINGS = re.compile(r"/\*[\s\S]*?\*/|--[^\n]*|'(?:[^']|'')*'")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NOT_FUNCS = {"IN", "EXISTS", "AS", "FROM", "JOIN", "ON", "AND", "OR", "NOT", "WHEN", "THEN", "ELSE", "VALUES", "USING",
              "TO", "SELECT", "WHERE", "HAVING", "BY", "OVER", "TABLE", "INTO", "ALL", "ANY", "SOME", "CONNECTION", "OPTIONS"}


def functions_in(sql: str, language: str) -> list[str]:
    """Function tokens a statement calls (strings and comments left out; "…" is a string in PROC SQL only)."""
    text = _STRINGS.sub(" ", sql or "")
    if language == "procsql":
        text = re.sub(r'"(?:[^"]|"")*"', " ", text)
    else:
        text = re.sub(r'"(?:[^"]|"")*"', "x", text)
    found: list[str] = []
    if re.search(r"\bcount\s*\(\s*distinct\b", text, re.I):
        found.append("COUNT(DISTINCT)")
    for m in _CALL.finditer(text):
        name = m.group(1).upper()
        if name not in _NOT_FUNCS and name not in found:
            found.append(name)
    if "||" in text:
        found.append("||")
    return found


def check(sql: str, language: str, singlestore: bool = False) -> list[dict[str, Any]]:
    """What the matrix says about the functions in ``sql`` for the tab's ``language``: ``unsupported`` (the dialect
    hasn't got it) and, when the statement reads a SingleStore library / caslib, ``no_pushdown`` (SAS / CAS does
    that part after reading the rows). Functions the matrix doesn't know aren't reported."""
    if language not in DIALECTS:
        return []
    known = compact()
    out = []
    for tok in functions_in(sql, language):
        fn = known.get(tok)
        if not fn:
            continue
        e = fn.get(language) or {}
        if e.get("s") is False:
            out.append({"function": tok, "issue": "unsupported", "alternative": fn["alternative"]})
        elif singlestore and e.get("p") == "no":
            out.append({"function": tok, "issue": "no_pushdown", "alternative": fn["alternative"],
                        "sql_functions_all": language == "procsql" and fn.get("access_map") == "all"})
    return out


def statement(fn: dict[str, Any], dialect: str, tables: dict[str, Any] | None = None) -> str:
    """The check statement of one matrix function in one dialect (scripts/sas_functions.py --verify, the --sas
    tests): an aggregate grouped by a character column, a scalar grouped by itself (so a function SingleStore
    can't run makes SAS read the rows), or a WHERE test (``"test": "where:…"``)."""
    cols = (tables or matrix()["test_tables"])[dialect][fn["table"]]
    test = fn["test"].get(dialect) if isinstance(fn["test"], dict) else fn["test"]
    where = test.startswith("where:")
    expr = test.removeprefix("where:").format(**cols)
    t = cols["t"]
    if where:
        return f"select count(*) as n from {t} where {expr}"
    if fn["kind"] == "agg":
        return f"select {cols['c2']}, {expr} as v from {t} group by {cols['c2']}"
    return f"select {expr} as v, count(*) as n from {t} group by 1"


def cas_pushdown(log: str) -> str:
    """FedSQL in CAS (method=true plan in the log): the whole query pushed down (``{Push Down}``), part of it
    through the SAS Embedded Process (``_pushed_``: e.g. the grouping), or none."""
    if "{Push Down}" in log:
        return "yes"
    if "_pushed_" in log:
        return "partly"
    return "no"


def pushdown_of(dialect: str, result: dict[str, Any]) -> str:
    """yes / no / partly / unknown from a sas_query result (PROC SQL and FedSQL: the Pushdown verdict)."""
    if dialect == "cas":
        return cas_pushdown(str(result.get("log") or ""))
    push = result.get("pushdown") or {}
    verdict = push.get("verdict")
    sent = [str(s.get("sql") or "") for s in push.get("sent") or [] if not str(s.get("sql") or "").startswith("(FedSQL")]
    # FedSQL's plan says "Push Down" also when only the table read went down (put(): SingleStore got
    # `select MSRP from CARS`); the driver trace's SQL tells.
    if verdict == "full" and sent and all(sas_sql._is_pass_all(s) for s in sent):
        return "no"
    return {"full": "yes", "partly": "no", "pulled": "no"}.get(verdict, "unknown")


def by_name(name: str) -> dict[str, Any]:
    for fn in matrix()["functions"]:
        if fn["name"].upper() == name.upper():
            return fn
    raise KeyError(name)


def guide_lines(language: str) -> str:
    """Compact function lists for one dialect, for the assistant's SAS guide."""
    if language not in DIALECTS:
        return ""
    known = compact()
    down, partly, local, missing = [], [], [], []
    for tok, fn in known.items():
        e = fn.get(language) or {}
        if e.get("s") is False:
            missing.append(tok)
        elif e.get("p") == "yes":
            down.append(tok)
        elif e.get("p") == "partly":
            partly.append(tok)
        elif e.get("p") == "no":
            local.append(tok + (" (pushed with SQL_FUNCTIONS=ALL)" if language == "procsql" and fn.get("access_map") == "all" else ""))
    alts = [f"{tok}: {fn['alternative'][:140]}" for tok, fn in known.items()
            if fn["alternative"] and ((fn.get(language) or {}).get("s") is False or (fn.get(language) or {}).get("p") == "no")]
    label = LABELS[language]
    lines = [f"Functions in {label} on a SingleStore {'caslib' if language == 'cas' else 'library'} (checked in SAS; "
             "see the SQL Editor's function matrix):"]
    if down:
        lines.append("- run in SingleStore (pushed down): " + ", ".join(down))
    if partly:
        lines.append("- aggregated with the SAS Embedded Process in SingleStore (grouping pushed down, last step in CAS): "
                     + ", ".join(partly))
    if local:
        lines.append("- work, but run in SAS / CAS after reading the rows (block full pushdown): " + ", ".join(local))
    if missing:
        lines.append(f"- not available in {label}: " + ", ".join(missing))
    if alts:
        lines.append("- instead: " + "; ".join(alts[:30]))
    return "\n".join(lines)
