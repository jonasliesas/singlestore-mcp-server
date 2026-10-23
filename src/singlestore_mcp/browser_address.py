"""A stable address (port + token) for the browser view, kept across server restarts.

A browser link is ``http://127.0.0.1:<port>/<token>/…``. Both used to be new in every server
process, so a tab left open across ``restart_server``, a crash restart or a Claude restart pointed
at a dead port. Now the address is kept in ``SINGLESTORE_MCP_HOME/browser/slot-<n>.json`` and the
next server process listens on it again, so the tab's next call reaches the new server.

Who owns which address: several servers share the home folder (Claude Desktop and Claude Code each
run one; tests use their own home). Each *running MCP client connection* owns one **slot**: the
long-lived process (the restart supervisor, or the server itself when it runs unsupervised) holds
an OS file lock on ``browser/slot-<n>.lock`` for its whole life and passes the slot number to the
server processes it starts (``SINGLESTORE_MCP_BROWSER_SLOT``). The servers of one supervisor
(after ``restart_server``, the standby, a crash restart) therefore reuse the same address, and a
second client takes the next free slot, never the first one's port. The OS drops the lock when the
process ends, so after a Claude restart the new supervisor takes the lowest free slot again. The
standalone workspace (``workspace_app``) keeps its own address in ``workspace.json``.

The token is a secret: never logged; the files are user-only where the OS allows (POSIX 0600 in a
0700 folder; on Windows the home folder is the user's own).

Stdlib only: the supervisor imports this.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import IO, Any

from . import fileio
from .paths import data_dir

ENV_SLOT = "SINGLESTORE_MCP_BROWSER_SLOT"
SLOTS = 8  # more MCP clients than this at once get a random address (not kept)

_held: dict[int, IO[bytes]] = {}  # slot -> open lock file (kept open: the lock lives as long as it does)


def _folder() -> Path:
    path = data_dir("browser")
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def _try_lock(path: Path) -> IO[bytes] | None:
    f = open(path, "a+b")  # noqa: SIM115 - kept open on purpose (holds the lock)
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def claim_slot() -> int | None:
    """Lock the first free slot for the rest of this process's life; None when all are taken."""
    if _held:
        return next(iter(_held))
    try:
        folder = _folder()
        for n in range(SLOTS):
            f = _try_lock(folder / f"slot-{n}.lock")
            if f is not None:
                _held[n] = f
                return n
    except OSError:
        pass
    return None


def release_slot() -> None:
    """Give up this process's slot (tests)."""
    for f in _held.values():
        try:
            f.close()
        except OSError:
            pass
    _held.clear()


def env_slot() -> int | None:
    try:
        n = int(os.environ.get(ENV_SLOT, ""))
    except ValueError:
        return None
    return n if 0 <= n < SLOTS else None


def new_token() -> str:
    return secrets.token_urlsafe(24)


def _load(path: Path) -> tuple[int, str] | None:
    try:
        data: Any = fileio.read_json(path, None)
    except OSError:
        return None
    if not isinstance(data, dict):
        return None
    port, token = data.get("port"), data.get("token")
    if isinstance(port, int) and 0 < port < 65536 and isinstance(token, str) and len(token) >= 16:
        return port, token
    return None


class SlotStore:
    """Reads and writes one slot's address."""

    def __init__(self, slot: int) -> None:
        self.slot = slot
        self.path = _folder() / f"slot-{slot}.json"

    def load(self) -> tuple[int, str] | None:
        return _load(self.path)

    def other_ports(self) -> set[int]:
        """The ports the other slots keep."""
        return {a[0] for p in self.path.parent.glob("slot-*.json") if p != self.path and (a := _load(p))}

    def save(self, port: int, token: str) -> None:
        fileio.write_text(self.path, json.dumps({"port": port, "token": token}))
        if os.name != "nt":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass


def store_for_this_process() -> SlotStore | None:
    """This server's slot: the one its supervisor holds, else one it locks itself (unsupervised)."""
    slot = env_slot()
    if slot is None and not os.environ.get("SINGLESTORE_MCP_SUPERVISED"):
        slot = claim_slot()  # unsupervised: this process is the long-lived one
    # Supervised without a slot (all taken): a random address, not kept.
    return SlotStore(slot) if slot is not None else None
