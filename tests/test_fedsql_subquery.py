"""FedSQL SELECT with a subquery (offline): detection, the derived-table rewrite the editor runs instead (the
CREATE TABLE … AS <select with a subquery> FedSQL pushes down whole fails on SingleStore with "Unknown database
'WORK'"; checked on Zeus 2026-10-30), the ORDER BY repeated outside, and the pre-run check's answers."""

from __future__ import annotations

import pytest

from singlestore_mcp import sas_sql
from singlestore_mcp.apps import sas_workspace

SUB = "select make, model, msrp from S2.CARS where msrp > (select avg(msrp) from S2.CARS) and origin = 'Asia'"


@pytest.mark.parametrize("sql", [
    SUB,
    "select make, count(*) as n from S2.CARS where make in (select make from S2.CARS where msrp > 60000) group by make",
    "select a.make from S2.CARS a where exists (select 1 from S2.CARS b where b.make = a.make and b.msrp > 80000)",
    "select make from S2.CARS group by make having avg(msrp) > (select avg(msrp) from S2.CARS)",
    "SELECT make FROM s2.cars WHERE msrp>(SELECT avg(msrp) FROM s2.cars);",
])
def test_subqueries_are_found(sql: str) -> None:
    sub = sas_sql.fedsql_subquery(sql)
    assert sub and sub["rewrite"], sql
    assert sub["rewrite"].startswith("select * from (\n") and ") as _s2q" in sub["rewrite"]
    assert not sub["rewrite"].rstrip().endswith(";")


@pytest.mark.parametrize("sql", [
    "select make, count(*) from S2.CARS group by make",
    "select * from (select make from S2.CARS) t",                       # a derived table only: runs as it is
    "select * from S2.CARS a join (select make, max(msrp) m from S2.CARS group by make) b on a.make = b.make",
    "select make from S2.CARS where model = '(select 1)'",              # in a string
    "select make from S2.CARS /* (select 1) */ where 1 = 1",            # in a comment
    "insert into work.x select * from S2.CARS where msrp > (select 1)",  # not a SELECT
    "proc fedsql; select 1; quit;",
])
def test_no_subquery_no_rewrite(sql: str) -> None:
    assert sas_sql.fedsql_subquery(sql) is None


def test_order_by_is_repeated_outside_the_derived_table() -> None:
    # SingleStore drops an ORDER BY inside a derived table (checked on Zeus): it goes outside, with output names.
    sub = sas_sql.fedsql_subquery(SUB + " order by msrp desc, Model")
    assert sub["rewrite"].endswith(") as _s2q\norder by msrp desc, Model")
    sub = sas_sql.fedsql_subquery("select a.make, count(*) n from S2.CARS a where a.make in (select make from S2.CARS) "
                                  "group by a.make order by 2 desc, a.make nulls last")
    assert sub["rewrite"].endswith("order by 2 desc, make nulls last")
    sub = sas_sql.fedsql_subquery('select "Make", msrp as price from S2.CARS where msrp > (select 1) order by price')
    assert sub["rewrite"].endswith("order by price")
    sub = sas_sql.fedsql_subquery("select * from S2.CARS where msrp > (select 1) order by invoice")  # *: any column
    assert sub["rewrite"].endswith("order by invoice")


@pytest.mark.parametrize("sql, why", [
    (SUB + " order by upper(make)", "isn't a column of the result"),
    (SUB + " order by invoice", "isn't one of the selected columns"),
    (SUB + " order by 7", "no such column"),
])
def test_order_by_that_cant_be_repeated_isnt_rewritten(sql: str, why: str) -> None:
    sub = sas_sql.fedsql_subquery(sql)
    assert sub["rewrite"] is None and why in sub["reason"]


def test_a_top_level_limit_runs_as_it_is() -> None:
    # A LIMIT keeps FedSQL from pushing the CREATE TABLE down (checked): nothing to rewrite or warn about.
    sub = sas_sql.fedsql_subquery(SUB + " order by msrp desc limit 5")
    assert sub == {"rewrite": None, "limit": True, "order_by": None, "reason": None}
    w = sas_sql.wrap("fedsql", SUB + " limit 5;")
    assert w["derived"] is None and "select * from (" not in w["code"]


def test_wrap_runs_the_derived_table() -> None:
    w = sas_sql.wrap("fedsql", SUB + ";", table="_S2R_ABCD1234")
    assert w["derived"] == "subquery" and w["kind"] == "select" and w["result"] == ("WORK", "_S2R_ABCD1234")
    code = w["code"]
    assert 'create table WORK."_S2R_ABCD1234" as\n  select * from (\n    select make, model, msrp from S2.CARS where msrp >' in code
    assert code.rstrip().endswith(") as _s2q;\nquit;")
    plain = sas_sql.wrap("fedsql", "select make from S2.CARS;")
    assert plain["derived"] is None and "_s2q" not in plain["code"]
    assert sas_sql.wrap("procsql", SUB + ";").get("derived") is None  # PROC SQL is not FedSQL


def test_pre_run_check_tool() -> None:
    d = sas_workspace.sas_fedsql_check(SUB).structured_content
    assert d["subquery"] and d["rewrite"] and not d["warn"]
    d = sas_workspace.sas_fedsql_check(SUB + " order by upper(make)").structured_content
    assert d["warn"] and "derived table" in d["message"] and "Unknown database 'WORK'" in d["message"]
    d = sas_workspace.sas_fedsql_check("select 1 from S2.CARS").structured_content
    assert not d["subquery"] and not d["warn"]
