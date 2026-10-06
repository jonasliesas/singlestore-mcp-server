"""Where the server keeps its own state: the notebook Python environment, the
standalone workspace's state file and the in-app assistant's working folder.

Default: ``~/.singlestore-mcp`` (SINGLESTORE_MCP_HOME overrides it). Not
%LOCALAPPDATA%: Windows gives packaged apps such as the Claude desktop app a
private copy of that folder, so the server started by Claude and the one
started from the desktop shortcut would see different environments.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir(*parts: str) -> Path:
    base = Path(os.environ.get("SINGLESTORE_MCP_HOME") or Path.home() / ".singlestore-mcp").expanduser()
    path = base.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
