"""Reshard script: copy a table to a new table with another shard key - written out as SQL for a DBA, never run.

The Schema Explorer's Distribution tab offers it next to a reshard simulation result ("Change shard key…").
``build_script`` is pure (the facts come in as arguments), so it is tested offline from recorded
SHOW CREATE TABLE text; ``reshard_script`` reads those facts from the cluster (read-only, one parallel batch).

Checked in the SingleStore 9.0 docs (and tried on 9.0.40 with zz_test_ tables in the ``test`` database):
- A shard key can't be changed in place: "Once created, the shard key for a table cannot be modified" (Shard Keys),
  ALTER TABLE: "Altering PRIMARY or SHARD keys is unsupported", Understanding Shard Key Selection: "SingleStore does
  not support the use of ALTER TABLE to add or change shard keys on existing tables". The documented way is a new
  table plus a copy and a rename (Creating a Columnstore Table: CREATE TABLE t2 … / DROP TABLE t / ALTER TABLE t2
  RENAME TO t). ``CREATE TABLE … LIKE … WITH DEEP COPY`` copies the definition (shard key included), so it can't
  change the key; CREATE TABLE … AS SELECT drops AUTO_INCREMENT ("will be created as a non-auto-increment column"),
  so the script uses the real SHOW CREATE TABLE text with only the SHARD KEY replaced, then INSERT … SELECT.
- Renaming: ``ALTER TABLE <t> RENAME [TO | AS] <new>`` (one table per statement). There is no RENAME TABLE statement
  (9.0.40: syntax error 1064, also for the MySQL multi-table form), so there is no atomic swap: between the two
  renames the name doesn't exist for a moment. A rename succeeds while views refer to the table; views follow the
  name (they fail while it is missing, then read the new table). DROP TABLE refuses while a view depends on it.
- Unique keys (PRIMARY / UNIQUE) must contain every shard key column (9.0.40: error 1744, "Unique key
  restrictions"), so such a key is refused before a script is made.
- AUTO_INCREMENT: on sharded tables "explicitly inserted values do not affect the automatically generated values";
  the docs say to run ``AGGREGATOR SYNC AUTO_INCREMENT [ON db[.table]] [ALL]`` after loading data (AGGREGATOR SYNC
  AUTO_INCREMENT page). The AUTO_INCREMENT= table option of SHOW CREATE TABLE is kept too.
- Skew check of the new table: information_schema.TABLE_STATISTICS (master partitions), the docs' skew score
  STDDEV/AVG of the rows (Detecting and resolving data skew), as the Distribution tab reads it.
Docs (v9.0): .../create-a-database/optimizing-table-data-structures/shard-keys/,
.../create-a-database/understanding-shard-key-selection/, .../create-a-database/creating-a-columnstore-table/,
.../reference/sql-reference/data-definition-language-ddl/alter-table/, .../create-table/, .../drop-table/,
.../reference/sql-reference/data-manipulation-language-dml/insert/, .../aggregator-sync-auto-increment/,
.../create-a-database/detecting-and-resolving-data-skew/ (all under https://docs.singlestore.com/db/v9.0).
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from .db import quote_identifier as qi

_D = "https://docs.singlestore.com/db/v9.0"
DOCS = {
    "shard_keys": f"{_D}/create-a-database/optimizing-table-data-structures/shard-keys/",
    "shard_key_selection": f"{_D}/create-a-database/understanding-shard-key-selection/",
    "columnstore_copy": f"{_D}/create-a-database/creating-a-columnstore-table/",
    "alter_table": f"{_D}/reference/sql-reference/data-definition-language-ddl/alter-table/",
    "create_table": f"{_D}/reference/sql-reference/data-definition-language-ddl/create-table/",
    "drop_table": f"{_D}/reference/sql-reference/data-definition-language-ddl/drop-table/",
    "insert": f"{_D}/reference/sql-reference/data-manipulation-language-dml/insert/",
    "auto_increment": f"{_D}/reference/sql-reference/data-manipulation-language-dml/aggregator-sync-auto-increment/",
    "skew": f"{_D}/create-a-database/detecting-and-resolving-data-skew/",
}
NEW_SUFFIX = "__reshard"
OLD_SUFFIX = "__old"
MAX_NAME = 64
MAX_COLUMNS = 8


class ReshardError(ValueError):
    """The script can't be made (bad key, reference table, name too long, …)."""


# ----------------------------------------------------------------------------- DDL scanning

def _scan_to_close(ddl: str, start: int) -> int:
    """Index of the ``)`` closing the parenthesis opened just before ``start``; skips `names`, 'strings' and
    "strings" (doubled quotes and backslash escapes inside strings)."""
    depth, i, n = 1, start, len(ddl)
    while i < n:
        ch = ddl[i]
        if ch in "`'\"":
            i = _skip_quoted(ddl, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ReshardError("Couldn't read the SHOW CREATE TABLE text (unbalanced parentheses).")


def _skip_quoted(s: str, i: int) -> int:
    """Index just after the quoted run that starts at ``i``."""
    q, j, n = s[i], i + 1, len(s)
    while j < n:
        c = s[j]
        if c == "\\" and q != "`":
            j += 2
            continue
        if c == q:
            if j + 1 < n and s[j + 1] == q:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _top_level_items(body: str) -> list[tuple[int, int]]:
    """(start, end) spans of the comma-separated items of a CREATE TABLE body (columns and keys)."""
    spans, depth, i, start, n = [], 0, 0, 0, len(body)
    while i < n:
        ch = body[i]
        if ch in "`'\"":
            i = _skip_quoted(body, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            spans.append((start, i))
            start = i + 1
        i += 1
    spans.append((start, n))
    return [(a, b) for a, b in spans if body[a:b].strip()]


def _names(text: str) -> list[str]:
    return [m.group(1).replace("``", "`") for m in re.finditer(r"`((?:[^`]|``)*)`", text)]


_HEAD = re.compile(r"^\s*CREATE\s+(?P<kind>(?:[A-Z]+\s+)*?)TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", re.I)
_SHARD = re.compile(r"^\s*SHARD\s+KEY\b", re.I)
_UNIQUE = re.compile(r"^\s*(?:PRIMARY\s+KEY|UNIQUE\b)", re.I)
_KEYWORD_ITEM = re.compile(r"^\s*(?:SHARD|SORT|PRIMARY|UNIQUE|FULLTEXT|KEY|INDEX|VECTOR|COLUMN\s+GROUP|CONSTRAINT|FOREIGN)\b", re.I)


def parse_ddl(ddl: str) -> dict[str, Any]:
    """The parts of a SHOW CREATE TABLE text the script needs: where the body is, its items, the shard key and the
    unique keys, columns (computed ones marked), table kind words (ROWSTORE / REFERENCE …)."""
    m = _HEAD.match(ddl)
    if not m:
        raise ReshardError("Not a CREATE TABLE statement.")
    kind = " ".join(m.group("kind").upper().split())
    name_at = m.end()
    if ddl[name_at:name_at + 1] != "`":
        raise ReshardError("Couldn't read the table name in the SHOW CREATE TABLE text.")
    name_end = _skip_quoted(ddl, name_at)
    # a qualified `db`.`t` name: skip it too
    if ddl[name_end:name_end + 2] == ".`":
        name_end = _skip_quoted(ddl, name_end + 1)
    open_at = ddl.index("(", name_end)
    close_at = _scan_to_close(ddl, open_at + 1)
    body = ddl[open_at + 1:close_at]
    items = _top_level_items(body)
    shard = None
    uniques: list[dict[str, Any]] = []
    columns: list[dict[str, Any]] = []
    for a, b in items:
        text = body[a:b]
        if _SHARD.match(text):
            paren = text.find("(")
            shard = {"span": (a, b), "columns": _names(text[paren:]) if paren >= 0 else []}
        elif _UNIQUE.match(text):
            paren = text.find("(")
            uniques.append({"text": " ".join(text.split()), "columns": _names(text[paren:_scan_to_close(text, paren + 1)]),
                            "primary": bool(re.match(r"\s*PRIMARY\b", text, re.I))})
        elif not _KEYWORD_ITEM.match(text):
            cm = re.match(r"\s*`((?:[^`]|``)*)`\s+(\S+)", text)
            if cm:
                columns.append({"name": cm.group(1).replace("``", "`"),
                                "computed": cm.group(2).upper() == "AS",
                                "auto_increment": bool(re.search(r"\bAUTO_INCREMENT\b", _strip_strings(text), re.I))})
    return {"kind": kind, "name_span": (name_at, name_end), "open": open_at, "close": close_at, "body": body,
            "shard": shard, "uniques": uniques, "columns": columns, "options": ddl[close_at + 1:]}


def _strip_strings(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] in "'\"":
            j = _skip_quoted(text, i)
            out.append("''")
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def new_table_ddl(ddl: str, database: str, new_name: str, columns: list[str]) -> tuple[str, dict[str, Any]]:
    """SHOW CREATE TABLE text with the table name replaced by `database`.`new_name` and only the SHARD KEY changed
    (an explicit clause is replaced in place; none - a key from the PRIMARY KEY, or a rowstore default - gets one
    added after the last item). Columns, SORT KEY, indexes, table kind and options stay as they are."""
    p = parse_ddl(ddl)
    if "REFERENCE" in p["kind"].split():
        raise ReshardError("A reference table has no shard key: every node holds a full copy.")
    if "TEMPORARY" in p["kind"].split():
        raise ReshardError("A temporary table can't be resharded this way.")
    key = f"SHARD KEY ({', '.join(qi(c) for c in columns)})"
    body = p["body"]
    if p["shard"]:
        a, b = p["shard"]["span"]
        text = body[a:b]
        lead = text[: len(text) - len(text.lstrip())]
        tail = text[len(text.rstrip()):]
        body = body[:a] + lead + key + tail + body[b:]
    else:
        stripped = body.rstrip()
        trail = body[len(stripped):]
        indent = re.search(r"\n([ \t]*)\S[^\n]*$", stripped)
        body = f"{stripped},\n{indent.group(1) if indent else '  '}{key}{trail}"
    a, b = p["name_span"]
    out = ddl[:a] + f"{qi(database)}.{qi(new_name)}" + ddl[b:p["open"] + 1] + body + ddl[p["close"]:]
    return out, p


# ----------------------------------------------------------------------------- the script

def _fmt_bytes(n: float | int | None) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def _fmt_secs(s: float) -> str:
    if s < 90:
        return f"{max(1, round(s))} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    return f"{s / 3600:.1f} h"


def _one_line(text: str, n: int = 400) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _key_list(cols: list[str]) -> str:
    return f"({', '.join(cols)})" if cols else "(keyless)"


def check_key(columns: Any, table_columns: list[str]) -> list[str]:
    """The candidate shard key, as the table spells its columns (case-sensitive like the simulation)."""
    if not isinstance(columns, list) or not columns or not all(isinstance(c, str) and c for c in columns):
        raise ReshardError("Pick the columns of the new shard key.")
    if len(columns) > MAX_COLUMNS:
        raise ReshardError(f"A shard key of at most {MAX_COLUMNS} columns.")
    if len(set(columns)) != len(columns):
        raise ReshardError("Each column once in the shard key.")
    missing = [c for c in columns if c not in table_columns]
    if missing:
        raise ReshardError(f"No column {', '.join(missing)} in the table (names are case-sensitive).")
    return list(columns)


def build_script(database: str, table: str, ddl: str, columns: list[str], facts: dict[str, Any] | None = None,
                 now: Any = None) -> dict[str, Any]:
    """The reshard script for ``database.table`` onto the shard key ``columns``. Pure: ``facts`` holds what was read
    from the cluster - rows, data_bytes, current_shard, pipelines [{name, state}], views [`db`.`v` names], grants
    [text], existing (names in the database), simulation {rsd_pct, current_rsd_pct, elapsed_ms, fraction,
    partition_count}, connection, prepared_by."""
    f = facts or {}
    new_name, old_name = f"{table}{NEW_SUFFIX}", f"{table}{OLD_SUFFIX}"
    if len(old_name) > MAX_NAME or len(new_name) > MAX_NAME:
        raise ReshardError(f"{table} is too long for the work names {new_name} / {old_name} (at most {MAX_NAME} characters).")
    create, p = new_table_ddl(ddl, database, new_name, columns)
    table_cols = [c["name"] for c in p["columns"]]
    columns = check_key(columns, table_cols)
    for u in p["uniques"]:
        missing = [c for c in columns if c.lower() not in {x.lower() for x in u["columns"]}]
        if missing:
            raise ReshardError(
                f"{u['text'][:120]} doesn't contain {', '.join(missing)}: SingleStore requires every unique key (PRIMARY / "
                "UNIQUE) to contain all shard key columns (error 1744, docs: Unique key restrictions). Pick shard key "
                f"columns from ({', '.join(u['columns'])}).")
    fq, fq_new, fq_old = (f"{qi(database)}.{qi(t)}" for t in (table, new_name, old_name))
    current = f.get("current_shard")
    if current is None:
        primary = next((u for u in p["uniques"] if u["primary"]), None)
        # no SHARD KEY clause: sharded on the PRIMARY KEY, else keyless
        current = p["shard"]["columns"] if p["shard"] else primary["columns"] if primary else []
    if current is not None and [c.lower() for c in current] == [c.lower() for c in columns]:
        raise ReshardError(f"{database}.{table} is already sharded on {_key_list(columns)}.")
    computed = [c["name"] for c in p["columns"] if c["computed"]]
    auto_inc = any(c["auto_increment"] for c in p["columns"])
    if computed:
        cols = ", ".join(qi(c["name"]) for c in p["columns"] if not c["computed"])
        insert = f"INSERT INTO {fq_new} ({cols})\nSELECT {cols} FROM {fq};"
    else:
        insert = f"INSERT INTO {fq_new} SELECT * FROM {fq};"

    rows = f.get("rows")
    data = f.get("data_bytes")
    warnings: list[str] = []
    if data:
        warnings.append(f"Space: {database}.{table} holds {_fmt_bytes(data)} (memory + disk"
                        f"{f', {int(rows):,} rows' if rows is not None else ''}). The copy needs as much again until the old "
                        f"table is dropped (step 6): about {_fmt_bytes(2 * float(data))} in all. Check free disk and "
                        "memory on the leaves first.")
    else:
        warnings.append("Space: the copy needs as much disk and memory as the table again until the old table is dropped (step 6).")
    pipes = f.get("pipelines") or []
    if pipes:
        listed = ", ".join(f"{qi(x['name'])} ({str(x.get('state') or '?').lower()})" for x in pipes)
        warnings.append(f"Pipelines load into {database}.{table}: {listed}. Stop them first (STOP PIPELINE, below) and "
                        "start them again after the swap (step 5): rows they load during the copy would be missing from the "
                        "new table. Check after the swap that they load into the new table (SHOW PIPELINES / pipeline status).")
    else:
        warnings.append(f"No pipeline loads into {database}.{table} directly (pipelines INTO PROCEDURE aren't checked).")
    views = f.get("views") or []
    if views:
        warnings.append(f"Views that refer to {database}.{table}: {', '.join(views)}. They follow the name: between the two "
                        "renames of step 5 they fail, afterwards they read the new table. Stored procedures and functions "
                        "that use the name aren't checked.")
    grants = f.get("grants") or []
    if grants:
        warnings.append(f"Grants on {database}.{table}: {'; '.join(grants)}. Table grants are by name: check them on the "
                        "new table after the swap (SHOW GRANTS / the Security view).")
    existing = set(f.get("existing") or [])
    for name in (new_name, old_name):
        if name in existing:
            warnings.append(f"{database}.{name} already exists: the script would fail at "
                            f"{'step 1' if name == new_name else 'step 5'}. Drop or rename it first.")
    sim = f.get("simulation") or {}
    elapsed = sim.get("elapsed_ms")
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        lo, hi = elapsed / 1000 * 2, elapsed / 1000 * 6
        sample = " (from a sample: every row was read, fewer moved)" if sim.get("fraction") else ""
        duration = (f"The simulation read and moved the rows in {_fmt_secs(elapsed / 1000)}{sample}; the copy also "
                    f"writes them (and builds the indexes): expect roughly {_fmt_secs(lo)}–{_fmt_secs(hi)} for step 2. "
                    "A rough guide, not a promise.")
    else:
        duration = "Step 2 reads, moves and writes every row: on a big table it takes a while (run the simulation first for an estimate)."
    warnings.append(f"Duration: {duration}")
    warnings.append(f"Run it in a quiet window: writes to {database}.{table} after step 2 starts are not in the new table "
                    "(stop the writers), and step 2 loads the cluster. DDL isn't transactional: run the steps one by one and "
                    "check each result. Between the two renames of step 5 the name doesn't exist for a moment (SingleStore "
                    "9.0 has no atomic swap).")
    if auto_inc:
        warnings.append("The table has an AUTO_INCREMENT column: the copy keeps the values, and AGGREGATOR SYNC "
                        "AUTO_INCREMENT (step 2) makes new values start above them (docs).")

    stamp = now or _dt.datetime.now().astimezone()
    head = [
        f"-- Reshard {database}.{table} onto a new shard key - SQL script for review",
        "-- Generated by SAS SpeedyStore Studio (Schema Explorer, Distribution tab). Nothing has been run.",
    ]
    if f.get("connection"):
        head.append(f"-- Connection: {f['connection']}")
    if f.get("prepared_by"):
        head.append(f"-- Prepared by: {f['prepared_by']} on {stamp.strftime('%Y-%m-%d %H:%M %z').strip()}")
    head.append(f"-- Shard key: {_key_list(current or [])} now -> {_key_list(columns)}")
    if sim.get("rsd_pct") is not None:
        parts = sim.get("partition_count")
        over = f" over {int(parts)} partitions" if parts else ""
        sampled = " (estimated from a sample)" if sim.get("fraction") else ""
        head.append(f"-- Simulation: {sim['rsd_pct']} % row variation predicted, {sim.get('current_rsd_pct', '?')} % now"
                    f"{over}{sampled}.")
    head.append("-- Checked against the cluster as it was when the script was made; review it before running.")
    head.append("--")
    head += [f"-- WARNING: {_one_line(w)}" for w in warnings]
    head += ["--",
             "-- A shard key can't be changed with ALTER TABLE (docs: Shard keys, ALTER TABLE), so this copies the table:",
             f"-- {DOCS['shard_keys']}",
             f"-- {DOCS['alter_table']}"]

    body: list[str] = []
    if pipes:
        body += ["", "-- 0. Stop the pipelines that load into the table (start them again after step 5)"]
        body += [f"STOP PIPELINE {qi(database)}.{qi(x['name'])};" for x in pipes if str(x.get("state", "")).lower() == "running"]
        if not any(str(x.get("state", "")).lower() == "running" for x in pipes):
            body.append("-- (none of them is running now)")
    body += ["", "-- 1. The new table: SHOW CREATE TABLE of the table with only the SHARD KEY changed", create.rstrip().rstrip(";") + ";"]
    body += ["", "-- 2. Copy the rows (they land in the partitions of the new shard key)", insert]
    if auto_inc:
        # 9.0.40 cuts a name at a backquote here (error 1103 "Table name cannot have space at the end"), so a table
        # name with one syncs the whole database (it only raises counters to the values the tables hold).
        if "`" in database:
            body += ["-- 9.0.40 refuses a name with a backquote in AGGREGATOR SYNC AUTO_INCREMENT (error 1103): check that",
                     "-- new AUTO_INCREMENT values start above the copied ones.",
                     f"-- AGGREGATOR SYNC AUTO_INCREMENT ON {fq_new} ALL;"]
        elif "`" in new_name:
            body += ["-- The whole database: 9.0.40 refuses a table name with a backquote here (error 1103).",
                     f"AGGREGATOR SYNC AUTO_INCREMENT ON {qi(database)} ALL;"]
        else:
            body.append(f"AGGREGATOR SYNC AUTO_INCREMENT ON {fq_new} ALL;")
    body += ["", "-- 3. Check: both counts must be equal before you go on",
             f"SELECT (SELECT COUNT(*) FROM {fq}) AS rows_old, (SELECT COUNT(*) FROM {fq_new}) AS rows_new;"]
    body += ["", "-- 4. Check the skew of the new table (rows per partition; the docs' skew score, under 10 % is fine)",
             "SELECT COUNT(*) AS partitions, SUM(`ROWS`) AS `rows`, MIN(`ROWS`) AS min_rows, MAX(`ROWS`) AS max_rows,",
             "       ROUND(STDDEV(`ROWS`) / AVG(`ROWS`) * 100, 1) AS row_variation_pct",
             "FROM information_schema.TABLE_STATISTICS",
             f"WHERE DATABASE_NAME = {_str(database)} AND TABLE_NAME = {_str(new_name)} AND PARTITION_TYPE = 'Master';"]
    body += ["", "-- 5. Swap the names (two statements: no atomic swap in SingleStore 9.0)",
             f"ALTER TABLE {fq} RENAME TO {fq_old};",
             f"ALTER TABLE {fq_new} RENAME TO {fq};"]
    running = [x for x in pipes if str(x.get("state", "")).lower() == "running"]
    if running:
        body.append("-- Start the pipelines again (check they load into the new table):")
        body += [f"START PIPELINE {qi(database)}.{qi(x['name'])};" for x in running]
    body += ["", "-- 6. When everything works on the new table: drop the old one (frees the space)",
             f"-- DROP TABLE {fq_old};", ""]
    text = "\n".join(head + body)
    slug = re.sub(r"[^A-Za-z0-9_]+", "-", f"{table}-{'-'.join(columns)}").strip("-").lower()[:50] or "table"
    return {
        "script": text, "warnings": warnings, "file_name": f"reshard-{stamp.strftime('%Y%m%d-%H%M')}-{slug}.sql",
        "new_table": new_name, "old_table": old_name, "create": create, "insert": insert, "columns": columns,
        "current_shard": current or [], "docs": DOCS,
    }


def _str(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


# ----------------------------------------------------------------------------- facts from the cluster (read-only)

def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _num(v: Any) -> float | None:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def clean_simulation(sim: Any) -> dict[str, Any]:
    """Only the numbers of a simulation result the page sends back (they go into comments)."""
    if not isinstance(sim, dict):
        return {}
    out = {}
    for k in ("rsd_pct", "current_rsd_pct", "elapsed_ms", "fraction", "partition_count"):
        v = _num(sim.get(k))
        if v is not None and v >= 0:
            out[k] = round(v, 6) if k == "fraction" else round(v, 1)
    return out


def refers_to(definition: str, view_db: str, database: str, table: str) -> bool:
    """Does a view definition (as information_schema.VIEWS shows it) read ``database.table``?"""
    t = re.escape(qi(table))
    if re.search(re.escape(qi(database)) + r"\s*\.\s*" + t, definition):
        return True
    # same database: the definition may name the table without its database (not after a ".")
    return view_db == database and re.search(r"(?<![.`])" + t + r"(?!\s*\.)", definition) is not None


def read_facts(database: str, table: str) -> dict[str, Any]:
    """Everything the script needs, in one parallel batch: SHOW CREATE TABLE, size and skew, pipelines that load into
    the table, views and grants that refer to it, names taken in the database, who makes the script."""
    from . import security as sec
    from . import security_admin as adm
    from .apps import schema_explorer as se
    from .db import db

    q, soft = sec._q, sec._soft
    (ddl, ddl_err), skew, pipes, views, tgrants, rgrants, names, me = db.parallel(
        lambda: se._show_create(database, table),
        soft(lambda: se.table_skew(database, table)),
        soft(lambda: q("SELECT PIPELINE_NAME, STATE FROM information_schema.PIPELINES WHERE DATABASE_NAME = %s"
                       " AND JSON_EXTRACT_STRING(CONFIG_JSON, 'table') = %s ORDER BY PIPELINE_NAME", (database, table))),
        soft(lambda: q("SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS"
                       " WHERE VIEW_DEFINITION LIKE CONCAT('%%', %s, '%%')", (_like(qi(table)),))),
        soft(lambda: q("SELECT GRANTEE, PRIVILEGE_TYPE FROM information_schema.TABLE_PRIVILEGES WHERE TABLE_SCHEMA = %s"
                       " AND TABLE_NAME = %s ORDER BY GRANTEE, PRIVILEGE_TYPE", (database, table))),
        soft(lambda: q("SELECT ROLE, PRIVILEGES FROM information_schema.ROLE_PRIVILEGES WHERE `DATABASE` = %s AND `TABLE` = %s"
                       " ORDER BY ROLE", (database, table))),
        soft(lambda: q("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN (%s, %s)",
                       (database, f"{table}{NEW_SUFFIX}", f"{table}{OLD_SUFFIX}"))),
        soft(sec._current_user),
    )
    if ddl is None:
        se._require_database(database)
        raise se.NotFoundError(f"Table {table!r} not found in database {database!r} (names are case-sensitive)."
                               + (f" ({ddl_err[:200]})" if ddl_err else ""))
    rows = lambda v: v if isinstance(v, list) else []  # noqa: E731 - soft reads give {"error"} instead of rows
    sk = skew if isinstance(skew, dict) and "error" not in skew else {}
    if sk.get("kind") == "reference":
        raise ReshardError(f"{database}.{table} is a reference table: every node holds a full copy, there is no shard key.")
    grants = [f"{r['PRIVILEGE_TYPE']} to {r['GRANTEE']}" for r in rows(tgrants)]
    grants += [f"{r['PRIVILEGES']} to ROLE {sec.quote_str(r['ROLE'])}" for r in rows(rgrants)]
    unread = [what for what, v in (("pipelines", pipes), ("views", views), ("grants", tgrants), ("role grants", rgrants))
              if not isinstance(v, list)]
    who = me if isinstance(me, dict) and "error" not in me else None
    return {
        "ddl": ddl,
        "rows": sk.get("rows_total"), "data_bytes": sk.get("data_bytes_total"),
        "current_shard": ((sk.get("keys") or {}).get("shard") or {}).get("columns") if sk else None,
        "pipelines": [{"name": r["PIPELINE_NAME"], "state": r["STATE"]} for r in rows(pipes)],
        "views": [f"{qi(r['TABLE_SCHEMA'])}.{qi(r['TABLE_NAME'])}" for r in rows(views)
                  if refers_to(str(r["VIEW_DEFINITION"] or ""), r["TABLE_SCHEMA"], database, table)],
        "grants": grants,
        "existing": [r["TABLE_NAME"] for r in rows(names)],
        "unread": unread,
        "connection": adm._connection_label(),
        "prepared_by": f"{who['user']}@{who['host']}" if who else None,
    }


def reshard_script(database: str, table: str, columns: list[str], simulation: Any = None) -> dict[str, Any]:
    """Read-only: the reshard script for one table and shard key (nothing runs)."""
    facts = read_facts(database, table)
    facts["simulation"] = clean_simulation(simulation)
    out = build_script(database, table, facts.pop("ddl"), columns, facts)
    if facts["unread"]:
        note = (f"Couldn't read the {', '.join(facts['unread'])} that refer to the table (no privilege?): check them yourself.")
        out["warnings"].append(note)
        out["script"] = out["script"].replace("\n--\n-- A shard key", f"\n-- WARNING: {note}\n--\n-- A shard key", 1)
    return {**out, "database": database, "table": table, "rows": facts["rows"], "data_bytes": facts["data_bytes"],
            "pipelines": facts["pipelines"], "views": facts["views"], "grants": facts["grants"]}
