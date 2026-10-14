"""SQL Editor autocomplete for SAS tabs, offline: which tables a statement names (aliases, quoted names, one-level
names), what "x." before the cursor means, the column cache (negative entries expire, a new SAS session clears
it) – run in node from apps/vendor/sas-complete.js – and the batched column lookup (``sas_lookup_columns``)
against a fake SAS Viya."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.parse
from typing import Any

import pytest
from support import APPS_DIR

from singlestore_mcp import sas_rest, sas_viya
from singlestore_mcp.apps import sas_workspace as ws

JS = APPS_DIR / "vendor" / "sas-complete.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def js(expr: str) -> Any:
    """Evaluate ``expr`` in node with S = the sas-complete module; returns its JSON value."""
    code = f"const S = require({json.dumps(str(JS))});\nprocess.stdout.write(JSON.stringify((() => {{ {expr} }})()));"
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(r.stdout)


# ----------------------------------------------------------------------------- table and alias detection

@needs_node
@pytest.mark.parametrize("sql,lang,expected", [
    ("select c. from s2.cars c", "procsql", [{"lib": "s2", "table": "cars", "alias": "c"}]),
    ("select * from s2.cars as c inner join sashelp.class k on c.a = k.b", "procsql",
     [{"lib": "s2", "table": "cars", "alias": "c"}, {"lib": "sashelp", "table": "class", "alias": "k"}]),
    ("select * from a.x, b.y t, z where 1", "procsql",
     [{"lib": "a", "table": "x", "alias": None}, {"lib": "b", "table": "y", "alias": "t"},
      {"lib": None, "table": "z", "alias": None}]),
    ('select * from "My Lib"."Cars ""2""" t', "cas", [{"lib": "My Lib", "table": 'Cars "2"', "alias": "t"}]),
    ("select * from 'a b'n.'c d'n as t", "procsql", [{"lib": "a b", "table": "c d", "alias": "t"}]),
    ("select * from cars where x > 1 order by x", "fedsql", [{"lib": None, "table": "cars", "alias": None}]),
    # Comments and strings don't count; a subquery's FROM does; a library still being typed doesn't.
    ("select * from (select a from w.b) t /* from x.y */ where q = 'from p.q'", "procsql",
     [{"lib": "w", "table": "b", "alias": None}]),
    ("select * from s2.", "procsql", []),
    ("select * from s2.\nwhere", "procsql", []),
    ('select "from x.y" from s2.cars', "procsql", [{"lib": "s2", "table": "cars", "alias": None}]),
])
def test_table_refs(sql: str, lang: str, expected: list) -> None:
    assert js(f"return S.tableRefs({json.dumps(sql)}, {json.dumps(lang)});") == expected


@needs_node
def test_qualifier_at_cursor() -> None:
    got = js("""return [S.qualifierAt("select c."), S.qualifierAt("select c.na"), S.qualifierAt("select s2.cars.mo"),
                        S.qualifierAt('select "My Lib"."Cars".'), S.qualifierAt("select x"), S.qualifierAt("a.b.c.d")];""")
    assert got == [{"parts": ["c"], "partial": ""}, {"parts": ["c"], "partial": "na"},
                   {"parts": ["s2", "cars"], "partial": "mo"}, {"parts": ["My Lib", "Cars"], "partial": ""}, None,
                   {"parts": ["b", "c"], "partial": "d"}]


@needs_node
def test_targets_resolve_aliases_tables_and_default_library() -> None:
    got = js("""
      const refs = (sql, lang) => S.tableRefs(sql, lang);
      return {
        alias: S.targetsFor(["C"], refs("select c. from s2.cars c", "procsql"), "procsql", null),
        table: S.targetsFor(["cars"], refs("select cars. from s2.cars", "procsql"), "procsql", null),
        oneLevelProcSql: S.targetsFor(["cars"], refs("select cars. from cars", "procsql"), "procsql", "SASHELP"),
        oneLevelWork: S.targetsFor(["cars"], refs("select cars. from cars", "procsql"), "procsql", null),
        oneLevelFedSql: S.targetsFor(["cars"], refs("select cars. from cars", "fedsql"), "fedsql", "SASHELP"),
        oneLevelCas: S.targetsFor(["t"], refs("select t. from cars t", "cas"), "cas", null),
        twoParts: S.targetsFor(["s2", "cars"], [], "procsql", null),
        library: S.targetsFor(["s2"], refs("select s2. from s2.cars", "procsql"), "procsql", null),
        stmt: S.statementTables("select * from s2.cars c join S2.CARS d on 1=1 join x", "procsql", "LIB"),
      };""")
    assert got["alias"] == [{"lib": "s2", "table": "cars"}]
    assert got["table"] == [{"lib": "s2", "table": "cars"}]
    assert got["oneLevelProcSql"] == [{"lib": "SASHELP", "table": "cars"}]
    assert got["oneLevelWork"] == [{"lib": "WORK", "table": "cars"}]
    assert got["oneLevelFedSql"] == [{"lib": "WORK", "table": "cars"}]   # FedSQL one-level names are WORK tables
    assert got["oneLevelCas"] == [{"lib": "CASUSER", "table": "cars"}]
    assert got["twoParts"] == [{"lib": "s2", "table": "cars"}]
    assert got["library"] is None                                         # "lib." → its tables, not columns
    assert got["stmt"] == [{"lib": "s2", "table": "cars"}, {"lib": "LIB", "table": "x"}]


# ----------------------------------------------------------------------------- the column cache

@needs_node
def test_column_cache_keys_negative_ttl_and_session() -> None:
    got = js("""
      let t = 1000;
      const c = new S.ColumnCache({ missingTtl: 500, now: () => t });
      const k1 = S.ColumnCache.key("dev", "sas", "s2", "cars"), k2 = S.ColumnCache.key("dev", "sas", "S2", "NOPE");
      const other = S.ColumnCache.key("prod", "sas", "S2", "CARS");
      const out = { sameKey: k1 === S.ColumnCache.key("dev", "sas", "S2", "CARS"), otherConn: k1 !== other };
      out.unknown0 = c.unknown([k1, k2, k1]).length;
      c.loading(k1); c.missing(k2);
      out.unknown1 = c.unknown([k1, k2]).length;
      c.ok(k1, [{ name: "Make" }], "CARS");
      out.ok = c.get(k1).status;
      t = 1499; out.missingBefore = c.get(k2)?.status ?? null;
      t = 1500; out.missingAfter = c.get(k2)?.status ?? null;
      const casKey = S.ColumnCache.key("dev", "cas", "CASUSER", "T");
      c.ok(casKey, [], "T");
      out.firstSession = c.session("dev|compute|A", "sas");
      out.sameSession = c.session("dev|compute|A", "sas");
      out.casSession = c.session("dev|cas|X", "cas");
      out.keptOk = c.get(k1)?.status ?? null;
      out.newSession = c.session("dev|compute|B", "sas");
      out.afterNew = c.get(k1)?.status ?? null;
      out.casKept = c.get(casKey)?.status ?? null;
      c.ok(k1, [], "CARS"); c.clear(); out.cleared = c.size;
      return out;""")
    assert got == {"sameKey": True, "otherConn": True, "unknown0": 2, "unknown1": 0, "ok": "ok",
                   "missingBefore": "missing", "missingAfter": None, "firstSession": False, "sameSession": False,
                   "casSession": False, "keptOk": "ok", "newSession": True, "afterNew": None, "casKept": "ok", "cleared": 0}


# ----------------------------------------------------------------------------- the batched lookup (fake Viya)

class FakeViya:
    """Answers the Compute data API like SAS Viya: columns per library/table; 404 for unknown ones."""

    def __init__(self, tables: dict[tuple[str, str], list[str]]) -> None:
        self.tables = tables
        self.calls: list[str] = []
        self.lock = threading.Lock()
        self.sid = "S1"
        self.gone_once = False

    def request(self, method: str, url: str, **_: Any) -> tuple[int, Any, dict[str, str]]:
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        with self.lock:
            self.calls.append(path)
        parts = path.split("/")  # ['', 'compute', 'sessions', sid, 'data', lib, (table, 'columns')]
        if self.gone_once:
            self.gone_once = False
            return 404, {"message": "The session was not found."}, {}
        lib = parts[5]
        if len(parts) == 6:  # the library's tables
            names = [t for (lb, t) in self.tables if lb == lib]
            if not names:
                return 404, {"message": f"Library {lib} not found"}, {}
            return 200, {"items": [{"name": n} for n in names]}, {}
        cols = self.tables.get((lib, parts[6]))
        if cols is None:
            return 404, {"message": f"Table {parts[6]} not found"}, {}
        return 200, {"items": [{"name": c, "type": "CHAR", "length": 8, "index": i} for i, c in enumerate(cols)]}, {}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeViya:
    f = FakeViya({("S2", "cars"): ["Make", "Model"], ("SASHELP", "CLASS"): ["Name", "Age"]})
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: {"name": "dev", "url": "https://viya.example"})
    monkeypatch.setattr(sas_rest, "request", f.request)

    def session(self: sas_rest.Compute) -> str:
        self.sid = self.sid or f.sid
        return self.sid

    monkeypatch.setattr(sas_rest.Compute, "session", session)
    monkeypatch.setattr(ws, "compute", sas_rest.Compute())
    return f


def test_lookup_batches_and_reports_missing(fake: FakeViya) -> None:
    pairs = ws._lookup_pairs([{"library": "sashelp", "table": "CLASS"}, {"library": "S2", "table": "nope"},
                              {"library": "SASHELP", "table": "class"}], "procsql")
    assert pairs == [("SASHELP", "CLASS"), ("S2", "nope")]           # librefs upper-cased, duplicates dropped
    d = ws.lookup_columns("procsql", pairs)
    by = {(t["library"], t["table"]): t for t in d["tables"]}
    assert [c["name"] for c in by[("SASHELP", "CLASS")]["columns"]] == ["Name", "Age"]
    assert by[("S2", "nope")]["missing"] and by[("S2", "nope")]["columns"] is None
    assert d["session"] == "dev|compute|S1"
    # One request per table, plus one table list for the library with a miss (to try its own spelling).
    assert sorted(fake.calls) == sorted(["/compute/sessions/S1/data/SASHELP/CLASS/columns",
                                         "/compute/sessions/S1/data/S2/nope/columns", "/compute/sessions/S1/data/S2"])


def test_lookup_retries_with_the_librarys_spelling(fake: FakeViya) -> None:
    d = ws.lookup_columns("procsql", [("S2", "CARS")])
    t = d["tables"][0]
    assert (t["table"], t["name"], t["missing"]) == ("CARS", "cars", False)
    assert [c["name"] for c in t["columns"]] == ["Make", "Model"]


def test_lookup_restarts_an_ended_session_once(fake: FakeViya) -> None:
    fake.gone_once = True
    ws.compute.sid = "OLD"
    d = ws.lookup_columns("procsql", [("SASHELP", "CLASS")])
    assert d["session"] == "dev|compute|S1" and not d["tables"][0]["missing"]


def test_lookup_limits(fake: FakeViya) -> None:
    with pytest.raises(ValueError):
        ws._lookup_pairs([{"library": "L", "table": f"T{i}"} for i in range(ws.MAX_LOOKUP + 1)], "procsql")
    with pytest.raises(ValueError):
        ws._lookup_pairs([{"library": "a/b", "table": "x"}], "procsql")
    assert ws._lookup_pairs([{"library": "My Lib", "table": "t"}], "cas") == [("My Lib", "t")]  # caslib case kept


def test_session_gone_is_not_a_missing_table() -> None:
    """Every Compute 404 names the session in its path detail; only an ended session counts (bodies as Viya sends them)."""
    path = "path: /compute/sessions/85657eb7-ses0000/data/SASHELP/X/columns"
    missing_table = {"httpStatusCode": 404, "errorCode": 5314, "message": "Table does not exist in the given library",
                     "details": ["ERROR: File SASHELP.X.DATA does not exist.", path]}
    no_libref = {"httpStatusCode": 404, "errorCode": 5301, "message": "Library has not been assigned.",
                 "details": ["ERROR: Libref NOSUCHLB is not assigned.", path]}
    gone = {"httpStatusCode": 404, "errorCode": 5837, "message": "Not Found",
            "details": ['A session with the ID "85657eb7-ses0000" could not be found.', path]}
    assert not sas_rest.session_gone(404, missing_table) and not sas_rest.session_gone(404, no_libref)
    assert sas_rest.session_gone(404, gone) and sas_rest.session_gone(410, {})
    assert sas_rest.session_gone(404, {**gone, "errorCode": None}) and not sas_rest.session_gone(500, gone)


# ----------------------------------------------------------------------------- dropped connections (sas_rest._send)

class DroppingServer:
    """A local HTTP server that closes the first ``drops`` connections after reading the request, without an answer
    (what an SSL EOF from a VPN / proxy / ingress looks like to the client), then answers 200."""

    def __init__(self, drops: int) -> None:
        import socket

        self.drops, self.requests = drops, 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/x"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests += 1
                if self.requests <= self.drops:
                    continue  # close without answering
                c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sas_rest, "_RETRY_WAITS", (0, 0))
    monkeypatch.setattr(sas_rest, "_local", threading.local())


def test_send_retries_a_dropped_get(no_wait: None) -> None:
    srv = DroppingServer(drops=2)
    status, _, raw = sas_rest._send(srv.url, "GET", None, {}, 10, True, True)
    assert (status, raw, srv.requests) == (200, b"{}", 3)


def test_send_gives_up_after_three_tries(no_wait: None) -> None:
    srv = DroppingServer(drops=5)
    with pytest.raises(Exception):
        sas_rest._send(srv.url, "GET", None, {}, 10, True, True)
    assert srv.requests == 3


def test_send_never_repeats_a_post_that_was_sent(no_wait: None) -> None:
    srv = DroppingServer(drops=1)
    with pytest.raises(Exception):
        sas_rest._send(srv.url, "POST", b"{}", {"Content-Type": "application/json"}, 10, True, True)
    assert srv.requests == 1   # it may have arrived (e.g. "create a SAS session"): not sent twice
