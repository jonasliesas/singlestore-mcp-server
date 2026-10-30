"""Kernel launcher for the kernel gateway (``STUDIO_NOTEBOOKS=gateway``, step 2a): one notebook kernel, with limits.

The gateway's kernelspec (written by ``kernel_gateway.py``) runs this file with the shared notebook environment's
Python for every kernel Jupyter Enterprise Gateway starts::

    <notebook python> kernel_launcher.py --data-dir /kernels --memory-mb 3072 --nice 10 --pip-mode mirror
        --pip-index https://<mirror>/simple --quota-gb 5 -f <connection file>

It runs before any user code and:

- finds the kernel's user from ``KERNEL_USERNAME`` (set by the hub's kernel broker, the only client the gateway
  accepts; a folder-safe id) and makes that user's folder on the kernel volume: ``<data>/users/<user>/files`` (the
  kernel's working directory and HOME) and ``.../pip`` (``pip install --target``: the user's own packages, first on
  ``sys.path``; the image's notebook environment stays read-only);
- gives the kernel a clean environment: only a short allowlist of the gateway's variables (PATH, locale, proxies,
  CA bundle) and the Jupyter kernel ids - never the gateway's auth token, ``EG_*`` / ``STUDIO_*`` settings or
  anything the hub sent besides ``KERNEL_USERNAME``;
- package installs follow ``--pip-mode``: ``mirror`` (PIP_INDEX_URL = the mirror), ``pypi`` (pypi.org), ``off``
  (PIP_NO_INDEX: ``pip install`` finds nothing);
- caps the kernel's memory (Linux: ``RLIMIT_AS``; Windows, for local tests: a job object's process memory limit) and
  lowers its CPU priority (``nice`` / below-normal priority class), with fewer BLAS / OpenMP threads so the address
  space cap isn't eaten by thread stacks;
- then becomes the kernel (Linux: ``exec`` - same process id, so the gateway's interrupt and kill reach it; Windows:
  runs it as a child in the job and waits).

Stdlib only and free of singlestore_mcp imports: it runs from its file path in the notebook environment.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

USER_RE = re.compile(r"^[a-z0-9_-][a-z0-9._-]{0,79}$")  # hub.spawner.safe_name: never "", "." or ".."
# From the gateway's own environment (never anything secret: the gateway's token is removed before kernels start).
KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "MPLBACKEND",
        # Windows (local tests): Python and sockets need these.
        "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PROGRAMDATA")
# Set by Jupyter for the kernel itself (ids, Windows interrupt / parent handles).
KEEP_PREFIXES = ("JPY_",)
KEEP_JUPYTER = ("KERNEL_ID", "KERNEL_LANGUAGE", "KERNEL_USERNAME")
PYPI = "https://pypi.org/simple"


def user_dirs(data_dir: str, user: str) -> dict[str, str]:
    base = os.path.join(data_dir, "users", user)
    dirs = {"base": base, "files": os.path.join(base, "files"), "pip": os.path.join(base, "pip"),
            "tmp": os.path.join(base, "tmp")}
    for path in dirs.values():
        os.makedirs(path, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
    return dirs


def kernel_env(source: dict[str, str], user: str, dirs: dict[str, str], pip_mode: str, pip_index: str,
               quota_gb: float) -> dict[str, str]:
    """The kernel's whole environment, built from scratch."""
    env = {k: v for k, v in source.items() if k in KEEP or k in KEEP_JUPYTER or k.startswith(KEEP_PREFIXES)}
    env.update({
        "HOME": dirs["files"], "USERPROFILE": dirs["files"],
        "TMPDIR": dirs["tmp"], "TEMP": dirs["tmp"], "TMP": dirs["tmp"],
        "KERNEL_USERNAME": user,
        # The user's own packages: before the image's (a newer pandas wins), installs go there.
        "PYTHONPATH": dirs["pip"],
        "PYTHONNOUSERSITE": "1",
        "PIP_TARGET": dirs["pip"],
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "S2_KERNEL_PIP_DIR": dirs["pip"],
        "S2_KERNEL_USER_DIR": dirs["base"],
        "S2_KERNEL_PIP_MODE": pip_mode,
        "S2_KERNEL_QUOTA_BYTES": str(int(quota_gb * 1024 ** 3)),
        # Fewer threads: each reserves address space (RLIMIT_AS) and the pod's CPUs are shared.
        "OPENBLAS_NUM_THREADS": "2", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "MALLOC_ARENA_MAX": "2",
        "MPLBACKEND": source.get("MPLBACKEND", "Agg"),
        "HF_HUB_DISABLE_XET": "1",
    })
    if pip_mode == "off":
        env["PIP_NO_INDEX"] = "1"
    elif pip_mode == "mirror" and pip_index:
        env["PIP_INDEX_URL"] = pip_index
    else:
        env["PIP_INDEX_URL"] = PYPI
    return env


def _posix_limits(memory_mb: int, nice: int) -> None:
    import resource

    if memory_mb > 0:
        cap = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if nice > 0:
        try:
            os.nice(nice)
        except OSError:
            pass


def _windows_job(memory_mb: int, nice: int) -> object | None:
    """A job object that caps each process's committed memory, kills the kernel when the launcher goes, and puts it at
    below-normal priority (local tests on Windows; the server is Linux)."""
    import ctypes
    from ctypes import wintypes

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Extended()
    flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if memory_mb > 0:
        flags |= 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = memory_mb * 1024 * 1024
    if nice > 0:
        flags |= 0x20  # JOB_OBJECT_LIMIT_PRIORITY_CLASS
        info.BasicLimitInformation.PriorityClass = 0x4000  # BELOW_NORMAL_PRIORITY_CLASS
    info.BasicLimitInformation.LimitFlags = flags
    kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # JobObjectExtendedLimitInformation
    return job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start one notebook kernel for the Studio kernel gateway.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--memory-mb", type=int, default=3072)
    parser.add_argument("--nice", type=int, default=10)
    parser.add_argument("--pip-mode", choices=("mirror", "pypi", "off"), default="off")
    parser.add_argument("--pip-index", default="")
    parser.add_argument("--quota-gb", type=float, default=5.0)
    parser.add_argument("-f", dest="connection_file", required=True)
    opts = parser.parse_args(argv)
    user = os.environ.get("KERNEL_USERNAME", "")
    if not USER_RE.match(user):
        print("kernel_launcher: KERNEL_USERNAME is missing or not a folder-safe user id; not starting.", file=sys.stderr)
        return 2
    dirs = user_dirs(opts.data_dir, user)
    env = kernel_env(dict(os.environ), user, dirs, opts.pip_mode, opts.pip_index, opts.quota_gb)
    cmd = [sys.executable, "-m", "ipykernel_launcher", "-f", opts.connection_file]
    os.chdir(dirs["files"])
    if os.name != "nt":
        _posix_limits(opts.memory_mb, opts.nice)
        os.execve(sys.executable, cmd, env)  # becomes the kernel (same pid: the gateway's signals reach it)
    import ctypes

    job = _windows_job(opts.memory_mb, opts.nice)
    # close_fds=False: the kernel inherits Jupyter's interrupt / parent handles (JPY_INTERRUPT_EVENT, JPY_PARENT_PID).
    proc = subprocess.Popen(cmd, env=env, cwd=dirs["files"], close_fds=False)
    if job:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1FFFFF, False, proc.pid)
        if handle:
            kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(handle))
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        return proc.wait()
    except KeyboardInterrupt:  # the gateway's interrupt reaches the kernel through its own event
        return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
