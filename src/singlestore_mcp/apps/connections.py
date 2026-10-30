"""Connections app: manage saved SingleStore connections and SAS Viya connections, and pick the active ones.

One SingleStore connection is active at a time; every app, Claude's tools and new
notebook kernels use it. Passwords never leave the server: the app sends them
when saving or testing, and only ever gets ``has_password`` back. Likewise one
SAS Viya connection is active (SAS cells in notebooks, SAS in the Workspace); its
tokens stay in the OS credential store and only "signed in as … until …" is shown.
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import connections, sas_viya, server_mode
from ._core import APP_ONLY, apps, browser_url, register_app, reset_browser_links, tool_result, with_browser_link

URI = "ui://singlestore/connections.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
CHANGES = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

register_app(URI, "connections.html", name="Connections", description="Saved SingleStore connections and the active one")


def _state() -> dict[str, Any]:
    act = connections.active()
    return {
        "active": act.name if act else None,
        "connections": [p.public() for p in connections.profiles()],
        "password_store": connections.password_store(),
        "server": server_mode.enabled(),  # on the server the hub owns the browser links (no "Reset browser links")
    }


def _summary(state: dict[str, Any]) -> str:
    lines = [f"Active connection: {state['active'] or '(none)'}"]
    for c in state["connections"]:
        where = c["url"] or f"{c['user']}@{c['host']}:{c['port']}"
        lines.append(f"- {c['name']}{' (active)' if c['name'] == state['active'] else ''}: {where}"
                     f"{f', database {c['database']}' if c['database'] else ''}")
    return "\n".join(lines)


@apps.tool(resource_uri=URI, title="SingleStore connections", annotations=READ_ONLY)
def connections_window() -> CallToolResult:
    """Open the Connections window: add, edit, test and switch between saved SingleStore connections.

    It's also the Connections view of the SingleStore Workspace. The result
    includes ``browser_url``: post it as a clickable link under the app.
    """
    state = _state()
    try:
        text = _summary(state) + "\n" + _sas_summary(_sas_state())
    except Exception:  # noqa: BLE001 - the SAS Viya list is extra here
        text = _summary(state)
    return tool_result(with_browser_link(text, state, "connections_window", {}), state)


# ------------------------------------------------------------------ app-only


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def connections_state() -> CallToolResult:
    """Saved connections, the active one and where passwords are stored."""
    state = _state()
    return tool_result(_summary(state), state)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def connection_save(profile: dict[str, Any], password: str | None = None, original_name: str | None = None,
                    activate: bool = False) -> CallToolResult:
    """Create or update a saved connection. ``password`` None keeps the stored one; "" removes it."""
    p = connections.save(profile, password, original_name)
    if activate:
        connections.activate(p.name)
    return tool_result(f"Saved {p.name}", _state())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def connection_delete(name: str) -> CallToolResult:
    """Delete a saved connection and its stored password."""
    connections.delete(name)
    return tool_result(f"Deleted {name}", _state())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def connection_test(profile: dict[str, Any] | None = None, password: str | None = None, name: str | None = None) -> CallToolResult:
    """Try a connection: a saved one by ``name``, or the settings from the form."""
    res = connections.test(profile, password, name)
    return tool_result("ok" if res["ok"] else res["error"], res)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def connection_activate(name: str) -> CallToolResult:
    """Make a saved connection the active one, and test it (token connections may need a sign-in first)."""
    connections.activate(name)
    check = connections.test(name=name)
    return tool_result(f"Active: {name}", {**_state(), "test": check})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def connection_sso_sign_in(name: str) -> CallToolResult:
    """Browser sign-in for SSO connections (Helios or Microsoft Entra ID): open the sign-in page in the user's
    browser and wait for the token (up to 2 min)."""
    info = connections.sso_sign_in(name)
    check = connections.test(name=name)
    return tool_result(f"Signed in for {name}", {**_state(), "token": info, "test": check})


# ------------------------------------------------------------------ SAS Viya connections
# Like the SingleStore ones: saved connections, one active. Tokens never leave the server; the app only gets
# who is signed in and until when. Nothing is saved as a side effect of signing in.


def _sas_state() -> dict[str, Any]:
    act = sas_viya.active_name()
    items = []
    for c in sas_viya.saved():
        try:
            info = sas_viya.token_info(c["name"])
        except Exception as exc:  # noqa: BLE001 - the credential store may fail; the list still shows
            info = {"signed_in": False, "problem": str(exc)[:200]}
        items.append({**c, **info, "active": c["name"] == act})
    return {"active": act, "connections": items, "password_store": connections.password_store()}


def _sas_summary(state: dict[str, Any]) -> str:
    lines = [f"Active SAS Viya connection: {state['active'] or '(none)'}"]
    for c in state["connections"]:
        lines.append(f"- {c['name']}{' (active)' if c['active'] else ''}: {c['url']}"
                     f"{', signed in as ' + c['user'] if c.get('signed_in') and c.get('user') else ''}")
    return "\n".join(lines)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_connections_state() -> CallToolResult:
    """Saved SAS Viya connections, the active one and each one's sign-in (from the stored token; no network)."""
    state = _sas_state()
    return tool_result(_sas_summary(state), state)


# (libref / auto_libname need no restart: the automatic library follows them before the next run)
_SESSION_KEYS = ("url", "compute_context", "cas_server", "startup_code", "ssl_verify", "client_id",
                 "sas9_signon", "sas9_server", "sas9_libraries")


def _reset_if_active_changes(name: str | None, values: dict[str, Any]) -> None:
    """End the Workspace's SAS sessions of connection ``name`` before its session settings change (so they're
    closed on the Viya they were started on, with its sign-in), or before it's renamed. Every connection has its
    own sessions (SQL Editor tabs can use one that isn't active)."""
    from .. import sas_rest

    if not name:
        return
    try:
        cur = sas_viya.settings(name)
    except LookupError:
        return
    renamed = bool(values.get("name")) and values.get("name") != name
    if renamed or any(k in values and values[k] is not None and values[k] != cur[k] for k in _SESSION_KEYS):
        sas_rest.reset(name)


def _prewarm_active(name: str | None) -> None:
    """Start the active SAS Viya connection's SAS session in the background (it was switched to, saved or signed in
    to); another connection's sessions stay lazy. Never fails the caller."""
    from .. import sas_prewarm

    try:
        if name and name == sas_viya.active_name():
            sas_prewarm.prewarm(name)
    except Exception:  # noqa: BLE001 - pre-warming is a convenience
        pass


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_connection_save(connection: dict[str, Any], original_name: str | None = None, activate: bool = False) -> CallToolResult:
    """Create or update a saved SAS Viya connection (name, Viya address, compute context, client ID, certificate
    check, SingleStore libref, CAS server, startup code). ``activate`` also makes it the active one."""

    _reset_if_active_changes(original_name, connection)
    c = sas_viya.save_connection(connection, original_name)
    if activate and sas_viya.active_name() != c["name"]:
        sas_viya.activate(c["name"])  # its own sessions: the other connection's keep running for tabs that use it
    _prewarm_active(c["name"])
    return tool_result(f"Saved {c['name']}", _sas_state())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def sas_connection_delete(name: str) -> CallToolResult:
    """Delete a saved SAS Viya connection and its sign-in."""
    from .. import sas_rest

    sas_viya.settings(name)  # LookupError before ending anything
    sas_rest.reset(name)
    sas_viya.delete_connection(name)
    return tool_result(f"Deleted {name}", _sas_state())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_connection_activate(name: str) -> CallToolResult:
    """Make a saved SAS Viya connection the active one: the Workspace's SAS views and new notebook kernels use it.
    Each connection has its own SAS sessions, so nothing is restarted (SQL Editor tabs pinned to the other
    connection keep theirs)."""
    if sas_viya.active_name() != name:
        sas_viya.settings(name)  # LookupError for an unknown name
        sas_viya.activate(name)
    _prewarm_active(name)
    return tool_result(f"Active SAS Viya connection: {name}", _sas_state())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_viya_state(name: str | None = None) -> CallToolResult:
    """Settings and sign-in status of a SAS Viya connection (default: the active one)."""
    info = sas_viya.status(name)
    return tool_result("signed in" if info.get("signed_in") else info.get("problem") or "not set up", info)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_viya_save(settings: dict[str, Any]) -> CallToolResult:
    """Update the active SAS Viya connection with the settings given (others stay; an empty address never
    blanks the saved one). Without any connection, an address creates one."""
    _reset_if_active_changes(sas_viya.active_name(), settings)
    sas_viya.save_settings(settings)
    _prewarm_active(sas_viya.active_name())
    return tool_result("saved", sas_viya.status())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_viya_sign_in(code: str | None = None, name: str | None = None) -> CallToolResult:
    """Sign in to a saved SAS Viya connection (default: the active one). Without ``code``: open SAS Logon in the
    browser. With ``code``: finish signing in with the code SAS Logon shows. Saves no settings."""
    if not code:
        return tool_result("Sign in in the browser, then paste the code.", sas_viya.sign_in_start(name))
    info = sas_viya.sign_in_finish(code, name)
    _prewarm_active(info.get("name"))
    return tool_result("Signed in to SAS Viya.", {**info, "state": _sas_state()})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_viya_sign_out(name: str | None = None) -> CallToolResult:
    """Forget the sign-in of a SAS Viya connection (default: the active one)."""
    from .. import sas_rest

    sas_rest.reset(name or sas_viya.active_name())
    sas_viya.sign_out(name)
    return tool_result("Signed out.", _sas_state())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_viya_test(name: str | None = None) -> CallToolResult:
    """Check a SAS Viya connection's sign-in and compute context (lists the compute contexts; starts no SAS
    session). Reuses the SAS Viya MCP server's or CLI's sign-in to the same Viya if there's no own one."""
    res = sas_viya.test(name)
    return tool_result("ok" if res["ok"] else res.get("error", "failed"), res)


HELIOS_CA_URL ="https://portal.singlestore.com/static/ca/singlestore_bundle.pem"


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def connection_helios_ca() -> CallToolResult:
    """Download SingleStore's CA bundle (needed for Helios / TLS) to ~/.singlestore-mcp once; returns its path."""
    import urllib.request

    from ..paths import data_dir

    path = data_dir() / "singlestore_bundle.pem"
    if not path.exists() or path.stat().st_size < 100:
        with urllib.request.urlopen(HELIOS_CA_URL, timeout=20) as res:
            pem = res.read()
        if b"BEGIN CERTIFICATE" not in pem:
            raise ValueError("The download from SingleStore didn't look like a certificate bundle.")
        path.write_bytes(pem)
    return tool_result(f"CA bundle at {path}", {"path": str(path)})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def browser_reset_links() -> CallToolResult:
    """Give the browser view a new secret token: every open browser tab and every browser link made before stops
    working (the tab shows "Session ended"). Restarts keep the token; this is the way to change it."""
    if not reset_browser_links():
        raise ValueError("This server has no browser view.")
    url = browser_url("connections_window", {})
    return tool_result("Browser links reset: open browser tabs show \"Session ended\"; get new links from Claude.",
                       {"reset": True, "browser_url": url})
