"""MCP Apps (interactive UIs) shipped by this server.

Importing this package registers every app's tools and ``ui://`` resources on
the shared ``apps`` extension, which server.py passes to ``MCPServer``.
"""

import importlib
import os
import sys
import traceback

from ._core import apps

_APP_MODULES = ("pipeline_monitor", "query_grid", "schema_explorer", "cluster_monitor", "sql_editor", "notebook")

for _name in _APP_MODULES:
    if os.environ.get("SINGLESTORE_MCP_DEV_SKIP_BROKEN_APPS"):
        # Dev host only: keep the other apps testable while one is mid-edit.
        try:
            importlib.import_module(f".{_name}", __name__)
        except Exception:
            print(f"[dev] app module {_name!r} failed to import and was skipped:", file=sys.stderr)
            traceback.print_exc()
    else:
        importlib.import_module(f".{_name}", __name__)

__all__ = ["apps"]
