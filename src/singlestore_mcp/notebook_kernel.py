"""Python kernels for the Notebook app.

Notebook code runs in a real IPython (Jupyter) kernel inside its own Python
environment, so the MCP server needs no data-science packages:

* ``setup()`` creates the environment once with uv (or use an existing
  Python via SINGLESTORE_MCP_NOTEBOOK_PYTHON) and installs ipykernel,
  jupyter_client, pandas, matplotlib and singlestoredb.
* Each open notebook gets one kernel, run by ``notebook_bridge.py`` in that
  environment and driven over JSON lines.
* A cell run returns a run id at once; the app polls ``poll()`` for output as
  it arrives (stdout, tables, charts, errors).

Kernels close after 30 idle minutes, and when the MCP server exits (the
bridge sees stdin close).
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from .paths import data_dir
from typing import Any

# What SingleStore's example notebooks expect besides the kernel itself.
PACKAGES = ["ipykernel", "jupyter_client", "pandas", "matplotlib", "singlestoredb",
            "sqlalchemy", "sqlalchemy-singlestoredb", "ibis-framework", "scikit-learn"]
PYTHON_VERSION = "3.12"
_IDLE_SECONDS = 1800
_MAX_KERNELS = 8
_MAX_RUNS = 200
_BRIDGE = Path(__file__).with_name("notebook_bridge.py")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env_dir() -> Path:
    return data_dir() / "notebook-env"


def kernel_python() -> Path | None:
    configured = os.environ.get("SINGLESTORE_MCP_NOTEBOOK_PYTHON")
    if configured:
        return Path(configured)
    exe = _env_dir() / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.exists() and (_env_dir() / ".s2-ready").exists() else None


# ------------------------------------------------------------------ environment setup

_setup = {"state": "idle", "log": [], "started": None}
_setup_lock = threading.Lock()


def environment_status() -> dict[str, Any]:
    py = kernel_python()
    with _setup_lock:
        state = _setup["state"]
        log = _setup["log"][-12:]
    if py and state not in ("installing",):
        return {"state": "ready", "python": str(py), "packages": PACKAGES, "missing": _missing_packages(), "log": log}
    return {"state": "missing" if state == "idle" else state, "log": log, "packages": PACKAGES,
            "folder": str(_env_dir()), "uv": bool(shutil.which("uv"))}


def _stamp() -> Path:
    return _env_dir() / ".s2-ready"


def _missing_packages() -> list[str]:
    """Packages added to PACKAGES since the environment was created (per its stamp)."""
    if os.environ.get("SINGLESTORE_MCP_NOTEBOOK_PYTHON"):
        return []
    try:
        have = set(_stamp().read_text(encoding="utf-8").split(","))
    except OSError:
        return []
    return [p for p in PACKAGES if p not in have] if have != {"ok"} else [p for p in PACKAGES[5:]]


def setup() -> dict[str, Any]:
    """Create the notebook environment, or add missing packages, in the background (idempotent)."""
    with _setup_lock:
        if _setup["state"] == "installing" or (kernel_python() and not _missing_packages()):
            return environment_status()
        _setup.update(state="installing", log=["Starting…"], started=time.time())
    threading.Thread(target=_install, daemon=True).start()
    return environment_status()


# One requirement per word: a name with optional [extras] and version pins.
# No options (nothing starting with "-"), so the box can't pass flags to uv.
_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?((==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+(,(==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+)*)?$")


def install_packages(specs: list[str]) -> dict[str, Any]:
    """Install packages into the notebook environment in the background (progress via environment_status)."""
    specs = [x.strip() for x in specs if x.strip()]
    if not specs:
        raise ValueError("Name at least one package.")
    bad = [x for x in specs if not _REQUIREMENT.match(x)]
    if bad:
        raise ValueError(f"Not a package name: {', '.join(bad)} (use names like pandas, plotly==5.24 or scikit-learn>=1.5).")
    if not kernel_python():
        raise LookupError("The notebook Python environment isn't installed yet.")
    with _setup_lock:
        if _setup["state"] == "installing":
            raise ValueError("Another install is still running.")
        _setup.update(state="installing", log=["Starting…"], started=time.time())
    threading.Thread(target=_install, args=(specs,), daemon=True).start()
    return environment_status()


_LIST_SCRIPT = r"""
import json, importlib.metadata as md
out = {}
for d in md.distributions():
    name = d.metadata.get("Name")
    if name and name.lower() not in out:
        out[name.lower()] = {"name": name, "version": d.version, "summary": (d.metadata.get("Summary") or "")[:200]}
print(json.dumps(sorted(out.values(), key=lambda p: p["name"].lower())))
"""


def list_packages() -> list[dict[str, str]]:
    """Installed distributions in the notebook environment: name, version, summary."""
    py = kernel_python()
    if not py:
        raise LookupError("The notebook Python environment isn't installed yet.")
    proc = subprocess.run([str(py), "-c", _LIST_SCRIPT], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"Couldn't list packages: {proc.stderr.strip()[-300:]}")
    core = {p.lower().replace("_", "-") for p in PACKAGES}
    pkgs = json.loads(proc.stdout)
    for p in pkgs:
        p["core"] = p["name"].lower().replace("_", "-") in core
    return pkgs


def _log(line: str) -> None:
    with _setup_lock:
        _setup["log"].append(line.rstrip()[:300])
        del _setup["log"][:-200]


def _install(extra: list[str] | None = None) -> None:
    """Create/complete the environment, or (``extra``) install the user's packages into it."""
    uv = shutil.which("uv")
    env = _env_dir()
    try:
        if not uv:
            raise RuntimeError("uv isn't on PATH. Install uv, or set SINGLESTORE_MCP_NOTEBOOK_PYTHON to a Python with "
                               + ", ".join(PACKAGES) + ".")
        env.parent.mkdir(parents=True, exist_ok=True)
        py = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if extra:
            target = kernel_python() or py
            steps = [([uv, "pip", "install", "--python", str(target), *extra], "Installing " + " ".join(extra) + "…")]
        else:
            steps = [] if py.exists() else [([uv, "venv", str(env), "--python", PYTHON_VERSION, "--seed"], "Creating the Python environment…")]
            steps.append(([uv, "pip", "install", "--python", str(py), *PACKAGES], "Installing " + ", ".join(PACKAGES) + "…"))
        for cmd, label in steps:
            _log(label)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.strip():
                    _log(line)
            if proc.wait() != 0:
                raise RuntimeError(f"{label.rstrip('…')} failed (exit code {proc.returncode}).")
        if not extra:
            _stamp().write_text(",".join(PACKAGES), encoding="utf-8")
        _log("Done." + (" Restart the kernel (↻) to use the new packages." if extra else ""))
        with _setup_lock:
            _setup["state"] = "idle"
    except Exception as exc:  # noqa: BLE001 - shown in the app
        _log(f"Error: {exc}")
        with _setup_lock:
            _setup["state"] = "error"


# ------------------------------------------------------------------ kernels and runs


def _kernel_env(python: Path) -> dict[str, str]:
    """Environment for the kernel: the notebook environment comes first on PATH,
    as if activated, so `!pip install`, `!pip3`, `!python` and `%pip` all use
    the kernel's own Python instead of whatever Python is first on the system PATH."""
    env = dict(os.environ)
    scripts = python.parent
    env_root = scripts.parent
    env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")
    if (env_root / "pyvenv.cfg").exists():
        env["VIRTUAL_ENV"] = str(env_root)
    env.pop("PYTHONHOME", None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    # Hugging Face's cache falls back to copies without symlinks on Windows
    # (no Developer Mode); it works, so the warning is only noise.
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    # Hugging Face's newer "Xet" downloads stall on some corporate networks
    # (proxies / TLS inspection); plain HTTPS downloads work everywhere.
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    return env


class _Run:
    def __init__(self, run_id: str, kernel: "_Kernel") -> None:
        self.id = run_id
        self.kernel = kernel
        self.outputs: list[dict[str, Any]] = []
        self.cleared = 0  # bumps when the cell cleared its output (clear_output)
        self.done = False
        self.status: str | None = None
        self.execution_count: int | None = None
        self.started = time.time()


class _Kernel:
    def __init__(self, notebook_id: str, python: Path) -> None:
        self.notebook_id = notebook_id
        self.ready = threading.Event()
        self.error: str | None = None
        self.last_used = time.time()
        self.busy_runs: set[str] = set()
        self.proc = subprocess.Popen(
            [str(python), str(_BRIDGE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW, env=_kernel_env(python),
        )
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(self.proc.stderr or []), daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            event = msg.get("event")
            if event == "ready":
                self.ready.set()
            elif event == "error" and "id" not in msg:
                self.error = msg.get("message")
                self.ready.set()
            elif event == "restarted":
                self.error = None
            run = _runs.get(msg.get("id", ""))
            if not run:
                continue
            with _lock:
                if event == "output":
                    run.outputs.append(msg["output"])
                elif event == "clear":
                    run.outputs.clear()
                    run.cleared += 1
                elif event == "done":
                    run.done, run.status, run.execution_count = True, msg.get("status"), msg.get("execution_count")
                    self.busy_runs.discard(run.id)
                    self.last_used = time.time()
        # Bridge ended: finish anything still waiting.
        with _lock:
            for rid in list(self.busy_runs):
                run = _runs.get(rid)
                if run and not run.done:
                    detail = "".join(self.stderr).strip()[-400:]
                    run.outputs.append({"output_type": "error", "ename": "KernelDied",
                                        "evalue": "The Python kernel stopped." + (f" {detail}" if detail else ""), "traceback": []})
                    run.done, run.status = True, "error"
            self.busy_runs.clear()
        self.ready.set()

    def close(self) -> None:
        try:
            self.send({"op": "shutdown"})
            self.proc.stdin and self.proc.stdin.close()
        except OSError:
            pass

        def reap() -> None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        threading.Thread(target=reap, daemon=True).start()


_kernels: dict[str, _Kernel] = {}
_runs: dict[str, _Run] = {}
_lock = threading.Lock()
_ids = itertools.count(1)
_reaper_started = False


def _kernel_for(notebook_id: str) -> _Kernel:
    python = kernel_python()
    if not python:
        raise LookupError("The notebook Python environment isn't installed yet.")
    with _lock:
        k = _kernels.get(notebook_id)
        if k and k.alive():
            return k
        if k:
            k.close()
        idle = sorted((x for x in _kernels.values() if not x.busy_runs), key=lambda x: x.last_used)
        while len(_kernels) >= _MAX_KERNELS and idle:
            old = idle.pop(0)
            old.close()
            _kernels.pop(old.notebook_id, None)
        k = _Kernel(notebook_id, python)
        _kernels[notebook_id] = k
        _start_reaper()
        return k


def _start_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def reap() -> None:
        while True:
            time.sleep(60)
            with _lock:
                for nid, k in list(_kernels.items()):
                    if not k.alive() or (not k.busy_runs and time.time() - k.last_used > _IDLE_SECONDS):
                        k.close()
                        _kernels.pop(nid, None)
                old = sorted(_runs.values(), key=lambda r: r.started)[:-_MAX_RUNS]
                for r in old:
                    if r.done:
                        _runs.pop(r.id, None)

    threading.Thread(target=reap, daemon=True).start()


def start(notebook_id: str) -> dict[str, Any]:
    """Start the notebook's kernel without running anything (so the first cell is quick)."""
    k = _kernel_for(notebook_id)
    k.last_used = time.time()
    return kernel_state(notebook_id)


def kernel_state(notebook_id: str) -> dict[str, Any]:
    with _lock:
        k = _kernels.get(notebook_id)
    if not k:
        return {"state": "stopped"}
    if not k.alive():
        return {"state": "dead", "error": k.error or "".join(k.stderr).strip()[-400:]}
    if k.error:
        return {"state": "error", "error": k.error}
    if not k.ready.is_set():
        return {"state": "starting"}
    return {"state": "busy" if k.busy_runs else "idle"}


def execute(notebook_id: str, code: str, database: str | None = None) -> str:
    k = _kernel_for(notebook_id)
    # %sql / %%sql magics default to the notebook's database.
    try:
        k.send({"op": "execute", "silent": True, "code": f"_s2_set_database({database!r})"})
    except OSError:
        pass
    run_id = f"r{next(_ids)}"
    run = _Run(run_id, k)
    with _lock:
        _runs[run_id] = run
        k.busy_runs.add(run_id)
        k.last_used = time.time()
    try:
        k.send({"id": run_id, "op": "execute", "code": code})
    except OSError as exc:
        with _lock:
            run.outputs.append({"output_type": "error", "ename": "KernelDied", "evalue": str(exc), "traceback": []})
            run.done, run.status = True, "error"
            k.busy_runs.discard(run_id)
    return run_id


def poll(run_id: str, after: int = 0, cleared: int = 0) -> dict[str, Any]:
    with _lock:
        run = _runs.get(run_id)
        if not run:
            raise LookupError(f"Unknown run {run_id!r} (the server may have restarted).")
        reset = run.cleared != cleared
        outputs = run.outputs[0 if reset else after:]
        return {
            "run_id": run_id,
            "outputs": outputs,
            "reset": reset,
            "count": len(run.outputs),
            "cleared": run.cleared,
            "done": run.done,
            "status": run.status,
            "execution_count": run.execution_count,
            "seconds": round(time.time() - run.started, 1),
        }


def interrupt(notebook_id: str) -> bool:
    with _lock:
        k = _kernels.get(notebook_id)
    if not k or not k.alive():
        return False
    k.send({"op": "interrupt"})
    return True


def restart(notebook_id: str) -> dict[str, Any]:
    with _lock:
        k = _kernels.get(notebook_id)
    if k and k.alive():
        k.send({"op": "restart"})
        return {"state": "restarting"}
    return start(notebook_id)


def shutdown(notebook_id: str) -> bool:
    with _lock:
        k = _kernels.pop(notebook_id, None)
    if k:
        k.close()
    return bool(k)


def close_all() -> None:
    with _lock:
        kernels = list(_kernels.values())
        _kernels.clear()
    for k in kernels:
        k.close()


import atexit  # noqa: E402

atexit.register(close_all)

if __name__ == "__main__":  # pragma: no cover - manual check
    print(json.dumps(environment_status(), indent=2))
    sys.exit(0)
