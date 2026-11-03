"""The hub's collector process (``python -m singlestore_mcp.collector``): the Studio repository for all users.

Started with the hub when STUDIO_COLLECTOR_PASSWORD (the service account's password, from a Secret) and a
SingleStore host (STUDIO_COLLECTOR_HOST, default STUDIO_DEFAULT_S2_HOST) are set. Its settings, the password among
them, go over its stdin; it listens on 127.0.0.1 only. Its folder is ``<data>/collector`` (the SQLite files and
their outbox: the buffer while the cluster is down).

Each Studio gets the collector's address, the cluster it watches and a token signed with this hub run's key
(``token_for``: the SAS user, the monitor group, Studio admin). A collector that exits is started again (after
5 s, then up to a minute between tries); the Studios keep their tokens, since the key stays.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any

from .. import collector as _col
from .audit import Audit
from .config import HubConfig

START_TIMEOUT_S = 60


class CollectorProcess:
    def __init__(self, cfg: HubConfig, audit: Audit) -> None:
        self.cfg = cfg
        self.audit = audit
        self.key = secrets.token_urlsafe(32).encode()
        self.port = 0
        self.source = ""
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._watch: threading.Thread | None = None
        self.on_ready: Any = None   # hub callback after each (re)start: running Studios get the new address

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}" if self.port else ""

    def home(self) -> str:
        path = self.cfg.data_dir / "collector"
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                path.chmod(0o700)
            except OSError:
                pass
        return str(path)

    def _settings(self) -> dict[str, Any]:
        s2 = self.cfg.collector_s2
        return {"key": self.key.decode(), "home": self.home(), "database": self.cfg.repository_db,
                "users_dir": str(self.cfg.users_dir()), "workloads_dir": str(self.cfg.data_dir / "shared" / "workloads"),
                "singlestore": {"host": s2["host"], "port": int(s2.get("port") or 3306), "user": self.cfg.collector_user,
                                "password": self.cfg.collector_password,
                                "ssl_disabled": str(s2.get("ssl_disabled", "")).lower() in ("1", "true", "yes", "on"),
                                "ssl_ca": s2.get("ssl_ca") or None}}

    def _env(self) -> dict[str, str]:
        keep = ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "VIRTUAL_ENV",
                "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "COMSPEC", "PATHEXT", "WINDIR", "PROGRAMDATA", "LOCALAPPDATA", "APPDATA")
        env = {k: v for k, v in os.environ.items() if k in keep}
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "HOME": self.home(), "USERPROFILE": self.home()})
        return env

    def _start_once(self) -> bool:
        log = open(os.path.join(self.home(), "collector.log"), "ab")  # noqa: SIM115 - the child writes to it
        cmd = [self.cfg.python or sys.executable, "-m", "singlestore_mcp.collector", "--stdin-config"]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=self._env(),
                                         cwd=self.home(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self.error = f"Couldn't start the collector: {exc}"
            return False
        finally:
            log.close()
        settings = self._settings()
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write((json.dumps(settings) + "\n").encode())
            self.proc.stdin.flush()
        except OSError:
            pass
        del settings
        found: dict[str, Any] = {}

        def read() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("COLLECTOR-READY "):
                    parts = line.split(" ", 2)
                    found.update(port=int(parts[1]), source=parts[2] if len(parts) > 2 else "")
                    break
            for _raw in self.proc.stdout:  # keep draining
                pass

        threading.Thread(target=read, name="collector-stdout", daemon=True).start()
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline and self.proc.poll() is None and "port" not in found:
            time.sleep(0.1)
        if "port" not in found:
            self.error = ("The collector didn't start in time." if self.proc.poll() is None
                          else f"The collector stopped while starting (exit {self.proc.returncode}); see collector.log.")
            self._kill()
            return False
        self.port, self.source, self.error = found["port"], found["source"], None
        self._ready.set()
        self.audit.record("collector_start", pid=self.proc.pid, source=self.source, database=self.cfg.repository_db)
        if self.on_ready is not None:
            threading.Thread(target=self.on_ready, name="collector-reseed", daemon=True).start()
        return True

    def start(self) -> None:
        """Start it and keep it running (in the background; ``wait_ready`` waits for the first start)."""
        def run() -> None:
            pause = 5.0
            while not self._stop.is_set():
                if self._start_once():
                    pause = 5.0
                    while not self._stop.is_set() and self.proc is not None and self.proc.poll() is None:
                        self._stop.wait(1.0)
                    if self._stop.is_set():
                        return
                    self.audit.record("collector_exited", code=self.proc.returncode if self.proc else None)
                    self.error = "The collector stopped; starting it again."
                else:
                    self.audit.record("collector_start_failed", reason=self.error)
                self._ready.clear()
                self.port = 0
                if self._stop.wait(pause):
                    return
                pause = min(60.0, pause * 2)

        self._watch = threading.Thread(target=run, name="collector-watch", daemon=True)
        self._watch.start()

    def wait_ready(self, timeout: float = START_TIMEOUT_S) -> bool:
        return self._ready.wait(timeout)

    def token_for(self, user: str, monitor: bool, admin: bool) -> str:
        return _col.sign(self.key, user, monitor, admin)

    def seed_for(self, user: str, monitor: bool, admin: bool) -> dict[str, Any] | None:
        """What a Studio gets (None while the collector isn't running: the Studio then keeps its own store)."""
        if not self.port:
            return None
        return {"url": self.url, "token": self.token_for(user, monitor, admin), "source": self.source}

    def status(self) -> dict[str, Any]:
        """For the admin page: running, the last sample / sync / flush and errors (no secrets)."""
        out: dict[str, Any] = {"running": bool(self.port), "error": self.error, "database": self.cfg.repository_db,
                               "source": self.source}
        if self.port:
            req = urllib.request.Request(f"{self.url}/status",
                                         headers={"Authorization": f"Bearer {_col.sign(self.key, 'hub', False, True, hub=True)}"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - 127.0.0.1
                    out.update(json.loads(resp.read()))
            except (OSError, ValueError) as exc:
                out["error"] = f"The collector didn't answer: {type(exc).__name__}"
        return out

    def _kill(self, grace: float = 5.0) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the collector stops when its stdin closes (and sends what is left)
            proc.wait(grace)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def stop(self) -> None:
        self._stop.set()
        self._kill(grace=10.0)
        self.port = 0
        self._ready.clear()
