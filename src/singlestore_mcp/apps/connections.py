"""Connections app: manage saved SingleStore connections and pick the active one.

One connection is active at a time; every app, Claude's tools and new
notebook kernels use it. Passwords never leave the server: the app sends them
when saving or testing, and only ever gets ``has_password`` back.
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
    return tool_result(with_browser_link(_summary(state), state, "connections_window", {}), state)


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


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_viya_state() -> CallToolResult:
    """SAS Viya settings and sign-in status (for SAS cells in notebooks)."""
    info = sas_viya.status()
    return tool_result("signed in" if info.get("signed_in") else info.get("problem") or "not set up", info)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_viya_save(settings: dict[str, Any]) -> CallToolResult:
    """Save the SAS Viya address, compute context, certificate check and SingleStore libref."""
    sas_viya.save_settings(settings)
    info = sas_viya.status()
    return tool_result("saved", info)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_viya_sign_in(code: str | None = None) -> CallToolResult:
    """Without ``code``: open SAS Logon in the browser. With ``code``: finish signing in with the code SAS Logon shows."""
    if not code:
        return tool_result("Sign in in the browser, then paste the code.", sas_viya.sign_in_start())
    return tool_result("Signed in to SAS Viya.", sas_viya.sign_in_finish(code))


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=CHANGES)
def sas_viya_sign_out() -> CallToolResult:
    """Forget the SAS Viya sign-in."""
    sas_viya.sign_out()
    return tool_result("Signed out.", sas_viya.status())


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sas_viya_test() -> CallToolResult:
    """Check the SAS Viya sign-in and compute context (lists the compute contexts; starts no SAS session)."""
    res = sas_viya.test()
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
