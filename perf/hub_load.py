"""Load test for the server pilot's hub (not a pytest test: run it by hand; no VPN, no cluster, no real SAS Logon).

    .venv\\Scripts\\python.exe perf\\hub_load.py [--users 10] [--n 200] [--seconds 30] [--workers 10]

The hub runs in this process with real Studio processes (python -m singlestore_mcp.studio_server) and the
test suite's fake SAS Logon. The load comes from separate worker processes, so the hub doesn't share a GIL
with its clients. Measures:

* sign-in: N users sign in at the same time -> sign-in done -> their Studio answers its first call
* overhead: the same calls through the hub and straight to the user's Studio (127.0.0.1:<port>/<token>/),
  a small tool call (sql_editor_state) and a large page (the SQL Editor's HTML, ~0.5 MB)
* concurrency: every user calls at once for ``--seconds``, ``--rate`` calls a second each (a busy page)
  -> p50 / p95 / p99, calls a second, errors; then a short flat-out burst: the hub's ceiling on this PC
* crash under load: one user's Studio is killed while the others go on -> time until it answers again,
  errors the other users saw (should be none)
* memory: the hub and every Studio after the load

Tool calls here don't reach the cluster: the cluster's own time (~120 ms+ a round trip) is what perf.py
measures; this measures what the hub adds on top. Writes perf/results/hub-<timestamp>.json.
"""

from __future__ import annotations

import argparse
import http.client
import json
import multiprocessing as mp
import os
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
RESULTS = Path(__file__).parent / "results"
PAGE = "ui://singlestore/sql-editor.html"


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 1) if xs else 0.0


def summary(ms: list[float]) -> dict[str, float]:
    return {"n": len(ms), "p50": pct(ms, 50), "p95": pct(ms, 95), "p99": pct(ms, 99),
            "mean": round(statistics.fmean(ms), 1) if ms else 0.0}


# ----------------------------------------------------------------------------- worker side (own process)

class Client:
    """One keep-alive-free HTTP client (the page's fetch opens a fresh request each time, like this)."""

    def __init__(self, port: int, prefix: str, cookies: str = "", origin: str = "") -> None:
        self.port, self.prefix, self.cookies, self.origin = port, prefix, cookies, origin

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int | str, int, float]:
        h = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookies:
            h["Cookie"] = self.cookies
        if method == "POST":
            h["Origin"], h["Content-Type"] = self.origin, "application/json"
        t0 = time.perf_counter()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            conn.request(method, self.prefix + path, body=body, headers=h)
            r = conn.getresponse()
            data = r.read()
            return r.status, len(data), (time.perf_counter() - t0) * 1000
        except (OSError, http.client.HTTPException) as exc:  # status "0 <error>": no HTTP answer at all
            return f"0 {type(exc).__name__} {getattr(exc, 'winerror', None) or getattr(exc, 'errno', '')}", 0, (time.perf_counter() - t0) * 1000
        finally:
            conn.close()

    def call(self, name: str, args: dict[str, Any] | None = None) -> tuple[int, int, float]:
        return self.request("POST", "call", json.dumps({"name": name, "arguments": args or {}}).encode())

    def page(self) -> tuple[int, int, float]:
        return self.request("GET", "resource?uri=" + PAGE)


def _client(spec: dict[str, Any]) -> Client:
    return Client(spec["port"], spec["prefix"], spec.get("cookies", ""), spec["origin"])


def w_overhead(spec: dict[str, Any], n: int) -> dict[str, Any]:
    """n small calls and n/4 large pages, alternating hub/direct per round so drift hits both alike."""
    hub, direct = _client(spec["hub"]), _client(spec["direct"])
    out: dict[str, list[float]] = {"call_hub": [], "call_direct": [], "page_hub": [], "page_direct": []}
    sizes: dict[str, int] = {}
    errors = 0
    for c in (hub, direct):  # warm-up
        c.call("sql_editor_state")
        c.page()
    for i in range(n):
        for key, c in (("hub", hub), ("direct", direct)) if i % 2 else (("direct", direct), ("hub", hub)):
            st, _, ms = c.call("sql_editor_state")
            errors += st != 200
            out[f"call_{key}"].append(ms)
            if i % 4 == 0:
                st, size, ms = c.page()
                errors += st != 200
                out[f"page_{key}"].append(ms)
                sizes["page"] = size
    return {"ms": out, "sizes": sizes, "errors": errors}


def w_load(spec: dict[str, Any], until: float, rate: float = 0) -> dict[str, Any]:
    """One user's page: one call at a time, ``rate`` calls a second (0: as fast as it can), until ``until``."""
    c = _client(spec["hub"])
    gap = 1 / rate if rate else 0.0
    nxt = time.time()
    ms: list[float] = []
    statuses: dict[str, int] = {}
    timeline: list[tuple[float, int]] = []
    i = 0
    while time.time() < until:
        if gap:
            nxt += gap
            time.sleep(max(0.0, nxt - time.time()))
        st, _, t = (c.page() if i % 10 == 9 else c.call("sql_editor_state"))
        i += 1
        statuses[str(st)] = statuses.get(str(st), 0) + 1
        timeline.append((round(time.time(), 3), st))
        if st == 200:
            ms.append(t)
        else:
            time.sleep(0.2)  # the page waits a moment after a "restarting" answer
    return {"user": spec["user"], "ms": ms, "statuses": statuses, "timeline": timeline}


# ----------------------------------------------------------------------------- hub side (this process)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--n", type=int, default=200, help="calls each way for the overhead comparison")
    ap.add_argument("--seconds", type=int, default=30, help="length of the concurrent load")
    ap.add_argument("--rate", type=float, default=5, help="calls a second per user (a busy page; 0 = flat out)")
    ap.add_argument("--burst-seconds", type=int, default=5, help="a short flat-out run: the hub's ceiling")
    ap.add_argument("--crash-seconds", type=int, default=30, help="length of the load with one crash in it")
    args = ap.parse_args()

    from fake_saslogon import TEST_CLIENT_SECRET, FakeSasLogon
    from test_hub import MASTER, sign_in

    from singlestore_mcp.hub.app import Hub
    from singlestore_mcp.hub.audit import Audit
    from singlestore_mcp.hub.config import HubConfig
    from singlestore_mcp.hub.spawner import rss_bytes

    report: dict[str, Any] = {"when": datetime.now().isoformat(timespec="seconds"), "users": args.users,
                              "python": sys.version.split()[0], "cpus": os.cpu_count()}
    logon = FakeSasLogon()
    names = [f"user{i:02d}" for i in range(args.users)]
    for u in names:
        logon.users[u] = u.title()
    tmp = Path(tempfile.mkdtemp(prefix="hubload-"))
    cfg = HubConfig(viya_url=logon.url, data_dir=tmp, master_key=MASTER, oauth_mode="redirect", client_id="studio-hub",
                    client_secret=TEST_CLIENT_SECRET, port=0, admins=(), max_users=args.users, metrics=False,
                    viya_name="Zeus", login_rate=10_000)
    hub = Hub(cfg, Audit(tmp / "hub" / "audit.jsonl", echo=False))
    port = hub.serve(block=False)
    cfg.public_url = f"http://127.0.0.1:{port}"
    logon.register_redirect("studio-hub", cfg.redirect_uri)
    rss0 = rss_bytes(os.getpid(), tree=False)
    try:
        # --- sign-in: everyone at once
        browsers: dict[str, Any] = {}
        times: dict[str, dict[str, float]] = {}

        def one(u: str) -> None:
            t0 = time.perf_counter()
            b = sign_in(hub, logon, u)
            t1 = time.perf_counter()
            deadline = time.time() + 180
            while time.time() < deadline:
                r = b.call("sql_editor_state")
                if r.status == 200:
                    break
                time.sleep(0.25)
            t2 = time.perf_counter()
            browsers[u] = b
            times[u] = {"sign_in_ms": round((t1 - t0) * 1000, 1), "ready_s": round(t2 - t0, 2), "ok": r.status == 200}

        t0 = time.perf_counter()
        threads = [threading.Thread(target=one, args=(u,)) for u in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        report["sign_in"] = {"all_ready_s": round(time.perf_counter() - t0, 2),
                             "sign_in_ms": summary([v["sign_in_ms"] for v in times.values()]),
                             "ready_s": summary([v["ready_s"] * 1000 for v in times.values()]),
                             "failed": [u for u, v in times.items() if not v["ok"]]}
        print("sign-in:", json.dumps(report["sign_in"]))

        def spec(u: str) -> dict[str, Any]:
            st = hub.spawner.get(u)
            b = browsers[u]
            return {"user": u,
                    "hub": {"port": port, "prefix": "/studio/", "origin": cfg.public_url,
                            "cookies": "; ".join(f"{k}={v}" for k, v in b.cookies.items())},
                    "direct": {"port": st.port, "prefix": f"/{st.token}/", "origin": f"http://127.0.0.1:{st.port}"}}

        ctx = mp.get_context("spawn")
        with ctx.Pool(args.users) as pool:  # one process a user: they mostly sleep between calls
            # --- overhead: one user, nothing else running
            o = pool.apply(w_overhead, (spec(names[0]), args.n))
            ms = o["ms"]
            report["overhead"] = {
                "call_hub": summary(ms["call_hub"]), "call_direct": summary(ms["call_direct"]),
                "page_hub": summary(ms["page_hub"]), "page_direct": summary(ms["page_direct"]),
                "page_bytes": o["sizes"].get("page"), "errors": o["errors"],
                "added_ms_call_p50": round(pct(ms["call_hub"], 50) - pct(ms["call_direct"], 50), 1),
                "added_ms_page_p50": round(pct(ms["page_hub"], 50) - pct(ms["page_direct"], 50), 1)}
            print("overhead:", json.dumps({k: v for k, v in report["overhead"].items()}))

            # --- concurrency: everyone at once
            until = time.time() + args.seconds
            res = pool.starmap(w_load, [(spec(u), until, args.rate) for u in names])
            allms = [m for r in res for m in r["ms"]]
            statuses: dict[str, int] = {}
            for r in res:
                for k, v in r["statuses"].items():
                    statuses[k] = statuses.get(k, 0) + v
            report["concurrent"] = {"seconds": args.seconds, "rate_per_user": args.rate, **summary(allms),
                                    "calls_per_s": round(sum(statuses.values()) / args.seconds, 1), "statuses": statuses,
                                    "per_user_p50": {r["user"]: pct(r["ms"], 50) for r in res}}
            print("concurrent:", json.dumps({k: v for k, v in report["concurrent"].items() if k != "per_user_p50"}))

            # --- burst: flat out for a few seconds (short: on Windows, ~120 s of TIME_WAIT per closed connection
            # runs out of local ports after some 16 000 connections)
            until = time.time() + args.burst_seconds
            res = pool.starmap(w_load, [(spec(u), until, 0) for u in names])
            bms = [m for r in res for m in r["ms"]]
            bst: dict[str, int] = {}
            for r in res:
                for k, v in r["statuses"].items():
                    bst[k] = bst.get(k, 0) + v
            report["burst"] = {"seconds": args.burst_seconds, **summary(bms),
                               "calls_per_s": round(sum(bst.values()) / args.burst_seconds, 1), "statuses": bst}
            print("burst:", json.dumps(report["burst"]))

            # --- memory after the load
            report["memory_mb"] = {
                "hub_added": round(((rss_bytes(os.getpid(), tree=False) or 0) - (rss0 or 0)) / 1e6, 1),
                "hub_process": round((rss_bytes(os.getpid(), tree=False) or 0) / 1e6, 1),
                "studios": {u: round((rss_bytes(hub.spawner.get(u).pid) or 0) / 1e6, 1) for u in names}}
            st_mb = list(report["memory_mb"]["studios"].values())
            report["memory_mb"]["studio_mean"] = round(statistics.fmean(st_mb), 1)
            print("memory:", json.dumps({k: v for k, v in report["memory_mb"].items() if k != "studios"}))

            # --- crash under load: kill user00's Studio a few seconds in
            until = time.time() + args.crash_seconds
            pending = pool.starmap_async(w_load, [(spec(u), until, args.rate) for u in names])
            time.sleep(5)
            victim = hub.spawner.get(names[0])
            killed_at = time.time()
            victim.proc.kill()
            res = pending.get()
            v = next(r for r in res if r["user"] == names[0])
            after = [(t, s) for t, s in v["timeline"] if t >= killed_at]
            back = next((t for t, s in after if s == 200), None)
            others: dict[str, int] = {}
            for r in res:
                if r["user"] != names[0]:
                    for k, n in r["statuses"].items():
                        others[k] = others.get(k, 0) + n
            report["crash"] = {"back_after_s": round(back - killed_at, 2) if back else None,
                               "victim_statuses": v["statuses"], "victim_slowest_ms": round(max(v["ms"], default=0), 1),
                               "others_statuses": others,
                               "others_p95_ms": pct([m for r in res if r["user"] != names[0] for m in r["ms"]], 95)}
            print("crash:", json.dumps(report["crash"]))
    finally:
        t0 = time.perf_counter()
        hub.close()
        report["close_s"] = round(time.perf_counter() - t0, 2)
        logon.close()
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"hub-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("close:", report["close_s"], "s  ->", out)


if __name__ == "__main__":
    main()
