"""Security view: users, roles & groups, resource pools, audit log (see ..security for the sources).

The model-visible tools (``security``, ``who_can_access``, ``user_privileges``, ``resource_pool_status``) all open
this view on the matching tab and only read; the page loads each tab through one app-only tool (one ``db.parallel``
batch). Changes (v2, ..security_admin) go through two app-only tools: ``security_change_plan`` (read-only preview)
and ``security_change_apply`` (refuses without ``confirm=True``, and without the typed name where the plan asks).
``security_change_script`` (read-only) writes changes out as a .sql script for a DBA instead of running them.
"""

from __future__ import annotations

from typing import Literal

from mcp.types import CallToolResult, ToolAnnotations

from .. import security as sec
from .. import security_admin as adm
from ._core import APP_ONLY, apps, register_app, tool_result, with_browser_link

URI = "ui://singlestore/security.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
Tab = Literal["users", "roles", "pools", "audit"]
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

register_app(
    URI,
    "security.html",
    name="Security",
    description=("Users, groups, roles and effective privileges, who can access what, resource pools / workload queue and"
                 " audit log settings; previewed, confirmed changes for users with the right privileges"),
)


def _audit_summary(d: dict) -> str:
    v = d["variables"]
    head = (f"Audit logging is {'ON' if d['enabled'] else 'OFF'} (auditlog_level = {d['level'] or '?'}; files in "
            f"{v.get('auditlogsdir', '?')} on each node). SingleStore has no SQL view of the audit log, so it isn't shown here.")
    return "\n".join([head, *d["notes"]])


_TAB_SUMMARY = {"users": sec.users_summary, "roles": sec.roles_summary, "pools": sec.pools_summary, "audit": _audit_summary}
_TAB_COLLECT = {"users": lambda: sec.collect_users(), "roles": sec.collect_roles, "pools": sec.collect_pools,
                "audit": adm.audit_status}


@apps.tool(resource_uri=URI, title="Security", annotations=READ_ONLY)
def security(tab: Tab = "users") -> CallToolResult:
    """Open the Security view of the SingleStore cluster (read-only).

    Tabs: "users" (every user with type, lock status, password age and expiry, default resource pool, groups,
    connections and risk flags such as ALL/SUPER on *.*, host '%', password never expires, locked, unused),
    "roles" (user -> group -> role -> privileges, and "who can access" a database or table) and "pools"
    (resource pool limits and live status, the workload manager queue with the limit each queued query waits
    for, and which users use which pool). Nothing is changed: where a change would help, the view shows SQL
    the user can copy.

    The result includes ``browser_url``, which opens this view full-window in the user's browser: post it
    as a clickable link right under the app.
    """
    data = _TAB_COLLECT[tab]()
    payload = {"kind": "tab", "tab": tab, "data": data}
    summary = with_browser_link(_TAB_SUMMARY[tab](data), payload, "security", {"tab": tab})
    return tool_result(summary, payload)


@apps.tool(resource_uri=URI, annotations=READ_ONLY)
def who_can_access(database: str, table: str | None = None) -> CallToolResult:
    """Every user who has privileges on a database (or one table in it), and how they got them.

    "How" is "direct" (granted to the user), "via role R ← group G" (user -> group -> role), on the database /
    table itself or globally on *.*. Administrative global privileges that don't open data (CREATE USER,
    PROCESS, ...) and USAGE are left out. Read-only. Opens the Security view on this lookup.

    Args:
        database: Database name (exact).
        table: Optional table in that database.
    """
    data = sec.who_can_access(database, table)
    payload = {"kind": "who", "tab": "roles", "data": data}
    summary = with_browser_link(sec.who_summary(data), payload, "security", {"tab": "roles"})
    return tool_result(summary, payload)


@apps.tool(resource_uri=URI, annotations=READ_ONLY)
def user_privileges(user: str, host: str | None = None) -> CallToolResult:
    """Effective privileges of one SingleStore user with the reason for each (direct, or via role ← group),
    its groups and roles, resource pool grants, and SHOW GRANTS (password hash hidden). Read-only.

    Args:
        user: User name.
        host: Host part ('%' etc.). Needed only when the name exists for several hosts.
    """
    data = sec.user_privileges(user, host)
    payload = {"kind": "user", "tab": "users", "data": data}
    summary = with_browser_link(sec.user_privileges_summary(data), payload, "security", {"tab": "users"})
    return tool_result(summary, payload)


@apps.tool(resource_uri=URI, annotations=READ_ONLY)
def resource_pool_status() -> CallToolResult:
    """Resource pools now: limits per pool, running / queued queries summed over nodes, the workload manager
    queue with the resource each queued query is waiting for (threads, connections or leaf memory), and the
    users whose default pool it is. Read-only. Opens the Security view on the Resource pools tab."""
    data = sec.collect_pools()
    data.pop("history", None)
    payload = {"kind": "tab", "tab": "pools", "data": data}
    summary = with_browser_link(sec.pools_summary(data), payload, "security", {"tab": "pools"})
    return tool_result(summary, payload)


# ----------------------------------------------------------------------------- app-only tools for the page

@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_users(unused_days: int = 90) -> CallToolResult:
    """Users tab data for the Security view."""
    data = sec.collect_users(unused_days)
    return tool_result(f"{len(data['users'])} users", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_user_detail(user: str, host: str) -> CallToolResult:
    """Effective privileges of one user, for the Users tab."""
    data = sec.user_privileges(user, host)
    return tool_result(f"{len(data['effective'])} privileges", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_roles() -> CallToolResult:
    """Roles & groups tab data for the Security view."""
    data = sec.collect_roles()
    return tool_result(f"{len(data['groups'])} groups, {len(data['roles'])} roles", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_who(database: str, table: str | None = None) -> CallToolResult:
    """Reverse lookup ("who can access") for the Roles & groups tab."""
    data = sec.who_can_access(database, table or None)
    return tool_result(f"{data['users_with_access']} users", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_tables(database: str) -> CallToolResult:
    """Tables of one database, for the "who can access" picker."""
    tables = sec.list_tables(database)
    return tool_result(f"{len(tables)} tables", {"tables": tables})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_pools() -> CallToolResult:
    """Resource pools tab data (with the per-pool history kept while the view refreshes)."""
    data = sec.collect_pools()
    return tool_result(f"{len(data['pools'])} pools", data)


# ----------------------------------------------------------------------------- v2: changes (preview + confirmation)

@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_admin_capabilities() -> CallToolResult:
    """What the connected user may change in the Security view (and why not), plus the privilege and pool-setting
    lists for the pickers. Read-only."""
    data = adm.capabilities(adm.load_context())
    allowed = [k for k, v in data.items() if isinstance(v, dict) and v.get("ok")]
    return tool_result(f"Allowed: {', '.join(allowed) or 'nothing'}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_change_plan(action: dict, pending: list[dict] | None = None) -> CallToolResult:
    """Read-only preview of one change: the exact statements (any password masked), warnings, blockers (lock-out,
    last admin, missing objects) and whether the user must type the object's name to confirm. Runs nothing.
    Works without the change privileges (``allowed`` says whether the connected user may run it).

    Args:
        action: {"action": one of create_user, drop_user, grant, revoke, revoke_all, create_role, drop_role,
            create_group, drop_group, grant_role, revoke_role, grant_group, revoke_group, create_pool, alter_pool,
            drop_pool, set_default_pool, grant_pool, revoke_pool, change_password, lock_user, unlock_user,
            set_login_policy, set_require, grant_view, plus its fields: user, host, role, group,
            grantee_type ("user"/"role"), privileges, database, table, with_grant_option, grant_option, pool,
            settings, default_pool, failed_login_attempts, password_lock_time (seconds; both 0 = off),
            require ("SSL"/"NONE"), columns and view (grant_view: a view with these columns of database.table,
            SELECT granted on it – 9.0 has no column-level grants)}. Never a password.
        pending: The changes already in the script basket (same shape), for a change that needs objects they
            create (the plan then says ``needs_script``).
    """
    data = adm.plan({k: v for k, v in (action or {}).items() if k != "password"}, pending)
    return tool_result(adm.summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_change_script(actions: list[dict], keep_order: bool = False) -> CallToolResult:
    """Read-only: one or more changes as a SQL script for review (e.g. by a DBA), instead of running them. The
    statements are what Run would execute, rebuilt from the cluster state now (each change checked as if the
    earlier ones had run), in a safe order (creates, grants, revokes, drops) unless ``keep_order``, with a header
    (connection, connected user, time, one line per change) and each change's warnings as ``-- WARNING:``
    comments. Runs nothing and needs none of the change privileges. Never takes or writes a password: CREATE USER
    and a password change get ``'<set password>'`` to fill in.

    Args:
        actions: The changes, each the same object as for security_change_plan.
        keep_order: Keep the given order instead of the safe order.
    """
    data = adm.script(list(actions or []), keep_order)
    return tool_result(data["script"], data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=WRITE)
def security_change_apply(action: dict, confirm: bool = False, typed_confirmation: str | None = None,
                          password: str | None = None, update_connection: bool = False) -> CallToolResult:
    """Run one previewed change (users, roles, groups, grants, resource pools) on the cluster.

    Refuses unless ``confirm`` is true: call it only after the user saw the plan (security_change_plan) and
    explicitly confirmed. Where the plan asks for it (DROP, revoke all, the connected user or an admin user),
    ``typed_confirmation`` must be the name the user typed. The plan is rebuilt from fresh cluster state first,
    and blockers (lock-out, last admin) refuse the change. ``password`` (create_user, change_password) is sent as a
    bound parameter and never returned, logged or stored.

    Args:
        action: The same action object as for security_change_plan.
        confirm: Must be true: the user's explicit confirmation.
        typed_confirmation: The object name the user typed, when the plan requires it.
        password: The new user's password (create_user) or the new password (change_password).
        update_connection: Changing your own password: after it succeeded, also save the new password for the
            active saved connection in the OS credential store.
    """
    data = adm.apply({k: v for k, v in (action or {}).items() if k != "password"}, confirm, typed_confirmation, password,
                     bool(update_connection))
    return tool_result(adm.summary(data), data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_columns(database: str, table: str) -> CallToolResult:
    """Columns of one table, for the column picker of "Grant columns (through a view)". Read-only."""
    cols = sec.list_columns(database, table)
    return tool_result(f"{len(cols)} columns", {"columns": cols})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def security_audit() -> CallToolResult:
    """Audit tab data: audit log settings and how to turn audit logging on. Read-only."""
    data = adm.audit_status()
    return tool_result(_audit_summary(data), data)
