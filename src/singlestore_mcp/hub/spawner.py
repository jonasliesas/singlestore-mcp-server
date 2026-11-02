"""One Studio process per signed-in user (``python -m singlestore_mcp.studio_server --stdin-config``).

Each user gets a folder ``<data>/users/<safe id>/`` with
- ``state/`` - SINGLESTORE_MCP_HOME: connections, encrypted secrets, editor tabs, history, workloads, logs
- ``files/`` - HOME: what the file browser shows as the user's own files (SQL folder inside)

The process listens on 127.0.0.1 on a port it picks, under a random path token; both are known only to the hub.
Its settings (token, the user's secrets key, the SAS Logon client secret, the fresh Viya tokens) go over its
stdin, never into its environment or command line. Closing stdin stops it; the hub kills it if it doesn't go.

Processes stop when idle (no request but the pages' heartbeats for ``idle_minutes``, and none in flight), at
sign-out, from the admin page, or when the hub stops. At most ``max_users`` run at once (an admin may start one more).

A Studio that exits on its own (crash) or stops answering (hung: ``HUNG_PINGS`` health pings in a row fail, or a
call times out and the ping fails too) is dropped and started again on the user's next request. One that keeps
crashing (``CRASH_LIMIT`` times in ``CRASH_WINDOW_S``) isn't restarted for a while, so a broken setup doesn't spin.
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
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .access import Access
from .audit import Audit
from .config import HubConfig

PUBLIC_BASE = "/studio/"
CRASH_LIMIT = 3          # crashes / hangs within CRASH_WINDOW_S before the hub stops restarting a Studio for a while
CRASH_WINDOW_S = 300
HUNG_PINGS = 3           # health pings in a row that may fail before a Studio counts as hung
PING_TIMEOUT_S = 5.0


class StudioFull(RuntimeError):
    """``max_users`` Studios run already."""


class StudioFailed(RuntimeError):
    """The Studio process didn't start (its log is in the user's state folder)."""


class StudioCrashLoop(StudioFailed):
    """The user's Studio stopped unexpectedly ``CRASH_LIMIT`` times lately: not restarted for a while."""


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
    restart: bool = False          # started again after a crash or hang (the pages say "restarting")
    busy: int = 0                  # requests in flight (a long call isn't "idle")
    ping_failures: int = 0
    gateway_key: str = field(default="", repr=False)   # this Studio's key at the kernel broker (STUDIO_NOTEBOOKS=gateway)

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def upstream(self, rest: str) -> str:
        return f"/{self.token}/{rest}"


def _rss_one(pid: int) -> int | None:
    """Resident memory of one process (Linux /proc, Windows psapi working set), or None."""
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
            kernel32.OpenProcess.restype = wintypes.HANDLE
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


def _win_children(pid: int) -> list[int]:
    """Direct child processes (Windows Toolhelp snapshot): a venv's python.exe is a launcher that runs the real
    interpreter as its child, so a Studio's memory is mostly in the child."""
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out: list[int] = []
    try:
        entry = PE()
        entry.dwSize = ctypes.sizeof(PE)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ParentProcessID == pid and entry.th32ProcessID != pid:
                out.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def _linux_children(pid: int) -> list[int]:
    out: list[int] = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            out += [int(x) for x in (task / "children").read_text().split()]
        except (OSError, ValueError):
            continue
    return out


def _children(pid: int) -> list[int]:
    try:
        if sys.platform == "win32":
            return _win_children(pid)
        if sys.platform.startswith("linux"):
            return _linux_children(pid)
    except (OSError, ValueError, AttributeError):
        pass
    return []


def rss_bytes(pid: int | None, tree: bool = True) -> int | None:
    """Resident memory of a process and (``tree``) everything it started: on Windows the real interpreter behind a
    venv's launcher, and a Studio's notebook kernels (bridge + ipykernel). None when the process is gone."""
    if not pid:
        return None
    total = _rss_one(pid)
    if total is None or not tree:
        return total
    seen, todo = {pid}, _children(pid)
    while todo and len(seen) < 200:
        child = todo.pop()
        if child in seen:
            continue
        seen.add(child)
        total += _rss_one(child) or 0
        todo += _children(child)
    return total


class Spawner:
    def __init__(self, cfg: HubConfig, audit: Audit) -> None:
        self.cfg = cfg
        self.audit = audit
        self._studios: dict[str, Studio] = {}
        self._crashes: dict[str, deque[float]] = {}
        self.access: dict[str, Access] = {}   # per user, from the hub's sign-in check (SAS groups)
        self.on_access: Any = None            # hub callback(user, report): a Studio re-checked the user's groups
        self.broker: Any = None               # hub.gateway.KernelBroker (STUDIO_NOTEBOOKS=gateway)
        self.collector: Any = None            # hub.collector.CollectorProcess (the Studio repository)
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
            "SINGLESTORE_MCP_NOTEBOOKS": "1" if self.notebooks_for(user) else "0",
            "SINGLESTORE_MCP_ASSISTANT": "1" if self.cfg.assistant else "0",
            "SINGLESTORE_MCP_METRICS": "1" if self.cfg.metrics else "0",
            "SINGLESTORE_MCP_SAS_IMPORT": "0",
            "SINGLESTORE_MCP_FILE_ROOTS": "",
            "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        })
        if self.cfg.gateway:
            # Kernels on the kernel gateway (the broker's URL and this Studio's key go over stdin, not here).
            env["SINGLESTORE_MCP_NOTEBOOKS_MODE"] = "gateway"
            env["SINGLESTORE_MCP_PIP_MODE"] = self.cfg.pip_mode
            if self.cfg.pip_index:
                env["SINGLESTORE_MCP_PIP_INDEX_URL"] = self.cfg.pip_index
        elif self.cfg.notebook_python and self.notebooks_for(user):
            env["SINGLESTORE_MCP_NOTEBOOK_PYTHON"] = self.cfg.notebook_python  # the shared notebook environment
        if os.environ.get("SINGLESTORE_MCP_SAS_PREWARM"):
            env["SINGLESTORE_MCP_SAS_PREWARM"] = os.environ["SINGLESTORE_MCP_SAS_PREWARM"]
        if self.cfg.assistant:
            # Ask Claude switched on: the organisation's Anthropic API key or Microsoft Foundry settings (from a Secret).
            for k, v in os.environ.items():
                if k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_FOUNDRY", "SINGLESTORE_MCP_CLAUDE") or k.startswith("ANTHROPIC_FOUNDRY_") \
                        or k.startswith("ANTHROPIC_DEFAULT_"):
                    env[k] = v
        return env

    def notebooks_for(self, user: str) -> bool:
        a = self.access.get(user)
        return a.notebooks if a is not None else self.cfg.notebooks

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
        # The user's notebook flag (enforced by the Studio's notebook tools), and the group rules it re-checks
        # after each Viya token renewal.
        rules = self.cfg.rules
        out["access"] = {"notebooks": self.notebooks_for(user), "viya_url": self.cfg.viya_url,
                         "rules": rules.to_json() if rules.wants_groups else None}
        if self.cfg.default_s2.get("host"):
            out["singlestore"] = {**self.cfg.default_s2, "user": user}
        if self.collector is not None:
            out["collector"] = self.collector_seed(user)
        return out

    def collector_seed(self, user: str) -> dict[str, Any] | None:
        """The Studio repository for this user: the collector's address and a token with what they may see (None:
        the collector isn't running, the Studio keeps its own store)."""
        a = self.access.get(user)
        return self.collector.seed_for(user, monitor=bool(a and a.monitor), admin=self.cfg.is_admin(user))

    # ------------------------------------------------------------ processes
    def get(self, user: str) -> Studio | None:
        with self._lock:
            st = self._studios.get(user)
        if st is not None and not st.alive() and st.ready.is_set() and not st.error:
            self.crashed(st, "exited")
            return None
        return st

    def running(self) -> list[Studio]:
        with self._lock:
            return list(self._studios.values())

    def ensure(self, user: str, tokens: dict[str, Any] | None = None, ip: str = "", admin: bool = False) -> Studio:
        """The user's Studio, started if needed (returns at once; wait with ``wait_ready``). A running one gets
        the fresh sign-in (``tokens``) handed over. ``admin``: may start one beyond ``max_users`` (to reach /admin
        when Studio is full)."""
        if self._stop.is_set():
            raise StudioFailed("The Studio server is stopping. Try again in a minute.")
        for dead in [x for x in self.running() if x.ready.is_set() and not x.alive() and not x.stopping and not x.error]:
            self.crashed(dead, "exited")  # a crashed Studio doesn't hold a place
        with self._lock:
            st = self._studios.get(user)
            if st is not None and (st.alive() or not st.ready.is_set()) and not st.stopping:
                fresh = st
            else:
                crashes = self._recent_crashes(user)
                if crashes >= CRASH_LIMIT:
                    raise StudioCrashLoop(f"Your Studio stopped unexpectedly {crashes} times in the last "
                                          f"{CRASH_WINDOW_S // 60} minutes, so it isn't started again right now. Try again "
                                          "in a few minutes, or ask the Studio admin (studio.log in your folder says why).")
                others = len([u for u in self._studios if u != user])
                if others >= self.cfg.max_users + (1 if admin else 0):
                    self.audit.record("studio_full", user=user, ip=ip, running=others)
                    raise StudioFull(f"SAS SpeedyStore Studio is full ({self.cfg.max_users} users). Try again later.")
                fresh = None
                st = Studio(user=user, token=secrets.token_urlsafe(32), restart=crashes > 0)
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
        if st.stopping:  # stopped (sign-out, admin, hub stop) while it was being started
            self._kill(st)
            st.error = "Studio was stopped."
            st.ready.set()
            return
        cfg = {"token": st.token, "secret_key": self.cfg.user_key(st.user), "user": st.user, "public_base": PUBLIC_BASE,
               "seed": self.seed(st.user, tokens)}
        if self.cfg.client_secret:
            cfg["sas_client"] = {"id": self.cfg.client_id, "secret": self.cfg.client_secret}
        if self.broker is not None and self.broker.url:
            st.gateway_key = self.broker.register_studio(st.user)
            cfg["gateway"] = {"url": self.broker.url, "key": st.gateway_key, "max_kernels": self.cfg.kernels_per_user}
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
            for raw in st.proc.stdout:  # type: ignore[union-attr] - keep draining (never block the child)
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("STUDIO-ACCESS ") and self.on_access is not None:
                    try:
                        self.on_access(st, json.loads(line[len("STUDIO-ACCESS "):]))
                    except Exception:  # noqa: BLE001 - a bad report changes nothing
                        pass

        threading.Thread(target=read_ready, name="studio-stdout", daemon=True).start()
        while time.time() < deadline and st.proc.poll() is None and "port" not in reader_result:
            time.sleep(0.1)
        port = reader_result.get("port", 0)
        if not port:
            st.error = "Studio didn't start in time." if st.proc.poll() is None else f"Studio stopped while starting (exit {st.proc.returncode})."
            if st.stopping:
                st.error = "Studio was stopped."
            else:
                self.audit.record("studio_start_failed", user=st.user, reason=st.error)
            self._kill(st, grace=1.0)
            st.ready.set()
            with self._lock:
                if self._studios.get(st.user) is st:
                    self._studios.pop(st.user, None)
                    if not st.stopping:
                        self._crashes.setdefault(st.user, deque()).append(time.time())
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
        self.push_seed(st, self.seed(st.user, tokens))

    def push_seed(self, st: Studio, seed: dict[str, Any]) -> None:
        """Hand a running Studio new settings (``studio_server.apply_seed``)."""
        try:
            self.wait_ready(st)
            body = json.dumps(seed).encode()
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

    @contextmanager
    def using(self, st: Studio) -> Iterator[Studio]:
        """A request in flight to ``st``: the idle stop waits for it, and its end counts as use."""
        with self._lock:
            st.busy += 1
        try:
            yield st
        finally:
            with self._lock:
                st.busy -= 1
            st.last_activity = time.time()

    def ping(self, st: Studio, timeout: float | None = None) -> bool:
        """Does the Studio answer at all (its own /ping, not the user's work)?"""
        if not st.port or not st.alive():
            return False
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", st.port, timeout=timeout or PING_TIMEOUT_S)
        try:
            conn.request("GET", st.upstream("ping"), headers={"Host": f"127.0.0.1:{st.port}"})
            res = conn.getresponse()
            res.read()
            return res.status == 200
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def stop(self, user: str, reason: str, by: str | None = None) -> bool:
        with self._lock:
            st = self._studios.pop(user, None)
            self._crashes.pop(user, None)  # a deliberate stop starts the count again
        if st is None:
            return False
        st.stopping = True
        self._kill(st)
        self._forget_kernels(st, reason)
        self.audit.record("studio_stop", user=user, reason=reason, by=by,
                          minutes=round((time.time() - st.started) / 60, 1))
        return True

    def crashed(self, st: Studio, reason: str) -> bool:
        """Drop a Studio that exited on its own or hangs (killed first); the user's next request starts a new one."""
        with self._lock:
            if self._studios.get(st.user) is not st or st.stopping:
                return False
            self._studios.pop(st.user, None)
            st.stopping = True
            self._crashes.setdefault(st.user, deque()).append(time.time())
        self._kill(st, grace=1.0)  # closes its stdin even when it's gone; a hung one is killed after 1 s
        self._forget_kernels(st, reason)
        self.audit.record("studio_stop", user=st.user, reason=reason, exit=st.proc.returncode if st.proc else None,
                          minutes=round((time.time() - st.started) / 60, 1))
        return True

    def crashed_recently(self, user: str) -> bool:
        """The user's last Studio ended by crashing or hanging (not by a stop) within ``CRASH_WINDOW_S``."""
        with self._lock:
            return self._recent_crashes(user) > 0

    def _recent_crashes(self, user: str) -> int:
        """(Under the lock.)"""
        q = self._crashes.get(user)
        now = time.time()
        while q and now - q[0] > CRASH_WINDOW_S:
            q.popleft()
        return len(q) if q else 0

    def _forget_kernels(self, st: Studio, reason: str) -> None:
        """Gateway: the stopped Studio's key stops working and its kernels are shut down (in the background) - only
        that Studio's, so a new Studio of the same user started meanwhile keeps its own."""
        if self.broker is not None and st.gateway_key:
            threading.Thread(target=self.broker.forget_studio, args=(st.user, f"studio {reason}", st.gateway_key),
                             daemon=True).start()

    @staticmethod
    def _kill(st: Studio, grace: float = 8.0) -> None:
        proc = st.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # asks it to stop
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
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
            if st.ready.is_set() and not st.alive() and not st.error:
                if self.crashed(st, "exited"):
                    stopped.append(st.user)
            elif st.ready.is_set() and not st.busy and now - st.last_activity > idle_s:
                if self.stop(st.user, "idle"):
                    stopped.append(st.user)
        return stopped

    def check_health(self) -> list[str]:
        """Ping every running Studio at once; one that failed ``HUNG_PINGS`` pings in a row is hung: drop it."""
        ready = [st for st in self.running() if st.ready.is_set() and st.port and st.alive()]
        answers: dict[int, bool] = {}

        def probe(st: Studio) -> None:
            answers[id(st)] = self.ping(st)

        threads = [threading.Thread(target=probe, args=(st,), daemon=True) for st in ready]
        for t in threads:
            t.start()
        for t in threads:
            t.join(PING_TIMEOUT_S + 2)  # a ping that hangs past its own timeout counts as failed
        hung = []
        for st in ready:
            if answers.get(id(st)):
                st.ping_failures = 0
                continue
            st.ping_failures += 1
            if st.ping_failures >= HUNG_PINGS and self.crashed(st, "hung"):
                hung.append(st.user)
        return hung

    def start_reaper(self, every_s: float = 30) -> None:
        def run() -> None:
            while not self._stop.wait(every_s):
                for step in (self.reap_once, self.check_health):
                    try:
                        step()
                    except Exception:  # noqa: BLE001 - keep reaping
                        pass

        self._reaper = threading.Thread(target=run, name="studio-reaper", daemon=True)
        self._reaper.start()

    def stop_all(self, reason: str = "hub stopped", grace: float = 10.0) -> None:
        """Stop every Studio at once: all are asked (stdin closed) together and get ``grace`` seconds in all
        before they are killed, so the hub stops within the pod's termination grace period (30 s)."""
        self._stop.set()
        with self._lock:
            studios = list(self._studios.values())
            self._studios.clear()
        for st in studios:
            st.stopping = True
            try:
                if st.proc is not None and st.proc.stdin:
                    st.proc.stdin.close()
            except OSError:
                pass
        deadline = time.time() + grace
        for st in studios:
            if st.proc is None:
                continue
            try:
                st.proc.wait(timeout=max(0.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                st.proc.kill()
                try:
                    st.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            self.audit.record("studio_stop", user=st.user, reason=reason, minutes=round((time.time() - st.started) / 60, 1))
