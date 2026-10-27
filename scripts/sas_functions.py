"""The SAS function matrix (src/singlestore_mcp/data/sas_functions.json): verify it in SAS, render the README table.

    .venv\\Scripts\\python.exe scripts\\sas_functions.py --verify [procsql fedsql cas] [--only NAME ...]
    .venv\\Scripts\\python.exe scripts\\sas_functions.py --table      # rewrite the README table between the markers

--verify starts an isolated test server (like ``pytest --sas``: tests/support.py, its own SAS sessions, the SAS
Viya sign-in copied from the SAS Viya MCP server / CLI) and runs each function's test statement read-only:
PROC SQL and FedSQL on the S2 library (SAS/ACCESS to SingleStore), FedSQL in CAS on a loaded table of a SAS
SpeedyStore caslib (``test_tables`` in the JSON). It records per dialect whether the statement ran ("supported")
and whether the function went to SingleStore ("pushdown": the Pushdown verdict - PROC SQL sastrace, FedSQL the
_method plan, CAS the method=true plan), marked ``"how": "verified"`` with the date. Rows without a run keep
what the documentation says (``"how": "documented"``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "src" / "singlestore_mcp" / "data" / "sas_functions.json"
README = ROOT / "README.md"
BEGIN, END = "<!-- sas-functions:begin -->", "<!-- sas-functions:end -->"
DIALECTS = ("procsql", "fedsql", "cas")


def load() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def save(data: dict[str, Any]) -> None:
    DATA.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def verify(dialects: list[str], only: set[str]) -> None:
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "src"))
    from support import McpSession, remove_links, seed_home, server_env

    from singlestore_mcp.sas_functions import pushdown_of, statement

    data = load()
    tables = data["test_tables"]
    today = dt.date.today().isoformat()
    home = Path(tempfile.mkdtemp(prefix="s2home_"))
    sqld = Path(tempfile.mkdtemp(prefix="s2sql_"))
    seed_home(home)
    s = McpSession(server_env(home, sqld), read_timeout=900)
    try:
        for fn in data["functions"]:
            if only and fn["name"].upper() not in only:
                continue
            for d in dialects:
                sql = statement(fn, d, tables)
                rid = "fm" + uuid.uuid4().hex[:10]
                r = s.call("sas_query", {"language": d, "sql": sql, "library": "CASUSER" if d == "cas" else None,
                                         "max_rows": 5, "history": False, "pushdown": True, "run_id": rid,
                                         "wait_ms": 180_000}, timeout=300)
                res = r.data if isinstance(r.data, dict) else {}
                if res.get("running"):
                    s.call("sas_stop", {"run_id": rid}, timeout=60)
                    print(f"{fn['name']:<20} {d:<8} timeout")
                    continue
                err = res.get("error") or (r.text[:300] if r.is_error else None)
                entry: dict[str, Any] = {"how": "verified", "date": today, "sql": sql}
                if err:
                    first = next((x for x in (res.get("log") or "").splitlines() if x.startswith("ERROR")), str(err))
                    entry.update(supported=False, pushdown="n/a", error=first[:200])
                else:
                    entry["supported"] = True
                    entry["pushdown"] = pushdown_of(d, res)
                    if d != "cas":
                        sent = [x.get("sql") for x in (res.get("pushdown") or {}).get("sent") or []]
                        if sent:
                            entry["sent"] = sent[0][:300]
                old = fn.get(d) or {}
                if old.get("note"):
                    entry["note"] = old["note"]
                fn[d] = entry
                print(f"{fn['name']:<20} {d:<8} {'ok ' if entry['supported'] else 'ERR'} pushdown={entry['pushdown']:<8} "
                      f"{entry.get('error', '')[:90]}", flush=True)
            save(data)
    finally:
        s.close()
        remove_links(home)


def _cell(e: dict[str, Any], dialect: str, fn: dict[str, Any]) -> str:
    if not e:
        return "?"
    if e.get("supported") is False:
        return "✗"
    p = e.get("pushdown")
    if dialect == "procsql" and p != "yes" and fn.get("access_map") == "all":
        p = "ALL"
    mark = {"yes": "✓ ↓", "no": "✓", "partly": "✓ (↓)", "ALL": "✓ (ALL)"}.get(p or "", "✓ ?")
    return mark + ("" if e.get("how") == "verified" else " *")


def table() -> str:
    data = load()
    lines = ["| Function | Category | PROC SQL | FedSQL | FedSQL in CAS | SingleStore | Instead |",
             "|---|---|---|---|---|---|---|"]
    for fn in data["functions"]:
        name = fn["name"].replace("|", "\\|")
        alt = (fn.get("alternative") or "").replace("|", "\\|")
        lines.append(f"| `{name}` | {fn['category']} | {_cell(fn.get('procsql') or {}, 'procsql', fn)} | "
                     f"{_cell(fn.get('fedsql') or {}, 'fedsql', fn)} | {_cell(fn.get('cas') or {}, 'cas', fn)} | "
                     f"{fn.get('singlestore') or ''} | {alt} |")
    return "\n".join(lines)


def write_table() -> None:
    text = README.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise SystemExit(f"README.md has no {BEGIN} … {END} markers")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    README.write_text(f"{head}{BEGIN}\n{table()}\n{END}{tail}", encoding="utf-8")
    print("README table updated")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", nargs="*", metavar="DIALECT", help="run the checks (procsql fedsql cas; default all)")
    ap.add_argument("--only", nargs="*", default=[], help="only these functions")
    ap.add_argument("--table", action="store_true", help="rewrite the README table")
    a = ap.parse_args()
    if a.verify is not None:
        verify([d for d in (a.verify or DIALECTS) if d in DIALECTS], {re.sub(r"\s+", " ", x).upper() for x in a.only})
    if a.table:
        write_table()
    if a.verify is None and not a.table:
        print(table())


if __name__ == "__main__":
    main()
