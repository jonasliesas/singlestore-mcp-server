"""Security view back end: users, groups, roles, privileges and resource pools (read-only).

Every statement here only reads (information_schema views, SHOW GRANTS, the local query history store);
nothing is granted, revoked, created or altered. Where a change would help, the page shows SQL the user
can copy and run themselves.

Sources (checked on SingleStore 9.0.40 and in the 9.0 docs):
- USERS: one row per user@host. TYPE is the authentication kind (NATIVE / JWT seen); PASSWORD_EXPIRATION is
  NULL unless the ``password_expiration_seconds`` engine variable is on, otherwise the seconds left;
  ACCOUNT_STATUS is LOCKED / OPEN (N/A = no lockout policy); CONNECTIONS = open connections now;
  DEFAULT_RESOURCE_POOL '' = none (the global ``resource_pool`` applies, else default_pool).
- USER_PRIVILEGES (global, ``*.*``), SCHEMA_PRIVILEGES (database), TABLE_PRIVILEGES, COLUMN_PRIVILEGES:
  privileges granted to the user directly. GRANTEE is "'user'@'host'". ALL PRIVILEGES shows up expanded into
  its parts there; SHOW GRANTS keeps the words "ALL PRIVILEGES".
- USERS_GROUPS (USER is "'user'@'host'", GROUP), GROUPS_ROLES (GROUP, ROLE), ROLE_PRIVILEGES (ROLE, DATABASE,
  TABLE, PRIVILEGES as a comma-separated list, '*' = all), GROUP_PRIVILEGES (GROUP, ROLE, DATABASE, TABLE,
  PRIVILEGES: the same chain already joined). Privileges reach a user only through user -> group -> role.
- CURRENT_USER_GROUPS / CURRENT_USER_ROLES / SHOW GRANTS: the fallback when the cluster-wide views can't be read.
- RESOURCE_POOLS (NULL limit = not set = no limit), MV_RESOURCE_POOL_STATUS (per node; running / queued now,
  killed / finished / queue time since the node started), RESOURCE_POOL_PRIVILEGES (GRANT USAGE ON RESOURCE POOL).
- WORKLOAD_MANAGEMENT_QUEUE: queued queries; a resource is lacking when required > (limit - used), with
  "limit" and "used" as of when the query was submitted (docs). MV_WORKLOAD_MANAGEMENT_STATUS: per node stats.
"""

from __future__ import annotations

import datetime
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import singlestoredb as s2

from .db import db

MARKER = "/* s2-security */"
IS = "information_schema"

# Privileges that don't give access to data on their own.
_NOT_ACCESS = {"USAGE", "SSL", "SERVICE_USER"}
# Global-only administrative privileges: they appear on *.* but don't open a database's data.
_ADMIN_ONLY = {
    "RELOAD", "SHUTDOWN", "PROCESS", "FILE READ", "FILE WRITE", "REPLICATION SLAVE", "REPLICATION CLIENT", "CREATE USER",
    "CLUSTER", "BACKUP", "CREATE DATABASE", "DROP DATABASE", "CREATE TABLESPACE", "CREATE POOL", "DROP POOL",
    "SYSTEM_VARIABLES_ADMIN", "OUTBOUND", "CONNECTION_ADMIN", "ALTER USER", "ALTER EVENT TRACE", "CREATE LINK",
    "DROP LINK", "SHOW LINK", "CREATE EXTENSION", "DROP EXTENSION", "SHOW EXTENSION", "ALTER EXTENSION",
    "CREATE EXTERNAL CATALOG", "DROP EXTERNAL CATALOG", "SHOW EXTERNAL CATALOG", "GRANT",
}
_GRANTEE = re.compile(r"^'((?:[^']|'')*)'@'((?:[^']|'')*)'$")
_PASSWORD = re.compile(r"(IDENTIFIED BY PASSWORD\s+)'[^']*'", re.I)


# ----------------------------------------------------------------------------- helpers

def _q(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    from .apps._core import jsonable_rows

    return jsonable_rows(db.execute(f"{MARKER} {sql}", params)[1])


def _soft(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap a read so a permission (or other SQL) error comes back as {"error": ...} instead of failing the batch."""

    def run() -> Any:
        try:
            return fn()
        except s2.Error as exc:
            return {"error": str(exc)[:300]}

    return run


def _ok(v: Any) -> bool:
    return not (isinstance(v, dict) and "error" in v)


def _rows(v: Any) -> list[dict[str, Any]]:
    return v if isinstance(v, list) else []


def quote_str(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def grantee(user: str, host: str) -> str:
    """The GRANTEE spelling the information_schema views use: 'user'@'host'."""
    return f"'{user.replace(chr(39), chr(39) * 2)}'@'{host.replace(chr(39), chr(39) * 2)}'"


def parse_grantee(g: str) -> tuple[str, str]:
    m = _GRANTEE.match(g or "")
    if not m:
        return g, "%"
    return m.group(1).replace("''", "'"), m.group(2).replace("''", "'")


def split_privs(text: Any) -> list[str]:
    return [p.strip().upper() for p in str(text or "").split(",") if p.strip()]


def mask_grants(lines: list[str]) -> list[str]:
    """SHOW GRANTS prints the password hash: never show it."""
    return [_PASSWORD.sub(r"\1'<hidden>'", line) for line in lines]


def _obj(database: str | None, table: str | None = None, column: str | None = None) -> str:
    d = database or "*"
    t = table or "*"
    return f"{d}.{t}" + (f" ({column})" if column else "")


def _cluster_now() -> str:
    return _q("SELECT NOW() AS now")[0]["now"]


def _age_days(ts: Any, now: datetime.datetime) -> int | None:
    if not ts:
        return None
    try:
        return max(0, (now - datetime.datetime.fromisoformat(str(ts))).days)
    except ValueError:
        return None


def show_grants(user: str, host: str) -> list[str]:
    rows = db.execute(f"{MARKER} SHOW GRANTS FOR {quote_str(user)}@{quote_str(host)}")[1]
    return mask_grants([str(next(iter(r.values()))) for r in rows])


def _current_user() -> dict[str, Any]:
    r = _q("SELECT CURRENT_USER() AS cu, USER() AS u")[0]
    user, _, host = str(r["cu"]).rpartition("@")
    return {"user": user, "host": host or "%", "login": r["u"]}


# ----------------------------------------------------------------------------- query history (local store)

def _history_users() -> dict[str, Any]:
    """Last traced query per user name from the local Query History store (read-only use of history_store)."""
    try:
        from . import history_store

        u = history_store.usage(None, limit=100000)
    except Exception as exc:  # noqa: BLE001 - the store is optional
        return {"error": str(exc)[:200], "users": {}}
    return {"users": {x["user"]: {"last": x["last"], "queries": x["queries"]} for x in u["users"]},
            "oldest": u.get("oldest"), "stored": u.get("stored"), "source": u.get("source")}


# ----------------------------------------------------------------------------- Users tab

def _global_privs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = out.setdefault(r["GRANTEE"], {"privs": set(), "grantable": False})
        g["privs"].add(str(r["PRIVILEGE_TYPE"]).upper())
        g["grantable"] |= str(r["IS_GRANTABLE"]).upper() == "YES"
    return out


def collect_users(unused_days: int = 90) -> dict[str, Any]:
    unused_days = max(1, min(3650, int(unused_days)))
    (users, gprivs, sprivs, tprivs, ug, gr, rp, now, me, pwvar, hist) = db.parallel(
        _soft(lambda: _q(
            "SELECT USER, HOST, TYPE, CONNECTIONS, IS_LOCAL, CREATED, PASSWORD_UPDATED, LAST_UPDATED,"
            " EFFECTIVE_FAILED_LOGIN_ATTEMPTS, EFFECTIVE_PASSWORD_LOCK_TIME, ACCOUNT_STATUS, PASSWORD_EXPIRATION,"
            f" DEFAULT_RESOURCE_POOL, COMMENT FROM {IS}.USERS WHERE IS_DELETED = 0 ORDER BY USER, HOST")),
        _soft(lambda: _q(f"SELECT GRANTEE, PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.USER_PRIVILEGES")),
        _soft(lambda: _q(f"SELECT GRANTEE, COUNT(*) AS n FROM {IS}.SCHEMA_PRIVILEGES GROUP BY GRANTEE")),
        _soft(lambda: _q(f"SELECT GRANTEE, COUNT(*) AS n FROM {IS}.TABLE_PRIVILEGES GROUP BY GRANTEE")),
        _soft(lambda: _q(f"SELECT USER, `GROUP` FROM {IS}.USERS_GROUPS")),
        _soft(lambda: _q(f"SELECT `GROUP`, ROLE FROM {IS}.GROUPS_ROLES")),
        _soft(lambda: _q(f"SELECT ROLE, `DATABASE`, `TABLE`, PRIVILEGES FROM {IS}.ROLE_PRIVILEGES")),
        _cluster_now,
        _current_user,
        _soft(lambda: _q(f"SELECT VARIABLE_VALUE AS v FROM {IS}.GLOBAL_VARIABLES WHERE VARIABLE_NAME IN"
                         " ('password_expiration_seconds', 'PASSWORD_EXPIRATION_SECONDS')")),
        _history_users,
    )
    if not _ok(users) or not users:
        return _limited_users(me, users)
    now_dt = datetime.datetime.fromisoformat(str(now))
    gp = _global_privs(_rows(gprivs))
    groups_of: dict[str, list[str]] = {}
    for r in _rows(ug):
        groups_of.setdefault(r["USER"], []).append(r["GROUP"])
    roles_of_group: dict[str, list[str]] = {}
    for r in _rows(gr):
        roles_of_group.setdefault(r["GROUP"], []).append(r["ROLE"])
    global_role_privs: dict[str, set[str]] = {}
    for r in _rows(rp):
        if r["DATABASE"] == "*" and r["TABLE"] == "*":
            global_role_privs.setdefault(r["ROLE"], set()).update(split_privs(r["PRIVILEGES"]))
    n_schema = {r["GRANTEE"]: int(r["n"]) for r in _rows(sprivs)}
    n_table = {r["GRANTEE"]: int(r["n"]) for r in _rows(tprivs)}
    pw_var = (_rows(pwvar) or [{}])[0].get("v")
    h = hist if isinstance(hist, dict) else {"users": {}}
    hist_users = h.get("users", {})
    hist_days = _age_days(h.get("oldest"), now_dt) if h.get("oldest") else None

    out = []
    for u in users:
        key = grantee(u["USER"], u["HOST"])
        g = gp.get(key, {"privs": set(), "grantable": False})
        groups = sorted(groups_of.get(key, []))
        roles = sorted({r for grp in groups for r in roles_of_group.get(grp, [])})
        via_roles = set().union(*(global_role_privs.get(r, set()) for r in roles)) if roles else set()
        hu = hist_users.get(u["USER"])
        last_seen = hu["last"] if hu else None
        if last_seen and _age_days(last_seen, now_dt) is not None and _age_days(last_seen, now_dt) < unused_days:
            unused: bool | None = False
        elif hist_days is not None and hist_days >= unused_days:
            unused = True
        else:
            unused = None  # the store doesn't cover the period: can't tell
        flags = []
        if "SUPER" in g["privs"] or "SUPER" in via_roles:
            flags.append("super")
        if g["grantable"]:
            flags.append("grant_option")
        if u["HOST"] == "%":
            flags.append("any_host")
        if str(u["TYPE"]).upper() != "JWT" and u["PASSWORD_EXPIRATION"] is None:
            flags.append("no_pw_expiry")
        if str(u["ACCOUNT_STATUS"]).upper() == "LOCKED":
            flags.append("locked")
        if unused:
            flags.append("unused")
        require_ssl = "SSL" in g["privs"]  # REQUIRE SSL shows as PRIVILEGE_TYPE 'SSL' (USERS has no column for it)
        if require_ssl:
            flags.append("require_ssl")
        out.append({
            "user": u["USER"], "host": u["HOST"], "type": u["TYPE"], "connections": int(u["CONNECTIONS"] or 0),
            "is_local": bool(u["IS_LOCAL"]), "created": u["CREATED"], "password_updated": u["PASSWORD_UPDATED"],
            "password_age_days": _age_days(u["PASSWORD_UPDATED"], now_dt),
            "password_expires_in_s": u["PASSWORD_EXPIRATION"],
            "failed_login_attempts": u["EFFECTIVE_FAILED_LOGIN_ATTEMPTS"],
            "password_lock_time": u["EFFECTIVE_PASSWORD_LOCK_TIME"],
            "account_status": u["ACCOUNT_STATUS"], "default_pool": u["DEFAULT_RESOURCE_POOL"] or None,
            "comment": u["COMMENT"] or None, "groups": groups, "roles": roles,
            "require_ssl": require_ssl,
            "global_privileges": sorted(g["privs"] - {"USAGE", "SSL"}), "role_global_privileges": sorted(via_roles),
            "schema_grants": n_schema.get(key, 0), "table_grants": n_table.get(key, 0),
            "last_seen": last_seen, "history_queries": hu["queries"] if hu else 0, "unused": unused,
            "flags": flags,
        })
    _add_all_privileges_flag(out)
    errors = {k: v["error"] for k, v in (("USER_PRIVILEGES", gprivs), ("USERS_GROUPS", ug), ("GROUPS_ROLES", gr),
                                         ("ROLE_PRIVILEGES", rp)) if not _ok(v)}
    return {
        "generated_at": now, "mode": "cluster", "me": me, "users": out, "unused_days": unused_days,
        "password_expiration_seconds": pw_var,
        "history": {"oldest": h.get("oldest"), "covers_days": hist_days, "stored": h.get("stored"),
                    "source": h.get("source"), "error": h.get("error"),
                    "note": "The local store keeps queries of 1 s or more and failed ones, by user name (not host)."},
        "errors": errors,
    }


_SHOW_GRANTS_MAX = 100


def _add_all_privileges_flag(users: list[dict[str, Any]]) -> None:
    """USER_PRIVILEGES lists ALL PRIVILEGES expanded, so read SHOW GRANTS (concurrently) to see the words."""
    targets = users[:_SHOW_GRANTS_MAX]
    if not targets:
        return

    def one(u: dict[str, Any]) -> Callable[[], Any]:
        return _soft(lambda: show_grants(u["user"], u["host"]))

    results = db.parallel(*[one(u) for u in targets])
    for u, lines in zip(targets, results):
        if not _ok(lines):
            u["all_checked"] = False
            continue
        u["all_checked"] = True
        if any(re.search(r"\bALL PRIVILEGES\b.*\bON \*\.\* TO\b", line, re.I) for line in lines):
            u["flags"].insert(0, "all")
    for u in users[_SHOW_GRANTS_MAX:]:
        u["all_checked"] = False


def _limited_users(me: dict[str, Any], users_result: Any) -> dict[str, Any]:
    """The connected user can't read the cluster-wide views: show only what it may see about itself."""
    groups, roles, grants = db.parallel(
        _soft(lambda: _q(f"SELECT `GROUP` FROM {IS}.CURRENT_USER_GROUPS")),
        _soft(lambda: _q(f"SELECT ROLE FROM {IS}.CURRENT_USER_ROLES")),
        _soft(lambda: mask_grants([str(next(iter(r.values()))) for r in db.execute(f"{MARKER} SHOW GRANTS")[1]])),
    )
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "mode": "limited", "me": me,
        "notice": ("The connected user can't read the cluster-wide user and privilege views"
                   + (f" ({users_result['error']})" if not _ok(users_result) else " (information_schema.USERS is empty)")
                   + ". Showing only the connected user's own groups, roles and grants."),
        "users": [], "groups": [r["GROUP"] for r in _rows(groups)], "roles": [r["ROLE"] for r in _rows(roles)],
        "grants": grants if isinstance(grants, list) else [], "errors": {},
    }


def users_summary(d: dict[str, Any]) -> str:
    if d["mode"] == "limited":
        return (f"{d['notice']}\nConnected as {d['me']['user']}@{d['me']['host']}; groups: {', '.join(d['groups']) or 'none'};"
                f" roles: {', '.join(d['roles']) or 'none'}.\n" + "\n".join(d["grants"][:20]))
    us = d["users"]
    lines = [f"{len(us)} user(s); connected as {d['me']['user']}@{d['me']['host']}."]
    for flag, label in (("all", "ALL PRIVILEGES on *.*"), ("super", "SUPER"), ("grant_option", "WITH GRANT OPTION on *.*"),
                        ("any_host", "host '%'"), ("no_pw_expiry", "password never expires"), ("locked", "locked"),
                        ("unused", f"no traced queries in {d['unused_days']} days"), ("require_ssl", "REQUIRE SSL")):
        names = [f"{u['user']}@{u['host']}" for u in us if flag in u["flags"]]
        if names:
            lines.append(f"- {label}: {', '.join(names[:30])}{' …' if len(names) > 30 else ''}")
    lines.append("Per user (type, status, connections, groups):")
    for u in us[:200]:
        lines.append(f"- {u['user']}@{u['host']} {u['type']} {u['account_status']} conns={u['connections']}"
                     f" groups={','.join(u['groups']) or '-'} pool={u['default_pool'] or '-'}"
                     f" pw_age={u['password_age_days']}d last_seen={u['last_seen'] or '-'}")
    if len(us) > 200:
        lines.append(f"… {len(us) - 200} more users (shown in the app)")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- effective privileges of one user

def _resolve_user(user: str, host: str | None) -> tuple[str, str]:
    if host is not None:
        return user, host
    rows = _q(f"SELECT HOST FROM {IS}.USERS WHERE USER = %s AND IS_DELETED = 0", (user,))
    hosts = [r["HOST"] for r in rows]
    if not hosts:
        raise LookupError(f"No user named {user!r} (or it isn't visible to the connected user).")
    if len(hosts) > 1:
        raise ValueError(f"{user!r} exists for several hosts ({', '.join(hosts)}): pass host=")
    return user, hosts[0]


def user_privileges(user: str, host: str | None = None) -> dict[str, Any]:
    user, host = _resolve_user(user, host)
    g = grantee(user, host)
    (glob, schema, table, column, groups, chain, roles, pools, grants, info) = db.parallel(
        _soft(lambda: _q(f"SELECT PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.USER_PRIVILEGES WHERE GRANTEE = %s", (g,))),
        _soft(lambda: _q(f"SELECT TABLE_SCHEMA, PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.SCHEMA_PRIVILEGES WHERE GRANTEE = %s", (g,))),
        _soft(lambda: _q(f"SELECT TABLE_SCHEMA, TABLE_NAME, PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.TABLE_PRIVILEGES"
                         " WHERE GRANTEE = %s", (g,))),
        _soft(lambda: _q(f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.COLUMN_PRIVILEGES"
                         " WHERE GRANTEE = %s", (g,))),
        _soft(lambda: _q(f"SELECT `GROUP` FROM {IS}.USERS_GROUPS WHERE USER = %s", (g,))),
        _soft(lambda: _q(f"SELECT gr.`GROUP`, gr.ROLE FROM {IS}.GROUPS_ROLES gr JOIN {IS}.USERS_GROUPS ug"
                         " ON ug.`GROUP` = gr.`GROUP` WHERE ug.USER = %s", (g,))),
        _soft(lambda: _q(f"SELECT rp.ROLE, rp.`DATABASE`, rp.`TABLE`, rp.PRIVILEGES FROM {IS}.ROLE_PRIVILEGES rp"
                         f" JOIN {IS}.GROUPS_ROLES gr ON gr.ROLE = rp.ROLE JOIN {IS}.USERS_GROUPS ug ON ug.`GROUP` = gr.`GROUP`"
                         " WHERE ug.USER = %s", (g,))),
        _soft(lambda: _q(f"SELECT RESOURCE_POOL, PRIVILEGE_TYPE FROM {IS}.RESOURCE_POOL_PRIVILEGES WHERE GRANTEE = %s", (g,))),
        _soft(lambda: show_grants(user, host)),
        _soft(lambda: _q(f"SELECT TYPE, DEFAULT_RESOURCE_POOL, ACCOUNT_STATUS, EFFECTIVE_FAILED_LOGIN_ATTEMPTS,"
                         f" EFFECTIVE_PASSWORD_LOCK_TIME, CONNECTIONS FROM {IS}.USERS WHERE USER = %s AND HOST = %s",
                         (user, host))),
    )
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    def add(priv: str, obj: str, scope: str, how: str, grantable: bool = False) -> None:
        r = rows.setdefault((priv, obj), {"privilege": priv, "object": obj, "scope": scope, "how": [], "grantable": False})
        if how not in r["how"]:
            r["how"].append(how)
        r["grantable"] |= grantable

    require_ssl = None if not _ok(glob) else False
    for r in _rows(glob):
        if r["PRIVILEGE_TYPE"] == "SSL":  # REQUIRE SSL, not a privilege
            require_ssl = True
        elif r["PRIVILEGE_TYPE"] != "USAGE":
            add(r["PRIVILEGE_TYPE"], "*.*", "global", "direct", r["IS_GRANTABLE"] == "YES")
    for r in _rows(schema):
        add(r["PRIVILEGE_TYPE"], _obj(r["TABLE_SCHEMA"]), "database", "direct", r["IS_GRANTABLE"] == "YES")
    for r in _rows(table):
        add(r["PRIVILEGE_TYPE"], _obj(r["TABLE_SCHEMA"], r["TABLE_NAME"]), "table", "direct", r["IS_GRANTABLE"] == "YES")
    for r in _rows(column):
        add(r["PRIVILEGE_TYPE"], _obj(r["TABLE_SCHEMA"], r["TABLE_NAME"], r["COLUMN_NAME"]), "column", "direct",
            r["IS_GRANTABLE"] == "YES")
    groups_by_role: dict[str, list[str]] = {}
    for r in _rows(chain):
        groups_by_role.setdefault(r["ROLE"], []).append(r["GROUP"])
    seen = set()
    for r in _rows(roles):
        key = (r["ROLE"], r["DATABASE"], r["TABLE"], r["PRIVILEGES"])
        if key in seen:
            continue
        seen.add(key)
        scope = "global" if r["DATABASE"] == "*" else "database" if r["TABLE"] == "*" else "table"
        obj = _obj(None if r["DATABASE"] == "*" else r["DATABASE"], None if r["TABLE"] == "*" else r["TABLE"])
        for grp in sorted(groups_by_role.get(r["ROLE"], [])):
            for p in split_privs(r["PRIVILEGES"]):
                add(p, obj, scope, f"via role {r['ROLE']} ← group {grp}")
    order = {"global": 0, "database": 1, "table": 2, "column": 3}
    effective = sorted(rows.values(), key=lambda r: (order[r["scope"]], r["object"], r["privilege"]))
    errors = {k: v["error"] for k, v in (("USER_PRIVILEGES", glob), ("USERS_GROUPS", groups), ("ROLE_PRIVILEGES", roles),
                                         ("SHOW GRANTS", grants)) if not _ok(v)}
    info_row = (_rows(info) or [{}])[0]
    return {
        "user": user, "host": host, "grantee": g, "type": info_row.get("TYPE"),
        "default_pool": info_row.get("DEFAULT_RESOURCE_POOL") or None, "account_status": info_row.get("ACCOUNT_STATUS"),
        "require_ssl": require_ssl, "failed_login_attempts": info_row.get("EFFECTIVE_FAILED_LOGIN_ATTEMPTS"),
        "password_lock_time": info_row.get("EFFECTIVE_PASSWORD_LOCK_TIME"), "connections": info_row.get("CONNECTIONS"),
        "groups": sorted(r["GROUP"] for r in _rows(groups)),
        "chain": sorted({(r["GROUP"], r["ROLE"]) for r in _rows(chain)}),
        "effective": effective,
        "resource_pools": [{"pool": r["RESOURCE_POOL"], "privilege": r["PRIVILEGE_TYPE"]} for r in _rows(pools)],
        "show_grants": grants if isinstance(grants, list) else [],
        "errors": errors,
    }


def user_privileges_summary(d: dict[str, Any]) -> str:
    lines = [f"Effective privileges of {d['grantee']} ({d['type'] or '?'}, {d['account_status'] or '?'}"
             f"{', REQUIRE SSL' if d.get('require_ssl') else ''}); groups: "
             f"{', '.join(d['groups']) or 'none'}; roles: {', '.join(sorted({r for _, r in d['chain']})) or 'none'}."]
    if d.get("failed_login_attempts"):
        lines.append(f"Failed-login lockout: after {d['failed_login_attempts']} failed logins, locked for {d['password_lock_time']} s.")
    by_obj: dict[tuple[str, str], list[str]] = {}
    for r in d["effective"]:
        by_obj.setdefault((r["object"], "; ".join(r["how"])), []).append(r["privilege"] + ("*" if r["grantable"] else ""))
    for (obj, how), privs in list(by_obj.items())[:150]:
        lines.append(f"- {obj}: {', '.join(privs)}  [{how}]")
    if any(r["grantable"] for r in d["effective"]):
        lines.append("(* = WITH GRANT OPTION)")
    if d["resource_pools"]:
        lines.append("Resource pools: " + ", ".join(f"{p['privilege']} ON {p['pool']}" for p in d["resource_pools"]))
    lines.append("SHOW GRANTS (password hash hidden):")
    lines += [f"  {g}" for g in d["show_grants"][:40]]
    for k, v in d["errors"].items():
        lines.append(f"Couldn't read {k}: {v}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- Roles & groups tab

def collect_roles() -> dict[str, Any]:
    groups, ug, gr, roles, rp, dbs = db.parallel(
        _soft(lambda: _q(f"SELECT GROUPNAME, NUM_USERS FROM {IS}.`GROUPS` ORDER BY GROUPNAME")),
        _soft(lambda: _q(f"SELECT USER, `GROUP` FROM {IS}.USERS_GROUPS")),
        _soft(lambda: _q(f"SELECT `GROUP`, ROLE FROM {IS}.GROUPS_ROLES")),
        _soft(lambda: _q(f"SELECT ROLE, PERMISSIONS FROM {IS}.ROLES ORDER BY ROLE")),
        _soft(lambda: _q(f"SELECT ROLE, `DATABASE`, `TABLE`, PRIVILEGES FROM {IS}.ROLE_PRIVILEGES")),
        _soft(lambda: _q(f"SELECT SCHEMA_NAME FROM {IS}.SCHEMATA ORDER BY SCHEMA_NAME")),
    )
    if not _ok(groups) and not _ok(roles):
        return {"mode": "limited", "notice": f"The connected user can't read the group and role views ({groups['error']}).",
                "groups": [], "roles": [], "databases": [r["SCHEMA_NAME"] for r in _rows(dbs)]}
    members: dict[str, list[str]] = {}
    for r in _rows(ug):
        u, h = parse_grantee(r["USER"])
        members.setdefault(r["GROUP"], []).append(f"{u}@{h}")
    roles_of: dict[str, list[str]] = {}
    groups_of_role: dict[str, list[str]] = {}
    for r in _rows(gr):
        roles_of.setdefault(r["GROUP"], []).append(r["ROLE"])
        groups_of_role.setdefault(r["ROLE"], []).append(r["GROUP"])
    privs: dict[str, list[dict[str, Any]]] = {}
    for r in _rows(rp):
        privs.setdefault(r["ROLE"], []).append({
            "object": _obj(None if r["DATABASE"] == "*" else r["DATABASE"], None if r["TABLE"] == "*" else r["TABLE"]),
            "privileges": split_privs(r["PRIVILEGES"])})
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "mode": "cluster",
        "groups": [{"group": g["GROUPNAME"], "num_users": int(g["NUM_USERS"] or 0), "members": sorted(members.get(g["GROUPNAME"], [])),
                    "roles": sorted(roles_of.get(g["GROUPNAME"], []))} for g in _rows(groups)],
        "roles": [{"role": r["ROLE"], "permissions": split_privs(r["PERMISSIONS"]), "groups": sorted(groups_of_role.get(r["ROLE"], [])),
                   "privileges": sorted(privs.get(r["ROLE"], []), key=lambda p: p["object"])} for r in _rows(roles)],
        "databases": [r["SCHEMA_NAME"] for r in _rows(dbs)],
        "errors": {k: v["error"] for k, v in (("GROUPS", groups), ("USERS_GROUPS", ug), ("GROUPS_ROLES", gr),
                                              ("ROLES", roles), ("ROLE_PRIVILEGES", rp)) if not _ok(v)},
    }


def roles_summary(d: dict[str, Any]) -> str:
    if d["mode"] == "limited":
        return d["notice"]
    used = [g for g in d["groups"] if g["members"]]
    lines = [f"{len(d['groups'])} group(s) ({len(used)} with members), {len(d['roles'])} role(s)."]
    for g in used[:60]:
        lines.append(f"- group {g['group']}: users {', '.join(g['members'][:10])}; roles {', '.join(g['roles']) or '-'}")
    return "\n".join(lines)


def list_tables(database: str) -> list[str]:
    rows = _q(f"SELECT TABLE_NAME FROM {IS}.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME LIMIT 5000", (database,))
    return [r["TABLE_NAME"] for r in rows]


def list_columns(database: str, table: str) -> list[str]:
    rows = _q(f"SELECT COLUMN_NAME FROM {IS}.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
              (database, table))
    return [r["COLUMN_NAME"] for r in rows]


def who_can_access(database: str, table: str | None = None) -> dict[str, Any]:
    if not database:
        raise ValueError("database is required")
    tparams: tuple[Any, ...] = (database,) if table is None else (database, table)
    tcond = "TABLE_SCHEMA = %s" + ("" if table is None else " AND TABLE_NAME = %s")
    glob, schema, tbl, col, gp, ug, users, exists = db.parallel(
        _soft(lambda: _q(f"SELECT GRANTEE, PRIVILEGE_TYPE FROM {IS}.USER_PRIVILEGES")),
        _soft(lambda: _q(f"SELECT GRANTEE, PRIVILEGE_TYPE FROM {IS}.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA = %s", (database,))),
        _soft(lambda: _q(f"SELECT GRANTEE, TABLE_NAME, PRIVILEGE_TYPE FROM {IS}.TABLE_PRIVILEGES WHERE {tcond}", tparams)),
        _soft(lambda: _q(f"SELECT GRANTEE, TABLE_NAME, COLUMN_NAME, PRIVILEGE_TYPE FROM {IS}.COLUMN_PRIVILEGES WHERE {tcond}", tparams)),
        _soft(lambda: _q(f"SELECT `GROUP`, ROLE, `DATABASE`, `TABLE`, PRIVILEGES FROM {IS}.GROUP_PRIVILEGES"
                         " WHERE `DATABASE` IN ('*', %s)" + ("" if table is None else " AND `TABLE` IN ('*', %s)"),
                         (database,) if table is None else (database, table))),
        _soft(lambda: _q(f"SELECT USER, `GROUP` FROM {IS}.USERS_GROUPS")),
        _soft(lambda: _q(f"SELECT USER, HOST FROM {IS}.USERS WHERE IS_DELETED = 0")),
        _soft(lambda: _q(f"SELECT COUNT(*) AS n FROM {IS}.SCHEMATA WHERE SCHEMA_NAME = %s", (database,))
              if table is None else
              _q(f"SELECT COUNT(*) AS n FROM {IS}.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s", (database, table))),
    )
    if not _ok(glob) or not _ok(users):
        raise ValueError("The connected user can't read the cluster-wide privilege views "
                         f"({(glob if not _ok(glob) else users)['error']}), so who has access can't be determined.")
    acc: dict[str, dict[str, dict[str, Any]]] = {}

    def add(g: str, how: str, scope: str, privs: list[str]) -> None:
        keep = [p for p in privs if p not in _NOT_ACCESS and not (scope.startswith("global") and p in _ADMIN_ONLY)]
        if keep:
            e = acc.setdefault(g, {}).setdefault(scope, {"privs": set(), "how": []})
            e["privs"].update(keep)
            if how not in e["how"]:
                e["how"].append(how)

    for r in _rows(glob):
        add(r["GRANTEE"], "direct", "global (*.*)", [str(r["PRIVILEGE_TYPE"]).upper()])
    for r in _rows(schema):
        add(r["GRANTEE"], "direct", f"database {database}", [str(r["PRIVILEGE_TYPE"]).upper()])
    for r in _rows(tbl):
        add(r["GRANTEE"], "direct", f"table {database}.{r['TABLE_NAME']}", [str(r["PRIVILEGE_TYPE"]).upper()])
    for r in _rows(col):
        add(r["GRANTEE"], "direct", f"column {database}.{r['TABLE_NAME']}.{r['COLUMN_NAME']}", [str(r["PRIVILEGE_TYPE"]).upper()])
    members: dict[str, list[str]] = {}
    for r in _rows(ug):
        members.setdefault(r["GROUP"], []).append(r["USER"])
    for r in _rows(gp):
        scope = ("global (*.*)" if r["DATABASE"] == "*" else f"database {database}" if r["TABLE"] == "*"
                 else f"table {database}.{r['TABLE']}")
        for g in members.get(r["GROUP"], []):
            add(g, f"via role {r['ROLE']} ← group {r['GROUP']}", scope, split_privs(r["PRIVILEGES"]))
    out = []
    for g, entries in acc.items():
        u, h = parse_grantee(g)
        for scope, e in sorted(entries.items(), key=lambda kv: (not kv[0].startswith("global"), kv[0])):
            out.append({"user": u, "host": h, "how": "; ".join(e["how"]), "hows": e["how"], "scope": scope,
                        "privileges": sorted(e["privs"]),
                        "kind": "global" if scope.startswith("global") else "direct" if "direct" in e["how"] else "role"})
    out.sort(key=lambda r: (r["user"].lower(), r["host"], r["kind"] != "global"))
    found = int((_rows(exists) or [{"n": 0}])[0]["n"])
    return {
        "database": database, "table": table, "exists": found > 0, "rows": out,
        "users_with_access": len({(r["user"], r["host"]) for r in out}), "total_users": len(_rows(users)),
        "notes": ["Global administrative privileges that don't open data (e.g. CREATE USER, PROCESS, CLUSTER) and"
                  " USAGE / SSL are left out.",
                  "SHOW METADATA lets a user see the object's metadata, not its rows.",
                  "Database and table names are matched exactly; grants that use wildcard patterns in names aren't expanded."],
    }


def who_summary(d: dict[str, Any]) -> str:
    target = d["database"] + (f".{d['table']}" if d["table"] else "")
    lines = [f"{d['users_with_access']} of {d['total_users']} user(s) have privileges on {target}"
             + ("" if d["exists"] else " (note: no such object was found)") + ":"]
    for r in d["rows"][:300]:
        lines.append(f"- {r['user']}@{r['host']}: {', '.join(r['privileges'])} on {r['scope']} [{r['how']}]")
    if len(d["rows"]) > 300:
        lines.append(f"… {len(d['rows']) - 300} more rows (shown in the app)")
    lines += [f"Note: {n}" for n in d["notes"]]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- Resource pools tab

_HISTORY_SECONDS = 900
_pool_history: deque[dict[str, Any]] = deque()
_pool_lock = threading.Lock()
_WLM_VARS = ("resource_pool", "workload_management", "workload_management_max_threads_per_leaf",
             "workload_management_max_connections_per_leaf", "workload_management_memory_queue_threshold",
             "workload_management_expected_aggregators", "workload_management_queue_size_allow_upgrade",
             "resource_governor_cpu_limit_mode", "resource_pool_statement_selector_function")


def _binding(q: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, label, unit in (("THREADS_PER_LEAF", "threads per leaf", ""), ("CONNECTIONS_PER_LEAF", "connections per leaf", ""),
                             ("LEAF_MEMORY", "leaf memory", "MB")):
        need, limit, used = q.get(key), q.get(f"{key}_LIMIT"), q.get(f"{key}_USED")
        local = q.get(f"{key}_USED (FROM LOCAL QUEUE)")
        lacking = None
        if need is not None and limit is not None and used is not None:
            lacking = float(need) > float(limit) - float(used)
        out.append({"resource": label, "unit": unit, "required": need, "limit": limit, "used": used,
                    "used_local_queue": local, "lacking": lacking})
    return out


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def collect_pools() -> dict[str, Any]:
    pools, status, queue, wlm, users, privs, gvars, nodes = db.parallel(
        _soft(lambda: _q(f"SELECT * FROM {IS}.RESOURCE_POOLS ORDER BY POOL_NAME")),
        _soft(lambda: _q(f"SELECT * FROM {IS}.MV_RESOURCE_POOL_STATUS")),
        _soft(lambda: _q(f"SELECT * FROM {IS}.WORKLOAD_MANAGEMENT_QUEUE ORDER BY START_TIME LIMIT 200")),
        _soft(lambda: _q(f"SELECT NODE_ID, TYPE, STAT, VALUE FROM {IS}.MV_WORKLOAD_MANAGEMENT_STATUS")),
        _soft(lambda: _q(f"SELECT USER, HOST, DEFAULT_RESOURCE_POOL FROM {IS}.USERS WHERE IS_DELETED = 0")),
        _soft(lambda: _q(f"SELECT GRANTEE, RESOURCE_POOL, PRIVILEGE_TYPE, IS_GRANTABLE FROM {IS}.RESOURCE_POOL_PRIVILEGES")),
        _soft(lambda: _q("SELECT LOWER(VARIABLE_NAME) AS name, VARIABLE_VALUE AS value FROM information_schema.GLOBAL_VARIABLES"
                         f" WHERE LOWER(VARIABLE_NAME) IN ({', '.join(['%s'] * len(_WLM_VARS))})", _WLM_VARS)),
        _soft(lambda: _q(f"SELECT ID, TYPE FROM {IS}.MV_NODES")),
    )
    if not _ok(pools):
        raise ValueError(f"Couldn't read information_schema.RESOURCE_POOLS: {pools['error']}")
    node_type = {r["ID"]: r["TYPE"] for r in _rows(nodes)}
    per_pool: dict[str, dict[str, Any]] = {}
    for r in _rows(status):
        p = per_pool.setdefault(r["POOL_NAME"], {"running": 0, "queued": 0, "killed": 0, "finished": 0, "queue_time_us": 0,
                                                 "memory_bytes": 0, "reported_nodes": 0, "nodes": 0, "per_node": []})
        p["nodes"] += 1
        reported = r["RUNNING_QUERIES"] is not None
        p["reported_nodes"] += 1 if reported else 0
        for k, col in (("running", "RUNNING_QUERIES"), ("queued", "QUEUEING_QUERIES"), ("killed", "KILLED_QUERIES"),
                       ("finished", "FINISHED_QUERIES"), ("queue_time_us", "TOTAL_QUEUE_TIME_US"), ("memory_bytes", "MEMORY_USAGE_BYTES")):
            p[k] += int(_num(r[col]))
        p["per_node"].append({"node_id": r["NODE_ID"], "node_type": node_type.get(r["NODE_ID"]), "running": r["RUNNING_QUERIES"],
                              "queued": r["QUEUEING_QUERIES"], "memory_bytes": r["MEMORY_USAGE_BYTES"],
                              "avg_queue_us": r["AVERAGE_QUEUE_TIME_US"], "cpu_mask": r["CPU_AFFINITY_MASK"]})
    gv = {r["name"]: r["value"] for r in _rows(gvars)}
    users_by_pool: dict[str, list[str]] = {}
    for u in _rows(users):
        users_by_pool.setdefault(u["DEFAULT_RESOURCE_POOL"] or "", []).append(f"{u['USER']}@{u['HOST']}")
    grants_by_pool: dict[str, list[dict[str, Any]]] = {}
    for r in _rows(privs):
        u, h = parse_grantee(r["GRANTEE"])
        grants_by_pool.setdefault(r["RESOURCE_POOL"], []).append({"user": f"{u}@{h}", "privilege": r["PRIVILEGE_TYPE"],
                                                                  "grantable": r["IS_GRANTABLE"] == "YES"})
    out_pools = []
    for p in _rows(pools):
        name = p["POOL_NAME"]
        st = per_pool.get(name)
        if st:
            st["avg_queue_ms"] = round(st["queue_time_us"] / st["finished"] / 1000, 2) if st["finished"] else None
        out_pools.append({
            "pool": name,
            "config": {k.lower(): p.get(k) for k in ("MEMORY_PERCENTAGE", "QUERY_MEMORY_PERCENTAGE", "QUERY_TIMEOUT", "MAX_CONCURRENCY",
                                                       "SOFT_CPU_LIMIT_PERCENTAGE", "HARD_CPU_LIMIT_PERCENTAGE", "MAX_QUEUE_DEPTH")},
            "status": st, "default_for": sorted(users_by_pool.get(name, [])), "grants": grants_by_pool.get(name, []),
        })
    stats: dict[str, dict[str, Any]] = {}
    for r in _rows(wlm):
        stats.setdefault(r["STAT"], {})[str(r["NODE_ID"])] = r["VALUE"]
    wlm_nodes = sorted({(r["NODE_ID"], r["TYPE"]) for r in _rows(wlm)})
    queue_rows = []
    for q in _rows(queue):
        queue_rows.append({"query": q.get("QUERY"), "start_time": q.get("START_TIME"), "query_type": q.get("QUERY_TYPE"),
                           "memory_type": q.get("MEMORY_TYPE"), "resources": _binding(q)})
    now = time.time()
    live = [n for n in out_pools if n["status"] and n["status"]["reported_nodes"]]  # NULL status: nothing to chart
    point = {"t": round(now, 1), "running": {n["pool"]: n["status"]["running"] for n in live},
             "queued": {n["pool"]: n["status"]["queued"] for n in live}, "wlm_queue": len(queue_rows)}
    with _pool_lock:
        _pool_history.append(point)
        while _pool_history and now - _pool_history[0]["t"] > _HISTORY_SECONDS:
            _pool_history.popleft()
        hist = list(_pool_history)
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pools": out_pools, "queue": queue_rows, "queue_error": None if _ok(queue) else queue["error"],
        "wlm": {"nodes": [{"id": i, "type": t} for i, t in wlm_nodes], "stats": stats,
                "error": None if _ok(wlm) else wlm["error"]},
        "variables": gv,
        "users_without_default": sorted(users_by_pool.get("", [])),
        "history": hist, "history_seconds": _HISTORY_SECONDS,
        "errors": {k: v["error"] for k, v in (("MV_RESOURCE_POOL_STATUS", status), ("USERS", users),
                                              ("RESOURCE_POOL_PRIVILEGES", privs)) if not _ok(v)},
    }


def pools_summary(d: dict[str, Any]) -> str:
    v = d["variables"]
    lines = [f"{len(d['pools'])} resource pool(s); global resource_pool = {v.get('resource_pool', '?')};"
             f" workload_management = {v.get('workload_management', '?')}."]
    for p in d["pools"]:
        c = {k: x for k, x in p["config"].items() if x is not None}
        st = p["status"] or {}
        state = ("status not reported" if not st or not st.get("reported_nodes") else
                 f"running {st['running']}, queued {st['queued']}, killed {st['killed']}, finished {st['finished']}"
                 f" (since node start), avg queue {st.get('avg_queue_ms')} ms, memory {st['memory_bytes']} B")
        lines.append(f"- {p['pool']}: {', '.join(f'{k}={x}' for k, x in c.items()) or 'no limits'}; {state};"
                     f" default for {len(p['default_for'])} user(s); {len(p['grants'])} USAGE grant(s)")
    lines.append(f"{len(d['queue'])} quer{'y' if len(d['queue']) == 1 else 'ies'} queued by the workload manager now.")
    for q in d["queue"][:15]:
        lacking = [r["resource"] for r in q["resources"] if r["lacking"]]
        lines.append(f"- since {q['start_time']} ({q['query_type']}): waiting for {', '.join(lacking) or 'unknown'}:"
                     f" {' '.join(str(q['query'] or '').split())[:140]}")
    if d["users_without_default"]:
        lines.append(f"{len(d['users_without_default'])} user(s) have no default pool (the global resource_pool applies).")
    return "\n".join(lines)
