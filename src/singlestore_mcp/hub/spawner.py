"""One Studio process per signed-in user (``python -m singlestore_mcp.studio_server --stdin-config``).

Each user gets a folder ``<data>/users/<safe id>/`` with
- ``state/`` - SINGLESTORE_MCP_HOME: connections, encrypted secrets, editor tabs, history, workloads, logs
- ``files/`` - HOME: what the file browser shows as the user's own files (SQL folder inside)

The process listens on 127.0.0.1 on a port it picks, under a random path token; both are known only to the hub.
Its settings (token, the user's secrets key, the SAS Logon client secret, the fresh Viya tokens) go over its
stdin, never into its environment or command line. Closing stdin stops it; the hub kills it if it doesn't go.

Processes stop when idle (no request but the pages' heartbeats for ``idle_minutes``), at sign-out, from the
admin page, or when the hub stops. At most ``max_users`` run at once.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import Audit
from .config import HubConfig

PUBLIC_BASE = "/studio/"


class StudioFull(RuntimeError):
    """``max_users`` Studios run already."""


class StudioFailed(RuntimeError):
    """The Studio process didn't start (its log is in the user's state folder)."""


def safe_name(user: str) -> str:
    """A folder name for a SAS user id: the id itself when it's plain, else a readable part + a hash."""
    low = user.strip().lower()
    plain = re.sub(r"[^a-z0-9._-]", "_", low).strip("._") or "user"
    if plain == low and len(plain) <= 64:
        return plain
    return f"{plain[:40]}-{hashlib.sha256(low.encode()).hexdigest()[:10]}"


@dataclass
class Studio:
    user: str
    port: int = 0
    token: str = ""
    proc: subprocess.Popen | None = None
    started: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    ready: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    stopping: bool = False

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def upstream(self, rest: str) -> str:
        return f"/{self.token}/{rest}"


def rss_bytes(pid: int | None) -> int | None:
    """Resident memory of a process (Linux /proc, Windows psapi), or None."""
    if not pid:
        return None
    try:
        if sys.platform.startswith("linux"):
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
            return None
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
            if not handle:
                return None
            try:
                pmc = PMC()
                pmc.cb = ctypes.sizeof(PMC)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    return int(pmc.WorkingSetSize)
            finally:
                kernel32.CloseHandle(handle)
    except (OSError, ValueError, AttributeError):
        return None
    return None


class Spawner:
    def __init__(self, cfg: HubConfig, audit: Audit) -> None:
        self.cfg = cfg
        self.audit = audit
        self._studios: dict[str, Studio] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    # ------------------------------------------------------------ folders
    def user_dirs(self, user: str) -> tuple[Path, Path]:
        base = self.cfg.users_dir() / safe_name(user)
        state, files = base / "state", base / "files"
        for p in (base, state, files, files / "SQL"):
            p.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                try:
                    p.chmod(0o700)
                except OSError:
                    pass
        return state, files

    def _env(self, user: str) -> dict[str, str]:
        state, files = self.user_dirs(user)
        keep = ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "VIRTUAL_ENV",
                "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE",
                "REQUESTS_CA_BUNDLE", "COMSPEC", "PATHEXT", "WINDIR", "PROGRAMDATA", "LOCALAPPDATA", "APPDATA")
        env = {k: v for k, v in os.environ.items() if k in keep}
        env.update({
            "SINGLESTORE_MCP_SERVER_MODE": "1",
            "SINGLESTORE_MCP_HOME": str(state),
            "HOME": str(files), "USERPROFILE": str(files),
            "SINGLESTORE_MCP_SQL_DIR": str(files / "SQL"),
            "SINGLESTORE_STUDIO_BIND": "127.0.0.1", "SINGLESTORE_STUDIO_PORT": "0",
            "SINGLESTORE_MCP_NOTEBOOKS": "1" if self.cfg.notebooks else "0",
            "SINGLESTORE_MCP_ASSISTANT": "1" if self.cfg.assistant else "0",
            "SINGLESTORE_MCP_METRICS": "1" if self.cfg.metrics else "0",
            "SINGLESTORE_MCP_SAS_IMPORT": "0",
            "SINGLESTORE_MCP_FILE_ROOTS": "",
            "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        })
        if os.environ.get("SINGLESTORE_MCP_SAS_PREWARM"):
            env["SINGLESTORE_MCP_SAS_PREWARM"] = os.environ["SINGLESTORE_MCP_SAS_PREWARM"]
        if self.cfg.assistant:
            # Ask Claude switched on: the organisation's Anthropic API key or Microsoft Foundry settings (from a Secret).
            for k, v in os.environ.items():
                if k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_FOUNDRY", "SINGLESTORE_MCP_CLAUDE") or k.startswith("ANTHROPIC_FOUNDRY_") \
                        or k.startswith("ANTHROPIC_DEFAULT_"):
                    env[k] = v
        return env

    def seed(self, user: str, tokens: dict[str, Any] | None) -> dict[str, Any]:
        """What a new or running Studio gets: the Viya connection with the user's sign-in and the default
        SingleStore connection (no password)."""
        viya: dict[str, Any] = {"name": self.cfg.viya_name, "url": self.cfg.viya_url, "client_id": self.cfg.client_id,
                                "ssl_verify": self.cfg.viya_verify, "activate": True}
        if self.cfg.viya_compute_context:
            viya["compute_context"] = self.cfg.viya_compute_context
        if tokens:
            viya["tokens"] = {k: tokens[k] for k in ("access_token", "refresh_token", "expires_in") if k in tokens}
        out: dict[str, Any] = {"viya": viya}
        if self.cfg.default_s2.get("host"):
            out["singlestore"] = {**self.cfg.default_s2, "user": user}
        return out

    # ------------------------------------------------------------ processes
    def get(self, user: str) -> Studio | None:
        with self._lock:
            st = self._studios.get(user)
        if st is not None and not st.alive() and st.ready.is_set():
            self._forget(st, "exited")
            return None
        return st

    def running(self) -> list[Studio]:
        with self._lock:
            return list(self._studios.values())

    def ensure(self, user: str, tokens: dict[str, Any] | None = None, ip: str = "") -> Studio:
        """The user's Studio, started if needed (returns at once; wait with ``wait_ready``). A running one gets
        the fresh sign-in (``tokens``) handed over."""
        with self._lock:
            st = self._studios.get(user)
            if st is not None and (st.alive() or not st.ready.is_set()) and not st.stopping:
                fresh = st
            else:
                if len(self._studios) >= self.cfg.max_users and user not in self._studios:
                    self.audit.record("studio_full", user=user, ip=ip, running=len(self._studios))
                    raise StudioFull(f"SAS SpeedyStore Studio is full ({self.cfg.max_users} users). Try again later.")
                fresh = None
                st = Studio(user=user, token=secrets.token_urlsafe(32))
                self._studios[user] = st
        if fresh is not None:
            if tokens:
                threading.Thread(target=self._reseed, args=(fresh, tokens), daemon=True).start()
            return fresh
        threading.Thread(target=self._start, args=(st, tokens), name=f"studio-start-{safe_name(user)}", daemon=True).start()
        return st

    def _start(self, st: Studio, tokens: dict[str, Any] | None) -> None:
        state, _files = self.user_dirs(st.user)
        log = open(state / "studio.log", "ab")  # noqa: SIM115 - the child writes to it for its whole life
        cmd = [self.cfg.python or sys.executable, "-m", "singlestore_mcp.studio_server", "--stdin-config"]
        try:
            st.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=self._env(st.user),
                                       cwd=str(state), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            log.close()
            st.error = f"Couldn't start Studio: {exc}"
            st.ready.set()
            self.audit.record("studio_start_failed", user=st.user, reason=type(exc).__name__)
            return
        log.close()  # the child has its own handle
        cfg = {"token": st.token, "secret_key": self.cfg.user_key(st.user), "user": st.user, "public_base": PUBLIC_BASE,
               "seed": self.seed(st.user, tokens)}
        if self.cfg.client_secret:
            cfg["sas_client"] = {"id": self.cfg.client_id, "secret": self.cfg.client_secret}
        try:
            assert st.proc.stdin is not None
            st.proc.stdin.write((json.dumps(cfg) + "\n").encode())
            st.proc.stdin.flush()
        except OSError:
            pass
        del cfg
        deadline = time.time() + self.cfg.start_timeout_s
        assert st.proc.stdout is not None
        port = 0
        reader_result: dict[str, int] = {}

        def read_ready() -> None:
            for raw in st.proc.stdout:  # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("STUDIO-READY "):
                    reader_result["port"] = int(line.split()[1])
                    break
            for _ in st.proc.stdout:  # type: ignore[union-attr] - keep draining (never block the child)
                pass

        threading.Thread(target=read_ready, name="studio-stdout", daemon=True).start()
        while time.time() < deadline and st.proc.poll() is None and "port" not in reader_result:
            time.sleep(0.1)
        port = reader_result.get("port", 0)
        if not port:
            st.error = "Studio didn't start in time." if st.proc.poll() is None else f"Studio stopped while starting (exit {st.proc.returncode})."
            self.audit.record("studio_start_failed", user=st.user, reason=st.error)
            self._kill(st)
            st.ready.set()
            with self._lock:
                if self._studios.get(st.user) is st:
                    self._studios.pop(st.user, None)
            return
        st.port = port
        st.last_activity = time.time()
        st.ready.set()
        self.audit.record("studio_start", user=st.user, pid=st.pid, ms=round((time.time() - st.started) * 1000))

    def wait_ready(self, st: Studio, timeout: float | None = None) -> Studio:
        if not st.ready.wait(timeout if timeout is not None else self.cfg.start_timeout_s + 5):
            raise StudioFailed("Studio is still starting. Try again in a moment.")
        if st.error or not st.port:
            raise StudioFailed(st.error or "Studio didn't start.")
        return st

    def _reseed(self, st: Studio, tokens: dict[str, Any]) -> None:
        """A running Studio gets the user's fresh SAS sign-in (they signed in again)."""
        try:
            self.wait_ready(st)
            body = json.dumps(self.seed(st.user, tokens)).encode()
            origin = f"http://127.0.0.1:{st.port}"
            req = urllib.request.Request(f"{origin}{st.upstream('hub/seed')}", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Origin": origin})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=20) as res:
                res.read()
        except Exception as exc:  # noqa: BLE001 - the user can sign in to SAS in Connections
            self.audit.record("studio_reseed_failed", user=st.user, reason=type(exc).__name__)

    def touch(self, st: Studio) -> None:
        st.last_activity = time.time()

    def stop(self, user: str, reason: str, by: str | None = None) -> bool:
        with self._lock:
            st = self._studios.pop(user, None)
        if st is None:
            return False
        st.stopping = True
        self._kill(st)
        self.audit.record("studio_stop", user=user, reason=reason, by=by,
                          minutes=round((time.time() - st.started) / 60, 1))
        return True

    def _forget(self, st: Studio, reason: str) -> None:
        with self._lock:
            if self._studios.get(st.user) is not st:
                return
            self._studios.pop(st.user, None)
        self.audit.record("studio_stop", user=st.user, reason=reason, exit=st.proc.returncode if st.proc else None)

    @staticmethod
    def _kill(st: Studio) -> None:
        proc = st.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # asks it to stop
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # ------------------------------------------------------------ idle stop
    def reap_once(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        idle_s = self.cfg.idle_minutes * 60
        stopped = []
        for st in self.running():
            if st.ready.is_set() and not st.alive():
                self._forget(st, "exited")
                stopped.append(st.user)
            elif st.ready.is_set() and now - st.last_activity > idle_s:
                if self.stop(st.user, "idle"):
                    stopped.append(st.user)
        return stopped

    def start_reaper(self, every_s: float = 30) -> None:
        def run() -> None:
            while not self._stop.wait(every_s):
                try:
                    self.reap_once()
                except Exception:  # noqa: BLE001 - keep reaping
                    pass

        self._reaper = threading.Thread(target=run, name="studio-reaper", daemon=True)
        self._reaper.start()

    def stop_all(self, reason: str = "hub stopped") -> None:
        self._stop.set()
        for st in self.running():
            self.stop(st.user, reason)
