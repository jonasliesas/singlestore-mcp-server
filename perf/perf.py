"""Performance report for the SingleStore MCP server (not a pytest test: run it by hand).

    .venv\\Scripts\\python.exe perf\\perf.py [--quick] [--latency-n 20] [--soak 60] [--cold 5]

Measures cold start, warm latency (p50 / p95 per tool), concurrency, large results, a mixed-load soak and
server memory, against the cluster from the ``singlestore`` entry in ~/.claude.json (VPN needed), with an
isolated server home like the test suite. Writes perf/results/<version>-<timestamp>.json and compares it
with the most recent earlier results file: p50 / p95 changes over 25% are printed as warnings (never a
failure; network latency to the cluster varies).
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from support import DB, McpSession, remove_links, seed_home, server_env  # noqa: E402

RESULTS = Path(__file__).parent / "results"
WARN_PCT = 25.0


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def server_rss_mb(since: float) -> list[float]:
    """Working set (MB) of server processes started after ``since`` (this run's supervisor + child)."""
    if sys.platform != "win32":
        return []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*singlestore_mcp.server*' } | "
          "ForEach-Object { '{0} {1} {2}' -f $_.ProcessId, $_.CreationDate.ToUniversalTime().ToString('o'), $_.WorkingSetSize }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    sizes = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            created = datetime.fromisoformat(parts[1][:26].rstrip("Z") + "+00:00").timestamp()
            if created >= since - 2:
                sizes.append(round(int(parts[2]) / 1e6, 1))
    return sizes


class Perf:
    def __init__(self, env: dict[str, str], args: argparse.Namespace) -> None:
        self.env = env
        self.args = args
        self.report: dict = {}

    def cold_starts(self) -> None:
        ready, first = [], []
        for _ in range(self.args.cold):
            t0 = time.perf_counter()
            with McpSession(self.env, read_timeout=120) as s:
                s.list_tools()
                t1 = time.perf_counter()
                s.call("list_databases")
                t2 = time.perf_counter()
            ready.append(round(t1 - t0, 2))
            first.append(round(t2 - t1, 2))
        self.report["cold_start"] = {"ready_s": ready, "first_query_s": first,
                                     "ready_median_s": round(statistics.median(ready), 2),
                                     "first_query_median_s": round(statistics.median(first), 2)}
        print("cold start (initialize + tools/list):", self.report["cold_start"], flush=True)

    def latency(self, s: McpSession) -> None:
        cases = [
            ("alerts_badge", {}, "local only (protocol overhead)"),
            ("run_sql", {"sql": "SELECT 1"}, "1 round trip"),
            ("list_databases", {}, ""),
            ("describe_table", {"table": "CARS", "database": DB}, ""),
            ("schema_explorer_databases", {}, ""),
            ("schema_explorer_tables", {"database": DB}, ""),
            ("schema_explorer_table", {"database": DB, "table": "CARS"}, ""),
            ("schema_explorer_preview", {"database": DB, "table": "CARS", "limit": 100}, ""),
            ("sql_editor_query", {"sql": "SELECT Make, AVG(MSRP) FROM CARS GROUP BY Make", "database": DB}, "group by"),
            ("query_grid", {"sql": "SELECT * FROM CARS", "database": DB}, "model-visible"),
            ("sql_editor_schema", {"database": DB}, "autocomplete schema"),
            ("cluster_monitor_data", {}, ""),
            ("pipeline_monitor_data", {}, ""),
            ("query_history_data", {"min_ms": 1000, "hours": 24, "limit": 200}, ""),
            ("query_history_trends", {"days": 30}, ""),
            ("alerts_check_now", {}, "all alert checks"),
            ("connections_state", {}, ""),
            ("workload_overview", {}, ""),
            ("app_page", {"uri": "ui://singlestore/sql-editor.html"}, "largest page"),
        ]
        out = []
        for name, args, label in cases:
            s.call(name, args)  # warm
            xs, last = [], None
            for _ in range(self.args.latency_n):
                last = s.call(name, args)
                xs.append(last.ms)
            r = {"tool": name, "label": label, "p50": round(statistics.median(xs)), "p95": round(pct(xs, 95)), "max": round(max(xs)),
                 "chars": last.chars, "error": last.is_error}
            out.append(r)
            print(f"  {name:28} {label:30} p50 {r['p50']:6} ms  p95 {r['p95']:6} ms  max {r['max']:6} ms  {r['chars']:9} ch"
                  + ("  ERROR " + last.text[:120] if last.is_error else ""), flush=True)
        self.report["latency"] = out

    def concurrency(self, s: McpSession) -> None:
        res = {}
        for k in (1, 5, 10, 20):
            t0 = time.perf_counter()
            rs = s.gather([("run_sql", {"sql": "SELECT SLEEP(0.5) AS s"})] * k)
            wall = (time.perf_counter() - t0) * 1000
            res[f"sleep0.5 x{k}"] = {"wall_ms": round(wall), "errors": sum(r.is_error for r in rs)}
            print(f"  {k:2} parallel SELECT SLEEP(0.5): wall {wall:6.0f} ms, errors {res[f'sleep0.5 x{k}']['errors']}", flush=True)
        calls = [("schema_explorer_databases", {}), ("schema_explorer_tables", {"database": DB}), ("sql_editor_schema", {"database": DB}),
                 ("connections_state", {}), ("alerts_badge", {}), ("cluster_monitor_data", {}), ("pipeline_monitor_data", {}),
                 ("query_history_data", {"min_ms": 1000, "hours": 24, "limit": 200})]
        t0 = time.perf_counter()
        for n, a in calls:
            s.call(n, a)
        serial = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        rs = s.gather(calls)
        par = (time.perf_counter() - t0) * 1000
        res["workspace open, 8 calls"] = {"serial_ms": round(serial), "parallel_ms": round(par), "errors": sum(r.is_error for r in rs)}
        print(f"  workspace-open mix (8 calls): serial {serial:.0f} ms, parallel {par:.0f} ms", flush=True)
        self.report["concurrency"] = res

    def large(self, s: McpSession) -> None:
        cases = [
            ("query_grid", {"sql": "SELECT * FROM sashelp.cars", "max_rows": 10000}, "428 rows"),
            ("sql_editor_query", {"sql": "SELECT a.* FROM sashelp.cars a, sashelp.cars b LIMIT 20000", "database": DB, "max_rows": 20000}, "20k rows x 15 cols"),
            ("sql_editor_query", {"sql": "SELECT a.* FROM sashelp.cars a, sashelp.cars b LIMIT 100000", "database": DB, "max_rows": 100000}, "100k rows asked"),
            ("run_sql", {"sql": "SELECT a.* FROM sashelp.cars a, sashelp.cars b LIMIT 5000", "max_rows": 5000}, "5000 rows, model-visible"),
            ("schema_explorer_tables", {"database": "sashelp"}, "354 tables"),
            ("query_history_data", {"min_ms": 0, "hours": 168, "limit": 2000}, "2000 events"),
        ]
        out = []
        for name, args, label in cases:
            r = s.call(name, args)
            d = r.data
            rows = len(d.get("rows", d.get("events", []))) if isinstance(d, dict) else None
            out.append({"tool": name, "label": label, "ms": r.ms, "text_chars": r.text_chars, "struct_chars": r.struct_chars,
                        "rows": rows, "error": r.is_error, "excerpt": r.text[:150] if r.is_error else ""})
            print(f"  {name:22} {label:26} {r.ms:8.0f} ms  rows {rows}, text {r.text_chars} / structured {r.struct_chars} chars"
                  + ("  ERROR " + r.text[:120] if r.is_error else ""), flush=True)
        self.report["large"] = out

    def soak(self, s: McpSession) -> None:
        seconds = self.args.soak
        mix = [("run_sql", {"sql": "SELECT COUNT(*) FROM CARS", "database": DB}), ("schema_explorer_table", {"database": DB, "table": "CARS"}),
               ("sql_editor_query", {"sql": "SELECT * FROM CARS LIMIT 50", "database": DB}), ("alerts_badge", {}),
               ("query_history_data", {"min_ms": 1000, "hours": 24, "limit": 100}), ("cluster_monitor_data", {})]
        t0 = time.perf_counter()
        first, last, n, errs = [], [], 0, 0
        while time.perf_counter() - t0 < seconds:
            name, args = mix[n % len(mix)]
            r = s.call(name, args)
            (first if time.perf_counter() - t0 < seconds / 3 else last).append(r.ms)
            errs += r.is_error
            n += 1
        self.report["soak"] = {"calls": n, "errors": errs, "seconds": seconds,
                               "first_third_median_ms": round(statistics.median(first)) if first else None,
                               "last_two_thirds_median_ms": round(statistics.median(last)) if last else None}
        print("  soak:", self.report["soak"], flush=True)

    def run(self) -> dict:
        self.cold_starts()
        start = time.time()
        with McpSession(self.env, read_timeout=600) as s:
            if s.call("list_databases", timeout=45).is_error:
                raise SystemExit("The cluster isn't reachable (VPN?): no performance run.")
            time.sleep(2)
            self.report["rss_mb_start"] = server_rss_mb(start)
            print(f"\n== warm latency ({self.args.latency_n} calls each)", flush=True)
            self.latency(s)
            print("\n== concurrency", flush=True)
            self.concurrency(s)
            print("\n== large results", flush=True)
            self.large(s)
            print(f"\n== {self.args.soak} s soak", flush=True)
            self.soak(s)
            self.report["rss_mb_end"] = server_rss_mb(start)
            print("server memory MB (supervisor, child) start", self.report["rss_mb_start"], "end", self.report["rss_mb_end"])
        return self.report


def compare(new: dict, old: dict, old_name: str) -> list[str]:
    """Warnings for p50 / p95 changes over WARN_PCT percent against an earlier results file."""
    warnings = []
    print(f"\n== compared with {old_name}")

    def check(what: str, a: float | None, b: float | None, floor: float = 5) -> None:
        if a is None or b is None or max(a, b) < floor:  # changes of a few ms are noise
            return
        change = (b - a) / a * 100 if a else 0
        flag = abs(change) > WARN_PCT
        line = f"  {'WARN' if flag else 'ok  '} {what:62} {a:8.0f} -> {b:8.0f}  ({change:+.0f}%)"
        print(line)
        if flag:
            warnings.append(line.strip())

    oc, nc = old.get("cold_start") or {}, new.get("cold_start") or {}
    check("cold start ready (median s, x1000)", (oc.get("ready_median_s") or 0) * 1000 or None, (nc.get("ready_median_s") or 0) * 1000 or None)
    olat = {(r["tool"], r["label"]): r for r in old.get("latency") or []}
    for r in new.get("latency") or []:
        o = olat.get((r["tool"], r["label"]))
        if o:
            name = f"{r['tool']} {r['label']}".strip()
            check(f"{name} p50 ms", o["p50"], r["p50"])
            check(f"{name} p95 ms", o["p95"], r["p95"])
    olarge = {(r["tool"], r["label"]): r for r in old.get("large") or []}
    for r in new.get("large") or []:
        o = olarge.get((r["tool"], r["label"]))
        if o and not r["error"] and not o["error"]:
            check(f"large: {r['tool']} {r['label']} ms", o["ms"], r["ms"], floor=50)
    return warnings


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--latency-n", type=int, default=20, help="calls per tool for the latency percentiles")
    ap.add_argument("--soak", type=int, default=60, help="seconds of mixed load")
    ap.add_argument("--cold", type=int, default=5, help="cold starts to measure")
    ap.add_argument("--quick", action="store_true", help="short run: 5 latency calls, 15 s soak, 2 cold starts")
    args = ap.parse_args()
    if args.quick:
        args.latency_n, args.soak, args.cold = 5, 15, 2

    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    tmp = Path(tempfile.mkdtemp(prefix="s2-perf-"))
    home, sql_dir = tmp / "home", tmp / "sql"
    sql_dir.mkdir(parents=True)
    seed_home(home)
    try:
        t0 = time.time()
        report = Perf(server_env(home, sql_dir), args).run()
    finally:
        remove_links(home)
        shutil.rmtree(tmp, ignore_errors=True)
    report["meta"] = {"version": version, "started": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
                      "seconds": round(time.time() - t0), "args": vars(args), "python": sys.version.split()[0]}

    RESULTS.mkdir(exist_ok=True)
    previous = sorted(RESULTS.glob("*.json"), key=lambda p: p.stat().st_mtime)
    out = RESULTS / f"{version}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwrote {out.relative_to(ROOT)}")
    if previous:
        old = json.loads(previous[-1].read_text(encoding="utf-8"))
        warnings = compare(report, old, previous[-1].name)
        print(f"\n{len(warnings)} change(s) over {WARN_PCT:.0f}%" + (":" if warnings else ""))
        for w in warnings:
            print("  " + w)
    else:
        print("(no earlier results to compare with)")


if __name__ == "__main__":
    main()
