"""Workspace start-up benchmark (not a pytest test: run it by hand, VPN needed).

    .venv\\Scripts\\python.exe perf\\startup.py [--runs 3] [--restarts 3] [--no-browser]

Measures what Jonas waits for when he opens the SingleStore Workspace, cold and right after
``restart_server``, against the cluster of the active saved connection, with an isolated server home
like the test suite (the running server is never touched):

* imports: wall time of ``import singlestore_mcp.server_impl`` and the biggest packages (-X importtime)
* cold: server spawn -> MCP handshake done -> first tool answer (``sql_editor``, the call that opens
  the Workspace) -> the calls the Workspace and its SQL view make on open (ms and size of each)
* restart: ``restart_server`` duration -> first ``sql_editor`` answer -> the same open sequence
* browser: the Workspace in Edge (browser view): page load -> SQL Editor frame -> schema tree shown,
  with every request the page makes (tool, ms, bytes)
* round trips: cluster round trips (connects + statements) per call of the open sequence, in-process

Writes perf/results/startup-<timestamp>.json and prints a comparison with the previous startup file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from support import DB, PYTHON, McpSession, remove_links, seed_home, server_env  # noqa: E402

RESULTS = Path(__file__).parent / "results"
SQL_EDITOR_URI = "ui://singlestore/sql-editor.html"
WORKSPACE_URI = "ui://singlestore/workspace.html"


def med(xs: list[float]) -> float:
    return round(statistics.median(xs), 3) if xs else 0.0


# ----------------------------------------------------------------------------- imports

def imports(env: dict[str, str], runs: int) -> dict[str, Any]:
    code = "import time;t=time.perf_counter();import singlestore_mcp.server_impl;print(time.perf_counter()-t)"
    walls = []
    for _ in range(runs):
        out = subprocess.run([str(PYTHON), "-c", code], env=env, cwd=ROOT, capture_output=True, text=True, check=True)
        walls.append(float(out.stdout.strip().splitlines()[-1]))
    err = subprocess.run([str(PYTHON), "-X", "importtime", "-c", "import singlestore_mcp.server_impl"], env=env, cwd=ROOT,
                         capture_output=True, text=True).stderr
    top: dict[str, int] = {}
    for line in err.splitlines():
        m = re.match(r"import time:\s+(\d+) \|\s+(\d+) \|( *)(\S+)", line)
        if m and len(m.group(3)) <= 3:  # top-level imports of server_impl and their packages
            pkg = m.group(4).split(".")[0] if not m.group(4).startswith("singlestore_mcp") else m.group(4)
            top[pkg] = top.get(pkg, 0) + int(m.group(2))
    biggest = dict(sorted(((k, round(v / 1e6, 3)) for k, v in top.items()), key=lambda kv: -kv[1])[:8])
    r = {"server_impl_import_s": [round(w, 3) for w in walls], "median_s": med(walls), "biggest_cumulative_s": biggest}
    print("imports:", r, flush=True)
    return r


# ----------------------------------------------------------------------------- open sequence over MCP

def open_sequence(s: McpSession, t_spawn: float | None = None) -> dict[str, Any]:
    """The calls a host + the Workspace + its SQL view make when the Workspace opens, in their order."""
    steps: list[dict[str, Any]] = []
    t0 = time.perf_counter()

    def step(name: str, r: Any, ms: float, chars: int) -> None:
        steps.append({"call": name, "ms": round(ms), "chars": chars, "error": bool(getattr(r, "is_error", False))})

    r = s.call("sql_editor", {"database": DB})          # the host opens the Workspace
    step("sql_editor", r, r.ms, r.chars)
    t = time.perf_counter()
    res = s.read_resource(WORKSPACE_URI)                # the host loads the Workspace page
    ws_html = res.contents[0].text
    step("resources/read workspace", None, (time.perf_counter() - t) * 1000, len(ws_html))
    head = re.search(r'<meta name="s2-head" content="([0-9a-f]+)">', ws_html)  # the Workspace sends it, if it has one
    # Workspace start: alerts badge and connection label in the background, tabs awaited, then the SQL view's page.
    bg = s.gather([("alerts_badge", {}), ("connections_state", {}), ("sql_editor_tabs", {})])
    for b in bg:
        step(b.name, b, b.ms, b.chars)
    r = s.call("app_page", {"uri": SQL_EDITOR_URI, **({"head": head.group(1)} if head else {})})
    step("app_page sql-editor", r, r.ms, r.chars)
    r = s.call("sql_editor_state", {})                   # the SQL view restores its draft
    step("sql_editor_state", r, r.ms, r.chars)
    rs = s.gather([("sql_editor_schema", {"database": DB}), ("sql_editor_assistant", {})])
    for b in rs:
        step(b.name, b, b.ms, b.chars)
    total = (time.perf_counter() - t0) * 1000
    out = {"steps": steps, "sequence_ms": round(total), "bytes": sum(x["chars"] for x in steps)}
    if t_spawn is not None:
        out["spawn_to_usable_ms"] = round((time.perf_counter() - t_spawn) * 1000)
    return out


def cold(env: dict[str, str], runs: int) -> dict[str, Any]:
    rows = []
    for i in range(runs):
        t0 = time.perf_counter()
        with McpSession(env, read_timeout=120) as s:
            handshake = time.perf_counter() - t0
            seq = open_sequence(s, t0)
            first = seq["steps"][0]["ms"]
        rows.append({"handshake_s": round(handshake, 2), "first_answer_ms": first, **seq})
        print(f"  cold {i + 1}: handshake {handshake:.2f} s, first sql_editor {first} ms, open sequence {seq['sequence_ms']} ms, "
              f"spawn->usable {seq['spawn_to_usable_ms']} ms", flush=True)
    return summarize(rows, "handshake_s")


def restarts(env: dict[str, str], runs: int, standby_wait: float) -> dict[str, Any]:
    rows = []
    with McpSession(env, read_timeout=120) as s:
        s.call("sql_editor", {"database": DB})
        s.call("app_page", {"uri": SQL_EDITOR_URI})
        for i in range(runs):
            # Like editing code between restarts: the supervisor prepares its standby 5 s after a start.
            time.sleep(standby_wait)
            t0 = time.perf_counter()
            r = s.call("restart_server")
            restart_ms = r.ms
            msg = (r.data or {}).get("message", "") if isinstance(r.data, dict) else r.text
            seq = open_sequence(s, t0)
            rows.append({"restart_ms": round(restart_ms), "message": msg, **seq})
            print(f"  restart {i + 1}: restart_server {restart_ms:.0f} ms, first sql_editor {seq['steps'][0]['ms']} ms, "
                  f"open sequence {seq['sequence_ms']} ms, restart->usable {seq['spawn_to_usable_ms']} ms", flush=True)
    return summarize(rows, "restart_ms")


def summarize(rows: list[dict[str, Any]], head: str) -> dict[str, Any]:
    per_call: dict[str, list[float]] = {}
    chars: dict[str, int] = {}
    for row in rows:
        for st in row["steps"]:
            per_call.setdefault(st["call"], []).append(st["ms"])
            chars[st["call"]] = st["chars"]
    return {
        "runs": rows,
        f"{head}_median": med([r[head] for r in rows]),
        "first_answer_ms_median": med([r["steps"][0]["ms"] for r in rows]),
        "sequence_ms_median": med([r["sequence_ms"] for r in rows]),
        "to_usable_ms_median": med([r["spawn_to_usable_ms"] for r in rows]),
        "per_call_ms_median": {k: med(v) for k, v in per_call.items()},
        "per_call_chars": chars,
    }


# ----------------------------------------------------------------------------- browser view

READY_JS = """() => {
  const ok = [...document.querySelectorAll('iframe')].length >= 0;
  return ok && document.title === 'SQL Editor' && !!document.querySelector('.cm-content')
    && document.querySelectorAll('.sq-row.tbl').length > 0;
}"""


def browser(env: dict[str, str], runs: int) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    channel = os.environ.get("S2_TEST_BROWSER_CHANNEL", "msedge")
    rows = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(**({"channel": channel} if channel not in ("", "bundled", "chromium") else {}))
        for i in range(runs):
            t_spawn = time.perf_counter()
            with McpSession(env, read_timeout=120) as s:
                url = s.ok("browser_link", {"tool": "sql_editor", "arguments": {"database": DB}})["url"]
                t_link = time.perf_counter()
                ctx = b.new_context(viewport={"width": 1400, "height": 900})
                page = ctx.new_page()
                reqs: list[dict[str, Any]] = []

                def on_done(req: Any) -> None:
                    try:
                        name = req.url.split("?")[0].rsplit("/", 1)[-1] or "host page"
                        if name == "call":
                            name = (req.post_data_json or {}).get("name", "call")
                        elif name == "resource":
                            name = "resource " + req.url.split("uri=")[-1][:60]
                        res = req.response()
                        size = int((res.all_headers().get("content-length") or 0)) if res else 0
                        tm = req.timing
                        reqs.append({"req": name, "start_ms": round(tm["startTime"]), "ms": round(tm["responseEnd"]), "bytes": size})
                    except Exception:  # noqa: BLE001
                        pass

                page.on("requestfinished", on_done)
                t0 = time.perf_counter()
                page.goto(url, wait_until="domcontentloaded")
                marks: dict[str, float] = {}
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and "schema_tree" not in marks:
                    for f in page.frames:
                        try:
                            title = f.evaluate("document.title")
                        except Exception:  # noqa: BLE001
                            continue
                        now = round((time.perf_counter() - t0) * 1000)
                        if title == "SingleStore Workspace" and "workspace_frame" not in marks:
                            if f.evaluate("!!document.querySelector('nav.ws-rail')"):
                                marks["workspace_frame"] = now
                        if title == "SQL Editor":
                            marks.setdefault("sql_frame", now)
                            try:
                                if f.evaluate("!!document.querySelector('.cm-content')"):
                                    marks.setdefault("editor_ready", now)
                                if f.evaluate("document.querySelectorAll('.sq-row.tbl').length > 0"):
                                    marks.setdefault("schema_tree", now)
                            except Exception:  # noqa: BLE001
                                pass
                    time.sleep(0.02)
                ctx.close()
            first = min((r["start_ms"] for r in reqs), default=0)
            for r in reqs:
                r["start_ms"] = r["start_ms"] - first
            row = {"spawn_to_link_ms": round((t_link - t_spawn) * 1000), "marks_ms": marks, "requests": reqs,
                   "bytes": sum(r["bytes"] for r in reqs)}
            rows.append(row)
            print(f"  browser {i + 1}: link after {row['spawn_to_link_ms']} ms; {marks}; {len(reqs)} requests, {row['bytes']} bytes", flush=True)
        b.close()
    keys = ("workspace_frame", "sql_frame", "editor_ready", "schema_tree")
    return {"runs": rows, "marks_ms_median": {k: med([r["marks_ms"].get(k, 0) for r in rows]) for k in keys},
            "bytes_median": med([r["bytes"] for r in rows])}


# ----------------------------------------------------------------------------- round trips (in-process)

def page_sizes(env: dict[str, str]) -> dict[str, Any]:
    """Characters each Workspace view's page brings through the host: whole, and without the shared head."""
    code = """
import json
from singlestore_mcp.apps import _core
import singlestore_mcp.server_impl  # registers every page
out = {}
for uri in sorted(_core._PAGES):
    _, full, lite = _core.page_parts(uri)
    out[uri.rsplit("/", 1)[-1]] = {"full": len(full), "without_head": len(lite)}
print("JSON" + json.dumps(out))
"""
    out = subprocess.run([str(PYTHON), "-c", code], env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)
    line = next((x for x in out.stdout.splitlines() if x.startswith("JSON")), None)
    if not line:  # older code without page_parts
        return {}
    r = json.loads(line[4:])
    for k, v in r.items():
        print(f"  {k:28} {v['full']:9} -> {v['without_head']:9} chars")
    return r


def round_trips(env: dict[str, str]) -> dict[str, Any]:
    """Connects and statements per call of the open sequence, counted in a child process (same server code)."""
    code = r'''
import asyncio, json, sys, time
import singlestoredb as s2
counts = {"connect": 0, "statements": 0, "connect_ms": []}
real = s2.connect
class Cur:
    def __init__(self, c): self._c = c
    def execute(self, *a, **k):
        counts["statements"] += 1
        return self._c.execute(*a, **k)
    def __getattr__(self, n): return getattr(self._c, n)
class Conn:
    def __init__(self, c): self._c = c
    def cursor(self, *a, **k): return Cur(self._c.cursor(*a, **k))
    def __getattr__(self, n): return getattr(self._c, n)
def connect(*a, **k):
    t = time.perf_counter(); c = real(*a, **k)
    counts["connect"] += 1; counts["connect_ms"].append(round((time.perf_counter() - t) * 1000))
    return Conn(c)
s2.connect = connect
from singlestore_mcp.server_impl import mcp
DB = sys.argv[1]
calls = [("sql_editor", {"database": DB}), ("alerts_badge", {}), ("connections_state", {}), ("sql_editor_tabs", {}),
         ("app_page", {"uri": "ui://singlestore/sql-editor.html"}), ("sql_editor_state", {}),
         ("sql_editor_schema", {"database": DB}), ("sql_editor_assistant", {})]
out = []
for name, args in calls:
    before = dict(counts, connect_ms=list(counts["connect_ms"]))
    t = time.perf_counter()
    asyncio.run(mcp.call_tool(name, args))
    out.append({"call": name, "ms": round((time.perf_counter() - t) * 1000), "connects": counts["connect"] - before["connect"],
                "statements": counts["statements"] - before["statements"]})
print("JSON" + json.dumps({"calls": out, "connect_ms": counts["connect_ms"]}))
'''
    env = {**env, "SINGLESTORE_MCP_NO_SUPERVISOR": "1"}
    out = subprocess.run([str(PYTHON), "-c", code, DB], env=env, cwd=ROOT, capture_output=True, text=True, timeout=300)
    line = next((x for x in out.stdout.splitlines() if x.startswith("JSON")), None)
    if not line:
        print("round trips: failed", out.stderr[-800:])
        return {}
    r = json.loads(line[4:])
    r["total_round_trips"] = sum(c["connects"] + c["statements"] for c in r["calls"])
    for c in r["calls"]:
        print(f"  {c['call']:22} {c['ms']:6} ms  connects {c['connects']}  statements {c['statements']}")
    print(f"  connect times (ms): {r['connect_ms']}; total round trips (connects + statements): {r['total_round_trips']}")
    return r


# ----------------------------------------------------------------------------- main

def compare(new: dict, old: dict, name: str) -> None:
    print(f"\n== compared with {name}")

    def row(what: str, a: Any, b: Any) -> None:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a:
            print(f"  {what:52} {a:10.2f} -> {b:10.2f}  ({(b - a) / a * 100:+.0f}%)")

    row("import server_impl (s)", old.get("imports", {}).get("median_s"), new.get("imports", {}).get("median_s"))
    for sec, head in (("cold", "handshake_s_median"), ("restart", "restart_ms_median")):
        o, n = old.get(sec) or {}, new.get(sec) or {}
        for k in (head, "first_answer_ms_median", "sequence_ms_median", "to_usable_ms_median"):
            row(f"{sec}: {k}", o.get(k), n.get(k))
        for k, v in (n.get("per_call_ms_median") or {}).items():
            row(f"{sec}: {k} ms", (o.get("per_call_ms_median") or {}).get(k), v)
        for k, v in (n.get("per_call_chars") or {}).items():
            row(f"{sec}: {k} chars", (o.get("per_call_chars") or {}).get(k), v)
    ob, nb = (old.get("browser") or {}).get("marks_ms_median") or {}, (new.get("browser") or {}).get("marks_ms_median") or {}
    for k, v in nb.items():
        row(f"browser: {k} ms", ob.get(k), v)
    row("browser: bytes", (old.get("browser") or {}).get("bytes_median"), (new.get("browser") or {}).get("bytes_median"))
    row("round trips on open", (old.get("round_trips") or {}).get("total_round_trips"), (new.get("round_trips") or {}).get("total_round_trips"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3, help="cold starts / browser opens to measure")
    ap.add_argument("--restarts", type=int, default=3)
    ap.add_argument("--restart-gap", type=float, default=10, help="seconds between restarts (the standby needs ~5 s + its load)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--label", default="", help="added to the results file name")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="s2-startup-"))
    home, sql_dir = tmp / "home", tmp / "sql"
    sql_dir.mkdir(parents=True)
    seed_home(home)
    report: dict[str, Any] = {}
    try:
        env = server_env(home, sql_dir)
        with McpSession(env, read_timeout=120) as s:
            if s.call("list_databases", timeout=45).is_error:
                raise SystemExit("The cluster isn't reachable (VPN?): no start-up run.")
        print("== imports", flush=True)
        report["imports"] = imports(env, 3)
        print("\n== cold start + Workspace open", flush=True)
        report["cold"] = cold(env, args.runs)
        print("\n== restart_server + Workspace open", flush=True)
        report["restart"] = restarts(env, args.restarts, args.restart_gap)
        if not args.no_browser:
            print("\n== browser view (Edge)", flush=True)
            report["browser"] = browser(env, args.runs)
        print("\n== view page sizes", flush=True)
        report["page_sizes"] = page_sizes(env)
        print("\n== cluster round trips on open (in-process)", flush=True)
        report["round_trips"] = round_trips(env)
    finally:
        remove_links(home)
        shutil.rmtree(tmp, ignore_errors=True)
    report["meta"] = {"started": datetime.now().isoformat(timespec="seconds"), "args": vars(args)}
    RESULTS.mkdir(exist_ok=True)
    previous = sorted(RESULTS.glob("startup-*.json"), key=lambda p: p.stat().st_mtime)
    out = RESULTS / f"startup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{'-' + args.label if args.label else ''}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwrote {out.relative_to(ROOT)}")
    if previous:
        compare(report, json.loads(previous[-1].read_text(encoding="utf-8")), previous[-1].name)


if __name__ == "__main__":
    asyncio.set_event_loop_policy(None)
    main()
