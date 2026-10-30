"""Audit log of the hub: sign-ins, sign-outs, Studio starts and stops, admin actions.

JSON lines in ``<data>/hub/audit.jsonl`` and on stderr (picked up by the cluster's log collector). Only names,
addresses, reasons and counts - never a password, code, token, cookie or key. ``record`` drops any field
whose name looks like a secret, as a second line of defence.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

_SECRET_WORDS = ("token", "secret", "password", "code", "verifier", "cookie", "key", "authorization")
_lock = threading.Lock()


class Audit:
    def __init__(self, path: Path, echo: bool = True) -> None:
        self.path = path
        self.echo = echo
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
        for k, v in fields.items():
            if any(w in k.lower() for w in _SECRET_WORDS):
                continue
            entry[k] = v if isinstance(v, (int, float, bool)) or v is None else str(v)[:300]
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.echo:
            print(f"audit {line}", file=sys.stderr, flush=True)
        return entry

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
