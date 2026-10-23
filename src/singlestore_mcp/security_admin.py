"""Security view v2: changes to users, roles, groups, grants and resource pools, with preview and confirmation.

Every change goes through two steps:

1. ``plan(action)`` (read-only) builds the exact statements, masks any password, and checks the change against
   the cluster as it is now: does the object exist, would it lock the connected user out, would it remove the
   last admin, does it touch the connected user or an admin user (then the user must type the object's name).
2. ``apply(action, confirm=True, typed=...)`` builds the plan again from fresh cluster state, refuses on any
   blocker, a missing confirmation or a wrong typed name, and runs the statements one by one (stopping at the
   first error).

``script(actions)`` (read-only) writes one or more planned changes out as a reviewable .sql script for a DBA
instead: same statements, safe order, warnings as comments, no password (a ``'<set password>'`` placeholder), and it
needs none of the change privileges.

Passwords: only ``create_user`` and ``change_password`` take one. It is sent to SingleStore as a bound parameter,
shown as ``'********'`` in every statement, result and error text, and never logged or stored (a script gets the
``'<set password>'`` placeholder). Every statement starts with ``/* s2-security-change */``; the local Query History
store leaves "/* s2-" statements out.

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

Security v2 extensions (ALTER USER / SET PASSWORD / CREATE USER / GRANT / CREATE VIEW in the 9.0 docs, tried on 9.0.40
with zz_test_ users and views):
- Password: ``ALTER USER u IDENTIFIED BY 'pw'`` (needs ALTER USER or GRANT) and ``SET PASSWORD FOR u = PASSWORD('pw')``
  both work; a user without ALTER USER can change only its own password, and only with SET PASSWORD (ALTER USER on
  itself is "Access denied"). PASSWORD_UPDATED changes; a lock stays; open connections keep working.
  ``IDENTIFIED BY`` together with ``ACCOUNT LOCK/UNLOCK`` is a syntax error (documented).
- ``ALTER USER u ACCOUNT LOCK / UNLOCK``: USERS.ACCOUNT_STATUS = LOCKED / OPEN; a locked login gets error 2449; open
  connections keep working; UNLOCK also clears a failed-login lockout. A cluster restart clears every lock (docs).
- ``ALTER USER u SET FAILED_LOGIN_ATTEMPTS = n PASSWORD_LOCK_TIME = s`` (seconds): both > 0 turns the lockout on,
  both 0 turns it off; anything else is error 2451. Shown in USERS.EFFECTIVE_FAILED_LOGIN_ATTEMPTS /
  EFFECTIVE_PASSWORD_LOCK_TIME. CREATE USER takes them after ``WITH`` (with DEFAULT RESOURCE POOL), then ``REQUIRE``.
- ``ALTER USER u REQUIRE SSL / NONE``: shows as PRIVILEGE_TYPE 'SSL' in USER_PRIVILEGES and "REQUIRE SSL" in SHOW
  GRANTS (USERS has no column for it); a non-TLS login gets error 2455; open connections keep working; other ALTER
  USER clauses keep it. X509 / SUBJECT aren't offered here.
- No column-level privileges: ``GRANT SELECT (c1, c2) ON db.t`` is accepted but grants SELECT on the WHOLE table
  (TABLE_PRIVILEGES gets the row, COLUMN_PRIVILEGES stays empty, every column is readable), and ``REVOKE SELECT (c)``
  revokes the whole table privilege. So the column picker creates a view with the chosen columns and grants SELECT
  on the view: views run as their definer (SECURITY_TYPE DEFINER), so the grantee needs nothing on the base table.
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
    "change_password": "Change password", "lock_user": "Lock account", "unlock_user": "Unlock account",
    "set_login_policy": "Failed-login lockout", "set_require": "Require TLS (SSL)",
    "grant_view": "Grant columns (through a view)",
}
# Actions that take a password (sent as a bound parameter; a script gets SCRIPT_PASSWORD instead).
PASSWORD_ACTIONS = {"create_user", "change_password"}
REQUIRE = ("SSL", "NONE")
INT_MAX = 2147483647  # USERS.EFFECTIVE_FAILED_LOGIN_ATTEMPTS / EFFECTIVE_PASSWORD_LOCK_TIME are INT

_D = "https://docs.singlestore.com/db/v9.0/reference/sql-reference"
DOCS = {
    "alter_user": f"{_D}/security-management-commands/alter-user/",
    "create_user": f"{_D}/security-management-commands/create-user/",
    "set_password": f"{_D}/security-management-commands/set-password/",
    "grant": f"{_D}/security-management-commands/grant/",
    "create_view": f"{_D}/data-definition-language-ddl/create-view/",
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
    (users, uprivs, sprivs, tprivs, ug, gr, roles, groups, rp, pools, poolprivs, dbs, me, grants, my_roles, gvars,
     tls) = db.parallel(
        _soft(lambda: _q(f"SELECT USER, HOST, TYPE, CONNECTIONS, DEFAULT_RESOURCE_POOL, ACCOUNT_STATUS,"
                         f" EFFECTIVE_FAILED_LOGIN_ATTEMPTS, EFFECTIVE_PASSWORD_LOCK_TIME FROM {IS}.USERS WHERE IS_DELETED = 0")),
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
        _soft(lambda: _q("SELECT @@resource_governor_cpu_limit_mode AS cpu_mode, @@privilege_transfer_mode AS transfer_mode,"
                         " @@sync_permissions AS sync_permissions")),
        _soft(lambda: _q("SHOW SESSION STATUS LIKE 'Ssl_cipher'")),
    )
    gv = (_rows(gvars) or [{}])[0]
    cipher = next((r.get("Value") for r in _rows(tls)), None)
    ctx: dict[str, Any] = {
        "me": {"user": me["user"], "host": me["host"]},
        "users": {sec.grantee(u["USER"], u["HOST"]): {"user": u["USER"], "host": u["HOST"],
                                                       "connections": int(u["CONNECTIONS"] or 0),
                                                       "default_pool": u["DEFAULT_RESOURCE_POOL"] or None,
                                                       "type": u.get("TYPE"), "status": u.get("ACCOUNT_STATUS"),
                                                       "failed_login_attempts": u.get("EFFECTIVE_FAILED_LOGIN_ATTEMPTS"),
                                                       "password_lock_time": u.get("EFFECTIVE_PASSWORD_LOCK_TIME")}
                  for u in _rows(users)},
        "users_readable": isinstance(users, list),
        "ssl": set(),  # GRANTEEs with REQUIRE SSL (PRIVILEGE_TYPE 'SSL' in USER_PRIVILEGES)
        # Does this app's connection use TLS? (Ssl_cipher of the session; None = couldn't tell)
        "my_tls": None if cipher is None else bool(cipher),
        "sync_permissions": str(gv.get("sync_permissions") or "").upper() or None,
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
        if p == "SSL":  # REQUIRE SSL, not a privilege (REVOKE SSL would be a syntax error)
            ctx["ssl"].add(r["GRANTEE"])
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
        "alter_user": cap(go or has("ALTER USER"), "Changing a user (default pool, password, lock, failed-login lockout, "
                          "REQUIRE SSL) needs ALTER USER or the grant option; your own password you can always change."),
        "create_pool": cap(has("CREATE POOL"), "Creating a resource pool needs the CREATE POOL privilege."),
        "alter_pool": cap(has("CREATE POOL") or has("DROP POOL"), "Changing a resource pool needs CREATE POOL or DROP POOL."),
        "drop_pool": cap(has("DROP POOL"), "Dropping a resource pool needs the DROP POOL privilege."),
        "pool_grants": cap(go, "Granting resource pool usage needs the grant option."),
        "cpu_mode": ctx["cpu_mode"], "transfer_mode": ctx["transfer_mode"],
        "privileges": PRIVILEGES, "global_only": sorted(GLOBAL_ONLY), "table_privileges": TABLE_PRIVILEGES,
        "pool_settings": {k: {"min": lo, "max": hi, "step": st} for k, (lo, hi, st) in POOL_SETTINGS.items()},
        "builtin_pools": sorted(BUILTIN_POOLS),
        "admins": sorted(admins(ctx)),
        "tls": ctx.get("my_tls"), "docs": DOCS,
        "pick": {  # for the pickers
        "users": sorted(({"user": u["user"], "host": u["host"], "default_pool": u["default_pool"],
                          "admin": g in admins(ctx), "me": g == me_g, "type": u.get("type"), "status": u.get("status"),
                          "require_ssl": g in ctx.get("ssl", set()),
                          "failed_login_attempts": u.get("failed_login_attempts"),
                          "password_lock_time": u.get("password_lock_time")} for g, u in ctx["users"].items()),
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
    "revoke_pool": "pool_grants", "change_password": "alter_user", "lock_user": "alter_user", "unlock_user": "alter_user",
    "set_login_policy": "alter_user", "set_require": "alter_user", "grant_view": "grants",
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


def _whole(value: Any, what: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ChangeError(f"{what} must be a whole number") from None
    if isinstance(value, float) and value != n:
        raise ChangeError(f"{what} must be a whole number")
    if n < 0 or n > INT_MAX:
        raise ChangeError(f"{what} must be 0–{INT_MAX}")
    return n


def login_policy(action: dict[str, Any], required: bool = True) -> tuple[int, int] | None:
    """(FAILED_LOGIN_ATTEMPTS, PASSWORD_LOCK_TIME): both > 0 (lockout on) or both 0 (off); 9.0.40 refuses anything
    else (error 2451). None when neither is given and ``required`` is False."""
    a, t = action.get("failed_login_attempts"), action.get("password_lock_time")
    if (a is None or a == "") and (t is None or t == ""):
        if required:
            raise ChangeError("Give FAILED_LOGIN_ATTEMPTS and PASSWORD_LOCK_TIME (both 0 turns the lockout off)")
        return None
    a = _whole(a if a not in (None, "") else 0, "FAILED_LOGIN_ATTEMPTS")
    t = _whole(t if t not in (None, "") else 0, "PASSWORD_LOCK_TIME")
    if (a == 0) != (t == 0):
        raise ChangeError("FAILED_LOGIN_ATTEMPTS and PASSWORD_LOCK_TIME go together: both above 0 turns the lockout on, "
                          "both 0 turns it off (SingleStore refuses anything else, error 2451)")
    return a, t


def _require(value: Any, optional: bool = False) -> str | None:
    r = str(value or "").strip().upper()
    if not r and optional:
        return None
    if r not in REQUIRE:
        raise ChangeError("REQUIRE must be SSL or NONE (X509 / SUBJECT aren't offered here)")
    return r


def _status(ctx: dict[str, Any], g: str) -> str:
    return str(ctx["users"].get(g, {}).get("status") or "").upper()


def locked(ctx: dict[str, Any]) -> set[str]:
    return {g for g in ctx["users"] if _status(ctx, g) == "LOCKED"}


def _columns(ctx: dict[str, Any], database: str, table: str, wanted: Any) -> list[str]:
    """The picked columns, checked against the table (names as the table spells them, in the order picked)."""
    if isinstance(wanted, str):
        wanted = [wanted]
    picked = [_name(c, "column") for c in (wanted or [])]
    if not picked:
        raise ChangeError("Pick at least one column")
    have = ctx.get("columns", {}).get((database, table))
    if have is None:
        return list(dict.fromkeys(picked))
    if not have:
        raise ChangeError(f"No table {database}.{table} (names are case-sensitive)")
    by_lower = {c.lower(): c for c in have}
    out = []
    for c in picked:
        real = by_lower.get(c.lower())
        if real is None:
            raise ChangeError(f"No column {c} in {database}.{table}")
        if real not in out:
            out.append(real)
    return out


def _has_select(ctx: dict[str, Any], action: dict[str, Any], g: str | None, database: str, table: str) -> str | None:
    """Where the grantee already has SELECT directly (on *.*, the database or the table), or None."""
    def hit(privs: Any) -> bool:
        return bool(privs) and bool({"SELECT", "ALL PRIVILEGES", "*"} & set(privs))

    if g is None:
        rp = ctx["role_privs"].get(action.get("role"), {})
        for key, where in (((None, None), "*.*"), ((database, None), f"{database}.*"), ((database, table), f"{database}.{table}")):
            if hit(rp.get(key)):
                return where
        return None
    if hit(ctx["global"].get(g)):
        return "*.*"
    if hit(ctx["schema"].get(g, {}).get(database)):
        return f"{database}.*"
    if hit(ctx["table"].get(g, {}).get((database, table))):
        return f"{database}.{table}"
    return None


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
        withs = []
        pool = action.get("default_pool")
        if pool:
            pool = _pool_name(pool)
            if ctx["pools"] and pool not in ctx["pools"]:
                raise ChangeError(f"No resource pool {pool}")
            withs.append(f"DEFAULT RESOURCE POOL = {pool}")
        policy = login_policy(action, required=False)
        if policy and policy[0]:
            withs.append(f"FAILED_LOGIN_ATTEMPTS = {policy[0]} PASSWORD_LOCK_TIME = {policy[1]}")
            warnings.append(f"After {policy[0]} failed logins the account is locked for {policy[1]} s (or until ACCOUNT UNLOCK).")
        if withs:
            sql += " WITH " + " ".join(withs)
        req = _require(action.get("require"), optional=True)
        if req:
            sql += f" REQUIRE {req}"
            if req == "SSL":
                warnings.append("REQUIRE SSL: the user can only log in over TLS (a non-TLS login gets error 2455).")
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

    elif kind == "change_password":
        user, host, g = _user_target(ctx, action)
        u = ctx["users"].get(g, {})
        me = _is_me(ctx, g)
        if str(u.get("type") or "").upper() == "JWT":
            blockers.append(f"{user}@{host} signs in with a JWT token: there is no password to change.")
        if not password:
            blockers.append("Enter the new password.")
        elif len(password) < 8:
            warnings.append("The password is shorter than 8 characters.")
        if me and not capabilities(ctx)["alter_user"]["ok"]:
            # Without ALTER USER, a user can change only its own password, and only with SET PASSWORD (9.0.40).
            sql = f"SET PASSWORD FOR {account(user, host)} = PASSWORD({_PW})"
            if ctx.get("sync_permissions") == "OFF":
                warnings.append("SET PASSWORD changes the password on this node only while sync_permissions is OFF (docs).")
        else:
            sql = f"ALTER USER {account(user, host)} IDENTIFIED BY {_PW}"
        stmts.append(_stmt(sql, password or ""))
        if me:
            typed = True
            warnings.append("This changes your own password: open connections keep working, but every new connection "
                            "(this app's pool, notebooks, other tools) needs the new password.")
        elif g in admins(ctx):
            typed = True
            warnings.append(f"This changes an admin account ({user}@{host}).")
        if _status(ctx, g) == "LOCKED":
            warnings.append("The account is locked: a new password doesn't unlock it (use Unlock).")
        if u.get("connections"):
            warnings.append(f"{user}@{host} has {u['connections']} open connection(s): they keep working with the old login.")
        phrase = user

    elif kind in ("lock_user", "unlock_user"):
        user, host, g = _user_target(ctx, action)
        status = _status(ctx, g)
        if kind == "lock_user":
            if _is_me(ctx, g):
                blockers.append(f"You are connected as {user}@{host}: locking your own account would lock you out "
                                "(every new connection, including this app's, would be refused).")
            others = admins(ctx) - locked(ctx) - {g}
            if g in admins(ctx) and not others:
                blockers.append(f"{user}@{host} is the last admin that isn't locked: refused.")
            if status == "LOCKED":
                blockers.append(f"{user}@{host} is already locked.")
            stmts.append(_stmt(f"ALTER USER {account(user, host)} ACCOUNT LOCK"))
            conns = ctx["users"].get(g, {}).get("connections", 0)
            warnings.append("New logins are refused (error 2449)" + (
                f"; its {conns} open connection(s) keep working until they disconnect or are killed." if conns else
                "; connections that are already open keep working."))
            warnings.append("A cluster restart clears every lock (docs): lock it again after a restart.")
            if g in admins(ctx):
                typed = True
                warnings.append(f"{user}@{host} is an admin. Admins left unlocked: {len(others)}.")
        else:
            if status and status != "LOCKED":
                blockers.append(f"{user}@{host} isn't locked (ACCOUNT_STATUS = {status}).")
            stmts.append(_stmt(f"ALTER USER {account(user, host)} ACCOUNT UNLOCK"))
            warnings.append("Clears a lock set with ACCOUNT LOCK and a lockout after too many failed logins (docs).")
        phrase = user

    elif kind == "set_login_policy":
        user, host, g = _user_target(ctx, action)
        att, lt = login_policy(action)
        u = ctx["users"].get(g, {})
        if (u.get("failed_login_attempts"), u.get("password_lock_time")) == (att, lt):
            blockers.append(f"{user}@{host} already has FAILED_LOGIN_ATTEMPTS = {att}, PASSWORD_LOCK_TIME = {lt}.")
        stmts.append(_stmt(f"ALTER USER {account(user, host)} SET FAILED_LOGIN_ATTEMPTS = {att} PASSWORD_LOCK_TIME = {lt}"))
        if att:
            warnings.append(f"After {att} failed logins the account is locked for {lt} s (or until ACCOUNT UNLOCK); a cluster "
                            "restart clears lockouts (docs).")
            if _is_me(ctx, g):
                typed = True
                warnings.append("This is your own account: mistyped passwords from any client (scripts, other tools) can "
                                "lock you out for that time.")
        else:
            warnings.append("Turns the failed-login lockout off for this user (both 0).")
        phrase = user

    elif kind == "set_require":
        user, host, g = _user_target(ctx, action)
        req = _require(action.get("require"))
        has = g in ctx.get("ssl", set())
        if ctx["users_readable"] and g in ctx["users"] and has == (req == "SSL"):
            blockers.append(f"{user}@{host} already has REQUIRE {req}.")
        stmts.append(_stmt(f"ALTER USER {account(user, host)} REQUIRE {req}"))
        if req == "SSL":
            warnings.append("New logins without TLS are refused (error 2455); connections that are already open keep working.")
            if _is_me(ctx, g):
                typed = True
                tls = ctx.get("my_tls")
                if tls is False:
                    warnings.insert(0, "LOCK-OUT RISK: this app's connection doesn't use TLS, so after this every new "
                                       "connection it opens as you would be refused. Turn TLS on in the connection first.")
                elif tls is None:
                    warnings.insert(0, "Couldn't tell whether this app's connection uses TLS: check it before requiring "
                                       "SSL for your own account.")
                else:
                    warnings.append("This app's connection uses TLS, so it keeps working.")
        else:
            warnings.append("TLS becomes optional for this user: clients that don't use TLS send the login unencrypted.")
            if _is_me(ctx, g):
                typed = True
                warnings.append("This changes your own account.")
        phrase = user

    elif kind == "grant_view":
        database = _name(action.get("database"), "database")
        table = _name(action.get("table"), "table")
        if ctx["databases"] and database not in ctx["databases"]:
            raise ChangeError(f"No database {database} (names are case-sensitive)")
        objects = ctx.get("objects", {}).get(database)
        if objects is not None and table not in objects:
            raise ChangeError(f"No table {database}.{table} (names are case-sensitive)")
        cols = _columns(ctx, database, table, action.get("columns"))
        view = _name(action.get("view"), "view name")
        if view == table:
            raise ChangeError("The view needs a name of its own")
        if objects is not None and view in objects:
            blockers.append(f"{database}.{view} already exists: pick another view name.")
        target, label, g = _grantee_sql(ctx, action)
        stmts.append(_stmt(f"CREATE VIEW {bt(database)}.{bt(view)} AS SELECT {', '.join(bt(c) for c in cols)} "
                           f"FROM {bt(database)}.{bt(table)}"))
        stmts.append(_stmt(f"GRANT SELECT ON {bt(database)}.{bt(view)} TO {target}"))
        warnings.append("SingleStore 9.0 has no column-level privileges: GRANT SELECT (col, …) ON a table is accepted but "
                        "grants the whole table (checked on 9.0.40). So a view with the chosen columns is created and "
                        "SELECT is granted on the view.")
        me = f"{ctx['me']['user']}@{ctx['me']['host']}"
        warnings.append(f"The view runs with its creator's rights (DEFINER = {me}): {label} needs nothing on "
                        f"{database}.{table}. If {me} loses SELECT on it or is dropped, the view stops working. Creating "
                        f"it needs CREATE VIEW on {database}.")
        where = _has_select(ctx, action, g, database, table)
        if where:
            warnings.insert(0, f"{label} already has SELECT on {where} directly, so it can still read every column of "
                               f"{database}.{table}: revoke that for the column restriction to hold.")
        warnings.append("Runs as two statements: if the GRANT fails, the view stays (drop it with DROP VIEW).")
        if g and (_is_me(ctx, g) or g in admins(ctx)):
            warnings.append(f"{label} is {'you' if _is_me(ctx, g) else 'an admin'}: admins can usually read the table anyway.")
        phrase = view
        refresh = ["users", "roles"]

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


def _own_password(ctx: dict[str, Any], action: dict[str, Any]) -> bool:
    if action.get("action") != "change_password" or not action.get("user"):
        return False
    host = action.get("host") if action.get("host") is not None else "%"
    return _is_me(ctx, sec.grantee(str(action["user"]).strip(), str(host)))


def _allowed(ctx: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    if _own_password(ctx, action):  # SET PASSWORD for yourself needs no privilege
        return {"ok": True, "reason": None}
    cap = capabilities(ctx).get(_ACTION_CAP.get(action.get("action", ""), ""), {"ok": True})
    return {"ok": bool(cap["ok"]), "reason": None if cap["ok"] else cap["reason"]}


def _check_allowed(ctx: dict[str, Any], action: dict[str, Any]) -> None:
    a = _allowed(ctx, action)
    if not a["ok"]:
        raise ChangeError(f"Not allowed for {ctx['me']['user']}@{ctx['me']['host']}: {a['reason']}")


def _enrich(ctx: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """Reads only a grant_view needs: the table's columns and the database's table/view names (once per context)."""
    if action.get("action") != "grant_view":
        return ctx
    d, t = action.get("database"), action.get("table")
    if not d or not isinstance(d, str):
        return ctx
    objects = ctx.setdefault("objects", {})
    columns = ctx.setdefault("columns", {})
    need_t = isinstance(t, str) and t and (d, t) not in columns
    if d in objects and not need_t:
        return ctx
    names, cols = db.parallel(
        _soft(lambda: _q(f"SELECT TABLE_NAME FROM {IS}.TABLES WHERE TABLE_SCHEMA = %s", (d,))),
        _soft(lambda: _q(f"SELECT COLUMN_NAME FROM {IS}.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
                         " ORDER BY ORDINAL_POSITION", (d, t)) if need_t else []),
    )
    if isinstance(names, list):
        objects[d] = {r["TABLE_NAME"] for r in names}
    if need_t and isinstance(cols, list):
        columns[(d, t)] = [r["COLUMN_NAME"] for r in cols]
    return ctx


def _active_profile() -> Any:
    try:
        from . import connections

        return connections.active()
    except Exception:  # noqa: BLE001 - informational
        return None


def connection_info(ctx: dict[str, Any]) -> dict[str, Any]:
    """For changing your own password: is the active saved connection this account, and can its stored password be
    updated after the change (only a saved password connection, not the SINGLESTORE_* one, a URL or a token login)?"""
    p = _active_profile()
    if p is None:
        return {"name": None, "uses_account": False, "can_update": False, "reason": "No saved connection is active."}
    info = {"name": p.name, "uses_account": p.user == ctx["me"]["user"], "can_update": False, "reason": None}
    if not info["uses_account"]:
        info["reason"] = f"The active connection {p.name} signs in as {p.user}, not as this account."
    elif p.builtin:
        info["reason"] = f"{p.name} comes from the SINGLESTORE_* settings: update SINGLESTORE_PASSWORD there yourself."
    elif p.url:
        info["reason"] = f"{p.name} uses a connection URL that holds the password: edit it in Connections."
    elif p.auth != "password":
        info["reason"] = f"{p.name} signs in with a token ({p.auth}), not a password."
    else:
        info["can_update"] = True
    return info


def _plan_on(ctx: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """The plan without a password: a password shows masked; the "enter a password" checks are left out."""
    _enrich(ctx, action)
    p = build(ctx, action, password="********" if action.get("action") in PASSWORD_ACTIONS else None)
    p["blockers"] = [b for b in p["blockers"] if not b.startswith(("Enter a password", "Enter the new password"))]
    p["warnings"] = [w for w in p["warnings"] if not w.startswith("The password is shorter")]
    return p


def plan(action: dict[str, Any], pending: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Read-only: the statements and checks for one change. No password is taken (it shows masked).

    Works without the change privileges too (for a script a DBA runs): ``allowed`` says whether the connected user
    may run it. ``pending`` (the script basket) is used only when the change needs objects the basket creates; the
    plan then has ``needs_script`` (it can go into the script, but can't run now)."""
    action = _no_password(action)
    ctx = load_context()
    try:
        p = _plan_on(ctx, action)
        p["needs_script"] = False
    except ChangeError:
        if not pending:
            raise
        p = _plan_on(_with_pending(ctx, pending), action)
        p["needs_script"] = True
        p["warnings"].insert(0, "Uses objects that changes earlier in the script create: it can go into the script, "
                                "but can't run on its own now.")
    p["allowed"] = _allowed(ctx, action)
    if _own_password(ctx, action):
        p["connection"] = connection_info(ctx)
    return public(p)


# ----------------------------------------------------------------------------- SQL scripts (nothing runs)
# The same plans as Run, written out as a .sql script for someone else (a DBA) to review and run. Read-only:
# reads the cluster state for the checks, never runs a change, needs none of the change privileges, and never
# holds a password (CREATE USER gets a placeholder the DBA replaces).

SCRIPT_PASSWORD = "'<set password>'"
# Safe order: creates (pools before the users that use them, roles and groups before grants), then grants and
# changes, then revokes, then drops (users before the groups, roles and pools they use).
_SCRIPT_PHASES = [
    ["create_pool", "create_role", "create_group", "create_user"],
    ["alter_pool", "grant", "grant_view", "grant_role", "grant_group", "grant_pool", "set_default_pool", "change_password",
     "set_require", "set_login_policy", "unlock_user", "lock_user"],
    ["revoke", "revoke_all", "revoke_role", "revoke_group", "revoke_pool"],
    ["drop_user", "drop_group", "drop_role", "drop_pool"],
]
_SCRIPT_ORDER = [k for phase in _SCRIPT_PHASES for k in phase]
_PHASE = {k: n for n, phase in enumerate(_SCRIPT_PHASES) for k in phase}
_PHASE_RANK = {k: i for i, k in enumerate(_SCRIPT_ORDER)}
MAX_SCRIPT_ITEMS = 200


def _no_password(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ChangeError("Each change must be an object with an \"action\"")
    return {k: v for k, v in action.items() if k != "password"}


def script_order(actions: list[dict[str, Any]], keep_order: bool = False) -> list[int]:
    """Indexes of ``actions`` in script order: by kind (see _SCRIPT_ORDER), the given order within a kind group
    (creates sorted by dependency: pools, roles, groups, users; drops the other way round)."""
    idx = list(range(len(actions)))
    if keep_order:
        return idx

    def key(i: int) -> tuple[int, int, int]:
        kind = actions[i].get("action", "")
        phase = _PHASE.get(kind, 1)
        # creates and drops: by dependency rank; grants / revokes: the user's order
        sub = _PHASE_RANK.get(kind, 0) if phase in (0, 3) else 0
        return phase, sub, i

    return sorted(idx, key=key)


def _advance(ctx: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """The context after the action ran (for the checks of later changes in the same script)."""
    keys = ("users", "global", "grantable", "schema", "table", "user_groups", "group_roles", "roles", "groups", "role_privs",
            "pools", "pool_grants", "ssl", "objects")
    c = {**ctx, **copy.deepcopy({k: ctx[k] for k in keys if k in ctx})}
    c.setdefault("ssl", set())
    kind = action["action"]
    user = action.get("user")
    host = action.get("host") if action.get("host") is not None else "%"
    g = sec.grantee(str(user).strip(), host) if user else None
    if kind == "create_user" and g:
        policy = login_policy(action, required=False) or (0, 0)
        c["users"][g] = {"user": str(user).strip(), "host": host, "connections": 0, "default_pool": action.get("default_pool") or None,
                         "status": "OPEN", "failed_login_attempts": policy[0], "password_lock_time": policy[1]}
        if _require(action.get("require"), optional=True) == "SSL":
            c["ssl"].add(g)
    elif kind in ("lock_user", "unlock_user") and g in c["users"]:
        c["users"][g]["status"] = "LOCKED" if kind == "lock_user" else "OPEN"
    elif kind == "set_login_policy" and g in c["users"]:
        c["users"][g]["failed_login_attempts"], c["users"][g]["password_lock_time"] = login_policy(action)
    elif kind == "set_require" and g:
        (c["ssl"].add if _require(action.get("require")) == "SSL" else c["ssl"].discard)(g)
    elif kind == "grant_view" and isinstance(c.get("objects", {}).get(action.get("database")), set):
        c["objects"][action["database"]].add(str(action.get("view")).strip())
    elif kind == "create_role":
        c["roles"].add(str(action.get("role")).strip())
    elif kind == "create_group":
        c["groups"].add(str(action.get("group")).strip())
    elif kind == "create_pool":
        c["pools"][action["pool"].strip()] = {k.lower(): v for k, v in pool_settings(action.get("settings"), ctx["cpu_mode"])}
    elif kind == "alter_pool":
        c["pools"].setdefault(action["pool"], {}).update({k.lower(): v for k, v in pool_settings(action.get("settings"), ctx["cpu_mode"])})
    elif kind == "drop_pool":
        c["pools"].pop(action.get("pool"), None)
    elif kind == "drop_role":
        c["roles"].discard(action.get("role"))
    elif kind == "drop_group":
        c["groups"].discard(action.get("group"))
    elif kind == "grant_role":
        c["group_roles"].setdefault(action.get("group"), set()).add(action.get("role"))
    elif kind == "grant_group" and g:
        c["user_groups"].setdefault(g, set()).add(action.get("group"))
    elif kind == "set_default_pool" and g in c["users"]:
        c["users"][g]["default_pool"] = action.get("pool")
    elif kind in ("grant_pool", "revoke_pool"):
        target = g if action.get("grantee_type", "user") == "user" else None
        if target and kind == "grant_pool":
            c["pool_grants"].append((target, action.get("pool")))
        elif target:
            c["pool_grants"] = [pg for pg in c["pool_grants"] if pg != (target, action.get("pool"))]
    elif kind in ("grant", "revoke"):
        privs = {" ".join(str(p).upper().split()) for p in action.get("privileges") or []}
        db_, tb = action.get("database") or None, action.get("table") or None
        if action.get("grantee_type") == "role":
            bucket = c["role_privs"].setdefault(action.get("role"), {}).setdefault((db_, tb), set())
        elif g:
            if not db_:
                bucket = c["global"].setdefault(g, set())
            elif not tb:
                bucket = c["schema"].setdefault(g, {}).setdefault(db_, set())
            else:
                bucket = c["table"].setdefault(g, {}).setdefault((db_, tb), set())
            if kind == "grant" and action.get("with_grant_option") and not db_:
                c["grantable"][g] = True
        else:
            bucket = set()
        if kind == "grant":
            bucket.update(privs)
        else:
            bucket.difference_update(privs)
    elif kind == "revoke_all" and g and action.get("grantee_type", "user") == "user":
        c["schema"].pop(g, None)
        c["table"].pop(g, None)
    after = simulate(c, action)  # drops, revokes, removals (what the admin checks read)
    return {**c, **{k: after[k] for k in ("users", "global", "grantable", "user_groups", "group_roles", "role_privs")}}


def _with_pending(ctx: dict[str, Any], pending: list[dict[str, Any]]) -> dict[str, Any]:
    """The context after the (valid) changes of the script basket, in script order; invalid ones are skipped."""
    actions = [_no_password(a) for a in pending[:MAX_SCRIPT_ITEMS]]
    for i in script_order(actions):
        try:
            _plan_on(ctx, actions[i])
            ctx = _advance(ctx, actions[i])
        except (ChangeError, KeyError, TypeError, ValueError):
            continue
    return ctx


def _script_statements(p: dict[str, Any]) -> list[str]:
    """The statements as the preview shows them, with the masked password replaced by the placeholder."""
    out = []
    for s in p["statements"]:
        text = s["display"]
        if p["action"] in PASSWORD_ACTIONS:
            text = text.replace(f"IDENTIFIED BY {MASK}", f"IDENTIFIED BY {SCRIPT_PASSWORD}").replace(
                f"PASSWORD({MASK})", f"PASSWORD({SCRIPT_PASSWORD})")
        out.append(text)
    return out


def _one_line(text: str, n: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "-", text).strip("-").lower()[:40] or "change"


def _connection_label() -> str:
    try:
        from . import connections

        p = connections.active()
    except Exception:  # noqa: BLE001 - the label is informational
        p = None
    if p is None:
        return "(unknown connection)"
    return f"{p.name} ({p.host}:{p.port})" if p.host else p.name  # never the URL: it can hold a password


def script(actions: list[dict[str, Any]], keep_order: bool = False, now: Any = None) -> dict[str, Any]:
    """A reviewable SQL script for one or more changes: the statements Run would execute (built by the same plan
    builder from the cluster state now, each change checked as if the earlier ones had run), in a safe order,
    with a header and each change's warnings as ``-- WARNING:`` comments. Runs nothing and needs none of the
    change privileges. Never holds a password: CREATE USER gets ``IDENTIFIED BY '<set password>'``."""
    import datetime as _dt

    if not actions:
        raise ChangeError("Add at least one change to the script")
    if len(actions) > MAX_SCRIPT_ITEMS:
        raise ChangeError(f"At most {MAX_SCRIPT_ITEMS} changes in one script")
    acts = [_no_password(a) for a in actions]
    ctx = load_context()
    me = f"{ctx['me']['user']}@{ctx['me']['host']}"
    items: list[dict[str, Any]] = []
    for n, i in enumerate(script_order(acts, keep_order), 1):
        a = acts[i]
        title = ACTIONS.get(a.get("action"), str(a.get("action")))
        try:
            p = _plan_on(ctx, a)
        except ChangeError as exc:
            raise ChangeError(f"Change {i + 1} ({title}): {exc}") from None
        if p["blockers"]:
            raise ChangeError(f"Change {i + 1} ({title}) is blocked: " + " ".join(p["blockers"]))
        allowed = _allowed(ctx, a)
        statements = _script_statements(p)
        warnings = list(p["warnings"])
        if a["action"] in PASSWORD_ACTIONS:
            whose = "the new user's password" if a["action"] == "create_user" else "the new password"
            warnings.insert(0, f"Replace '<set password>' with {whose} before running (the script never holds a real password).")
        items.append({"n": n, "index": i, "action": a["action"], "title": p["title"], "statements": statements,
                      "warnings": warnings, "summary": f"{p['title']}: {_one_line(statements[0].rstrip(';'), 90)}" if statements else p["title"],
                      "allowed": allowed})
        ctx = _advance(ctx, a)

    stamp = now or _dt.datetime.now().astimezone()
    conn = _connection_label()
    head = [
        "-- SingleStore security changes - SQL script for review",
        "-- Generated by the SingleStore MCP Workspace (Security view). Nothing has been run.",
        f"-- Connection: {conn}",
        f"-- Prepared by: {me} on {stamp.strftime('%Y-%m-%d %H:%M %z').strip()}",
        f"-- Changes ({len(items)}):",
        *[f"--   {it['n']}. {it['summary']}" for it in items],
        "-- Order: " + ("as listed by the author." if keep_order else
                        "creates, then grants and other changes, then revokes, then drops."),
        "-- Checked against the cluster as it was when the script was made; review it before running.",
    ]
    if any(it["action"] == "create_user" for it in items):
        head.append("-- WARNING: CREATE USER has the placeholder '<set password>': set a real password before running.")
    if any(it["action"] == "change_password" for it in items):
        head.append("-- WARNING: the password change has the placeholder '<set password>': set the new password before running.")
    lacking = sorted({it["allowed"]["reason"] for it in items if not it["allowed"]["ok"]})
    if lacking:
        head.append(f"-- NOTE: {me} can't run all of this; whoever runs it needs: " + " ".join(lacking))
    body: list[str] = []
    for it in items:
        body += ["", f"-- {it['n']}. {it['title']}"]
        body += [f"-- WARNING: {_one_line(w, 400)}" for w in it["warnings"]]
        body += it["statements"]
    text = "\n".join(head + body) + "\n"
    if len(items) == 1:
        a = acts[items[0]["index"]]
        obj = next((a[k] for k in ("view", "user", "role", "group", "pool") if a.get(k)), "")
        what = _slug(f"{a['action'].replace('_', '-')}-{obj}")
    else:
        what = f"{len(items)}-changes"
    return {
        "script": text, "count": len(items), "statements": sum(len(it["statements"]) for it in items),
        "items": [{k: it[k] for k in ("n", "index", "action", "title", "summary", "statements", "warnings", "allowed")} for it in items],
        "file_name": f"security-{stamp.strftime('%Y%m%d-%H%M')}-{what}.sql",
        "connection": conn,
    }


def apply(action: dict[str, Any], confirm: bool = False, typed: str | None = None, password: str | None = None,
          update_connection: bool = False) -> dict[str, Any]:
    """Run one change after the user confirmed its plan. Builds the plan again from fresh cluster state.

    ``update_connection`` (your own password change only): after it succeeded, also store the new password for the
    active saved connection in the OS credential store, so the app's next connections use it."""
    kind = action.get("action", "")
    if not confirm:
        raise ChangeError("Refused: this changes users, privileges or resource pools on the shared cluster. Show the user "
                          "the plan (security_change_plan) and call again with confirm=True only after they confirmed.")
    if kind not in PASSWORD_ACTIONS:
        password = None
    ctx = load_context()
    _check_allowed(ctx, action)
    _enrich(ctx, action)
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
    out = {
        "action": kind, "title": p["title"], "results": results,
        "ok": not failed, "ran": sum(r["status"] == "ok" for r in results), "refresh": p["refresh"],
        "warnings": p["warnings"],
    }
    if kind == "change_password" and _own_password(ctx, action) and not failed:
        info = connection_info(ctx)
        out["connection"] = {"name": info["name"], "updated": False, "note": info["reason"]}
        if update_connection and info["can_update"] and password:
            try:
                from . import connections

                connections.set_password(info["name"], password)
                out["connection"].update(updated=True, note=f"The new password is saved for the connection {info['name']}.")
            except Exception as exc:  # noqa: BLE001 - the password itself changed; say what to do
                out["connection"]["note"] = (f"The password changed, but saving it for {info['name']} failed "
                                             f"({mask_sql(str(exc), password)[:200]}): update it in Connections.")
        elif info["can_update"]:
            out["connection"]["note"] = f"Update the password of the connection {info['name']} in Connections."
    return out


def summary(d: dict[str, Any]) -> str:
    if "results" in d:
        lines = [f"{d['title']}: {'done' if d['ok'] else 'failed'} ({d['ran']} of {len(d['results'])} statement(s) ran)."]
        lines += [f"- {r['status']}: {r['statement']}" + (f" -> {r['error']}" if r.get("error") else "") for r in d["results"]]
        if d.get("connection", {}).get("note"):
            lines.append(d["connection"]["note"])
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
