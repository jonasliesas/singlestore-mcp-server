"""Entry point for the SingleStore MCP server.

Kept free of heavy imports: the process Claude starts is a small relay
(``supervisor.py``) that runs the real server (``server_impl.py``) as a child
process, so ``restart_server`` can replace it without dropping the
connection. Importing the MCP SDK alone takes ~2 s, so only the child pays it.

    singlestore-mcp-server              # console script
    python -m singlestore_mcp.server    # same

SINGLESTORE_MCP_NO_SUPERVISOR=1 runs the server in this process directly.
"""

from __future__ import annotations

import os
from typing import Any


def main() -> None:
    if os.environ.get("SINGLESTORE_MCP_SUPERVISED") or os.environ.get("SINGLESTORE_MCP_NO_SUPERVISOR"):
        from .server_impl import run_stdio

        run_stdio()
    else:
        from .supervisor import run

        run()


def __getattr__(name: str) -> Any:
    # ``from singlestore_mcp.server import mcp`` (and other names) keep working.
    from . import server_impl

    return getattr(server_impl, name)


if __name__ == "__main__":
    main()
