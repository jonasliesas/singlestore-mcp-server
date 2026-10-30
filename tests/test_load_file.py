"""Load file into table (apps/load_file.py): reading and detecting (encoding, delimiter, header, null markers, decimal
comma, date formats), type suggestions and the CREATE TABLE, the CREATE TABLE check, column mapping, row conversion,
the size limit, the pipeline template and server mode – offline. The cluster test (marked) loads small files into
test.zz_test_load_* tables (LOAD DATA LOCAL INFILE and the INSERT fallback, bad rows, Cancel) and drops them."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp.apps import load_file as lf


@pytest.fixture
def files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SINGLESTORE_MCP_SQL_DIR", str(tmp_path / "sql"))
    monkeypatch.setenv("SINGLESTORE_MCP_FILE_ROOTS", str(tmp_path / "data"))
    monkeypatch.delenv("SINGLESTORE_MCP_SERVER_MODE", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "sql").mkdir()
    return tmp_path / "data"


def _write(folder: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    p = folder / name
    p.write_bytes(text.encode(encoding))
    return p


SAS_EXPORT = ("KUNDE_ID;NAVN;BELØP;DATO;TID\n"
              "1;Ås Bil;12,50;01.02.2024;01FEB2024:10:15:00\n"
              "2;Øre AS;.;15.03.2024;15MAR2024:23:59:59\n"
              "3;Blåbær;7,00;31.12.2023;31DEC2023:00:00:00\n")


# ------------------------------------------------------------------------------------------------ detection

def test_detects_a_windows_1252_sas_export(files: Path) -> None:
    p = _write(files, "kunder.csv", SAS_EXPORT, "cp1252")
    pv = lf.preview(str(p), database="test", connection=False)
    o = pv["options"]
    assert o["encoding"] == "cp1252" and "Windows-1252" in pv["detected"]["encoding_note"]
    assert o["delimiter"] == ";" and o["header"] is True and o["decimal_comma"] is True
    assert "." in o["null_markers"] and "" in o["null_markers"]  # SAS's missing number
    cols = {c["name"]: c for c in pv["columns"]}
    assert list(cols) == ["KUNDE_ID", "NAVN", "BELØP", "DATO", "TID"]
    assert cols["KUNDE_ID"]["sql_type"] == "BIGINT"
    assert cols["BELØP"]["kind"] == "num" and cols["BELØP"]["sql_type"].startswith("DECIMAL(") and cols["BELØP"]["sql_type"].endswith(",2)")
    assert cols["DATO"]["sql_type"] == "DATE" and cols["DATO"]["format"] == "DD.MM.YYYY"
    assert cols["TID"]["sql_type"] == "DATETIME(6)" and cols["TID"]["format"] == "DDMONYYYY:HH:MM:SS"
    assert cols["NAVN"]["sql_type"] == "VARCHAR(32)"
    assert pv["rows"][0][1] == "Ås Bil" and pv["replaced_chars"] == 0
    assert pv["estimated_rows"] == 3 and pv["whole_file_in_sample"]
    # The same file read as UTF-8 (the user's choice) shows replaced characters.
    assert lf.preview(str(p), encoding="utf-8", connection=False)["replaced_chars"] > 0


def test_utf8_bom_tab_and_no_header(files: Path) -> None:
    p = _write(files, "a.tsv", "\ufeff1\t2024-01-02 10:00:00.5\tx\n2\t2024-01-03 11:00:00\ty\n")
    pv = lf.preview(str(p), connection=False)
    assert pv["options"]["encoding"] == "utf-8-sig" and pv["options"]["delimiter"] == "\t"
    assert pv["options"]["header"] is False
    assert [c["name"] for c in pv["columns"]] == ["col1", "col2", "col3"]
    assert pv["columns"][1]["sql_type"] == "DATETIME(6)" and pv["columns"][1]["format"] == "YYYY-MM-DD HH:MM:SS"


def test_utf8_and_comma(files: Path) -> None:
    p = _write(files, "b.csv", 'id,name,amount\n1,"Smith, J",1.5e3\n2,"Ås ""quoted""",2\n')
    pv = lf.preview(str(p), connection=False)
    assert pv["options"]["encoding"] == "utf-8" and pv["options"]["delimiter"] == ","
    assert pv["rows"][1][1] == 'Ås "quoted"'
    assert {c["name"]: c["sql_type"] for c in pv["columns"]}["amount"] == "DOUBLE"


def test_encoding_detection_edges() -> None:
    assert lf.detect_encoding(b"plain ascii")[0] == "utf-8"
    assert lf.detect_encoding("blåbær".encode())[0] == "utf-8"
    assert lf.detect_encoding("blåbær".encode()[:-1])[0] == "utf-8"  # the sample ends inside a character
    assert lf.detect_encoding("blåbær".encode("cp1252"))[0] == "cp1252"
    assert lf.detect_encoding(b"x\x81y\xe5")[0] == "latin-1"


@pytest.mark.parametrize(("values", "kind", "sql"), [
    (["1", "-2", "300"], "int", "BIGINT"),
    (["007", "123"], "text", "VARCHAR(32)"),          # leading zeros stay text (zip codes, account numbers)
    (["12345678901234567890"], "num", "DECIMAL(22,0)"),
    (["1.25", "100.5", "3"], "num", "DECIMAL(11,2)"),
    (["1e10", "2.5"], "num", "DOUBLE"),
    (["2024-01-31", ""], "date", "DATE"),
    (["10:15", "23:59:59"], "time", "TIME"),
    (["", ""], "text", "VARCHAR(255)"),
    (["x" * 40], "text", "VARCHAR(128)"),
    (["x" * 9000], "text", "LONGTEXT"),
])
def test_type_inference(values: list[str], kind: str, sql: str) -> None:
    info = lf.infer_column(values, {""})
    assert (info["kind"], info["sql_type"]) == (kind, sql)


def test_ambiguous_day_month_is_flagged() -> None:
    info = lf.infer_column(["01/02/2024", "03/04/2024"], {""})
    assert info["kind"] == "date" and info["format"] == "DD/MM/YYYY"
    assert "MM/DD/YYYY" in info["format_choices"]
    assert lf.infer_column(["12/31/2024"], {""})["format"] == "MM/DD/YYYY"


def test_names_keys_and_create_table(files: Path) -> None:
    assert lf.clean_names(["Kunde id", "kunde id", "", "Beløp (NOK)"]) == ["Kunde_id", "kunde_id_2", "col3", "Beløp_NOK"]
    assert lf.table_name_from(r"C:\x\Salg 2024-Q1.csv") == "salg_2024_q1"
    p = _write(files, "s.csv", "order_id,day,city\n1,2024-01-01,Oslo\n2,2024-01-02,Bergen\n3,2024-01-02,Oslo\n")
    pv = lf.preview(str(p), database="test", table="zz_x", connection=False)
    assert pv["shard_key"] == "order_id" and pv["sort_key"] == "day"
    ddl = pv["create_sql"]
    assert ddl.startswith("CREATE TABLE `test`.`zz_x` (\n  `order_id` BIGINT,")
    assert "SORT KEY (`day`)" in ddl and "SHARD KEY (`order_id`)" in ddl
    assert lf.check_create_sql(ddl, "test", "zz_x").endswith(")")
    # No unique id: keyless; no date: an empty sort key.
    cols = [{"name": "city", "kind": "text", "sql_type": "VARCHAR(32)", "max_len": 6}]
    assert "SHARD KEY ()" in lf.create_table_sql("d", "t", cols, *lf.suggest_keys(cols, [["Oslo"], ["Oslo"]], {""}))
    assert "SORT KEY ()" in lf.create_table_sql("d", "t", cols, None, None)


@pytest.mark.parametrize("sql", [
    "DROP TABLE test.zz_x",
    "CREATE TABLE test.other (a INT)",
    "CREATE TABLE zz_x (a INT); DROP TABLE y",
    "CREATE TEMPORARY TABLE zz_x (a INT)",
    "CREATE TABLE sasdp.zz_x (a INT)",
])
def test_create_table_check_refuses(sql: str) -> None:
    with pytest.raises(ValueError):
        lf.check_create_sql(sql, "test", "zz_x")


def test_create_table_check_accepts() -> None:
    assert lf.check_create_sql("-- mine\nCREATE ROWSTORE TABLE `zz_x` (a INT, b VARCHAR(5) DEFAULT ';');", "test", "zz_x") \
        == "CREATE ROWSTORE TABLE `zz_x` (a INT, b VARCHAR(5) DEFAULT ';')"
    assert lf.check_create_sql("create table `test`.`zz_x` (a int) /* note */ ;  -- end", "test", "zz_x").startswith("create table")


def test_mapping_by_name() -> None:
    source = [{"name": "Kunde_ID", "index": 0, "kind": "int", "source": "Kunde ID"}, {"name": "navn", "index": 1, "kind": "text"},
              {"name": "extra", "index": 2, "kind": "text"}]
    target = [{"name": "kunde_id", "data_type": "bigint", "type": "bigint(20)", "nullable": False, "default": None},
              {"name": "NAVN", "data_type": "varchar", "type": "varchar(50)", "nullable": True, "default": None},
              {"name": "created", "data_type": "datetime", "type": "datetime", "nullable": False, "default": None},
              {"name": "id", "data_type": "bigint", "type": "bigint", "nullable": False, "default": None, "auto": True}]
    m = lf.map_columns(source, target)
    assert [(p["target"], p["source"]) for p in m["pairs"]] == [("kunde_id", "Kunde_ID"), ("NAVN", "navn"), ("created", None), ("id", None)]
    assert m["skipped_source"] == ["extra"] and m["required_missing"] == ["created"]
    m2 = lf.map_columns(source, target, {"kunde_id": "Kunde_ID", "NAVN": None, "created": "extra", "id": None})
    assert [(p["target"], p["source"]) for p in m2["pairs"]] == [("kunde_id", "Kunde_ID"), ("NAVN", None), ("created", "extra"), ("id", None)]
    assert m2["required_missing"] == []


# ------------------------------------------------------------------------------------------------ converting rows

def test_row_conversion_and_tsv() -> None:
    opts = lf.Options("utf-8", ";", '"', True, ["", "."], True)
    pairs = [{"target": "id", "source": "id", "source_index": 0, "kind": "int"},
             {"target": "amount", "source": "amount", "source_index": 1, "kind": "num"},
             {"target": "d", "source": "d", "source_index": 2, "kind": "date", "format": "DD.MM.YYYY"},
             {"target": "ts", "source": "ts", "source_index": 3, "kind": "datetime", "format": "DDMONYYYY:HH:MM:SS"},
             {"target": "note", "source": "note", "source_index": 4, "kind": "text"}]
    plan = lf.Plan(pairs, opts, 5)
    row = plan.row(["1", "1.234,5", "01.02.2024", "01FEB2024:10:15:00.25", "tab\there\\x"])
    assert row == ["1", "1234.5", "2024-02-01", "2024-02-01 10:15:00.250000", "tab\there\\x"]
    assert lf.Plan.tsv(row) == "1\t1234.5\t2024-02-01\t2024-02-01 10:15:00.250000\ttab\\there\\\\x\n"
    assert plan.row([" 2 ", ".", "", "", ""]) == ["2", None, None, None, None]
    assert lf.Plan.tsv([None, "a\nb"]) == "\\N\ta\\nb\n"
    for bad, why in ((["x", "1", "", "", ""], "id: 'x' isn't a whole number"), (["1", "1", "31.02.2024", "", ""], "isn't a date"),
                     (["1", "2"], "2 field(s), expected 5")):
        with pytest.raises(lf.BadValue, match=re.escape(why)):
            plan.row(bad)


def test_iter_file_lines_and_encoding_error(files: Path) -> None:
    p = _write(files, "m.csv", 'a,b\n1,"two\nlines"\n\n3,x\n')
    opts = lf.Options("utf-8", ",", '"', True, [""], False)
    assert list(lf.iter_file(p, opts)) == [(2, ["1", "two\nlines"]), (5, ["3", "x"])]
    bad = _write(files, "w.csv", "a,b\n1,blå\n", "cp1252")
    with pytest.raises(ValueError, match="can't be read as UTF-8"):
        list(lf.iter_file(bad, opts))


# ------------------------------------------------------------------------------------------------ limits, pipeline, server mode

def test_size_limit_and_confirmation(files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(files, "big.csv", "a\n" + "1\n" * 600_000)
    monkeypatch.setenv("SINGLESTORE_MCP_LOAD_LIMIT_MB", "1")
    pv = lf.preview(str(p), connection=False)
    assert pv["over_limit"] and pv["limit_mb"] == 1
    base = {"path": str(p), "database": "test", "table": "zz_t", "mode": "append", "options": pv["options"], "columns": pv["columns"],
            "width": 1, "expected_size": pv["size"]}
    with pytest.raises(ValueError, match="confirmation"):
        lf.load_file_start(**base)
    with pytest.raises(ValueError, match="second confirmation"):
        lf.load_file_start(**base, confirmed=True)
    with pytest.raises(ValueError, match="changed since the preview"):
        lf.load_file_start(**{**base, "expected_size": 5}, confirmed=True, confirmed_large=True)
    monkeypatch.setenv("SINGLESTORE_MCP_LOAD_LIMIT_MB", "100")
    assert not lf.preview(str(p), connection=False)["over_limit"]


def test_pipeline_template_has_placeholders_only(files: Path) -> None:
    p = _write(files, "kunder.csv", SAS_EXPORT, "cp1252")
    pv = lf.preview(str(p), database="test", table="kunder", connection=False)
    t = lf.pipeline_sql("test", "kunder", pv["columns"], lf.Options.from_dict(pv["options"]), "kunder.csv", pv["create_sql"])
    s3, az = t["s3"], t["azure"]
    assert "CREATE PIPELINE `load_kunder` AS" in s3 and "LOAD DATA S3 '<bucket>/<path>/kunder.csv'" in s3
    assert '"aws_access_key_id": "<access key id>"' in s3 and '"aws_secret_access_key": "<secret access key>"' in s3
    assert "LOAD DATA AZURE '<container>/<path>/kunder.csv'" in az and '"account_key": "<account key>"' in az
    for sql in (s3, az):
        assert "CREATE TABLE `test`.`kunder`" in sql and "USE `test`;" in sql
        assert "FIELDS TERMINATED BY ';' OPTIONALLY ENCLOSED BY '\"'" in sql and "IGNORE 1 LINES" in sql
        assert "`DATO` = STR_TO_DATE(NULLIF(@DATO, ''), '%d.%m.%Y')" in sql
        assert "`BELØP` = REPLACE(REPLACE(NULLIF(@BELØP, ''), '.', ''), ',', '.')" in sql
        assert "NULL DEFINED BY '.'" in sql
        assert "save it as UTF-8" in sql


def test_server_mode_turns_it_off(files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write(files, "a.csv", "a\n1\n")
    monkeypatch.setenv("SINGLESTORE_MCP_SERVER_MODE", "1")
    info = lf.load_file_info().structured_content
    assert info["available"] is False and "server" in info["reason"]
    for call in (lambda: lf.load_file_browse(str(files)), lambda: lf.load_file_preview(str(p)),
                 lambda: lf.load_file_start(str(p), "test", "t", "new", {}, [], 1, 2, confirmed=True)):
        with pytest.raises(ValueError, match="isn't available on the server"):
            call()
    monkeypatch.delenv("SINGLESTORE_MCP_SERVER_MODE")
    assert lf.load_file_info().structured_content["available"] is True


def test_browse_lists_data_files_only(files: Path) -> None:
    _write(files, "a.csv", "x\n")
    _write(files, "b.sql", "select 1;")
    _write(files, "c.TSV", "x\n")
    (files / "sub").mkdir()
    data = lf.load_file_browse(str(files)).structured_content
    assert sorted(f["name"] for f in data["files"]) == ["a.csv", "c.TSV"] and [f["name"] for f in data["folders"]] == ["sub"]
    assert data["manage"] is False
    with pytest.raises(ValueError, match="outside"):
        lf.load_file_browse(str(Path(files.anchor) / "Windows"))


# ------------------------------------------------------------------------------------------------ cluster

def _wait(mcp: Any, job_id: str, timeout: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = mcp.ok("load_file_status", {"job_id": job_id, "wait_ms": 2000})
        if st["done"]:
            return st
    raise AssertionError("the load didn't finish")


def _start(mcp: Any, path: Path, table: str, **extra: Any) -> dict[str, Any]:
    pv = mcp.ok("load_file_preview", {"path": str(path), "database": "test", "table": table})
    mode = "append" if pv["target"]["exists"] else "new"
    args = {"path": str(path), "database": "test", "table": table, "mode": mode, "options": pv["options"], "columns": pv["columns"],
            "width": pv["width"], "expected_size": pv["size"], "confirmed": True,
            "create_sql": pv.get("create_sql") if mode == "new" else None, **extra}
    return mcp.ok("load_file_start", args)


@pytest.mark.cluster
def test_load_into_the_cluster(cluster: Any, sql_dir: Path) -> None:
    """LOAD DATA LOCAL INFILE into a new table, a bad row (stops, then skipped), the INSERT fallback appending, the
    row-count check and the query history; every zz_test_load_* table is dropped afterwards."""
    mcp = cluster
    tag = f"{int(time.time()) % 100000}"
    t1, t2 = f"zz_test_load_a{tag}", f"zz_test_load_b{tag}"
    good = _write(sql_dir, f"zz_load_{tag}.csv", SAS_EXPORT, "cp1252")
    bad = _write(sql_dir, f"zz_load_bad_{tag}.csv", SAS_EXPORT + "4;Feil;1,00\n", "cp1252")
    try:
        st = _wait(mcp, _start(mcp, good, t1)["job_id"])
        assert st["phase"] == "done", st
        assert st["method"] == "local_infile" and st["rows_loaded"] == 3
        r = st["result"]
        assert r["check_ok"] and r["count_after"] == 3
        rows = mcp.ok("run_sql", {"sql": f"SELECT KUNDE_ID, NAVN, `BELØP`, DATO, TID FROM test.{t1} ORDER BY KUNDE_ID"})["rows"]
        assert rows[0]["NAVN"] == "Ås Bil" and str(rows[0]["BELØP"]) in ("12.5", "12.50") and str(rows[1]["BELØP"]) == "None"
        assert str(rows[0]["DATO"]).startswith("2024-02-01") and str(rows[0]["TID"]).startswith("2024-02-01")

        # A bad row stops the load before anything is written (the new table is removed again) ...
        st = _wait(mcp, _start(mcp, bad, t2)["job_id"])
        assert st["phase"] == "bad_rows" and st["bad_count"] == 1 and st["bad"][0]["line"] == 5, st
        assert st["result"].get("table_removed") is True
        # ... and is skipped on request, with the INSERT fallback appending to the existing table.
        st = _wait(mcp, _start(mcp, bad, t1, skip_bad=True, method="insert")["job_id"])
        assert st["phase"] == "done" and st["method"] == "insert" and st["rows_loaded"] == 3 and st["bad_count"] == 1, st
        assert st["result"]["count_before"] == 3 and st["result"]["count_after"] == 6 and st["result"]["check_ok"]

        hist = mcp.ok("sql_editor_history", {"search": t1, "limit": 10})
        entries = hist.get("entries") or hist.get("history") or []
        assert any("LOAD DATA LOCAL INFILE" in (e.get("sql") or "") for e in entries), hist
        assert any((e.get("sql") or "").startswith("CREATE TABLE") for e in entries)
    finally:
        for t in (t1, t2):
            mcp.call("run_sql", {"sql": f"DROP TABLE IF EXISTS test.{t}"})
