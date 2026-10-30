"""Server mode: SAS SpeedyStore Studio as a web app on a shared server (no Claude, no stdio).

The hub (``singlestore_mcp.hub``) starts one Studio process per signed-in user (``studio_server``) with
SINGLESTORE_MCP_SERVER_MODE=1. This module holds the switches the rest of the code asks:

- ``enabled()``             - running as a server (not on the user's own laptop)
- ``notebooks_enabled()``   - Python notebooks (off in server mode unless SINGLESTORE_MCP_NOTEBOOKS=1)
- ``assistant_enabled()``   - the in-app Claude assistants (off in server mode unless SINGLESTORE_MCP_ASSISTANT=1)
- ``keyring_enabled()``     - the OS credential store; off in server mode (secrets go to the encrypted file store)
- ``secret_key()``          - the key of the user's encrypted secrets file, handed over by the hub (never on disk)
- ``sas_client_secret()``   - the secret of the hub's SAS Logon client (refreshing the user's Viya token)
- ``features()``            - the same switches for the apps' UI

Outside server mode every switch answers as before (notebooks, assistants and the keyring on), so the
normal Claude / stdio mode is unchanged. Stdlib only, cheap to import.
"""

from __future__ import annotations

import os
from typing import Any

_TRUE = ("1", "true", "yes", "on")
_state: dict[str, Any] = {"secret_key": None, "sas_client_secret": None, "user": None}


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE


def enabled() -> bool:
    return _flag("SINGLESTORE_MCP_SERVER_MODE", False)


def notebooks_enabled() -> bool:
    return not enabled() or _flag("SINGLESTORE_MCP_NOTEBOOKS", False)


def assistant_enabled() -> bool:
    return not enabled() or _flag("SINGLESTORE_MCP_ASSISTANT", False)


def keyring_enabled() -> bool:
    return _flag("SINGLESTORE_MCP_KEYRING", not enabled())


def set_secret_key(key: str | bytes | None) -> None:
    _state["secret_key"] = key.encode() if isinstance(key, str) else key


def secret_key() -> bytes | None:
    return _state["secret_key"]


def set_sas_client(client_id: str | None, secret: str | None) -> None:
    _state["sas_client_secret"] = (client_id, secret) if client_id and secret else None


def sas_client_secret(client_id: str) -> str | None:
    """The secret of SAS Logon client ``client_id``, when the hub handed one over (else a public client)."""
    pair = _state["sas_client_secret"]
    return pair[1] if pair and pair[0] == client_id else None


def set_user(user: str | None) -> None:
    _state["user"] = user


def user() -> str | None:
    return _state["user"]


NOTEBOOKS_OFF = "Notebooks are off on this server (SAS SpeedyStore Studio server pilot)."
ASSISTANT_OFF = "Ask Claude is off on this server (SAS SpeedyStore Studio server pilot)."
SIGN_IN_OFF = ("This sign-in opens a browser on the machine running Studio, so it isn't available on the server. "
               "Use a password or a pasted JWT for this connection.")


def features() -> dict[str, Any]:
    """What the apps should show (sent with the Workspace's opening result)."""
    return {"server": enabled(), "notebooks": notebooks_enabled(), "assistant": assistant_enabled(), "user": user(),
            "notes": {"notebooks": None if notebooks_enabled() else NOTEBOOKS_OFF,
                      "assistant": None if assistant_enabled() else ASSISTANT_OFF}}
