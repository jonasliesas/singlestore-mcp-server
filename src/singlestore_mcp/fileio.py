"""Small state files written atomically and read safely on Windows.

On Windows a rename onto a file fails with PermissionError while anyone has the
target (or the new temp file) open: another request reading it, the second
server process (Claude's and the standalone workspace share the state folder),
or antivirus scanning a file that was just written - measured on the owner's
laptop: about 1 in 20 ``os.replace`` calls in %TEMP% with nobody else involved,
nearly every one while a reader polls the file. A read can fail the same way
while the file is being replaced. Both are brief, so they are retried.

A read that keeps failing raises instead of returning a default: callers like
the SQL Editor's tab list would otherwise take "unreadable" for "missing" and
start over, losing the user's tabs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

RETRIES = 12  # ~1.6 s in all, far longer than a scan or a concurrent read takes


def _pause(attempt: int) -> None:
    time.sleep(min(0.25, 0.02 * (attempt + 1)))


def replace(tmp: Path, path: Path) -> None:
    """``os.replace(tmp, path)``, retried while Windows reports the file as in use."""
    for attempt in range(RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == RETRIES - 1:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            _pause(attempt)


def write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (a temp file per process and thread, then a retried replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    replace(tmp, path)


def write_json(path: Path, value: Any, **dumps: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, **(dumps or {"separators": (",", ":")})))


def read_text(path: Path) -> str:
    """The file's text; FileNotFoundError when it doesn't exist; PermissionError retried, then raised."""
    for attempt in range(RETRIES):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except PermissionError:
            if attempt == RETRIES - 1:
                raise
            _pause(attempt)
    raise AssertionError("unreachable")


def read_json(path: Path, default: Any) -> Any:
    """Parsed JSON, or ``default`` when the file is missing or not valid JSON. Raises OSError when it
    exists but can't be read (still in use after the retries), so a caller never mistakes it for missing."""
    try:
        text = read_text(path)
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default
