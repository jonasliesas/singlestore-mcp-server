"""The Studio repository's copy of what users keep in files: each user's alerts and the shared workloads with their runs.

The files stay what the Studios read and write (``alerts`` and ``workload`` work the same on the desktop and with the
cluster away); the collector mirrors them to the repository database once a minute (``FileMirror.sync``):

- **Changed rows only.** A digest of each row's ``doc`` (the record as JSON) says what was sent, seeded from the
  database when the collector starts. While the cluster is down the files simply wait, and the next sync catches up.
- **Deletes follow, but a missing file never deletes.** An alert or workload gone from a file that is there and could
  be read is deleted from the database. A user's missing ``alerts.json``, a missing ``workloads.json`` or a file being
  written deletes nothing, so a lost volume can't empty the repository.
- **Restore** at the first contact with the database: a user without an ``alerts.json`` gets it back (rules,
  settings, watermarks and alerts), and a workload folder that was empty when the collector started gets its workloads
  and runs back. Nothing is overwritten.

Besides ``doc``, the tables have columns for SQL and SAS readers; times are DATETIME(6) in UTC. Their sizes are limited by
the stores themselves (``alerts.MAX_ALERTS`` per user, ``workload.MAX_RUNS_KEPT`` per workload), so they have no retention.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .repository import _V, Repository, Table

_BIN = "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"   # workload names differ by case
_T = "DATETIME(6)"
ALERTS = Table("alerts", "", ("user", "alert_id"), (
    ("user", f"{_V} NOT NULL"), ("alert_id", "VARCHAR(32) NOT NULL"), ("kind", _V), ("severity", "VARCHAR(16)"), ("title", "TEXT"),
    ("detail", "TEXT"), ("count", "BIGINT"), ("first_seen", _T), ("last_seen", _T), ("ended", _T), ("acked", _T),
    ("active", "TINYINT"), ("source", _V), ("doc", "LONGTEXT NOT NULL"), ("updated", f"{_T} NOT NULL")), sort="last_seen")
ALERT_STATE = Table("alert_state", "", ("user",), (
    ("user", f"{_V} NOT NULL"), ("doc", "LONGTEXT NOT NULL"), ("updated", f"{_T} NOT NULL")), sort="user")
WORKLOADS = Table("workloads", "", ("name",), (
    ("name", f"{_BIN} NOT NULL"), ("doc", "LONGTEXT NOT NULL"), ("updated", f"{_T} NOT NULL")), sort="name")
WORKLOAD_RUNS = Table("workload_runs", "", ("id",), (
    ("id", "VARCHAR(40) NOT NULL"), ("workload", _BIN), ("label", "TEXT"), ("state", "VARCHAR(16)"), ("started", _T), ("finished", _T),
    ("elapsed_s", "DOUBLE"), ("doc", "LONGTEXT NOT NULL"), ("updated", f"{_T} NOT NULL")), sort="started")
FILE_TABLES: tuple[Table, ...] = (ALERTS, ALERT_STATE, WORKLOADS, WORKLOAD_RUNS)

_RUN_ID = re.compile(r"^[\w\-]{1,40}$")
_USER_FOLDER = re.compile(r"^[a-z0-9_-][a-z0-9._-]{0,63}$")   # what hub.spawner.safe_name gives
_STATE_KEYS = ("rules", "settings", "watermarks")


def _when(value: Any) -> str | None:
    """An epoch time from a file as a UTC DATETIME string."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return _dt.datetime.fromtimestamp(value, _dt.timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    except (OverflowError, OSError, ValueError):
        return None


def _doc(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(doc: str) -> str:
    return hashlib.sha256(doc.encode("utf-8", "surrogatepass")).hexdigest()


def _read(path: Path) -> Any:
    """A JSON file, or None while it can't be read (being written: next sync)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class FileMirror:
    def __init__(self, repo: Repository, users_dir: str | Path | None, workloads_dir: str | Path | None) -> None:
        self.repo = repo
        self.users = Path(users_dir) if users_dir else None
        self.workloads = Path(workloads_dir) if workloads_dir else None
        self._was_empty = self.workloads is not None and not (self.workloads / "workloads.json").exists() \
            and not any((self.workloads / "runs").glob("*.json"))
        self._sent: dict[str, dict[tuple[Any, ...], str]] | None = None   # table -> key -> digest of its doc
        self._lock = threading.Lock()
        self.status: dict[str, Any] = {"last_sync": None, "last_error": None, "alerts": None, "workloads": None, "runs": None,
                                       "restored": None}

    # ------------------------------------------------------------ first contact: what is there, and restore
    def _contact(self) -> None:
        self.repo.ensure_schema()
        stored = {t.name: self.repo.read_table(t) for t in FILE_TABLES}
        restored = self._restore(stored)
        self.status["restored"] = {"at": round(time.time(), 1), **restored} if any(restored.values()) else None
        self._sent = {t.name: {tuple(r[k] for k in t.pk): _digest(r["doc"]) for r in stored[t.name]} for t in FILE_TABLES}

    def _restore(self, stored: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        out = {"alert_files": 0, "workloads": 0, "runs": 0}
        if self.users is not None:
            out["alert_files"] = self._restore_alerts(stored)
        if self.workloads is not None and self._was_empty:
            out["workloads"], out["runs"] = self._restore_workloads(stored)
            self._was_empty = False
        return out

    def _restore_alerts(self, stored: dict[str, list[dict[str, Any]]]) -> int:
        from . import fileio

        states = {r["user"]: r["doc"] for r in stored[ALERT_STATE.name]}
        alerts: dict[str, list[dict[str, Any]]] = {}
        for r in stored[ALERTS.name]:
            alerts.setdefault(r["user"], []).append(json.loads(r["doc"]))
        n = 0
        for user in sorted({*states, *alerts}):
            path = self.users / user / "state" / "alerts.json"
            if not _USER_FOLDER.match(user) or path.exists():
                continue
            self.users.mkdir(mode=0o700, parents=True, exist_ok=True)  # a new volume has no users folder yet
            for folder in (self.users / user, path.parent):
                folder.mkdir(mode=0o700, exist_ok=True)
            # The list runs oldest to newest, as alerts._record keeps it.
            mine = sorted(alerts.get(user, []), key=lambda a: a.get("last_seen") or 0)
            fileio.write_text(path, json.dumps({**json.loads(states.get(user) or "{}"), "alerts": mine}, indent=1, default=str))
            n += 1
        return n

    def _restore_workloads(self, stored: dict[str, list[dict[str, Any]]]) -> tuple[int, int]:
        from . import workload

        defs = {r["name"]: json.loads(r["doc"]) for r in stored[WORKLOADS.name]}
        added = 0
        if defs:
            with workload._editing():  # noqa: SLF001 - the Studios' lock on the shared folder
                current = workload._load_all()  # noqa: SLF001
                missing = {k: v for k, v in defs.items() if k not in current}
                if missing:
                    workload._save_all({**current, **missing})  # noqa: SLF001
                added = len(missing)
        runs = self.workloads / "runs"
        n = 0
        for r in stored[WORKLOAD_RUNS.name]:
            path = runs / f"{r['id']}.json"
            if _RUN_ID.match(r["id"]) and not path.exists():
                runs.mkdir(parents=True, exist_ok=True)
                path.write_text(r["doc"], encoding="utf-8")
                n += 1
        return added, n

    # ------------------------------------------------------------ the sync
    def _apply(self, t: Table, wanted: dict[tuple[Any, ...], dict[str, Any]], owned: Any = None) -> None:
        """Send the rows that changed and delete the ones gone. ``owned(key)``: which stored rows this sync speaks for
        (default: all of them)."""
        sent = self._sent[t.name]
        now = _when(time.time())
        changed = {k: r for k, r in wanted.items() if sent.get(k) != _digest(r["doc"])}
        if changed:
            self.repo._upsert(t, [[{**r, "updated": now}.get(c) for c in t.names] for r in changed.values()])  # noqa: SLF001
            sent.update({k: _digest(r["doc"]) for k, r in changed.items()})
        gone = [k for k in sent if k not in wanted and (owned is None or owned(k))]
        if gone:
            self.repo.delete_rows(t, gone)
            for k in gone:
                del sent[k]

    def _sync_alerts(self) -> None:
        if self.users is None or not self.users.is_dir():
            return
        alerts: dict[tuple[Any, ...], dict[str, Any]] = {}
        states: dict[tuple[Any, ...], dict[str, Any]] = {}
        users: set[str] = set()
        for path in self.users.glob("*/state/alerts.json"):
            data = _read(path)
            if not isinstance(data, dict):
                continue
            user = path.parent.parent.name
            users.add(user)
            states[(user,)] = {"user": user, "doc": _doc({k: data.get(k) for k in _STATE_KEYS})}
            for a in data.get("alerts") or []:
                if isinstance(a, dict) and a.get("id") and a.get("key"):
                    alerts[(user, str(a["id"]))] = {
                        "user": user, "alert_id": str(a["id"]), "doc": _doc(a), "active": 1 if a.get("active") else 0,
                        **{k: a.get(k) for k in ("kind", "severity", "title", "detail", "count", "source")},
                        **{k: _when(a.get(k)) for k in ("first_seen", "last_seen", "ended", "acked")}}
        self._apply(ALERTS, alerts, lambda k: k[0] in users)
        self._apply(ALERT_STATE, states, lambda k: k[0] in users)
        self.status["alerts"] = len(alerts)

    def _sync_workloads(self) -> None:
        if self.workloads is None:
            return
        data = _read(self.workloads / "workloads.json")
        if isinstance(data, dict):
            defs = {(w["name"],): {"name": w["name"], "doc": _doc(w)} for w in data.get("workloads") or []
                    if isinstance(w, dict) and w.get("name")}
            self._apply(WORKLOADS, defs)
            self.status["workloads"] = len(defs)
        runs: dict[tuple[Any, ...], dict[str, Any]] = {}
        unread: set[str] = set()
        folder = self.workloads / "runs"
        if not folder.is_dir():
            return
        for path in folder.glob("*.json"):
            if not _RUN_ID.match(path.stem):
                continue
            d = _read(path)
            if not isinstance(d, dict):
                unread.add(path.stem)  # being written: its row stays
                continue
            elapsed = d.get("elapsed_s")
            runs[(path.stem,)] = {
                "id": path.stem, "workload": d.get("workload"), "label": d.get("label"), "state": d.get("state"),
                "started": _when(d.get("started")), "finished": _when(d.get("finished")),
                "elapsed_s": elapsed if isinstance(elapsed, (int, float)) else None, "doc": _doc(d)}
        self._apply(WORKLOAD_RUNS, runs, lambda k: k[0] not in unread)
        self.status["runs"] = len(runs)

    def sync(self) -> None:
        """Raises when the repository can't be reached (again at the next sync; the files are the buffer)."""
        with self._lock:
            try:
                if self._sent is None:
                    self._contact()
                self._sync_alerts()
                self._sync_workloads()
            except Exception as exc:
                self.status["last_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                raise
            self.status.update(last_sync=round(time.time(), 1), last_error=None)
