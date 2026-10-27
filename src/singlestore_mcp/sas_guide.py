"""SAS SQL guides for the SQL Editor's in-app assistant: one per "Run as" language.

The assistant's question carries the guide of the tab's language only (PROC SQL, FedSQL or FedSQL in CAS), plus
the tab's library / caslib and the columns the editor knows (``context_text``). The rules come from the SAS
documentation (SQL Procedure User's Guide, FedSQL Language Reference, FedSQL Programming for CAS, SAS/ACCESS
for Relational Databases) and were checked by running them in the Workspace's SAS session (SAS Viya, SAS/ACCESS
to SingleStore, SAS SpeedyStore caslibs): see tests/test_sas_guide.py and the README.
"""

from __future__ import annotations

import json
from typing import Any

from . import sas_functions

LANGUAGES = ("procsql", "fedsql", "cas")
LABELS = {"procsql": "PROC SQL", "fedsql": "FedSQL (PROC FEDSQL)", "cas": "FedSQL in CAS"}

_COMMON = """\
How the SQL Editor runs SAS (this tab runs {label}):
- The user writes only the SQL and the editor wraps it: {wrap}. So answer with the bare statement(s) for \
{label} in ```sql blocks - no `proc sql;` / `proc fedsql;` / `quit;` around them - unless the user asks for a \
SAS program or the task needs one (DATA step, other PROCs, macro code, explicit pass-through): then give one \
complete plain SAS block; text that starts with proc / data / % / libname / options runs as plain SAS code, \
unwrapped, in the SAS Compute session.
- A SELECT becomes `CREATE TABLE <result table> AS <your select>` and fills the result grid. Each statement \
(separated by `;`) runs as its own step: tables in WORK / CASUSER and macro variables stay in the session \
between statements, nothing else does.
- Writes run without asking only into {safe}; anything else asks the user first. Put new tables in {safe} \
unless the user names another library.
- Your read-only tools query SingleStore directly, not SAS. When the tab's library / caslib is SingleStore \
(the context says which database), describe_table / read_query on that database show the real table and \
column names; otherwise use the columns listed below and don't invent any. Say when you couldn't check a name. \
Write SingleStore table names in their exact case everywhere (S2.CARS, S2.cars_big): a DATA step or PROC \
with `set s2.cars` fails ("File S2.cars.DATA does not exist") when the table is CARS.
- On SingleStore libraries prefer pushdown-friendly SQL and say when something won't push down (it then runs \
in SAS after reading the rows: slow on big tables). The tab's Pushdown tab shows what SingleStore received."""

_WRAP = {
    "procsql": "`proc sql outobs=<cap>; <statement>; quit;` (one-level names use the tab's library via "
               "`options user=`, else WORK)",
    "fedsql": "`proc fedsql; <statement>; quit;` (one-level names are WORK tables: the tab's library is NOT used, "
              "so always write library.table)",
    "cas": "the CAS action `fedSql.execDirect` in the Workspace's CAS session (one-level names use the tab's caslib, "
           "else CASUSER)",
}
_SAFE = {"procsql": "WORK", "fedsql": "WORK", "cas": "CASUSER"}

GUIDES = {
    "procsql": """\
PROC SQL (SAS SQL dialect) - rules and pitfalls:
- Tables are libref.table (SASHELP.CARS, S2.CARS, WORK.X). Names are case-insensitive; SAS/ACCESS finds \
SingleStore tables in any case (S2.cars_big works). Odd names: name literals 'my col'n. Both '...' and "..." \
are strings, never identifiers.
- No LIMIT / TOP / FETCH FIRST (the editor warns about LIMIT). Rows are capped with the PROC SQL option \
OUTOBS=, which the editor sets: for "top N" write ORDER BY and tell the user to set OUTOBS = N in the tab's \
SAS options (gear), or give a plain SAS step `proc sql outobs=10; ... quit;`. INOBS= and the data set option \
(obs=N) limit the rows READ, not the result - wrong for top N after sorting.
- CALCULATED reuses a computed column: `msrp - invoice as profit ... where calculated profit > 5000`. GROUP BY \
/ ORDER BY take column numbers (group by 1, 2) or aliases.
- Remerging: summary functions next to non-grouped columns (`select make, msrp, avg(msrp) as a ... group by \
make`) remerges the group value onto each row (a NOTE, not an error); useful for "vs. group average", never \
pushed down.
- SAS functions: put(x, fmt.), input(), catx(), strip(), scan(), substr(), upcase(), coalesce(), intnx(), \
intck(), year()/month()/qtr()/day(), datepart(), today(), round(), missing(). Column modifiers: \
`avg(msrp) as avg_msrp format=dollar12.2 label='Average'`. No window functions (no RANK() OVER): use a plain \
SAS step (PROC RANK) or a correlated subquery; avoid the undocumented monotonic().
- Dates are numbers (days since 1960), datetimes seconds. Literals: '01JAN2024'd, '01JAN2024:13:00:00'dt, \
'13:00't (ANSI DATE '2024-01-01' is FedSQL, not PROC SQL). Month buckets: \
`intnx('month', date, 0, 'b') as month format=date9.` or `put(date, yymmn6.)`.
- Missing values: `x is missing` / `x is null` / `x = .`; character missing is ' '. CASE WHEN ... END.
- Set operators: UNION / EXCEPT / INTERSECT [ALL] [CORR]; OUTER UNION CORR stacks rows by column name.
- Data set options in FROM: `sashelp.cars(where=(origin='Asia') keep=make model)`; also on CREATE TABLE.
- CREATE TABLE work.x AS SELECT ...; DROP TABLE work.x; DESCRIBE TABLE x (writes to the log); VALIDATE \
<select> checks syntax only. Metadata: dictionary.tables / dictionary.columns (libname and memname in uppercase).
- Macro variables: `select max(msrp) into :maxmsrp trimmed from sashelp.cars` (one statement), then \
`where msrp = &maxmsrp` (next statement; inside strings use "&var"). A SELECT ... INTO isn't shown in the grid.
- Explicit pass-through (native SingleStore SQL) can't be split over statements: give one plain SAS block
  proc sql;
    connect using S2;    /* the SingleStore library's own connection: no credentials in code */
    select * from connection to S2 (SELECT Origin, COUNT(*) AS n FROM CARS GROUP BY Origin);
    disconnect from S2;
  quit;
  Inside the parentheses it's SingleStore SQL (LIMIT, backticks, SingleStore functions; table names \
case-sensitive).
- A plain `proc fedsql; ... quit;` step (when asked for one) uses FedSQL rules: LIMIT n (no FETCH FIRST, no \
OUTOBS), DATE '2024-01-01' literals, no CALCULATED, double-quoted mixed-case names, library.table always. `execute (...) by S2;` runs a non-query on the cluster: only when the user asks for that change.
- Implicit pass-through on a SingleStore library (SAS/ACCESS writes SingleStore SQL itself): pushed down \
fully - WHERE (date literals too), GROUP BY / HAVING, count/sum/avg/min/max, count(distinct), CASE, \
coalesce, upcase, year()/month(), CALCULATED, scalar subqueries, joins and ORDER BY within that one library. \
Not pushed down (rows read into SAS): joins/unions with WORK / SASHELP / other libraries, remerging, \
intnx/intck, catx, put() formats, substr/round/length (only with the LIBNAME option SQL_FUNCTIONS=ALL) - only \
the WHERE goes down. Say so when your SQL does this, and prefer the pushed-down functions listed below.""",

    "fedsql": """\
FedSQL (PROC FEDSQL, ANSI SQL:1999 dialect) on SAS libraries - rules and pitfalls:
- Always write library.table: one-level names are WORK. SASHELP can't be read by FedSQL ("schema name SASHELP \
was not found": it's a concatenated library): copy the table first with plain SAS \
(`data work.cars; set sashelp.cars; run;`) and query WORK.CARS, or suggest PROC SQL.
- Identifiers: unquoted names are case-insensitive and treated as UPPERCASE; double quotes keep the exact case \
("Make", S2."cars_big"). SingleStore table names are case-sensitive, so a table that isn't all uppercase must \
be quoted exactly (S2.cars_big fails, S2."cars_big" works); columns match unquoted. Single quotes = strings.
- No CALCULATED: repeat the expression (`where "MSRP" - "Invoice" > 5000`) or select from a derived table. \
GROUP BY / ORDER BY take column numbers or aliases.
- Rows: LIMIT n [OFFSET m] on the outermost SELECT (no OUTOBS, no TOP) - applied in SAS, see pushdown below. \
No WITH (CTEs): use derived tables. \
No window functions (RANK() OVER is a syntax error): use a correlated subquery or a plain SAS PROC RANK step.
- No SAS data set options in FROM (where=, keep=, obs=), no format= / label= column modifiers; put(x, fmt.) works.
- Dates: DATE '2024-01-01', TIMESTAMP '2024-01-01 13:00:00', TIME '13:00:00'. '01JAN2024'd is a syntax error \
in FedSQL. year() / month() / day() / intnx() work on DATE columns (SAS date columns of a library are DATE).
- Types: CAST(x AS INTEGER | BIGINT | DOUBLE | VARCHAR(n) | CHAR(n) | DATE). WORK (BASE) stores DOUBLE / CHAR \
only: "BASE driver, creation of a BIGINT column ... A DOUBLE ..." warnings are harmless.
- Text: `||` keeps CHAR padding, so `trim("Make") || ' - ' || trim("Model")`; catx() doesn't exist in FedSQL. \
upper(), lower(), substring(x from 1 for 3) / substr(), coalesce(), round(x, 2).
- Missing values are NULL: `is null`. CASE WHEN ... END, UNION / EXCEPT / INTERSECT, derived tables work.
- On a SingleStore library, a subquery in WHERE (`x > (select avg(x) ...)`, `x in (select ...)`) fails with \
"Unknown database 'WORK'": FedSQL pushes the editor's whole CREATE TABLE WORK... AS down to SingleStore. Join \
a derived table instead (`from S2.CARS c, (select avg("MSRP") as a from S2.CARS) t where c."MSRP" > t.a`, \
pushed down fully); scalar subqueries in the SELECT list work.
- CREATE TABLE work.x AS SELECT ...; to replace it, first `DROP TABLE work.x FORCE;` ({options replace=true} \
is for CAS only; the BASE driver rejects it).
- Pushdown on a SingleStore library (checked with the driver trace): with all tables in that library FedSQL \
sends WHERE, joins, GROUP BY with aggregates, count(distinct), CASE, year()/month() and UNION to SingleStore. \
It never sends LIMIT / OFFSET, and a GROUP BY together with ORDER BY or LIMIT isn't pushed either (SingleStore \
gets a plain table read, SAS does the rest). So on a big table (S2."cars_big", 20 M rows) \
`select * ... limit 5` reads the whole table into SAS and can bring the SAS session down: for a sample or a \
top N of rows use PROC SQL (`(obs=5)` goes down as LIMIT) or explicit pass-through, or filter with WHERE first; \
for a top N of groups aggregate without ORDER BY / LIMIT (pushed down) and sort the few rows in the grid. \
Queries mixing WORK or other libraries run (partly) in SAS - say so.""",

    "cas": """\
FedSQL in CAS (fedSql.execDirect on caslibs) - rules and pitfalls:
- Supported statements: SELECT, CREATE TABLE ... AS SELECT, DROP TABLE. No INSERT / UPDATE / DELETE, no WITH \
(CTEs: "Unsupported SQL statement"; use derived tables), no window functions.
- Names: caslib.table (PUBLIC.HMEQ, CASUSER.X); one-level names use the tab's caslib. Unquoted names are \
case-insensitive and come back as UPPERCASE column names; double quotes keep the case ("Make"). A caslib \
name with odd characters is quoted: "My caslib".TABLE.
- Only tables loaded in CAS can be queried. A caslib source table that isn't loaded (the context marks \
tables "not loaded") fails with "Table ... does not exist or cannot be accessed". Say so: the user loads it \
first (a SAS SpeedyStore caslib - type SingleStore - only registers it, queries still run live in SingleStore); \
a session-scope load from plain SAS code (PROC CASUTIL) runs in another CAS session than the editor's, so only \
a promoted (global) table is seen here - that's a change on the shared server, so only on request. Or offer \
PROC SQL / FedSQL on the matching SAS/ACCESS library instead.
- ORDER BY only together with LIMIT (the result goes into a CAS table, which has no order): without LIMIT \
it fails with "Unsupported operation in FedSQL query: ORDER BY". For "sorted" answers add LIMIT n, or leave \
ORDER BY out and say the grid can sort. LIMIT n [OFFSET m] works.
- No CALCULATED, no SAS data set options; repeat expressions or use a derived table. GROUP BY 1, 2 works.
- Dates: DATE '2024-01-01', TIMESTAMP '2024-01-01 12:00:00' ('01JAN2024'd is a syntax error). Date functions \
need DATE / TIMESTAMP values: year(today()) fails (today() is a SAS date number), EXTRACT() isn't there; \
don't do date arithmetic on SAS date numbers without saying it's untested.
- Missing: `is null` (`= .` also works). CAST(x AS VARCHAR(n) | INTEGER | BIGINT | DOUBLE), put(x, fmt.), \
upper(), substr(), coalesce(), round(); `||` keeps CHAR padding: trim(a) || ' / ' || trim(b).
- Subqueries (scalar and in FROM), joins, UNION, HAVING, count(distinct) work; tables from different \
caslibs can be joined.
- Write into CASUSER: `create table CASUSER.x {options replace=true} as select ...` (session scope: it goes \
away with the session). Other caslibs are shared: only when asked (the editor asks first).
- SAS SpeedyStore caslibs (type SingleStore): queries on their loaded tables are pushed down to SingleStore \
(the SAS Embedded Process does WHERE / GROUP BY / aggregates); joins with CASUSER or other caslibs run in CAS.""",
}


def guide(language: str) -> str:
    """The guide for one tab language (procsql, fedsql, cas); '' for anything else (SingleStore SQL)."""
    if language not in GUIDES:
        return ""
    common = _COMMON.format(label=LABELS[language], wrap=_WRAP[language], safe=_SAFE[language])
    try:
        funcs = sas_functions.guide_lines(language)
    except (OSError, ValueError, KeyError):  # a broken data file mustn't stop the assistant
        funcs = ""
    return "\n\n".join(x for x in (common, GUIDES[language], funcs) if x)


def _col(c: dict[str, Any]) -> str:
    name = str(c.get("name") or "?")
    bits = [str(c.get("type") or "")]
    if c.get("length") and str(c.get("type")) == "char":
        bits[0] += f"({c['length']})"
    if c.get("format"):
        bits.append(str(c["format"]))
    if c.get("loaded") is False:
        bits.append("not loaded")
    return f"{name} {' '.join(b for b in bits if b)}".strip()


def context_text(language: str, sas: dict[str, Any] | None) -> str:
    """The tab's SAS context for the question: library / caslib (and the SingleStore database behind it),
    its tables, the columns the editor knows and the tab's options. Kept short (a few KB at most)."""
    sas = sas or {}
    where = "caslib" if language == "cas" else "library"
    lib = sas.get("library")
    lines = [f"The tab runs {LABELS.get(language, language)}; its {where}: {lib or ('CASUSER' if language == 'cas' else 'none (WORK)')}."]
    info = sas.get("library_info") or {}
    if info:
        kind = info.get("singlestore_kind")
        engine = info.get("engine") or info.get("type")
        desc = f"{where.capitalize()} {info.get('name') or lib}: engine/type {engine}"
        if kind == "speedystore":
            desc += " (SAS SpeedyStore caslib: queries run live in SingleStore)"
        elif info.get("singlestore") or str(engine or "").upper() in ("SSTORE", "S2", "SINGLESTORE"):
            desc += " (SingleStore)"
        if info.get("database"):
            desc += f", SingleStore database {info['database']} (check names there with describe_table)"
        lines.append(desc + ".")
    others = sas.get("singlestore_libraries") or []
    if others:
        lines.append("SingleStore libraries in the session: " + ", ".join(
            f"{x.get('name')}={x.get('database') or '?'}" for x in others[:12]) + ".")
    tables = sas.get("tables") or []
    if tables:
        names = []
        for t in tables[:150]:
            if isinstance(t, dict):
                names.append(str(t.get("name")) + (" (not loaded)" if t.get("loaded") is False else ""))
            else:
                names.append(str(t))
        more = f" (+{len(tables) - 150} more)" if len(tables) > 150 else ""
        lines.append(f"Tables in {lib}: " + ", ".join(names) + more + ".")
    cols = sas.get("columns") or {}
    budget = 6000
    for ref, cs in list(cols.items())[:12]:
        if not isinstance(cs, list):
            continue
        line = f"Columns of {ref}: " + ", ".join(_col(c) for c in cs[:120] if isinstance(c, dict))
        if len(line) > budget:
            break
        budget -= len(line)
        lines.append(line)
    opts = sas.get("options") or {}
    shown = {k: v for k, v in opts.items() if k in ("outobs", "noexec", "pushdown") and v not in (None, "", False)}
    if shown:
        lines.append("Tab options: " + json.dumps(shown) + ".")
    return "\n".join(lines)
