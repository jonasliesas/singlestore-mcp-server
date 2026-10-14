"""Security view: users, roles & groups, resource pools. Read-only (see ..security for the sources).

The model-visible tools (``security``, ``who_can_access``, ``user_privileges``, ``resource_pool_status``) all open
this view on the matching tab; the page loads each tab through one app-only tool (one ``db.parallel`` batch).
"""

from __future__ import annotations

from typing import Literal

from mcp.types import CallToolResult, ToolAnnotations

from .. import security as sec
from ._core import APP_ONLY, apps, register_app, tool_result, with_browser_link

URI = "ui://singlestore/security.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
Tab = Literal["users", "roles", "pools"]

register_app(
    URI,
    "security.html",
    name="Security",
    description="Users, groups, roles and effective privileges, who can access what, and resource pools / workload queue",
)

_TAB_SUMMARY = {"users": sec.users_summary, "roles": sec.roles_summary, "pools": sec.pools_summary}
_TAB_COLLECT = {"users": lambda: sec.collect_users(), "roles": sec.collect_roles, "pools": sec.collect_pools}


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
