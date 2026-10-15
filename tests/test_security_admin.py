"""Security view v2: SQL building, password masking, lock-out / last-admin checks and the confirmation gate
(offline, against a fake cluster state), and real changes on the cluster with zz_test_ objects only (cleaned up in
teardown and checked to be gone).

Cluster rule for these tests: only objects whose names start with ``zz_test_`` are created, changed, granted to or
dropped; passwords are random, generated at runtime and never printed."""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from support import DB, McpSession

from singlestore_mcp import security_admin as adm
from singlestore_mcp.security import grantee

# ============================================================================= offline unit tests

ME = grantee("admin", "%")
BOB = grantee("bob", "%")
ANN = grantee("ann", "10.0.0.%")


def ctx(**over: Any) -> dict[str, Any]:
    """A small cluster: admin (connected, admin), ann (second admin), bob (plain user in group analysts)."""
    c: dict[str, Any] = {
        "me": {"user": "admin", "host": "%"},
        "users": {
            ME: {"user": "admin", "host": "%", "connections": 2, "default_pool": None},
            ANN: {"user": "ann", "host": "10.0.0.%", "connections": 0, "default_pool": None},
            BOB: {"user": "bob", "host": "%", "connections": 1, "default_pool": "etl"},
        },
        "users_readable": True,
        "global": {ME: {"CREATE USER", "SELECT", "CREATE POOL", "DROP POOL"}, ANN: {"CREATE USER"}, BOB: {"USAGE"}},
        "grantable": {ME: True, ANN: True},
        "schema": {BOB: {"SASDP": {"SELECT", "INSERT"}}},
        "table": {BOB: {("SASDP", "CARS"): {"UPDATE"}}},
        "user_groups": {BOB: {"analysts"}},
        "group_roles": {"analysts": {"reader"}},
        "roles": {"reader", "empty"},
        "groups": {"analysts", "nobody"},
        "role_privs": {"reader": {("SASDP", None): {"SELECT"}}},
        "pools": {"default_pool": {}, "system_auto": {}, "system_optimizer_pool": {},
                  "etl": {"soft_cpu_limit_percentage": 60}},
        "pool_grants": [(BOB, "etl")],
        "databases": {"SASDP", "information_schema"},
        "my_grants": ["GRANT SELECT, CREATE USER, CREATE POOL, DROP POOL ON *.* TO 'admin'@'%' IDENTIFIED BY PASSWORD '<hidden>' WITH GRANT OPTION"],
        "my_roles": set(),
        "cpu_mode": "SOFT",
        "transfer_mode": "grant_option",
    }
    c.update(over)
    return c


def stmts(plan: dict[str, Any]) -> list[str]:
    return [s["display"] for s in plan["statements"]]


class TestSqlBuilding:
    def test_create_user_masks_password_and_binds_it(self) -> None:
        pw = "S3cr'et%pw"
        p = adm.build(ctx(), {"action": "create_user", "user": "zz_test_new", "host": "%", "default_pool": "etl"}, password=pw)
        assert stmts(p) == ["CREATE USER 'zz_test_new'@'%' IDENTIFIED BY '********' WITH DEFAULT RESOURCE POOL = etl;"]
        s = p["statements"][0]
        assert pw not in s["display"] and pw not in repr(adm.public(p))
        # The password goes as a parameter; the literal % of the host is escaped for the driver's formatting.
        assert s["params"] == (pw,)
        assert s["sql"] == "/* s2-security-change */ CREATE USER 'zz_test_new'@'%%' IDENTIFIED BY %s WITH DEFAULT RESOURCE POOL = etl"
        assert s["sql"] % ("'x'",)  # formats cleanly
        assert not p["blockers"]

    def test_create_user_needs_password_and_new_name(self) -> None:
        p = adm.build(ctx(), {"action": "create_user", "user": "bob"}, password="")
        assert any("already exists" in b for b in p["blockers"])
        assert any("password" in b.lower() for b in p["blockers"])

    def test_quoting(self) -> None:
        p = adm.build(ctx(groups={"o'neil"}), {"action": "drop_group", "group": "o'neil"})
        assert stmts(p) == ["DROP GROUP 'o''neil';"]
        assert adm.priv_level("we`ird", "t") == "`we``ird`.`t`"
        assert adm.account("a\\b", "%") == "'a\\\\b'@'%'"

    def test_names_are_validated(self) -> None:
        with pytest.raises(adm.ChangeError):
            adm.build(ctx(), {"action": "create_role", "role": "bad\nname"})
        with pytest.raises(adm.ChangeError):
            adm.build(ctx(), {"action": "create_pool", "pool": "x; DROP"})
        with pytest.raises(adm.ChangeError, match="Unknown privilege"):
            adm.build(ctx(), {"action": "grant", "user": "bob", "privileges": ["SELECT; DROP"]})
        with pytest.raises(adm.ChangeError, match="global"):
            adm.build(ctx(), {"action": "grant", "user": "bob", "privileges": ["SUPER"], "database": "SASDP"})
        with pytest.raises(adm.ChangeError, match="No database"):
            adm.build(ctx(), {"action": "grant", "user": "bob", "privileges": ["SELECT"], "database": "nope"})
        with pytest.raises(adm.ChangeError, match="Wildcard"):
            adm.build(ctx(databases=set()), {"action": "grant", "user": "bob", "privileges": ["SELECT"], "database": "#x_"})

    def test_grant_levels(self) -> None:
        c = ctx()
        g = adm.build(c, {"action": "grant", "user": "bob", "privileges": ["select", "insert"], "database": "SASDP"})
        assert stmts(g) == ["GRANT SELECT, INSERT ON `SASDP`.* TO 'bob'@'%';"]
        t = adm.build(c, {"action": "grant", "grantee_type": "role", "role": "reader", "privileges": ["SELECT"],
                          "database": "SASDP", "table": "CARS", "with_grant_option": True})
        assert stmts(t) == ["GRANT SELECT ON `SASDP`.`CARS` TO ROLE 'reader' WITH GRANT OPTION;"]
        assert any("WITH GRANT OPTION" in w for w in t["warnings"])
        gl = adm.build(c, {"action": "grant", "user": "bob", "privileges": ["CREATE USER"]})
        assert stmts(gl) == ["GRANT CREATE USER ON *.* TO 'bob'@'%';"]
        assert any("administrator" in w for w in gl["warnings"])

    def test_revoke_and_grant_option(self) -> None:
        p = adm.build(ctx(), {"action": "revoke", "user": "bob", "privileges": ["INSERT"], "database": "SASDP", "grant_option": True})
        assert stmts(p) == ["REVOKE INSERT ON `SASDP`.* FROM 'bob'@'%';", "REVOKE GRANT OPTION ON `SASDP`.* FROM 'bob'@'%';"]
        miss = adm.build(ctx(), {"action": "revoke", "user": "bob", "privileges": ["DELETE"], "database": "SASDP"})
        assert any("Not granted directly" in w for w in miss["warnings"])

    def test_revoke_all_lists_each_object(self) -> None:
        p = adm.build(ctx(), {"action": "revoke_all", "user": "bob"})
        assert stmts(p) == ["REVOKE INSERT, SELECT ON `SASDP`.* FROM 'bob'@'%';", "REVOKE UPDATE ON `SASDP`.`CARS` FROM 'bob'@'%';"]
        assert p["confirm"]["typed"] and p["confirm"]["phrase"] == "bob"
        r = adm.build(ctx(), {"action": "revoke_all", "grantee_type": "role", "role": "reader"})
        assert stmts(r) == ["REVOKE SELECT ON `SASDP`.* FROM ROLE 'reader';"]
        empty = adm.build(ctx(), {"action": "revoke_all", "grantee_type": "role", "role": "empty"})
        assert empty["blockers"]

    def test_roles_and_groups(self) -> None:
        c = ctx()
        assert stmts(adm.build(c, {"action": "create_role", "role": "zz_test_r"})) == ["CREATE ROLE 'zz_test_r';"]
        assert stmts(adm.build(c, {"action": "create_group", "group": "zz_test_g"})) == ["CREATE GROUP 'zz_test_g';"]
        assert stmts(adm.build(c, {"action": "grant_role", "role": "empty", "group": "analysts"})) == ["GRANT ROLE 'empty' TO 'analysts';"]
        assert stmts(adm.build(c, {"action": "revoke_role", "role": "reader", "group": "analysts"})) == ["REVOKE ROLE 'reader' FROM 'analysts';"]
        assert stmts(adm.build(c, {"action": "grant_group", "group": "nobody", "user": "bob"})) == ["GRANT GROUP 'nobody' TO 'bob'@'%';"]
        assert stmts(adm.build(c, {"action": "revoke_group", "group": "analysts", "user": "bob"})) == ["REVOKE GROUP 'analysts' FROM 'bob'@'%';"]
        assert adm.build(c, {"action": "grant_role", "role": "reader", "group": "analysts"})["blockers"]
        assert adm.build(c, {"action": "revoke_group", "group": "nobody", "user": "bob"})["blockers"]
        d = adm.build(c, {"action": "drop_role", "role": "reader"})
        assert stmts(d) == ["DROP ROLE 'reader';"] and d["confirm"]["typed"] and d["confirm"]["phrase"] == "reader"
        assert any("analysts" in w for w in d["warnings"])

    def test_pools(self) -> None:
        c = ctx()
        p = adm.build(c, {"action": "create_pool", "pool": "zz_test_p", "settings": {"memory_percentage": 30, "QUERY_TIMEOUT": "60",
                                                                                   "MAX_CONCURRENCY": None}})
        assert stmts(p) == ["CREATE RESOURCE POOL `zz_test_p` WITH MEMORY_PERCENTAGE = 30, QUERY_TIMEOUT = 60;"]
        a = adm.build(c, {"action": "alter_pool", "pool": "etl", "settings": {"MAX_CONCURRENCY": 0}})
        assert stmts(a) == ["ALTER RESOURCE POOL `etl` SET MAX_CONCURRENCY = 0;"]
        with pytest.raises(adm.ChangeError, match="multiple of 5"):
            adm.build(c, {"action": "create_pool", "pool": "zz_test_p", "settings": {"QUERY_TIMEOUT": 7}})
        with pytest.raises(adm.ChangeError, match="HARD"):
            adm.build(c, {"action": "create_pool", "pool": "zz_test_p", "settings": {"HARD_CPU_LIMIT_PERCENTAGE": 10}})
        with pytest.raises(adm.ChangeError, match="built-in"):
            adm.build(c, {"action": "alter_pool", "pool": "default_pool", "settings": {"MEMORY_PERCENTAGE": 50}})
        cpu = adm.build(c, {"action": "create_pool", "pool": "zz_test_p", "settings": {"SOFT_CPU_LIMIT_PERCENTAGE": 50}})
        assert any("sum to 110" in b for b in cpu["blockers"])
        drop = adm.build(c, {"action": "drop_pool", "pool": "etl"})
        assert any("default pool of bob@%" in b for b in drop["blockers"])
        assert stmts(adm.build(c, {"action": "set_default_pool", "user": "bob", "pool": "default_pool"})) == [
            "ALTER USER 'bob'@'%' SET DEFAULT RESOURCE POOL = default_pool;"]
        assert stmts(adm.build(c, {"action": "grant_pool", "pool": "etl", "grantee_type": "role", "role": "reader"})) == [
            "GRANT USAGE ON RESOURCE POOL `etl` TO ROLE 'reader';"]
        assert stmts(adm.build(c, {"action": "revoke_pool", "pool": "etl", "user": "bob"})) == [
            "REVOKE USAGE ON RESOURCE POOL `etl` FROM 'bob'@'%';"]

    def test_mask_sql(self) -> None:
        assert adm.mask_sql("near 'a''b' at line 1", "a'b") == "near '********' at line 1"
        assert adm.mask_sql("nothing", None) == "nothing"


class TestLockout:
    def test_drop_yourself_is_refused(self) -> None:
        p = adm.build(ctx(), {"action": "drop_user", "user": "admin", "host": "%"})
        assert any("lock you out" in b for b in p["blockers"])

    def test_last_admin_is_refused(self) -> None:
        c = ctx()
        c["global"][ANN] = set()  # ann is no admin any more: admin is the last one
        p = adm.build(c, {"action": "revoke", "user": "admin", "privileges": ["CREATE USER"]})
        assert p["blockers"] and any("no admin" in b or "last admin" in b for b in p["blockers"])

    def test_dropping_another_admin_needs_typing(self) -> None:
        p = adm.build(ctx(), {"action": "revoke", "user": "ann", "host": "10.0.0.%", "privileges": ["CREATE USER"]})
        assert not p["blockers"]
        assert p["confirm"]["typed"] and p["confirm"]["phrase"] == "ann"
        assert any("admin" in w for w in p["warnings"])

    def test_own_admin_rights_warn_when_others_remain(self) -> None:
        p = adm.build(ctx(), {"action": "revoke", "user": "admin", "privileges": ["SELECT"], "grant_option": True})
        assert not p["blockers"] and p["confirm"]["typed"]
        assert any("your own admin rights" in w for w in p["warnings"])

    def test_dropping_the_only_other_admin_when_alone(self) -> None:
        c = ctx()
        c["grantable"] = {ANN: True}  # only ann is an admin; the connected user isn't
        p = adm.build(c, {"action": "drop_user", "user": "ann", "host": "10.0.0.%"})
        assert any("last admin" in b for b in p["blockers"])

    def test_role_based_admin_loss_warns(self) -> None:
        c = ctx()
        c["role_privs"]["reader"][(None, None)] = {"CREATE USER"}
        p = adm.build(c, {"action": "revoke_group", "group": "analysts", "user": "bob"})
        assert p["confirm"]["typed"] and any("through a role" in w for w in p["warnings"])

    def test_plain_change_needs_no_typing(self) -> None:
        p = adm.build(ctx(), {"action": "grant", "user": "bob", "privileges": ["SELECT"], "database": "SASDP"})
        assert not p["confirm"]["typed"] and p["confirm"]["phrase"] is None


class TestCapabilities:
    def test_from_show_grants(self) -> None:
        cap = adm.capabilities(ctx())
        assert cap["users"]["ok"] and cap["grants"]["ok"] and cap["create_pool"]["ok"] and cap["drop_pool"]["ok"]
        assert cap["admins"] == sorted([ME, ANN])

    def test_plain_user_gets_reasons(self) -> None:
        c = ctx(me={"user": "bob", "host": "%"}, my_grants=["GRANT USAGE ON *.* TO 'bob'@'%'"])
        cap = adm.capabilities(c)
        for k in ("users", "grants", "roles", "create_pool", "drop_pool", "alter_user"):
            assert not cap[k]["ok"] and cap[k]["reason"]

    def test_parse_global_grants(self) -> None:
        privs, go = adm._parse_global_grants([
            "GRANT SELECT ON `SASDP`.* TO 'x'@'%'",
            "GRANT CREATE USER, ALTER USER ON *.* TO 'x'@'%' WITH GRANT OPTION"])
        assert privs == {"CREATE USER", "ALTER USER"} and go


class TestApplyGate:
    def test_refuses_without_confirm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(adm, "load_context", lambda: pytest.fail("must not read the cluster"))
        with pytest.raises(adm.ChangeError, match="confirm=True"):
            adm.apply({"action": "create_role", "role": "zz_test_x"})

    def test_refuses_wrong_typed_name_and_runs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ran: list[Any] = []
        monkeypatch.setattr(adm, "load_context", lambda: ctx())
        monkeypatch.setattr(adm.db, "execute", lambda *a, **k: ran.append(a))
        with pytest.raises(adm.ChangeError, match="type 'nobody'"):
            adm.apply({"action": "drop_group", "group": "nobody"}, confirm=True, typed="nobod")
        with pytest.raises(adm.ChangeError, match="lock you out"):
            adm.apply({"action": "drop_user", "user": "admin"}, confirm=True, typed="admin")
        assert ran == []

    def test_runs_and_masks_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import singlestoredb as s2

        pw = "Zz9-" + secrets.token_hex(8)
        seen: list[Any] = []

        def fake_execute(sql: str, params: Any = None, **kw: Any) -> Any:
            seen.append((sql, params))
            raise s2.ProgrammingError(f"1064: syntax error near '{pw}'")

        monkeypatch.setattr(adm, "load_context", lambda: ctx())
        monkeypatch.setattr(adm.db, "execute", fake_execute)
        out = adm.apply({"action": "create_user", "user": "zz_test_u"}, confirm=True, password=pw)
        assert not out["ok"] and out["results"][0]["status"] == "error"
        assert pw not in repr(out) and pw not in adm.summary(out)
        assert seen[0][1] == (pw,) and pw not in seen[0][0]

    def test_plan_never_takes_a_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(adm, "load_context", lambda: ctx())
        p = adm.plan({"action": "create_user", "user": "zz_test_u"})
        assert p["statements"] == ["CREATE USER 'zz_test_u'@'%' IDENTIFIED BY '********';"] and not p["blockers"]
        assert not p["confirm"]["typed"]

    def test_not_allowed_without_privilege(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the privilege the preview still works (for a script) but says so; apply refuses."""
        monkeypatch.setattr(adm, "load_context", lambda: ctx(me={"user": "bob", "host": "%"},
                                                               my_grants=["GRANT USAGE ON *.* TO 'bob'@'%'"]))
        monkeypatch.setattr(adm.db, "execute", lambda *a, **k: pytest.fail("must not run anything"))
        p = adm.plan({"action": "create_user", "user": "zz_test_u"})
        assert p["allowed"]["ok"] is False and "CREATE USER" in p["allowed"]["reason"]
        assert p["statements"] == ["CREATE USER 'zz_test_u'@'%' IDENTIFIED BY '********';"]
        with pytest.raises(adm.ChangeError, match="CREATE USER"):
            adm.apply({"action": "create_user", "user": "zz_test_u"}, confirm=True, password="x" * 12)


# ============================================================================= SQL script for a DBA (offline)

BASKET = [
    {"action": "grant", "grantee_type": "role", "role": "zz_test_r", "privileges": ["SELECT"], "database": "SASDP"},
    {"action": "drop_group", "group": "nobody"},
    {"action": "create_role", "role": "zz_test_r"},
    {"action": "create_user", "user": "zz_test_u", "default_pool": "zz_test_p"},
    {"action": "create_pool", "pool": "zz_test_p", "settings": {"MEMORY_PERCENTAGE": 10}},
    {"action": "grant_role", "role": "zz_test_r", "group": "analysts"},
]


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch):
    """A fake cluster; any statement execution fails the test (scripts never run anything)."""
    monkeypatch.setattr(adm, "load_context", lambda: ctx())
    monkeypatch.setattr(adm.db, "execute", lambda *a, **k: pytest.fail("a script must not run anything"))
    monkeypatch.setattr(adm, "_connection_label", lambda: "Test cluster (s2.example:3306)")
    return monkeypatch


class TestScript:
    def test_header_order_and_placeholder(self, offline: pytest.MonkeyPatch) -> None:
        out = adm.script(BASKET)
        text = out["script"]
        sql = [line for line in text.splitlines() if line and not line.startswith("--")]
        assert sql == [
            "CREATE RESOURCE POOL `zz_test_p` WITH MEMORY_PERCENTAGE = 10;",
            "CREATE ROLE 'zz_test_r';",
            "CREATE USER 'zz_test_u'@'%' IDENTIFIED BY '<set password>' WITH DEFAULT RESOURCE POOL = zz_test_p;",
            "GRANT SELECT ON `SASDP`.* TO ROLE 'zz_test_r';",
            "GRANT ROLE 'zz_test_r' TO 'analysts';",
            "DROP GROUP 'nobody';",
        ]
        head = text.split("\n\n")[0]
        assert "Nothing has been run" in head and "Connection: Test cluster (s2.example:3306)" in head
        assert "Prepared by: admin@%" in head and "Changes (6):" in head
        assert "--   3. Create user: CREATE USER 'zz_test_u'@'%' IDENTIFIED BY '<set password>'" in head
        assert "-- WARNING: Host '%' lets the user connect from any host." in text
        assert "-- WARNING: Replace '<set password>'" in text
        assert out["count"] == 6 and out["statements"] == 6
        assert [i["index"] for i in out["items"]] == [4, 2, 3, 0, 5, 1]
        assert out["file_name"].startswith("security-") and out["file_name"].endswith("-6-changes.sql")

    def test_keep_order(self, offline: pytest.MonkeyPatch) -> None:
        acts = [{"action": "revoke_group", "group": "analysts", "user": "bob"}, {"action": "create_role", "role": "zz_test_r"}]
        assert [i["action"] for i in adm.script(acts)["items"]] == ["create_role", "revoke_group"]
        kept = adm.script(acts, keep_order=True)
        assert [i["action"] for i in kept["items"]] == ["revoke_group", "create_role"]
        assert "as listed by the author" in kept["script"]

    def test_never_a_password(self, offline: pytest.MonkeyPatch) -> None:
        pw = "Zz9-" + secrets.token_hex(8)
        out = adm.script([{"action": "create_user", "user": "zz_test_u", "password": pw}])
        assert pw not in repr(out) and "********" not in out["script"]
        assert "IDENTIFIED BY '<set password>'" in out["script"]
        assert out["file_name"].endswith("-create-user-zz_test_u.sql")
        p = adm.plan({"action": "create_user", "user": "zz_test_u", "password": pw},
                     pending=[{"action": "create_role", "role": "zz_test_r", "password": pw}])
        assert pw not in repr(p)

    def test_same_statements_as_run(self, offline: pytest.MonkeyPatch) -> None:
        for a in ({"action": "grant", "user": "bob", "privileges": ["SELECT", "insert"], "database": "SASDP", "with_grant_option": True},
                  {"action": "revoke_all", "grantee_type": "user", "user": "bob"},
                  {"action": "alter_pool", "pool": "etl", "settings": {"MAX_CONCURRENCY": 0}}):
            p = adm.plan(a)
            s = adm.script([a])
            assert s["items"][0]["statements"] == p["statements"]
            assert all(f"-- WARNING: {w}" in s["script"] for w in p["warnings"])

    def test_without_change_privileges(self, offline: pytest.MonkeyPatch) -> None:
        offline.setattr(adm, "load_context", lambda: ctx(me={"user": "bob", "host": "%"}, my_grants=["GRANT USAGE ON *.* TO 'bob'@'%'"]))
        out = adm.script([{"action": "create_role", "role": "zz_test_r"}, {"action": "create_user", "user": "zz_test_u"}])
        assert "CREATE ROLE 'zz_test_r';" in out["script"]
        assert "-- NOTE: bob@% can't run all of this" in out["script"] and "CREATE USER privilege" in out["script"]
        assert not out["items"][0]["allowed"]["ok"]

    def test_validates_names_and_blockers(self, offline: pytest.MonkeyPatch) -> None:
        with pytest.raises(adm.ChangeError, match=r"Change 2 \(Create role\).*already exists"):
            adm.script([{"action": "create_group", "group": "zz_test_g"}, {"action": "create_role", "role": "reader"}])
        with pytest.raises(adm.ChangeError, match="control characters"):
            adm.script([{"action": "create_role", "role": "bad\nname"}])
        with pytest.raises(adm.ChangeError, match="Pool names"):
            adm.script([{"action": "create_pool", "pool": "1; DROP"}])
        with pytest.raises(adm.ChangeError, match="No user"):
            adm.script([{"action": "drop_user", "user": "ghost"}])
        with pytest.raises(adm.ChangeError, match="already exists"):  # the same create twice in one script
            adm.script([{"action": "create_role", "role": "zz_test_r"}, {"action": "create_role", "role": "zz_test_r"}])
        with pytest.raises(adm.ChangeError, match="at least one"):
            adm.script([])

    def test_basket_dependencies(self, offline: pytest.MonkeyPatch) -> None:
        grant = {"action": "grant", "grantee_type": "role", "role": "zz_test_r", "privileges": ["SELECT"], "database": "SASDP"}
        with pytest.raises(adm.ChangeError, match="No role zz_test_r"):
            adm.plan(grant)
        p = adm.plan(grant, pending=[{"action": "create_role", "role": "zz_test_r"}])
        assert p["needs_script"] and p["statements"] == ["GRANT SELECT ON `SASDP`.* TO ROLE 'zz_test_r';"]
        assert not adm.plan({"action": "create_role", "role": "zz_test_x"}, pending=[{"action": "create_role", "role": "zz_test_r"}])["needs_script"]
        # A user created, put in a group and dropped in one script: safe order, each checked after the earlier ones.
        out = adm.script([{"action": "drop_user", "user": "zz_test_u"}, {"action": "create_user", "user": "zz_test_u"},
                          {"action": "grant_group", "group": "nobody", "user": "zz_test_u"}])
        assert [i["action"] for i in out["items"]] == ["create_user", "grant_group", "drop_user"]
        # Lock-out checks see the earlier changes: with ann dropped, admin revoking its own rights leaves no admin.
        with pytest.raises(adm.ChangeError, match="blocked"):
            adm.script([{"action": "drop_user", "user": "ann", "host": "10.0.0.%"},
                        {"action": "revoke_all", "grantee_type": "user", "user": "admin"}], keep_order=True)


# ============================================================================= cluster: zz_test_ objects only

PREFIX = "zz_test_"


def _leftovers(mcp: McpSession) -> list[str]:
    sql = ("SELECT CONCAT('user ', USER) AS o FROM information_schema.USERS WHERE USER LIKE 'zz\\_test\\_%' UNION ALL "
           "SELECT CONCAT('role ', ROLE) FROM information_schema.ROLES WHERE ROLE LIKE 'zz\\_test\\_%' UNION ALL "
           "SELECT CONCAT('group ', GROUPNAME) FROM information_schema.`GROUPS` WHERE GROUPNAME LIKE 'zz\\_test\\_%' UNION ALL "
           "SELECT CONCAT('pool ', POOL_NAME) FROM information_schema.RESOURCE_POOLS WHERE POOL_NAME LIKE 'zz\\_test\\_%'")
    r = mcp.ok("run_sql", {"sql": sql})
    rows = r.get("rows", []) if isinstance(r, dict) else []
    return [str(next(iter(x.values())) if isinstance(x, dict) else x[0]) for x in rows]


def _cleanup(mcp: McpSession, names: dict[str, str]) -> None:
    """Drop every zz_test_ object of this run through the app (which tests the DROP paths), then with plain
    IF EXISTS statements as a fallback. Errors like "doesn't exist" are ignored; safe to call twice."""
    assert all(v.startswith(PREFIX) for v in names.values())
    for action in (
        {"action": "set_default_pool", "user": names["user"], "pool": "default_pool"},
        {"action": "drop_user", "user": names["user"]},
        {"action": "drop_user", "user": names["user2"]},
        {"action": "drop_group", "group": names["group"]},
        {"action": "drop_role", "role": names["role"]},
        {"action": "drop_pool", "pool": names["pool"]},
    ):
        typed = action.get("user") or action.get("group") or action.get("role") or action.get("pool")
        mcp.call("security_change_apply", {"action": action, "confirm": True, "typed_confirmation": typed}, timeout=60)
    for sql in (f"DROP USER IF EXISTS '{names['user']}'@'%', '{names['user2']}'@'%'", f"DROP ROLE IF EXISTS '{names['role']}'",
                f"DROP GROUP IF EXISTS '{names['group']}'"):
        mcp.call("run_sql", {"sql": sql}, timeout=60)
    if any(names["pool"] in x for x in _leftovers(mcp)):
        mcp.call("run_sql", {"sql": f"DROP RESOURCE POOL {names['pool']}"}, timeout=60)


@pytest.fixture
def zz(cluster: McpSession):
    tag = secrets.token_hex(3)
    names = {"user": f"{PREFIX}u{tag}", "user2": f"{PREFIX}v{tag}", "role": f"{PREFIX}r{tag}", "group": f"{PREFIX}g{tag}",
             "pool": f"{PREFIX}p{tag}"}
    try:
        yield cluster, names
    finally:
        _cleanup(cluster, names)
        left = [x for x in _leftovers(cluster) if tag in x]
        assert not left, f"zz_test_ objects left behind: {left}"


def _apply(mcp: McpSession, action: dict[str, Any], **kw: Any) -> dict[str, Any]:
    plan = mcp.ok("security_change_plan", {"action": action})
    assert not plan["blockers"], plan
    args = {"action": action, "confirm": True, **kw}
    if plan["confirm"]["typed"]:
        args["typed_confirmation"] = plan["confirm"]["phrase"]
    out = mcp.ok("security_change_apply", args, timeout=60)
    assert out["ok"], out
    return out


@pytest.mark.cluster
def test_change_round_trip_on_zz_test_objects(zz) -> None:
    mcp, n = zz
    pw = secrets.token_urlsafe(18)  # never printed
    cap = mcp.ok("security_admin_capabilities")
    if not (cap["users"]["ok"] and cap["roles"]["ok"] and cap["create_pool"]["ok"]):
        pytest.skip("The connected user can't manage users, roles and pools")

    # Refused without confirmation, and the password never comes back.
    r = mcp.call("security_change_apply", {"action": {"action": "create_user", "user": n["user"]}, "password": pw})
    assert r.is_error and "confirm" in r.text
    out = _apply(mcp, {"action": "create_user", "user": n["user"]}, password=pw)
    assert pw not in repr(out) and "'********'" in out["results"][0]["statement"]
    _apply(mcp, {"action": "create_user", "user": n["user2"]}, password=secrets.token_urlsafe(18))

    _apply(mcp, {"action": "create_role", "role": n["role"]})
    _apply(mcp, {"action": "create_group", "group": n["group"]})
    _apply(mcp, {"action": "grant_role", "role": n["role"], "group": n["group"]})
    _apply(mcp, {"action": "grant_group", "group": n["group"], "user": n["user"]})
    _apply(mcp, {"action": "grant", "grantee_type": "role", "role": n["role"], "privileges": ["SELECT"], "database": DB})
    _apply(mcp, {"action": "grant", "user": n["user2"], "privileges": ["SELECT", "INSERT"], "database": DB, "table": "CARS"})
    _apply(mcp, {"action": "create_pool", "pool": n["pool"], "settings": {"MEMORY_PERCENTAGE": 10, "QUERY_TIMEOUT": 30}})
    _apply(mcp, {"action": "alter_pool", "pool": n["pool"], "settings": {"MAX_CONCURRENCY": 4}})
    _apply(mcp, {"action": "set_default_pool", "user": n["user"], "pool": n["pool"]})
    _apply(mcp, {"action": "grant_pool", "pool": n["pool"], "user": n["user2"]})

    detail = mcp.ok("security_user_detail", {"user": n["user"], "host": "%"})
    assert detail["default_pool"] == n["pool"] and n["group"] in detail["groups"]
    assert any(p["object"] == f"{DB}.*" and p["privilege"] == "SELECT" for p in detail["effective"])
    assert all(pw not in g for g in detail["show_grants"])

    # Dropping a pool that is someone's default is blocked in the plan.
    drop_pool = mcp.ok("security_change_plan", {"action": {"action": "drop_pool", "pool": n["pool"]}})
    assert drop_pool["blockers"] and drop_pool["confirm"]["typed"]
    # A DROP needs the typed name.
    r = mcp.call("security_change_apply", {"action": {"action": "drop_role", "role": n["role"]}, "confirm": True,
                                           "typed_confirmation": "wrong"})
    assert r.is_error and "type" in r.text

    _apply(mcp, {"action": "revoke_pool", "pool": n["pool"], "user": n["user2"]})
    _apply(mcp, {"action": "revoke_all", "user": n["user2"]})
    d2 = mcp.ok("security_user_detail", {"user": n["user2"], "host": "%"})
    assert not d2["effective"], d2["effective"]
    _apply(mcp, {"action": "revoke", "grantee_type": "role", "role": n["role"], "privileges": ["SELECT"], "database": DB})
    _apply(mcp, {"action": "revoke_group", "group": n["group"], "user": n["user"]})
    _apply(mcp, {"action": "revoke_role", "role": n["role"], "group": n["group"]})

    # The password text isn't in the cluster's query views either (checked without sending it: the texts that
    # mention the test user are read back and searched locally).
    rows = mcp.ok("run_sql", {"sql": f"SELECT QUERY_TEXT AS t FROM information_schema.MV_PLANCACHE WHERE QUERY_TEXT LIKE '%{n['user']}%'"})
    texts = [str(next(iter(x.values())) if isinstance(x, dict) else x[0]) for x in rows.get("rows", [])]
    assert all(pw not in t for t in texts)
