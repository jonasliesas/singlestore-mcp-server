"""Recovering from network breaks without a restart, offline: the SingleStore pool against a tiny MySQL-protocol
server (``fake_mysql``) and the SAS Viya client against a stand-in Viya (``fake_viya``), each behind a local
fault-injecting TCP proxy (``faultproxy``: drop every connection, blackhole = connections go silent for good,
refuse, come back). Timeouts are shortened so the whole file runs in well under a minute.

What a network break looks like here, and what must happen:
- dropped connections (VPN reconnect): the next call opens a fresh connection, no error;
- an outage: a readable error ("… reconnects by itself …") quickly, the health line says "down", and the next call
  after the network is back works – no restart;
- silent (half-open) connections after a sleep / VPN switch: found by a bounded ping after a network event, or closed
  at once when the route moved to another local address (a query waiting on one is retried on a fresh connection);
- a login that never finishes, or a pool whose connections all hang, gives up after a clear limit;
- SAS: kept-alive HTTP connections are replaced after a network event, a session Viya ended meanwhile is restarted
  with a one-time notice, the pre-warm retries right after the network returns, a failed token renewal over a dead
  network isn't reported as "sign in again", and the proxy / direct route is chosen again after failures.
"""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import singlestoredb as s2
from fake_mysql import FakeMySQL
from fake_viya import FakeViya
from faultproxy import FaultProxy

from singlestore_mcp import connections, netguard, preconnect, sas_prewarm, sas_rest, sas_viya
from singlestore_mcp import db as db_mod


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(netguard, "CONNECT_TIMEOUT_S", 2.0)
    monkeypatch.setattr(netguard, "CONNECT_DEADLINE_S", 5.0)  # roomy: a loaded test machine
    monkeypatch.setattr(netguard, "PING_TIMEOUT_S", 0.5)
    monkeypatch.setattr(netguard, "PROBE_EVERY_S", 0.1)
    netguard.reset_for_tests()
    yield
    netguard.reset_for_tests()


def _timed(fn: Any, limit: float = 20.0) -> tuple[Any, float]:
    """(result or the exception, seconds); fails the test if ``fn`` hangs past ``limit``."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["v"] = fn()
        except BaseException as exc:  # noqa: BLE001 - returned
            box["v"] = exc

    t0 = time.perf_counter()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(limit)
    assert not th.is_alive(), f"hung for more than {limit} s"
    return box["v"], time.perf_counter() - t0


# ----------------------------------------------------------------------------- SingleStore pool

@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch):
    fake = FakeMySQL()
    proxy = FaultProxy(fake.host, fake.port)
    monkeypatch.setenv("SINGLESTORE_HOST", "127.0.0.1")
    monkeypatch.setenv("SINGLESTORE_PORT", str(proxy.port))
    monkeypatch.setenv("SINGLESTORE_USER", "u")
    monkeypatch.setenv("SINGLESTORE_PASSWORD", "pw")
    monkeypatch.setenv("SINGLESTORE_SSL_DISABLED", "1")
    monkeypatch.delenv("SINGLESTORE_URL", raising=False)
    pool = db_mod.Database()
    pool._adopted = True  # no standby hand-over here
    monkeypatch.setattr(db_mod, "db", pool)  # the health probe pings this pool
    yield {"fake": fake, "proxy": proxy, "db": pool}
    pool.close()
    proxy.close()
    fake.close()


def q(pool: db_mod.Database, sql: str = "SELECT 1") -> Any:
    return pool.execute(sql)[1]


def test_connections_have_keepalive_and_a_connect_timeout(net) -> None:
    opts = netguard.socket_options()
    assert opts[socket.SOL_SOCKET][socket.SO_KEEPALIVE] == 1
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert opts[socket.IPPROTO_TCP][socket.TCP_KEEPIDLE] == 10
    kw = connections.connect_kwargs(connections.active())
    assert kw["socket_options"] == opts and kw["connect_timeout"] == 2


def test_dropped_connections_are_replaced_without_an_error(net) -> None:
    pool, proxy = net["db"], net["proxy"]
    assert q(pool) == [{"v": "1"}]
    proxy.drop()  # the VPN reconnected: every open connection is gone
    value, s = _timed(lambda: q(pool))
    assert value == [{"v": "1"}] and s < 2


def test_an_outage_gives_a_readable_error_and_the_next_call_after_it_works(net) -> None:
    pool, proxy = net["db"], net["proxy"]
    q(pool)
    proxy.drop()
    proxy.refuse()
    err, s = _timed(lambda: q(pool))
    assert isinstance(err, db_mod.ConnectionLost) and s < 5
    assert "reconnects by itself" in str(err) and "no restart" in str(err)
    assert netguard.is_down("singlestore")
    proxy.restore()
    # The health probe (what the watcher / the Workspace's poll run) finds it back without any other call.
    netguard.probe("singlestore", force=True)
    deadline = time.monotonic() + 5
    while netguard.is_down("singlestore") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not netguard.is_down("singlestore")
    assert q(pool) == [{"v": "1"}]


def test_a_login_that_never_finishes_gives_up(net, monkeypatch: pytest.MonkeyPatch) -> None:
    pool, proxy = net["db"], net["proxy"]
    monkeypatch.setattr(netguard, "CONNECT_DEADLINE_S", 2.0)
    proxy.blackhole(accept_new=True)  # TCP connects, then nothing answers
    err, s = _timed(lambda: q(pool))
    assert isinstance(err, db_mod.ConnectionLost) and "longer than 2 s" in str(err) and s < 4
    proxy.restore()
    assert q(pool) == [{"v": "1"}]


def test_silent_connections_after_a_sleep_are_found_by_a_bounded_ping(net) -> None:
    pool, proxy = net["db"], net["proxy"]
    q(pool)
    proxy.blackhole(accept_new=False)  # the pooled connection is dead without anyone saying so
    proxy.restore()
    netguard.bump("resume")  # what the watcher does when the clock jumped (the laptop slept)
    value, s = _timed(lambda: q(pool))
    assert value == [{"v": "1"}] and s < 2  # ping timeout (0.5 s) + a fresh connection


def test_a_query_waiting_on_a_connection_whose_route_moved_is_retried(net, monkeypatch: pytest.MonkeyPatch) -> None:
    pool, proxy = net["db"], net["proxy"]
    q(pool)
    box: dict[str, Any] = {}
    th = threading.Thread(target=lambda: box.update(v=_timed(lambda: q(pool, "SELECT SLEEP(1)"))))
    th.start()
    time.sleep(0.3)
    proxy.blackhole(accept_new=False)  # the VPN moved: the old connection will never answer
    proxy.restore()
    monkeypatch.setattr(netguard, "route_source", lambda peer, family=socket.AF_INET: "10.99.99.99")
    assert netguard.check_routes() >= 1  # what the watcher does every couple of seconds
    monkeypatch.setattr(netguard, "route_source", lambda peer, family=socket.AF_INET: "127.0.0.1")
    th.join(10)
    value, s = box["v"]
    assert value == [{"v": "0"}] and s < 4  # read again on a fresh connection, not hung


def test_a_write_cut_off_by_a_network_break_is_not_sent_twice(net, monkeypatch: pytest.MonkeyPatch) -> None:
    pool, proxy, fake = net["db"], net["proxy"], net["fake"]
    q(pool)
    box: dict[str, Any] = {}
    th = threading.Thread(target=lambda: box.update(v=_timed(lambda: pool.execute("DO SLEEP(1)"))))
    th.start()
    time.sleep(0.3)
    proxy.blackhole(accept_new=False)
    proxy.restore()
    monkeypatch.setattr(netguard, "route_source", lambda peer, family=socket.AF_INET: "10.99.99.99")
    netguard.check_routes()
    th.join(10)
    err, s = box["v"]
    assert isinstance(err, db_mod.ConnectionLost) and "may or may not have run" in str(err) and s < 4
    assert fake.queries.count("DO SLEEP(1)") == 1


def test_a_stream_before_its_first_row_is_retried_after_the_route_moved(net, monkeypatch: pytest.MonkeyPatch) -> None:
    pool, proxy = net["db"], net["proxy"]
    q(pool)

    def stream() -> list[Any]:
        rows: list[Any] = []
        for _cols, chunk in pool.stream("SELECT SLEEP(1)"):
            rows += chunk
        return rows

    box: dict[str, Any] = {}
    th = threading.Thread(target=lambda: box.update(v=_timed(stream)))
    th.start()
    time.sleep(0.3)
    proxy.blackhole(accept_new=False)
    proxy.restore()
    monkeypatch.setattr(netguard, "route_source", lambda peer, family=socket.AF_INET: "10.99.99.99")
    netguard.check_routes()
    monkeypatch.setattr(netguard, "route_source", lambda peer, family=socket.AF_INET: "127.0.0.1")
    th.join(10)
    assert box["v"][0] == [{"v": "0"}]


def test_idle_connections_from_an_old_address_are_not_used(net, monkeypatch: pytest.MonkeyPatch) -> None:
    pool, proxy = net["db"], net["proxy"]
    q(pool)
    before = proxy.accepted
    monkeypatch.setattr(netguard, "moved", lambda info: True)  # opened from an address the route no longer uses
    q(pool)
    assert proxy.accepted == before + 1  # replaced by a fresh connection, no wait


def test_waiting_for_a_free_connection_is_bounded(monkeypatch: pytest.MonkeyPatch, net) -> None:
    monkeypatch.setenv("SINGLESTORE_MCP_POOL_SIZE", "1")
    monkeypatch.setattr(db_mod, "POOL_WAIT_S", 0.5)
    pool = db_mod.Database()
    pool._adopted = True
    busy = threading.Thread(target=lambda: q(pool, "SELECT SLEEP(2)"))
    busy.start()
    time.sleep(0.3)
    err, s = _timed(lambda: q(pool))
    assert isinstance(err, db_mod.ConnectionLost) and "stayed busy" in str(err) and s < 2
    busy.join(5)
    assert q(pool) == [{"v": "1"}]
    pool.close()


def test_the_standby_ping_is_bounded(net) -> None:
    proxy = net["proxy"]
    conn = s2.connect(**connections.connect_kwargs(connections.active()), autocommit=True)
    preconnect._ping(conn)
    proxy.blackhole(accept_new=False)
    err, s = _timed(lambda: preconnect._ping(conn))
    assert isinstance(err, Exception) and s < 2  # not handed over; was: hung until the OS gave up


def test_a_route_that_goes_and_comes_back_checks_the_services_at_once(net, monkeypatch: pytest.MonkeyPatch) -> None:
    pool = net["db"]
    q(pool)
    probed: list[bool] = []
    monkeypatch.setattr(netguard, "probe_all", lambda force=False: probed.append(force))
    monkeypatch.setattr(netguard, "route_source", lambda peer, family=socket.AF_INET: None)  # VPN off: no route
    assert netguard.check_routes() == 0  # connections aren't closed for a missing route (a short blip may pass) …
    assert probed == [True]  # … but the services are checked now
    monkeypatch.setattr(netguard, "route_source", lambda peer, family=socket.AF_INET: "127.0.0.1")  # back
    netguard.check_routes()
    assert probed == [True, True]
    netguard.check_routes()
    assert probed == [True, True]  # nothing changed: nothing to do
    assert q(pool) == [{"v": "1"}]


def test_the_watcher_notices_a_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    netguard.on_event(seen.append)
    clock = {"mono": 1000.0, "wall": 5000.0}
    monkeypatch.setattr(netguard, "_clock", lambda: (clock["mono"], clock["wall"]))
    monkeypatch.setattr(netguard, "check_routes", lambda: 0)
    calls = {"n": 0}

    def fake_sleep(_s: float) -> None:
        calls["n"] += 1
        clock["mono"] += 2
        clock["wall"] += 2 if calls["n"] != 2 else 3600  # the second tick comes an hour later: it slept
        if calls["n"] > 3:
            raise SystemExit

    monkeypatch.setattr(netguard, "_sleep", fake_sleep)
    with pytest.raises(SystemExit):
        netguard._watch()
    assert seen.count("resume") == 1
    netguard._listeners.remove(seen.append)


# ----------------------------------------------------------------------------- SAS Viya REST

@pytest.fixture
def viya(monkeypatch: pytest.MonkeyPatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeViya()
    proxy = FaultProxy("127.0.0.1", fake.port)
    settings = {"name": "fake", "url": f"http://127.0.0.1:{proxy.port}", "compute_context": "SAS Studio compute context",
                "client_id": "vscode", "ssl_verify": False, "libref": "S2", "cas_server": "cas-shared-default",
                "startup_code": "", "auto_libname": False, "prewarm": True, "sas9_signon": "", "sas9_server": "",
                "sas9_libraries": ""}
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: dict(settings))
    monkeypatch.setattr(sas_viya, "active_name", lambda: "fake")
    monkeypatch.setattr(sas_viya, "token", lambda min_valid=300, name=None: "tok")
    monkeypatch.setattr(sas_viya, "token_info", lambda name: {"signed_in": True})
    monkeypatch.setitem(sas_viya._route, "direct", False)
    monkeypatch.setattr(sas_rest, "_sessions", {})
    monkeypatch.setattr(sas_prewarm, "_state", {})
    monkeypatch.setattr(sas_prewarm, "_threads", {})
    monkeypatch.setenv(sas_prewarm.ENV, "1")
    worker = ThreadPoolExecutor(1)  # like an MCP worker thread: reused, so are its kept-alive HTTP connections
    yield {"fake": fake, "proxy": proxy, "settings": settings, "worker": worker,
           "on_worker": lambda fn: worker.submit(fn).result(30)}
    worker.shutdown(wait=False)
    proxy.close()
    fake.close()


def _run(c: sas_rest.Compute) -> dict[str, Any]:
    return c.run("data _null_; run;")


def test_kept_alive_http_connections_are_replaced_after_a_network_event(viya) -> None:
    c, _ = sas_rest.sessions()
    on = viya["on_worker"]
    assert on(lambda: _run(c))["state"] == "completed"
    viya["proxy"].blackhole(accept_new=False)  # the kept connection went silent (VPN switch / sleep)
    viya["proxy"].restore()
    netguard.bump("resume")
    t0 = time.perf_counter()
    assert on(lambda: _run(c))["state"] == "completed"
    assert time.perf_counter() - t0 < 2  # was: the job POST waited its whole 60 s timeout, then failed


def test_a_get_on_a_silent_kept_connection_is_retried_fresh(viya) -> None:
    on = viya["on_worker"]
    url = f"{viya['settings']['url']}/compute/contexts"
    assert on(lambda: sas_rest.request("GET", url))[0] == 200
    viya["proxy"].blackhole(accept_new=False)
    viya["proxy"].restore()
    t0 = time.perf_counter()
    assert on(lambda: sas_rest.request("GET", url, timeout=1))[0] == 200
    assert time.perf_counter() - t0 < 3


def test_a_session_viya_ended_meanwhile_is_restarted_once_with_a_notice(viya) -> None:
    c, _ = sas_rest.sessions()
    on = viya["on_worker"]
    on(lambda: _run(c))
    viya["proxy"].drop()
    viya["proxy"].refuse()
    err = on(lambda: pytest.raises(sas_rest.SasError, _run, c))
    assert "retried by itself" in str(err.value) and netguard.is_down("sas")
    viya["fake"].forget_sessions()  # Viya ended the session while the network was away
    viya["proxy"].restore()
    res = on(lambda: _run(c))
    assert res["state"] == "completed" and res["log"][0]["line"].startswith("NOTE: The SAS session had ended")
    assert viya["fake"].created == 2 and not netguard.is_down("sas")
    assert any("restarted" in n["text"] for n in netguard.snapshot()["notices"])
    assert not on(lambda: _run(c))["log"][0]["line"].startswith("NOTE: The SAS session had ended")  # only once


def test_the_prewarm_retries_right_after_the_network_returns(viya) -> None:
    c, _ = sas_rest.sessions()
    viya["on_worker"](lambda: _run(c))
    viya["proxy"].drop()
    viya["proxy"].refuse()
    sas_prewarm._state["fake"] = {"state": "ready", "checked": 0}  # the keep-alive check is due
    sas_prewarm.prewarm()
    assert sas_prewarm.wait(timeout=10)["state"] == "unreachable"
    viya["proxy"].restore()
    assert sas_prewarm.prewarm()["state"] == "unreachable"  # not hammered: after RETRY_S …
    sas_prewarm.prewarm(retry=True)  # … or at once when the page / the probe says the network is back
    assert sas_prewarm.wait(timeout=10)["state"] == "ready"


def test_the_sas_probe_finds_viya_back_and_rechecks_the_session(viya) -> None:
    c, _ = sas_rest.sessions()
    viya["on_worker"](lambda: _run(c))
    viya["proxy"].drop()
    viya["proxy"].refuse()
    with pytest.raises(sas_rest.SasError):
        viya["on_worker"](lambda: _run(c))
    assert netguard.is_down("sas")
    viya["proxy"].restore()
    assert netguard.probe("sas", force=True)
    deadline = time.monotonic() + 5
    while netguard.is_down("sas") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not netguard.is_down("sas")


def test_a_token_renewal_over_a_dead_network_keeps_the_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: {"name": "z", "url": "https://z.example", "ssl_verify": True,
                                                                  "client_id": "vscode"})
    monkeypatch.setattr(sas_viya, "_stored", lambda name: {"access": "a", "refresh": "r", "exp": time.time() - 10,
                                                           "url": "https://z.example"})

    def post(*a: Any, **k: Any) -> Any:
        raise netguard.Unreachable("Couldn't reach SAS Viya at https://z.example: timed out")

    monkeypatch.setattr(sas_viya, "_post_token", post)
    with pytest.raises(netguard.Unreachable) as err:
        sas_viya.token(name="z")
    assert not isinstance(err.value, sas_viya.NeedsSignIn) and "kept" in str(err.value)


def test_proxy_or_direct_is_chosen_again_after_failures(viya, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"{viya['settings']['url']}/compute/contexts"
    dead = FaultProxy("127.0.0.1", 9)
    dead.refuse()
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{dead.port}")
    on = viya["on_worker"]
    # The configured proxy can't be reached: Viya is reached directly, and that's remembered.
    assert on(lambda: sas_rest.request("GET", url))[0] == 200 and sas_viya._route["direct"] is True
    # Another network: now only the proxy reaches Viya (the direct route is refused).
    good = FaultProxy("127.0.0.1", viya["fake"].port)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{good.port}")
    viya["proxy"].drop()
    viya["proxy"].refuse()
    assert on(lambda: sas_rest.request("GET", url))[0] == 200 and sas_viya._route["direct"] is False
    good.close()
    dead.close()


# ----------------------------------------------------------------------------- protocol (a real server process)

def _offline_server_env(tmp_path: Any, port: int) -> dict[str, str]:
    from support import server_env

    home, sql = tmp_path / "home", tmp_path / "sql"
    home.mkdir()
    sql.mkdir()
    return server_env(home, sql, {
        "SINGLESTORE_HOST": "127.0.0.1", "SINGLESTORE_PORT": str(port), "SINGLESTORE_USER": "u",
        "SINGLESTORE_PASSWORD": "pw", "SINGLESTORE_SSL_DISABLED": "1", "SINGLESTORE_URL": "", "SINGLESTORE_DATABASE": "",
        "SINGLESTORE_MCP_NO_STANDBY": "1", "SINGLESTORE_MCP_CONNECT_TIMEOUT": "2", "SINGLESTORE_MCP_CONNECT_DEADLINE": "3",
        "SINGLESTORE_MCP_PING_TIMEOUT": "1", "SINGLESTORE_MCP_PROBE_EVERY": "0.5",
    })


def test_protocol_outage_during_a_tool_call_then_recovery_without_restart(tmp_path) -> None:
    """Over MCP stdio, through the supervisor (as Claude runs it): a tool call that is running when the network goes
    gets a readable error within seconds, and once the network is back the same server answers again."""
    from support import McpSession

    with FakeMySQL() as fake, FaultProxy(fake.host, fake.port) as proxy, \
            McpSession(_offline_server_env(tmp_path, proxy.port), read_timeout=60) as s:
        assert s.ok("run_sql", {"sql": "SELECT 1"})["rows"] == [{"v": "1"}]
        box: dict[str, Any] = {}
        th = threading.Thread(target=lambda: box.update(r=s.call("run_sql", {"sql": "SELECT SLEEP(2)"}, timeout=60)))
        th.start()
        time.sleep(0.6)
        proxy.drop()  # the VPN goes while the query runs …
        proxy.refuse()  # … and stays away
        th.join(60)
        r = box["r"]
        assert r.is_error and "reconnects by itself" in r.text and "no restart" in r.text, r.text[:300]
        assert r.ms < 15_000
        health = s.ok("connection_health", {"probe": "none"})
        assert health["services"]["singlestore"]["state"] == "down"

        proxy.restore()
        assert s.ok("run_sql", {"sql": "SELECT 1"})["rows"] == [{"v": "1"}]
        health = s.ok("connection_health", {"probe": "none"})
        sing = health["services"]["singlestore"]
        assert sing["state"] == "ok" and sing["recovered"] and sing["down_s"] is not None  # same server: no restart
