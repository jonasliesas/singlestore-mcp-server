"""Security view v2: changes to users, roles, groups, grants and resource pools, with preview and confirmation.

Every change goes through two steps:

1. ``plan(action)`` (read-only) builds the exact statements, masks any password, and checks the change against
   the cluster as it is now: does the object exist, would it lock the connected user out, would it remove the
   last admin, does it touch the connected user or an admin user (then the user must type the object's name).
2. ``apply(action, confirm=True, typed=...)`` builds the plan again from fresh cluster state, refuses on any
   blocker, a missing confirmation or a wrong typed name, and runs the statements one by one (stopping at the
   first error).

Passwords: only ``create_user`` takes one. It is sent to SingleStore as a bound parameter, shown as
``'********'`` in every statement, result and error text, and never logged or stored. Every statement starts with
``/* s2-security-change */``; the local Query History store leaves "/* s2-" statements out.

Syntax checked against the SingleStore 9.0 docs (CREATE/DROP/ALTER USER, GRANT, REVOKE, CREATE/DROP ROLE,
CREATE/DROP GROUP, GRANT/REVOKE ROLE, GRANT/REVOKE GROUP, CREATE/ALTER/DROP RESOURCE POOL, "Administer default
resource pools") and tried on 9.0.40 with zz_test_ objects. Found on 9.0.40, beyond the docs:
- ``REVOKE ALL PRIVILEGES, GRANT OPTION FROM user`` needs every privilege the revoker could revoke (an admin
  without SUPER gets "Access denied"), and ``... FROM ROLE`` is a syntax error. "Revoke all" therefore revokes
  the privileges the grantee holds, one statement per object.
- ``ALTER RESOURCE POOL p SET a = 1, b = 2`` (no parentheses); a limit can't be set back to NULL, except
  MAX_CONCURRENCY = 0 (= no limit, documented).
- A user's default pool can't be cleared (``= ''`` is a syntax error); set it to default_pool instead.
- ``DROP RESOURCE POOL`` fails while the pool is some user's default pool (error 2289).
- USER_PRIVILEGES lists WITH GRANT OPTION on *.* as IS_GRANTABLE = YES and an extra PRIVILEGE_TYPE 'GRANT' row;
  ROLE_PRIVILEGES doesn't show a role's grant option.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import singlestoredb as s2

from . import security as sec
from .db import db

MARKER = "/* s2-security-change */"
MASK = "'********'"
IS = "information_schema"

BUILTIN_POOLS = {"default_pool", "system_auto", "system_optimizer_pool"}
MAX_USER_POOLS = 10

# GRANT priv_type list from the 9.0 GRANT reference (GRANT OPTION is the "with grant option" tick instead).
PRIVILEGES = [
    "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "INDEX", "CREATE VIEW", "SHOW VIEW", "ALTER VIEW",
    "DROP VIEW", "CREATE TEMPORARY TABLES", "LOCK TABLES", "EXECUTE", "CREATE ROUTINE", "ALTER ROUTINE", "SHOW ROUTINE",
    "CREATE PIPELINE", "DROP PIPELINE", "ALTER PIPELINE", "START PIPELINE", "SHOW PIPELINE", "SHOW METADATA", "BACKUP",
    "RELOAD", "CREATE DATABASE", "DROP DATABASE", "CREATE LINK", "DROP LINK", "SHOW LINK", "OUTBOUND", "ALTER EVENT TRACE",
    "PROCESS", "FILE READ", "FILE WRITE", "SUPER", "CREATE USER", "ALTER USER", "SYSTEM_VARIABLES_ADMIN", "CLUSTER",
    "CREATE POOL", "DROP POOL", "ALL PRIVILEGES",
]
# Global-only (*.*) per the 9.0 GRANT reference.
GLOBAL_ONLY = {"PROCESS", "FILE READ", "FILE WRITE", "SHOW METADATA", "SUPER", "CREATE USER", "SYSTEM_VARIABLES_ADMIN",
               "CLUSTER", "CREATE POOL", "DROP POOL"}
# Offered on a single table (the data and view privileges).
TABLE_PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "INDEX", "CREATE VIEW", "SHOW VIEW",
                    "ALTER VIEW", "DROP VIEW", "ALL PRIVILEGES"]
# Privileges that make a user an administrator of users and grants (together with the grant option).
ADMIN_PRIVS = {"SUPER", "CREATE USER"}

# Resource pool settings: name -> (min, max, step); max None = no upper bound.
POOL_SETTINGS: dict[str, tuple[int, int | None, int]] = {
    "MEMORY_PERCENTAGE": (1, 100, 1),
    "QUERY_MEMORY_PERCENTAGE": (1, 100, 1),
    "QUERY_TIMEOUT": (5, None, 5),
    "SOFT_CPU_LIMIT_PERCENTAGE": (1, 100, 1),
    "HARD_CPU_LIMIT_PERCENTAGE": (1, 100, 1),
    "MAX_CONCURRENCY": (0, None, 1),
    "MAX_QUEUE_DEPTH": (1, None, 1),
}

ACTIONS = {
    "create_user": "Create user", "drop_user": "Drop user", "grant": "Grant privileges", "revoke": "Revoke privileges",
    "revoke_all": "Revoke all privileges", "create_role": "Create role", "drop_role": "Drop role",
    "create_group": "Create group", "drop_group": "Drop group", "grant_role": "Grant role to group",
    "revoke_role": "Revoke role from group", "grant_group": "Add user to group", "revoke_group": "Remove user from group",
    "create_pool": "Create resource pool", "alter_pool": "Change resource pool", "drop_pool": "Drop resource pool",
    "set_default_pool": "Set default resource pool", "grant_pool": "Grant resource pool usage",
    "revoke_pool": "Revoke resource pool usage",
}

_PW = "\x00password\x00"  # where the password goes; names can't hold control characters
_POOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class ChangeError(ValueError):
    """A change that can't be planned or run (bad input, refused, missing confirmation)."""


# ----------------------------------------------------------------------------- SQL building

def q(s: str) -> str:
    """A SQL string literal (users, hosts, roles and groups are written as strings)."""
    return sec.quote_str(s)


def bt(s: str) -> str:
    return "`" + str(s).replace("`", "``") + "`"


def account(user: str, host: str) -> str:
    return f"{q(user)}@{q(host)}"


def priv_level(database: str | None, table: str | None) -> str:
    if not database:
        return "*.*"
    return f"{bt(database)}.{bt(table) if table else '*'}"


def _name(value: Any, what: str, max_len: int = 64) -> str:
    s = str(value or "").strip() if what != "host" else str(value if value is not None else "%")
    if not s:
        raise ChangeError(f"{what} is required")
    if len(s) > max_len:
        raise ChangeError(f"{what} is longer than {max_len} characters")
    if _CTRL.search(s):
        raise ChangeError(f"{what} contains control characters")
    return s


def _pool_name(value: Any) -> str:
    s = str(value or "").strip()
    if not _POOL_NAME.match(s):
        raise ChangeError("Pool names here are letters, digits and _ (not starting with a digit), at most 64 characters.")
    return s


def _privs(values: Any, level: str) -> list[str]:
    if isinstance(values, str):
        values = [values]
    out = []
    for v in values or []:
        p = " ".join(str(v).upper().split())
        if p not in PRIVILEGES:
            raise ChangeError(f"Unknown privilege {v!r}")
        if level != "global" and p in GLOBAL_ONLY:
            raise ChangeError(f"{p} can only be granted on *.* (global)")
        if level == "table" and p not in TABLE_PRIVILEGES:
            raise ChangeError(f"{p} isn't offered on a single table")
        if p not in out:
            out.append(p)
    if not out:
        raise ChangeError("Pick at least one privilege")
    if "ALL PRIVILEGES" in out and len(out) > 1:
        raise ChangeError("ALL PRIVILEGES can't be combined with other privileges")
    return out


def pool_settings(settings: Any, mode: str | None) -> list[tuple[str, int]]:
    """Validated (name, value) pairs in a stable order."""
    out = []
    for k, v in (settings or {}).items():
        key = str(k).upper()
        if v is None or v == "":
            continue
        if key not in POOL_SETTINGS:
            raise ChangeError(f"Unknown resource pool setting {k!r}")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ChangeError(f"{key} must be a whole number") from None
        if isinstance(v, float) and v != n:
            raise ChangeError(f"{key} must be a whole number")
        lo, hi, step = POOL_SETTINGS[key]
        if n < lo or (hi is not None and n > hi):
            raise ChangeError(f"{key} must be {lo}{f'–{hi}' if hi is not None else ' or more'}")
        if n % step:
            raise ChangeError(f"{key} must be a multiple of {step} seconds")
        out.append((key, n))
    names = {k for k, _ in out}
    mode = (mode or "SOFT").upper()
    if "SOFT_CPU_LIMIT_PERCENTAGE" in names and mode != "SOFT":
        raise ChangeError(f"SOFT_CPU_LIMIT_PERCENTAGE needs resource_governor_cpu_limit_mode = SOFT (it is {mode})")
    if "HARD_CPU_LIMIT_PERCENTAGE" in names and mode != "HARD":
        raise ChangeError(f"HARD_CPU_LIMIT_PERCENTAGE needs resource_governor_cpu_limit_mode = HARD (it is {mode})")
    order = list(POOL_SETTINGS)
    return sorted(out, key=lambda kv: order.index(kv[0]))


def mask_sql(text: str, password: str | None) -> str:
    """Hide a password anywhere in a text (statement, error message)."""
    if not password:
        return text
    for form in {password, password.replace("'", "''"), password.replace("\\", "\\\\").replace("'", "''"),
                 password.replace("\\", "\\\\").replace("'", "\\'")}:
        if form:
            text = text.replace(form, "********")
    return text


# ----------------------------------------------------------------------------- cluster state

def _rows(v: Any) -> list[dict[str, Any]]:
    return v if isinstance(v, list) else []


def _soft(fn: Any) -> Any:
    return sec._soft(fn)


def _q(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return sec._q(sql, params)


def _my_grants() -> list[str]:
    return sec.mask_grants([str(next(iter(r.values()))) for r in db.execute(f"{sec.MARKER} SHOW GRANTS")[1]])


def load_context() -> dict[str, Any]:
    """Everything the checks need, in one parallel batch (each read soft: a missing view gives {"error"})."""
    (users, uprivs, sprivs, tprivs, ug, gr, roles, groups, rp, pools, poolprivs, dbs, me, grants, my_roles, gvars) = db.parallel(
        _soft(lambda: _q(f"SELECT USER, HOST, CONNECTIONS, DEFAULT_RESOURCE_POOL FROM {IS}.USERS WHERE IS_DELETED = 0")),
        _soft(lambda: _q(f"SELECT GRANTEE, PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.USER_PRIVILEGES")),
        _soft(lambda: _q(f"SELECT GRANTEE, TABLE_SCHEMA, PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.SCHEMA_PRIVILEGES")),
        _soft(lambda: _q(f"SELECT GRANTEE, TABLE_SCHEMA, TABLE_NAME, PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.TABLE_PRIVILEGES")),
        _soft(lambda: _q(f"SELECT USER, `GROUP` FROM {IS}.USERS_GROUPS")),
        _soft(lambda: _q(f"SELECT `GROUP`, ROLE FROM {IS}.GROUPS_ROLES")),
        _soft(lambda: _q(f"SELECT ROLE FROM {IS}.ROLES")),
        _soft(lambda: _q(f"SELECT GROUPNAME FROM {IS}.`GROUPS`")),
        _soft(lambda: _q(f"SELECT ROLE, `DATABASE`, `TABLE`, PRIVILEGES FROM {IS}.ROLE_PRIVILEGES")),
        _soft(lambda: _q(f"SELECT * FROM {IS}.RESOURCE_POOLS")),
        _soft(lambda: _q(f"SELECT GRANTEE, RESOURCE_POOL FROM {IS}.RESOURCE_POOL_PRIVILEGES")),
        _soft(lambda: _q(f"SELECT SCHEMA_NAME FROM {IS}.SCHEMATA")),
        sec._current_user,
        _soft(_my_grants),
        _soft(lambda: _q(f"SELECT ROLE FROM {IS}.CURRENT_USER_ROLES")),
        _soft(lambda: _q("SELECT @@resource_governor_cpu_limit_mode AS cpu_mode, @@privilege_transfer_mode AS transfer_mode")),
    )
    gv = (_rows(gvars) or [{}])[0]
    ctx: dict[str, Any] = {
        "me": {"user": me["user"], "host": me["host"]},
        "users": {sec.grantee(u["USER"], u["HOST"]): {"user": u["USER"], "host": u["HOST"],
                                                       "connections": int(u["CONNECTIONS"] or 0),
                                                       "default_pool": u["DEFAULT_RESOURCE_POOL"] or None}
                  for u in _rows(users)},
        "users_readable": isinstance(users, list),
        "global": {}, "grantable": {}, "schema": {}, "table": {},
        "user_groups": {}, "group_roles": {}, "roles": {r["ROLE"] for r in _rows(roles)},
        "groups": {g["GROUPNAME"] for g in _rows(groups)},
        "role_privs": {},
        "pools": {p["POOL_NAME"]: {k.lower(): p.get(k) for k in p if k != "POOL_NAME"} for p in _rows(pools)},
        "pool_grants": [(r["GRANTEE"], r["RESOURCE_POOL"]) for r in _rows(poolprivs)],
        "databases": {r["SCHEMA_NAME"] for r in _rows(dbs)},
        "my_grants": grants if isinstance(grants, list) else [],
        "my_roles": {r["ROLE"] for r in _rows(my_roles)},
        "cpu_mode": str(gv.get("cpu_mode") or "SOFT").upper(),
        "transfer_mode": gv.get("transfer_mode"),
    }
    for r in _rows(uprivs):
        p = str(r["PRIVILEGE_TYPE"]).upper()
        if p == "GRANT":
            ctx["grantable"][r["GRANTEE"]] = True
            continue
        ctx["global"].setdefault(r["GRANTEE"], set()).add(p)
        if str(r["IS_GRANTABLE"]).upper() == "YES":
            ctx["grantable"][r["GRANTEE"]] = True
    for r in _rows(sprivs):
        ctx["schema"].setdefault(r["GRANTEE"], {}).setdefault(r["TABLE_SCHEMA"], set()).add(str(r["PRIVILEGE_TYPE"]).upper())
    for r in _rows(tprivs):
        ctx["table"].setdefault(r["GRANTEE"], {}).setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), set()).add(
            str(r["PRIVILEGE_TYPE"]).upper())
    for r in _rows(ug):
        ctx["user_groups"].setdefault(r["USER"], set()).add(r["GROUP"])
    for r in _rows(gr):
        ctx["group_roles"].setdefault(r["GROUP"], set()).add(r["ROLE"])
    for r in _rows(rp):
        db_ = None if r["DATABASE"] == "*" else r["DATABASE"]
        tb = None if r["TABLE"] == "*" else r["TABLE"]
        ctx["role_privs"].setdefault(r["ROLE"], {}).setdefault((db_, tb), set()).update(sec.split_privs(r["PRIVILEGES"]))
    return ctx


# ----------------------------------------------------------------------------- what the connected user may do

def _parse_global_grants(lines: list[str]) -> tuple[set[str], bool]:
    """Privileges on *.* and the grant option, from SHOW GRANTS of the connected user."""
    privs: set[str] = set()
    grant_option = False
    for line in lines:
        m = re.match(r"^\s*GRANT\s+(.+?)\s+ON\s+\*\.\*\s+TO\s", line, re.I)
        if not m:
            continue
        for p in m.group(1).split(","):
            p = " ".join(p.upper().split())
            if p:
                privs.add(p)
        if re.search(r"\bWITH GRANT OPTION\b", line, re.I):
            grant_option = True
    return privs, grant_option


def capabilities(ctx: dict[str, Any]) -> dict[str, Any]:
    """Which kinds of change the connected user can make, and why not (from its own grants and roles)."""
    privs, grant_option = _parse_global_grants(ctx["my_grants"])
    me_g = sec.grantee(ctx["me"]["user"], ctx["me"]["host"])
    privs |= ctx["global"].get(me_g, set())
    grant_option = grant_option or ctx["grantable"].get(me_g, False)
    for role in ctx["my_roles"]:
        privs |= ctx["role_privs"].get(role, {}).get((None, None), set())
    allp = "ALL PRIVILEGES" in privs or "*" in privs
    has = (lambda p: allp or "SUPER" in privs or p in privs)
    go = grant_option or "SUPER" in privs

    def cap(ok: bool, why: str) -> dict[str, Any]:
        return {"ok": bool(ok), "reason": None if ok else why}

    return {
        "me": ctx["me"],
        "global_privileges": sorted(privs), "grant_option": go,
        "users": cap(has("CREATE USER"), "Creating and dropping users needs the CREATE USER privilege."),
        "grants": cap(go, "GRANT and REVOKE need the grant option (WITH GRANT OPTION) on a scope that covers the object."),
        "roles": cap(go, "Roles and groups need the GRANT privilege (the grant option on *.*)."),
        "alter_user": cap(go or has("ALTER USER"), "Setting a user's default pool needs ALTER USER or the grant option."),
        "create_pool": cap(has("CREATE POOL"), "Creating a resource pool needs the CREATE POOL privilege."),
        "alter_pool": cap(has("CREATE POOL") or has("DROP POOL"), "Changing a resource pool needs CREATE POOL or DROP POOL."),
        "drop_pool": cap(has("DROP POOL"), "Dropping a resource pool needs the DROP POOL privilege."),
        "pool_grants": cap(go, "Granting resource pool usage needs the grant option."),
        "cpu_mode": ctx["cpu_mode"], "transfer_mode": ctx["transfer_mode"],
        "privileges": PRIVILEGES, "global_only": sorted(GLOBAL_ONLY), "table_privileges": TABLE_PRIVILEGES,
        "pool_settings": {k: {"min": lo, "max": hi, "step": st} for k, (lo, hi, st) in POOL_SETTINGS.items()},
        "builtin_pools": sorted(BUILTIN_POOLS),
        "admins": sorted(admins(ctx)),
        "pick": {  # for the pickers
        "users": sorted(({"user": u["user"], "host": u["host"], "default_pool": u["default_pool"],
                          "admin": g in admins(ctx), "me": g == me_g} for g, u in ctx["users"].items()),
                        key=lambda u: (u["user"].lower(), u["host"])),
        "roles": sorted(ctx["roles"]), "groups": sorted(ctx["groups"]),
        "pools": [{"pool": name, "builtin": name in BUILTIN_POOLS, "config": cfg} for name, cfg in sorted(ctx["pools"].items())],
        "databases": sorted(d for d in ctx["databases"] if d.lower() not in ("information_schema", "cluster", "memsql")),
        },
    }


_ACTION_CAP = {
    "create_user": "users", "drop_user": "users", "grant": "grants", "revoke": "grants", "revoke_all": "grants",
    "create_role": "roles", "drop_role": "roles", "create_group": "roles", "drop_group": "roles", "grant_role": "roles",
    "revoke_role": "roles", "grant_group": "roles", "revoke_group": "roles", "create_pool": "create_pool",
    "alter_pool": "alter_pool", "drop_pool": "drop_pool", "set_default_pool": "alter_user", "grant_pool": "pool_grants",
    "revoke_pool": "pool_grants",
}


# ----------------------------------------------------------------------------- admins and lock-out

def admins(ctx: dict[str, Any]) -> set[str]:
    """Users (GRANTEE spelling) that can administer users and grants directly: SUPER or CREATE USER on *.* with
    the grant option. Admin rights that come only through roles aren't counted, so "last admin" errs on the
    safe side."""
    out = set()
    for g in ctx["users"]:
        privs = ctx["global"].get(g, set())
        if ctx["grantable"].get(g) and (privs & ADMIN_PRIVS or "ALL PRIVILEGES" in privs):
            out.add(g)
    return out


def role_admins(ctx: dict[str, Any]) -> set[str]:
    """Users that get SUPER or CREATE USER on *.* through a role."""
    out = set()
    for g in ctx["users"]:
        for grp in ctx["user_groups"].get(g, set()):
            for role in ctx["group_roles"].get(grp, set()):
                if ctx["role_privs"].get(role, {}).get((None, None), set()) & (ADMIN_PRIVS | {"*"}):
                    out.add(g)
    return out


def simulate(ctx: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """The context after the action, for the admin and lock-out checks (only what those checks read)."""
    after = copy.deepcopy({k: ctx[k] for k in ("users", "global", "grantable", "user_groups", "group_roles", "role_privs")})
    kind = action["action"]
    g = sec.grantee(action.get("user", ""), action.get("host") or "%") if action.get("user") else None
    if kind == "drop_user" and g:
        after["users"].pop(g, None)
        after["global"].pop(g, None)
        after["grantable"].pop(g, None)
        after["user_groups"].pop(g, None)
    elif kind == "revoke" and g and action.get("grantee_type", "user") == "user" and not action.get("database"):
        privs = set(action.get("privileges") or [])
        if "ALL PRIVILEGES" in privs:
            after["global"][g] = set()
        else:
            after["global"][g] = after["global"].get(g, set()) - privs
        if action.get("grant_option"):
            after["grantable"][g] = False
    elif kind == "revoke_all" and g and action.get("grantee_type", "user") == "user":
        after["global"][g] = set()
        after["grantable"][g] = False
    elif kind == "revoke_group" and g:
        after["user_groups"].get(g, set()).discard(action.get("group"))
    elif kind == "drop_group":
        for groups in after["user_groups"].values():
            groups.discard(action.get("group"))
        after["group_roles"].pop(action.get("group"), None)
    elif kind == "revoke_role":
        after["group_roles"].get(action.get("group"), set()).discard(action.get("role"))
    elif kind == "drop_role":
        for roles in after["group_roles"].values():
            roles.discard(action.get("role"))
        after["role_privs"].pop(action.get("role"), None)
    elif kind == "revoke" and action.get("grantee_type") == "role" and not action.get("database"):
        rp = after["role_privs"].get(action.get("role"), {})
        privs = set(action.get("privileges") or [])
        rp[(None, None)] = set() if "ALL PRIVILEGES" in privs else rp.get((None, None), set()) - privs
    elif kind == "revoke_all" and action.get("grantee_type") == "role":
        after["role_privs"].pop(action.get("role"), None)
    return {**ctx, **after}


def lockout_checks(ctx: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """Blockers (would lock the connected user out / remove the last admin) and warnings for one action."""
    after = simulate(ctx, action)
    me = sec.grantee(ctx["me"]["user"], ctx["me"]["host"])
    blockers: list[str] = []
    warnings: list[str] = []
    typed = False
    before_admins, after_admins = admins(ctx), admins(after)
    before_role, after_role = role_admins(ctx), role_admins(after)
    if me in ctx["users"] and me not in after["users"]:
        blockers.append(f"You are connected as {ctx['me']['user']}@{ctx['me']['host']}: dropping this user would lock you out.")
    if me in before_admins and me not in after_admins and me in after["users"]:
        if after_admins:
            warnings.append("This takes your own admin rights away (CREATE USER / SUPER with the grant option on *.*): "
                            "you won't be able to undo it from here.")
            typed = True
        else:
            blockers.append("This would remove your own admin rights and leave no admin user: refused.")
    if before_admins and not after_admins and not blockers:
        blockers.append("This would leave no user with CREATE USER / SUPER and the grant option on *.* (the last admin): refused.")
    lost = (before_admins - after_admins) - {me}
    if lost and after_admins:
        warnings.append("Removes admin rights from: " + ", ".join(sorted(lost)) + f". Admins left: {len(after_admins)}.")
        typed = True
    lost_role = (before_role - after_role)
    if lost_role:
        warnings.append("These users lose SUPER / CREATE USER that came through a role: " + ", ".join(sorted(lost_role)) + ".")
        typed = True
        if me in lost_role and me not in before_admins:
            warnings.append("That includes you: you may not be able to undo this from here.")
    return {"blockers": blockers, "warnings": warnings, "typed": typed}


# ----------------------------------------------------------------------------- plan

def _stmt(sql: str, password: str | None = None) -> dict[str, Any]:
    """One statement: what is shown (password masked) and what runs (password as a bound parameter)."""
    if password is None:
        return {"display": sql + ";", "sql": f"{MARKER} {sql}", "params": None}
    display = sql.replace(_PW, MASK)
    run = f"{MARKER} {sql}".replace("%", "%%").replace(_PW, "%s")
    return {"display": display + ";", "sql": run, "params": (password,)}


def _user_target(ctx: dict[str, Any], action: dict[str, Any], must_exist: bool = True) -> tuple[str, str, str]:
    user = _name(action.get("user"), "user", 320 if must_exist else 32)
    host = _name(action.get("host") if action.get("host") is not None else "%", "host", 255)
    g = sec.grantee(user, host)
    if must_exist and ctx["users_readable"] and g not in ctx["users"]:
        raise ChangeError(f"No user {user}@{host}")
    return user, host, g


def _is_me(ctx: dict[str, Any], g: str) -> bool:
    return g == sec.grantee(ctx["me"]["user"], ctx["me"]["host"])


def _grantee_sql(ctx: dict[str, Any], action: dict[str, Any]) -> tuple[str, str, str | None]:
    """(SQL target, label, GRANTEE key or None for a role)."""
    if action.get("grantee_type") == "role":
        role = _name(action.get("role"), "role")
        if ctx["roles"] and role not in ctx["roles"]:
            raise ChangeError(f"No role {role}")
        return f"ROLE {q(role)}", f"role {role}", None
    user, host, g = _user_target(ctx, action)
    return account(user, host), f"{user}@{host}", g


def _level(ctx: dict[str, Any], action: dict[str, Any]) -> tuple[str, str | None, str | None]:
    database = action.get("database") or None
    table = action.get("table") or None
    if table and not database:
        raise ChangeError("A table needs its database")
    if database:
        database = _name(database, "database")
        if database.startswith("#"):
            raise ChangeError("Wildcard database patterns (#...) aren't offered here")
        if ctx["databases"] and database not in ctx["databases"]:
            raise ChangeError(f"No database {database} (names are case-sensitive)")
    if table:
        table = _name(table, "table")
    return ("table" if table else "database" if database else "global"), database, table


def _held(ctx: dict[str, Any], action: dict[str, Any], g: str | None) -> list[tuple[str | None, str | None, set[str], bool]]:
    """Privileges the grantee holds directly, per object: (database, table, privileges, grant option)."""
    out: list[tuple[str | None, str | None, set[str], bool]] = []
    if g is None:
        for (d, t), privs in sorted(ctx["role_privs"].get(action.get("role"), {}).items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
            if privs:
                out.append((d, t, set(privs), False))
        return out
    gl = ctx["global"].get(g, set()) - {"USAGE"}
    if gl or ctx["grantable"].get(g):
        out.append((None, None, gl, bool(ctx["grantable"].get(g))))
    for d, privs in sorted(ctx["schema"].get(g, {}).items()):
        out.append((d, None, set(privs) - {"GRANT OPTION"}, "GRANT OPTION" in privs))
    for (d, t), privs in sorted(ctx["table"].get(g, {}).items()):
        out.append((d, t, set(privs) - {"GRANT OPTION"}, "GRANT OPTION" in privs))
    return out


def build(ctx: dict[str, Any], action: dict[str, Any], password: str | None = None) -> dict[str, Any]:
    """The plan for one action: statements (password masked in "display"), warnings, blockers and the confirmation
    it needs. Pure apart from ``ctx`` (from ``load_context``), so it can be tested offline."""
    kind = action.get("action")
    if kind not in ACTIONS:
        raise ChangeError(f"Unknown action {kind!r}")
    stmts: list[dict[str, Any]] = []
    warnings: list[str] = []
    blockers: list[str] = []
    typed = kind.startswith("drop_") or kind == "revoke_all"
    phrase: str | None = None
    refresh = ["users"]

    if kind == "create_user":
        user, host, g = _user_target(ctx, action, must_exist=False)
        if g in ctx["users"]:
            blockers.append(f"{user}@{host} already exists.")
        if not password:
            blockers.append("Enter a password for the new user.")
        elif len(password) < 8:
            warnings.append("The password is shorter than 8 characters.")
        sql = f"CREATE USER {account(user, host)} IDENTIFIED BY {_PW}"
        pool = action.get("default_pool")
        if pool:
            pool = _pool_name(pool)
            if ctx["pools"] and pool not in ctx["pools"]:
                raise ChangeError(f"No resource pool {pool}")
            sql += f" WITH DEFAULT RESOURCE POOL = {pool}"
        if host == "%":
            warnings.append("Host '%' lets the user connect from any host.")
        stmts.append(_stmt(sql, password or ""))
        phrase = user

    elif kind == "drop_user":
        user, host, g = _user_target(ctx, action)
        conns = ctx["users"].get(g, {}).get("connections", 0)
        if conns:
            warnings.append(f"{user}@{host} has {conns} open connection(s): DROP USER doesn't close them (docs); "
                            "they keep working until they disconnect or are killed.")
        groups = sorted(ctx["user_groups"].get(g, set()))
        if groups:
            warnings.append(f"Member of group(s) {', '.join(groups)}.")
        stmts.append(_stmt(f"DROP USER {account(user, host)}"))
        phrase = user

    elif kind in ("grant", "revoke"):
        level, database, table = _level(ctx, action)
        privs = _privs(action.get("privileges"), level)
        target, label, g = _grantee_sql(ctx, action)
        lvl = priv_level(database, table)
        if kind == "grant":
            wgo = bool(action.get("with_grant_option"))
            stmts.append(_stmt(f"GRANT {', '.join(privs)} ON {lvl} TO {target}" + (" WITH GRANT OPTION" if wgo else "")))
            if wgo:
                warnings.append(f"WITH GRANT OPTION lets {label} pass these privileges on to others.")
            if level == "global":
                warnings.append(f"On *.*: {label} gets these privileges in every database, including new ones.")
            if level == "global" and (set(privs) & ADMIN_PRIVS or "ALL PRIVILEGES" in privs):
                warnings.append(f"{', '.join(sorted(set(privs) & (ADMIN_PRIVS | {'ALL PRIVILEGES'})))} on *.* makes {label} an administrator.")
        else:
            go = bool(action.get("grant_option"))
            stmts.append(_stmt(f"REVOKE {', '.join(privs)} ON {lvl} FROM {target}"))
            if go:
                stmts.append(_stmt(f"REVOKE GRANT OPTION ON {lvl} FROM {target}"))
            held = {(d, t): p for d, t, p, _ in _held(ctx, action, g)}
            have = held.get((database, table), set())
            missing = [p for p in privs if p != "ALL PRIVILEGES" and p not in have]
            if missing and (g is None or ctx["users_readable"]):
                warnings.append(f"Not granted directly on {lvl} to {label}: {', '.join(missing)}"
                                " (it may come through a role or a wider scope; REVOKE then changes nothing or fails).")
        if g and (_is_me(ctx, g) or g in admins(ctx)):
            typed = True
            warnings.append(f"This changes {'your own' if _is_me(ctx, g) else 'an admin'} account ({label}).")
        phrase = (action.get("user") if g else action.get("role"))
        refresh = ["users", "roles"]

    elif kind == "revoke_all":
        target, label, g = _grantee_sql(ctx, action)
        held = _held(ctx, action, g)
        for d, t, privs, go in held:
            lvl = priv_level(d, t)
            p = sorted(privs - {"USAGE"})
            if "*" in p or "ALL PRIVILEGES" in p:
                p = ["ALL PRIVILEGES"]
            if p:
                stmts.append(_stmt(f"REVOKE {', '.join(p)} ON {lvl} FROM {target}"))
            if go:
                stmts.append(_stmt(f"REVOKE GRANT OPTION ON {lvl} FROM {target}"))
        if not stmts:
            blockers.append(f"{label} holds no privileges directly: nothing to revoke.")
        warnings.append("Revokes every privilege held directly, one statement per object (REVOKE ALL PRIVILEGES needs every "
                        "privilege the revoker holds, so it's not used). Privileges through roles and groups stay; USAGE stays.")
        if g:
            pools = [p for gg, p in ctx["pool_grants"] if gg == g]
            if pools:
                warnings.append(f"Resource pool usage grants stay: {', '.join(pools)}.")
        phrase = action.get("user") if g else action.get("role")
        refresh = ["users", "roles"]

    elif kind in ("create_role", "drop_role"):
        role = _name(action.get("role"), "role")
        if kind == "create_role":
            if role in ctx["roles"]:
                blockers.append(f"Role {role} already exists.")
            stmts.append(_stmt(f"CREATE ROLE {q(role)}"))
        else:
            if ctx["roles"] and role not in ctx["roles"]:
                raise ChangeError(f"No role {role}")
            groups = sorted(gr for gr, roles in ctx["group_roles"].items() if role in roles)
            if groups:
                warnings.append(f"Granted to group(s) {', '.join(groups)}: their members lose what the role gives.")
            stmts.append(_stmt(f"DROP ROLE {q(role)}"))
        phrase = role
        refresh = ["roles", "users"]

    elif kind in ("create_group", "drop_group"):
        group = _name(action.get("group"), "group")
        if kind == "create_group":
            if group in ctx["groups"]:
                blockers.append(f"Group {group} already exists.")
            stmts.append(_stmt(f"CREATE GROUP {q(group)}"))
        else:
            if ctx["groups"] and group not in ctx["groups"]:
                raise ChangeError(f"No group {group}")
            members = sorted(sec.parse_grantee(u)[0] for u, gs in ctx["user_groups"].items() if group in gs)
            if members:
                warnings.append(f"{len(members)} member(s) lose what the group's roles give: {', '.join(members[:20])}.")
            stmts.append(_stmt(f"DROP GROUP {q(group)}"))
        phrase = group
        refresh = ["roles", "users"]

    elif kind in ("grant_role", "revoke_role"):
        role = _name(action.get("role"), "role")
        group = _name(action.get("group"), "group")
        if ctx["roles"] and role not in ctx["roles"]:
            raise ChangeError(f"No role {role}")
        if ctx["groups"] and group not in ctx["groups"]:
            raise ChangeError(f"No group {group}")
        has = role in ctx["group_roles"].get(group, set())
        if kind == "grant_role":
            if has:
                blockers.append(f"Group {group} already has role {role}.")
            stmts.append(_stmt(f"GRANT ROLE {q(role)} TO {q(group)}"))
        else:
            if not has:
                blockers.append(f"Group {group} doesn't have role {role}.")
            stmts.append(_stmt(f"REVOKE ROLE {q(role)} FROM {q(group)}"))
        phrase = group
        refresh = ["roles", "users"]

    elif kind in ("grant_group", "revoke_group"):
        group = _name(action.get("group"), "group")
        user, host, g = _user_target(ctx, action)
        if ctx["groups"] and group not in ctx["groups"]:
            raise ChangeError(f"No group {group}")
        member = group in ctx["user_groups"].get(g, set())
        if kind == "grant_group":
            if member:
                blockers.append(f"{user}@{host} is already in group {group}.")
            stmts.append(_stmt(f"GRANT GROUP {q(group)} TO {account(user, host)}"))
        else:
            if not member:
                blockers.append(f"{user}@{host} isn't in group {group}.")
            stmts.append(_stmt(f"REVOKE GROUP {q(group)} FROM {account(user, host)}"))
        if _is_me(ctx, g) or g in admins(ctx):
            typed = True
            warnings.append(f"This changes {'your own' if _is_me(ctx, g) else 'an admin'} account.")
        phrase = user
        refresh = ["users", "roles"]

    elif kind in ("create_pool", "alter_pool", "drop_pool"):
        pool = _pool_name(action.get("pool"))
        refresh = ["pools", "users"]
        phrase = pool
        if kind != "create_pool" and pool in BUILTIN_POOLS:
            raise ChangeError(f"{pool} is a built-in pool: it can't be changed or dropped.")
        if kind == "drop_pool":
            if ctx["pools"] and pool not in ctx["pools"]:
                raise ChangeError(f"No resource pool {pool}")
            users = sorted(f"{u['user']}@{u['host']}" for u in ctx["users"].values() if u["default_pool"] == pool)
            if users:
                blockers.append(f"{pool} is the default pool of {', '.join(users[:20])}: set their default pool to another "
                                "pool first (SingleStore refuses to drop it).")
            warnings.append("Connections now using this pool can only run DDL until they switch pool (docs).")
            stmts.append(_stmt(f"DROP RESOURCE POOL {bt(pool)}"))
        else:
            settings = pool_settings(action.get("settings"), ctx["cpu_mode"])
            exists = pool in ctx["pools"]
            if kind == "create_pool":
                if exists:
                    blockers.append(f"Resource pool {pool} already exists.")
                user_pools = [p for p in ctx["pools"] if p not in BUILTIN_POOLS]
                if len(user_pools) >= MAX_USER_POOLS:
                    blockers.append(f"There are already {len(user_pools)} user-defined pools (the limit is {MAX_USER_POOLS}).")
                body = f"CREATE RESOURCE POOL {bt(pool)}" + (
                    " WITH " + ", ".join(f"{k} = {v}" for k, v in settings) if settings else "")
            else:
                if ctx["pools"] and not exists:
                    raise ChangeError(f"No resource pool {pool}")
                if not settings:
                    raise ChangeError("Change at least one setting")
                body = f"ALTER RESOURCE POOL {bt(pool)} SET " + ", ".join(f"{k} = {v}" for k, v in settings)
            for key in ("SOFT_CPU_LIMIT_PERCENTAGE", "HARD_CPU_LIMIT_PERCENTAGE"):
                new = dict(settings).get(key)
                if new is None:
                    continue
                others = sum(int(p.get(key.lower()) or 0) for name, p in ctx["pools"].items() if name != pool)
                if others + new > 100:
                    blockers.append(f"{key}: the pools would sum to {others + new} (at most 100 across all pools).")
            if "MAX_CONCURRENCY" in dict(settings) and dict(settings)["MAX_CONCURRENCY"] == 0:
                warnings.append("MAX_CONCURRENCY = 0 removes the concurrency limit.")
            warnings.append("Takes effect immediately; running queries above a new memory limit aren't killed (docs).")
            stmts.append(_stmt(body))

    elif kind == "set_default_pool":
        user, host, g = _user_target(ctx, action)
        pool = _pool_name(action.get("pool"))
        if ctx["pools"] and pool not in ctx["pools"]:
            raise ChangeError(f"No resource pool {pool}")
        if ctx["users"].get(g, {}).get("default_pool") == pool:
            blockers.append(f"{pool} is already the default pool of {user}@{host}.")
        stmts.append(_stmt(f"ALTER USER {account(user, host)} SET DEFAULT RESOURCE POOL = {pool}"))
        warnings.append("Applies to new connections; a default pool can't be cleared, only set to another pool (e.g. default_pool).")
        if _is_me(ctx, g):
            typed = True
            warnings.append("This changes your own account.")
        phrase = user
        refresh = ["users", "pools"]

    elif kind in ("grant_pool", "revoke_pool"):
        pool = _pool_name(action.get("pool"))
        if ctx["pools"] and pool not in ctx["pools"]:
            raise ChangeError(f"No resource pool {pool}")
        target, label, g = _grantee_sql(ctx, action)
        if kind == "grant_pool":
            stmts.append(_stmt(f"GRANT USAGE ON RESOURCE POOL {bt(pool)} TO {target}"))
            if pool in BUILTIN_POOLS:
                warnings.append(f"{pool} is built in: every user can already use it.")
        else:
            stmts.append(_stmt(f"REVOKE USAGE ON RESOURCE POOL {bt(pool)} FROM {target}"))
        phrase = pool
        refresh = ["pools", "users"]

    lk = lockout_checks(ctx, {**action, "privileges": [" ".join(str(p).upper().split()) for p in action.get("privileges") or []]})
    blockers += lk["blockers"]
    warnings += lk["warnings"]
    typed = typed or lk["typed"]
    reasons = []
    if kind.startswith("drop_"):
        reasons.append("it drops an object")
    if kind == "revoke_all":
        reasons.append("it revokes all privileges")
    if typed and not reasons:
        reasons.append("it changes your own account, an admin account or admin rights")
    return {
        "action": kind, "title": ACTIONS[kind],
        "statements": stmts,
        "warnings": warnings, "blockers": blockers,
        "confirm": {"typed": typed, "phrase": phrase if typed else None, "reasons": reasons},
        "refresh": refresh,
    }


def public(plan_: dict[str, Any]) -> dict[str, Any]:
    """The plan as the page and the model see it: statements only in their masked display form."""
    return {**plan_, "statements": [s["display"] for s in plan_["statements"]]}


def _check_allowed(ctx: dict[str, Any], kind: str) -> None:
    cap = capabilities(ctx).get(_ACTION_CAP.get(kind, ""), {"ok": True})
    if not cap["ok"]:
        raise ChangeError(f"Not allowed for {ctx['me']['user']}@{ctx['me']['host']}: {cap['reason']}")


def plan(action: dict[str, Any]) -> dict[str, Any]:
    """Read-only: the statements and checks for one change. No password is taken (it shows masked)."""
    ctx = load_context()
    _check_allowed(ctx, action.get("action", ""))
    p = build(ctx, action, password="********" if action.get("action") == "create_user" else None)
    p["blockers"] = [b for b in p["blockers"] if not b.startswith("Enter a password")]
    p["warnings"] = [w for w in p["warnings"] if not w.startswith("The password is shorter")]
    return public(p)


def apply(action: dict[str, Any], confirm: bool = False, typed: str | None = None, password: str | None = None) -> dict[str, Any]:
    """Run one change after the user confirmed its plan. Builds the plan again from fresh cluster state."""
    kind = action.get("action", "")
    if not confirm:
        raise ChangeError("Refused: this changes users, privileges or resource pools on the shared cluster. Show the user "
                          "the plan (security_change_plan) and call again with confirm=True only after they confirmed.")
    if kind != "create_user":
        password = None
    ctx = load_context()
    _check_allowed(ctx, kind)
    p = build(ctx, action, password=password)
    if p["blockers"]:
        raise ChangeError("Refused: " + " ".join(p["blockers"]))
    if p["confirm"]["typed"] and (typed or "").strip() != (p["confirm"]["phrase"] or ""):
        raise ChangeError(f"Refused: type {p['confirm']['phrase']!r} to confirm ({', '.join(p['confirm']['reasons'])}).")
    results = []
    failed = False
    for s in p["statements"]:
        if failed:
            results.append({"statement": s["display"], "status": "skipped"})
            continue
        try:
            db.execute(s["sql"], s["params"], fetch=False)
            results.append({"statement": s["display"], "status": "ok"})
        except s2.Error as exc:
            failed = True
            results.append({"statement": s["display"], "status": "error", "error": mask_sql(str(exc), password)[:500]})
    return {
        "action": kind, "title": p["title"], "results": results,
        "ok": not failed, "ran": sum(r["status"] == "ok" for r in results), "refresh": p["refresh"],
        "warnings": p["warnings"],
    }


def summary(d: dict[str, Any]) -> str:
    if "results" in d:
        lines = [f"{d['title']}: {'done' if d['ok'] else 'failed'} ({d['ran']} of {len(d['results'])} statement(s) ran)."]
        lines += [f"- {r['status']}: {r['statement']}" + (f" -> {r['error']}" if r.get("error") else "") for r in d["results"]]
        return "\n".join(lines)
    lines = [f"Plan for {d['title']} (nothing ran):"] + [f"  {s}" for s in d["statements"]]
    lines += [f"Blocked: {b}" for b in d["blockers"]] + [f"Warning: {w}" for w in d["warnings"]]
    if d["confirm"]["typed"]:
        lines.append(f"Needs the name typed to confirm: {d['confirm']['phrase']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- audit log (read-only status)

AUDIT_DOCS = "https://docs.singlestore.com/db/v9.0/security/audit-logging/configuring-audit-logging/"


def audit_status() -> dict[str, Any]:
    """The audit log settings (read-only) and how to turn audit logging on. The log itself is files on each node's
    disk (auditlogsdir), not a table: SingleStore has no SQL view of it."""
    rows, nodes = db.parallel(
        _soft(lambda: _q("SELECT LOWER(VARIABLE_NAME) AS name, VARIABLE_VALUE AS value FROM information_schema.GLOBAL_VARIABLES"
                         " WHERE LOWER(VARIABLE_NAME) LIKE 'audit%'")),
        _soft(lambda: _q(f"SELECT ID, IP_ADDR, PORT, TYPE FROM {IS}.MV_NODES ORDER BY ID")),
    )
    variables = {r["name"]: r["value"] for r in _rows(rows)}
    level = str(variables.get("auditlog_level") or "").upper()
    node_list = [{"id": n["ID"], "host": n["IP_ADDR"], "port": n["PORT"], "type": n["TYPE"]} for n in _rows(nodes)]
    operator = any(".svc-" in str(n["host"]) or str(n["host"]).startswith("node-") for n in node_list)
    return {
        "variables": variables, "level": level or None, "enabled": bool(level) and level != "OFF",
        "readable_via_sql": False, "nodes": node_list, "kubernetes_operator": operator,
        "error": None if isinstance(rows, list) else rows.get("error"),
        "docs": {
            "configure": AUDIT_DOCS,
            "levels": "https://docs.singlestore.com/db/v9.0/security/audit-logging/audit-logging-levels/",
            "format": "https://docs.singlestore.com/db/v9.0/security/audit-logging/audit-log-file-format/",
            "operator": "https://docs.singlestore.com/db/v9.1/reference/singlestore-operator-reference/configure-audit-logging/",
        },
        "steps": {
            "toolbox": [
                'sdb-admin update-config --all --key "auditlog_level" --value "ADMIN-ONLY"',
                'sdb-admin update-config --all --key "auditlog_retention_period" --value "30"',
                "sdb-admin restart-node --all",
                "SHOW GLOBAL VARIABLES LIKE 'audit%';",
            ],
            "operator": [
                "# In the MemsqlCluster custom resource (spec.globalVariables), then kubectl apply -f <file>.yaml:",
                "globalVariables:",
                "  auditlog_level: \"ADMIN-ONLY\"",
                "  auditlog_retention_period: \"30\"",
            ],
        },
        "notes": [
            "Audit logging is an Enterprise edition feature, off by default (auditlog_level = OFF).",
            "The audit variables only take effect when a node starts: changing them means a rolling restart.",
            "Each node writes its own CSV files to auditlogsdir (auditlog_<host>-<port>_<date>_<time>.log), rotated by size "
            "(auditlog_rotation_size, 128 MB) and time (auditlog_rotation_time, 1 h); auditlog_retention_period (days) or "
            "auditlog_retention_size (MB) removes old files (0 = keep forever).",
            "LOGINS-ONLY logs logins; ADMIN-ONLY adds DDL and GRANT/REVOKE/CREATE USER; WRITES-ONLY adds DML; ALL-QUERIES adds "
            "SELECTs. Queries are logged as their parameterized form unless a *-PLAINTEXT level is set; passwords are left out.",
            "There's no information_schema view of the audit log. Reading it through SQL needs a pipeline or LOAD DATA from the "
            "files, which needs FILE READ and file-system access on the nodes; FS pipelines aren't supported with the Kubernetes "
            "Operator. Ship the files with a log collector (e.g. Filebeat/Fluent Bit from each pod's auditlogsdir) into a table "
            "or a SIEM instead.",
        ],
    }
