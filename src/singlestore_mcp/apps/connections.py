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

from .. import connections, sas_viya
from ._core import APP_ONLY, apps, register_app, tool_result, with_browser_link

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


_SESSION_KEYS = ("url", "compute_context", "cas_server", "startup_code", "ssl_verify", "client_id")


def _reset_if_active_changes(name: str | None, values: dict[str, Any]) -> None:
    """End the Workspace's SAS sessions before connection ``name``'s session settings change, if it's the active
    one (so they're closed on the Viya they were started on, with its sign-in)."""
    from .. import sas_rest

    act = sas_viya.active_name()
    if not act or name != act:
        return
    cur = sas_viya.settings(act)
    if any(k in values and values[k] is not None and values[k] != cur[k] for k in _SESSION_KEYS):
        sas_rest.reset()


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_connection_save(connection: dict[str, Any], original_name: str | None = None, activate: bool = False) -> CallToolResult:
    """Create or update a saved SAS Viya connection (name, Viya address, compute context, client ID, certificate
    check, SingleStore libref, CAS server, startup code). ``activate`` also makes it the active one."""
    from .. import sas_rest

    _reset_if_active_changes(original_name, connection)
    c = sas_viya.save_connection(connection, original_name)
    if activate and sas_viya.active_name() != c["name"]:
        sas_rest.reset()
        sas_viya.activate(c["name"])
    return tool_result(f"Saved {c['name']}", _sas_state())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def sas_connection_delete(name: str) -> CallToolResult:
    """Delete a saved SAS Viya connection and its sign-in."""
    from .. import sas_rest

    if sas_viya.active_name() == name:
        sas_rest.reset()
    sas_viya.delete_connection(name)
    return tool_result(f"Deleted {name}", _sas_state())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_connection_activate(name: str) -> CallToolResult:
    """Make a saved SAS Viya connection the active one: the Workspace's SAS sessions restart on it, and new
    notebook kernels use it."""
    from .. import sas_rest

    if sas_viya.active_name() != name:
        sas_viya.settings(name)  # LookupError before ending any session
        sas_rest.reset()
        sas_viya.activate(name)
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
    return tool_result("saved", sas_viya.status())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_viya_sign_in(code: str | None = None, name: str | None = None) -> CallToolResult:
    """Sign in to a saved SAS Viya connection (default: the active one). Without ``code``: open SAS Logon in the
    browser. With ``code``: finish signing in with the code SAS Logon shows. Saves no settings."""
    if not code:
        return tool_result("Sign in in the browser, then paste the code.", sas_viya.sign_in_start(name))
    info = sas_viya.sign_in_finish(code, name)
    return tool_result("Signed in to SAS Viya.", {**info, "state": _sas_state()})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_viya_sign_out(name: str | None = None) -> CallToolResult:
    """Forget the sign-in of a SAS Viya connection (default: the active one)."""
    from .. import sas_rest

    if name is None or name == sas_viya.active_name():
        sas_rest.reset()
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
