"""CAS "Load into this session" (offline): the table.loadTable / dropTable requests (session scope only, never
promoted, the caslib unchanged), the size from table.fileInfo and the confirmation above the limit, where the table
is after the load (tableDetails: SpeedyStore live vs copied into CAS memory – rows recorded on Zeus 2026-10-30), the
tools against a fake CAS session (a global table is never dropped or replaced), and the "load again" offer after the
CAS session restarted."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from singlestore_mcp import cas_loads, sas_rest, sas_viya
from singlestore_mcp.apps import sas_workspace as sw
from singlestore_mcp.sas_sql import SasError

# table.fileInfo rows as Zeus answered them for CARS: a SpeedyStore caslib (CSASDP) and a standard one (SASDPSTD).
SS_FILEINFO = [{"Permission": "-rw-rw-rw-", "Name": "CARS", "Type": "BASE TABLE", "Storage": "COLUMNSTORE", "Rows": 428,
                "Columns": 15, "Size": 102720, "UncompressedSize": 102720}]
STD_FILEINFO = [{"Catalog": "SASDPSTD", "Schema": "MyDB", "Name": "CARS", "Type": "TABLE", "Description": ""},
                {"Catalog": "SASDPSTD", "Schema": "SASDP", "Name": "CARS", "Type": "TABLE", "Description": ""}]
SS_DETAILS = [{"Node": "ALL", "Blocks": 0, "Active": 0, "Rows": 428, "DataSize": 102720, "Allocated": 0,
               "AllocatedMemory": 0, "TableLocation": "SINGLESTORE"}]
STD_DETAILS = [{"Node": "ALL", "Blocks": 1, "Active": 1, "Rows": 428, "DataSize": 130112, "Allocated": 1,
                "AllocatedMemory": 130112, "TableLocation": "CAS"}]


def test_load_params_are_session_scope_only() -> None:
    p = cas_loads.load_params("CSASDP", "CARS")
    assert p == {"caslib": "CSASDP", "path": "CARS",
                 "casOut": {"caslib": "CSASDP", "name": "CARS", "promote": False, "replace": False}}
    p = cas_loads.load_params("Public", "cars.sashdat", where="Origin = 'Asia'", columns=["Make", "MSRP"], replace=True)
    assert p["casOut"] == {"caslib": "Public", "name": "cars", "promote": False, "replace": True}
    assert p["where"] == "Origin = 'Asia'" and p["vars"] == [{"name": "Make"}, {"name": "MSRP"}]
    assert "where" not in cas_loads.load_params("X", "T", where="   ")
    with pytest.raises(ValueError):
        cas_loads.load_params("X", "T", where="x" * 10_001)
    assert cas_loads.drop_params("CSASDP", "CARS") == {"caslib": "CSASDP", "name": "CARS", "quiet": True}


def test_columns_from_the_ui() -> None:
    assert cas_loads.parse_columns("Make, Model,\nMSRP, make") == ["Make", "Model", "MSRP"]
    assert cas_loads.parse_columns(None) == [] and cas_loads.parse_columns(["a", " "]) == ["a"]
    with pytest.raises(ValueError):
        cas_loads.parse_columns("ok, bad\x01name")


def test_size_from_file_info() -> None:
    assert cas_loads.file_info(SS_FILEINFO, "cars") == {"name": "CARS", "rows": 428, "columns": 15, "bytes": 102720,
                                                         "schema": None}
    std = cas_loads.file_info(STD_FILEINFO, "CARS", "SASDP")
    assert std["schema"] == "SASDP" and std["bytes"] is None and std["rows"] is None
    assert cas_loads.file_info([{"Name": "cars.sashdat", "Size": 5}], "cars")["bytes"] == 5
    assert cas_loads.file_info(SS_FILEINFO, "NOPE") is None


def test_confirmation_only_for_big_or_unknown_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SINGLESTORE_MCP_CAS_LOAD_CONFIRM_MB", raising=False)
    assert cas_loads.confirm_bytes() == 100_000_000
    assert cas_loads.needs_confirmation("live", None) is None            # SpeedyStore: nothing is copied
    assert cas_loads.needs_confirmation("live", 10**12) is None
    assert cas_loads.needs_confirmation("copy", 102_720) is None
    assert "can't tell" in cas_loads.needs_confirmation("copy", None)
    assert "about 250.0 MB" in cas_loads.needs_confirmation("copy", 250_000_000)
    monkeypatch.setenv("SINGLESTORE_MCP_CAS_LOAD_CONFIRM_MB", "0.05")
    assert cas_loads.needs_confirmation("copy", 102_720)
    assert cas_loads.expected("speedystore") == "live" and cas_loads.expected("standard") == "copy"
    assert cas_loads.expected(None) == "copy"


def test_where_the_table_is_after_the_load() -> None:
    ss = cas_loads.details(SS_DETAILS)
    assert ss == {"location": "SINGLESTORE", "memory": 0, "rows": 428, "live": True}
    assert cas_loads.describe(ss) == "Live – queries run in SingleStore (no copy in CAS memory)"
    std = cas_loads.details(STD_DETAILS)
    assert std == {"location": "CAS", "memory": 130112, "rows": 428, "live": False}
    assert cas_loads.describe(std) == "Copied into CAS memory (130.1 KB)"


def test_loads_are_lost_when_the_session_restarts() -> None:
    cas_loads.clear("T1")
    cas_loads.remember("T1", {"caslib": "CSASDP", "table": "CARS", "path": "CARS", "sid": "A"})
    assert [x["table"] for x in cas_loads.state("T1", "A")["loaded"]] == ["CARS"]
    st = cas_loads.state("T1", "B")  # a new CAS session
    assert not st["loaded"] and [x["table"] for x in st["lost"]] == ["CARS"]
    assert cas_loads.state("T1", None)["lost"]  # ended, not restarted yet
    cas_loads.forget("T1", "csasdp", "cars")
    assert cas_loads.state("T1", "B") == {"loaded": [], "lost": []}


# ----------------------------------------------------------------------------- the tools (fake CAS session)


class FakeCas:
    """A CAS session answering the actions of a load the way Zeus did (see the rows above)."""

    def __init__(self, caslib: str = "CSASDP", kind: str = "SingleStore", global_table: bool = False) -> None:
        self.conn, self.sid, self.lock, self.current_run = "Zeus", "CAS-1", threading.RLock(), None
        self.caslib, self.kind = caslib, kind
        self.loaded: dict[str, dict[str, Any]] = {}
        if global_table:
            self.loaded["CARS"] = {"Name": "CARS", "Rows": 428, "Columns": 15, "Global": 1}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def action(self, name: str, params: dict[str, Any] | None = None, *, check: bool = True, timeout: Any = None) -> dict[str, Any]:
        p = params or {}
        self.calls.append((name, p))
        ok = {"severity": "Normal", "status": "", "log": ""}
        if name == "table.caslibInfo":
            return {**ok, "results": {"CASLibInfo": {"schema": [{"name": "Name"}, {"name": "Type"}],
                                                      "rows": [[self.caslib, self.kind]]}}}
        if name == "table.fileInfo":
            rows = SS_FILEINFO if self.kind == "SingleStore" else STD_FILEINFO
            return {**ok, "results": {"FileInfo": {"schema": [{"name": k} for k in rows[0]], "rows": [list(r.values()) for r in rows]}}}
        if name == "table.tableInfo":
            t = self.loaded.get(str(p.get("name")).upper())
            if not t:
                return {**ok, "severity": "Error", "results": {}, "log": "ERROR: The table was not found."}
            return {**ok, "results": {"TableInfo": {"schema": [{"name": k} for k in t], "rows": [list(t.values())]}}}
        if name == "table.loadTable":
            assert p["casOut"]["promote"] is False and p["casOut"]["caslib"] == p["caslib"]
            rows = 428 if self.kind == "SingleStore" or not p.get("where") else 158
            self.loaded[p["casOut"]["name"].upper()] = {"Name": p["casOut"]["name"], "Rows": rows,
                                                         "Columns": len(p.get("vars") or []) or 15, "Global": 0}
            return {**ok, "results": {}, "log": "NOTE: Cloud Analytic Services made the external data from CARS available."}
        if name == "table.tableDetails":
            rows = SS_DETAILS if self.kind == "SingleStore" else STD_DETAILS
            return {**ok, "results": {"TableDetails": {"schema": [{"name": k} for k in rows[0]], "rows": [list(rows[0].values())]}}}
        if name == "table.dropTable":
            self.loaded.pop(str(p["name"]).upper(), None)
            return {**ok, "results": {}}
        raise AssertionError(name)


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sas_viya, "active_name", lambda: "Zeus")
    monkeypatch.setattr(sas_viya, "settings", lambda name=None: {"name": name or "Zeus", "cas_server": "cas-shared-default"})
    cas_loads.clear("Zeus")

    def use(cas: FakeCas) -> FakeCas:
        monkeypatch.setattr(sas_rest, "sessions", lambda conn=None: (None, cas))
        return cas

    yield use
    cas_loads.clear("Zeus")


def test_speedystore_load_is_live_and_ignores_where(fake_env) -> None:
    cas = fake_env(FakeCas())
    info = sw.cas_load_info("CSASDP", "CARS").structured_content
    assert info["mode"] == "live" and info["bytes"] == 102720 and info["confirm"] is None and info["loaded"] is None
    d = sw.cas_load_table("CSASDP", "CARS", where="Origin = 'Asia'", columns="Make, Model").structured_content
    assert d["live"] and d["location"] == "SINGLESTORE" and d["where_ignored"] and d["where"] is None
    assert "Live – queries run in SingleStore" in d["summary"]
    load = next(p for n, p in cas.calls if n == "table.loadTable")
    assert "where" not in load and load["vars"] == [{"name": "Make"}, {"name": "Model"}]
    assert [x["table"] for x in sw.cas_session_loads().structured_content["loaded"]] == ["CARS"]
    # The table list marks it as this session's (Unload) and live.
    t = next(x for x in sw.cas_table_list([{"Name": "CARS", "Rows": 428, "Columns": 2, "Global": 0}], [], None) if x["name"] == "CARS")
    assert t["loaded"] and not t["global"]
    # Unload drops the session table; the registry forgets it.
    u = sw.cas_unload_table("CSASDP", "CARS").structured_content
    assert u["dropped"] and ("table.dropTable", {"caslib": "CSASDP", "name": "CARS", "quiet": True}) in cas.calls
    assert sw.cas_session_loads().structured_content == {"loaded": [], "lost": [], "summary": "0 loaded, 0 to reload"}


def test_standard_caslib_copies_and_asks_when_size_is_unknown(fake_env) -> None:
    cas = fake_env(FakeCas("SASDPSTD", "singlestore_standard"))
    d = sw.cas_load_table("SASDPSTD", "CARS", where="Origin = 'Asia'").structured_content
    assert d["needs_confirmation"] and "can't tell" in d["reason"]
    assert not any(n == "table.loadTable" for n, _ in cas.calls)  # nothing loaded before the second yes
    d = sw.cas_load_table("SASDPSTD", "CARS", where="Origin = 'Asia'", confirmed=True).structured_content
    assert not d["live"] and d["memory"] == 130112 and d["rows"] == 158 and d["where"] == "Origin = 'Asia'"
    # Loading again replaces this session's table (replace=True), never a global one.
    sw.cas_load_table("SASDPSTD", "CARS", confirmed=True)
    loads = [p for n, p in cas.calls if n == "table.loadTable"]
    assert [p["casOut"]["replace"] for p in loads] == [False, True]


def test_a_global_table_is_never_replaced_or_dropped(fake_env) -> None:
    cas = fake_env(FakeCas(global_table=True))
    with pytest.raises(SasError, match="already loaded globally"):
        sw.cas_load_table("CSASDP", "CARS")
    with pytest.raises(SasError, match="global"):
        sw.cas_unload_table("CSASDP", "CARS")
    assert not any(n in ("table.loadTable", "table.dropTable") for n, _ in cas.calls)


def test_reload_after_the_session_restarted(fake_env) -> None:
    cas = fake_env(FakeCas())
    sw.cas_load_table("CSASDP", "CARS", columns=["Make"])
    cas.sid, cas.loaded = "CAS-2", {}  # Restart SAS session / idle timeout: a new CAS session
    st = sw.cas_session_loads().structured_content
    assert not st["loaded"] and [x["table"] for x in st["lost"]] == ["CARS"]
    r = sw.cas_reload_tables().structured_content
    assert [x["table"] for x in r["reloaded"]] == ["CARS"] and not r["failed"]
    assert [x["table"] for x in sw.cas_session_loads().structured_content["loaded"]] == ["CARS"]
    again = [p for n, p in cas.calls if n == "table.loadTable"][-1]
    assert again["vars"] == [{"name": "Make"}]
    cas.sid = "CAS-3"
    assert sw.cas_reload_tables(forget=True).structured_content["forgotten"] == 1
    assert sw.cas_session_loads().structured_content["lost"] == []


class _BusyLock:
    def acquire(self, timeout: float = -1) -> bool:
        return False

    def release(self) -> None:
        raise AssertionError("not held")


def test_busy_cas_session_is_reported(fake_env) -> None:
    cas = fake_env(FakeCas())
    cas.lock = _BusyLock()  # a FedSQL run in a tab holds the CAS session
    with pytest.raises(SasError, match="busy"):
        sw.cas_load_info("CSASDP", "CARS")
    assert not cas.calls
