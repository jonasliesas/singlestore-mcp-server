"""The kernel gateway pod's entry point (``STUDIO_NOTEBOOKS=gateway``, step 2a): Jupyter Enterprise Gateway, set up.

    python -m singlestore_mcp.kernel_gateway          # in the gateway pod (same image as the hub)

Writes, into a runtime folder (``STUDIO_GATEWAY_RUNTIME``, default ``/tmp/studio-gateway``):

- the only kernelspec the gateway offers, ``studio-python``: the shared notebook environment's Python running
  ``kernel_launcher.py`` with this deployment's limits (memory, CPU priority, kernel volume, pip mode, quota) baked
  into its command line, so neither the hub nor a user can change them;
- a gateway config file that takes the gateway's auth token out of the process environment (kernels can't inherit
  it) and makes the gateway process non-dumpable (other processes of the same OS user - the kernels - can't read
  its ``/proc/<pid>/environ`` or memory);

then runs Enterprise Gateway as its child (this process stays PID 1 of the container, non-dumpable, reaping and
forwarding SIGTERM like tini would - tini itself would keep the token readable in ``/proc/1/environ``) (``STUDIO_GATEWAY_EG``, default ``/opt/gateway-env/bin/jupyter-enterprisegateway``) with
token auth, the max-kernels limits as a backstop to the hub's own, idle culling (busy kernels are kept) and no
kernel listing.

Settings (the gateway Deployment's environment, from the same ConfigMap as the hub + the gateway Secret):
STUDIO_GATEWAY_AUTH_TOKEN (required, 32+ characters; the hub has the same), STUDIO_GATEWAY_BIND (0.0.0.0),
STUDIO_GATEWAY_PORT (8888), STUDIO_NOTEBOOK_PYTHON (the kernels' Python), STUDIO_KERNEL_DATA_DIR (``/kernels``: the
kernel volume), STUDIO_KERNEL_MEMORY_MB (3072), STUDIO_KERNEL_NICE (10), STUDIO_KERNEL_QUOTA_GB (5),
STUDIO_PIP_MODE / STUDIO_PIP_INDEX_URL, STUDIO_KERNELS_TOTAL (20), STUDIO_KERNELS_PER_USER (3),
STUDIO_KERNEL_IDLE_MINUTES (30).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import server_mode

KERNEL_NAME = "studio-python"
LAUNCHER = Path(__file__).with_name("kernel_launcher.py")
# The gateway's own variables kernels may inherit (the launcher filters again).
INHERITED = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY",
             "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "MPLBACKEND",
             "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PROGRAMDATA")

# Runs inside the gateway process when it reads its config (before it starts any kernel).
_CONFIG = r'''
import os, sys
c = get_config()  # noqa: F821 - traitlets config file
# (Config files may be read more than once: the token is kept on sys, never again in os.environ.)
sys._studio_eg_token = getattr(sys, "_studio_eg_token", "") or os.environ.pop("EG_AUTH_TOKEN", "")
c.EnterpriseGatewayApp.auth_token = sys._studio_eg_token
if not c.EnterpriseGatewayApp.auth_token:
    sys.exit("studio gateway: no auth token")
if os.name == "nt":
    # Local tests on Windows only: Enterprise Gateway's local process proxy lacks the interrupt event that
    # jupyter_client uses there (on Linux, interrupts are signals to the kernel's process group).
    from enterprise_gateway.services.processproxies.processproxy import LocalProcessProxy
    LocalProcessProxy.win32_interrupt_event = property(lambda self: getattr(self.local_proc, "win32_interrupt_event", None))
if sys.platform.startswith("linux"):
    try:
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 0: /proc/<pid>/environ, mem: root only
    except Exception:
        pass
'''


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class GatewaySettings:
    token: str
    python: str
    data_dir: str
    runtime: str
    bind: str = "0.0.0.0"
    port: int = 8888
    memory_mb: int = 3072
    nice: int = 10
    quota_gb: float = 5.0
    pip_mode: str = "off"
    pip_index: str = ""
    kernels_total: int = 20
    kernels_per_user: int = 3
    idle_minutes: int = 30
    eg: str = "/opt/gateway-env/bin/jupyter-enterprisegateway"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        token = os.environ.get("STUDIO_GATEWAY_AUTH_TOKEN", "").strip()
        if len(token) < 32:
            raise SystemExit("Set STUDIO_GATEWAY_AUTH_TOKEN (32+ random characters, the same as the hub's) - from a Secret.")
        mode, index = server_mode.pip_settings()
        try:
            quota = float(os.environ.get("STUDIO_KERNEL_QUOTA_GB", "") or 5)
        except ValueError:
            quota = 5.0
        return cls(token=token, python=os.environ.get("STUDIO_NOTEBOOK_PYTHON", "").strip() or "/opt/notebook-env/bin/python",
                   data_dir=os.environ.get("STUDIO_KERNEL_DATA_DIR", "").strip() or "/kernels",
                   runtime=os.environ.get("STUDIO_GATEWAY_RUNTIME", "").strip() or "/tmp/studio-gateway",
                   bind=os.environ.get("STUDIO_GATEWAY_BIND", "").strip() or "0.0.0.0", port=_int("STUDIO_GATEWAY_PORT", 8888),
                   memory_mb=_int("STUDIO_KERNEL_MEMORY_MB", 3072), nice=_int("STUDIO_KERNEL_NICE", 10), quota_gb=quota,
                   pip_mode=mode, pip_index=index, kernels_total=max(1, _int("STUDIO_KERNELS_TOTAL", 20)),
                   kernels_per_user=max(1, _int("STUDIO_KERNELS_PER_USER", 3)),
                   idle_minutes=max(1, _int("STUDIO_KERNEL_IDLE_MINUTES", 30)),
                   eg=os.environ.get("STUDIO_GATEWAY_EG", "").strip() or "/opt/gateway-env/bin/jupyter-enterprisegateway")


def prepare(s: GatewaySettings) -> dict[str, str]:
    """Write the kernelspec and the config; returns the gateway process's environment."""
    root = Path(s.runtime)
    spec_dir = root / "kernels" / KERNEL_NAME
    spec_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("config", "runtime", "data"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    Path(s.data_dir, "users").mkdir(parents=True, exist_ok=True)
    argv = [s.python, str(LAUNCHER), "--data-dir", s.data_dir, "--memory-mb", str(s.memory_mb), "--nice", str(s.nice),
            "--pip-mode", s.pip_mode, "--quota-gb", str(s.quota_gb)]
    if s.pip_mode == "mirror" and s.pip_index:
        argv += ["--pip-index", s.pip_index]
    spec = {"display_name": "Studio Python", "language": "python", "interrupt_mode": "signal",
            "argv": [*argv, "-f", "{connection_file}"], "metadata": {"studio": {"memory_mb": s.memory_mb, "pip_mode": s.pip_mode}}}
    (spec_dir / "kernel.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
    (root / "config" / "jupyter_enterprise_gateway_config.py").write_text(_CONFIG, encoding="utf-8")
    if os.name != "nt":
        for p in (root, root / "runtime"):
            os.chmod(p, 0o700)
    env = {k: v for k, v in os.environ.items() if k in INHERITED or k in ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "USER", "LOGNAME", "USERNAME",
                                                                          "LOCALAPPDATA", "APPDATA")}
    env.update({
        "EG_AUTH_TOKEN": s.token,
        "JUPYTER_PATH": str(root),            # the only kernelspec: studio-python
        "JUPYTER_CONFIG_DIR": str(root / "config"),
        "JUPYTER_RUNTIME_DIR": str(root / "runtime"),
        "JUPYTER_DATA_DIR": str(root / "data"),
        "JUPYTER_CONFIG_PATH": str(root / "config"),
        "EG_INHERITED_ENVS": ",".join(INHERITED),
        "EG_CLIENT_ENVS": "",                 # only KERNEL_* from the hub (the launcher keeps just KERNEL_USERNAME)
        "EG_MAX_KERNELS_PER_USER": str(s.kernels_per_user + 1),  # +1: the hub's package installs
        "EG_KERNEL_LAUNCH_TIMEOUT": "60",
        "EG_LIST_KERNELS": "False",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def command(s: GatewaySettings) -> list[str]:
    return [s.eg, f"--ip={s.bind}", f"--port={s.port}", "--port_retries=0",
            f"--EnterpriseGatewayApp.default_kernel_name={KERNEL_NAME}",
            f"--EnterpriseGatewayApp.max_kernels={s.kernels_total + 2}",
            f"--KernelSpecManager.whitelist=['{KERNEL_NAME}']", "--KernelSpecManager.ensure_native_kernel=False",
            f"--MappingKernelManager.cull_idle_timeout={s.idle_minutes * 60}", "--MappingKernelManager.cull_interval=60",
            "--MappingKernelManager.cull_connected=True", "--MappingKernelManager.cull_busy=False",
            f"--log-level={s.log_level}"]


def main() -> int:
    s = GatewaySettings.from_env()
    if not Path(s.eg).exists() and not shutil.which(s.eg):
        raise SystemExit(f"Enterprise Gateway not found at {s.eg} (STUDIO_GATEWAY_EG; the image's INSTALL_GATEWAY=1).")
    if not Path(s.python).exists():
        raise SystemExit(f"The kernels' Python {s.python} isn't there (STUDIO_NOTEBOOK_PYTHON; the image's INSTALL_NOTEBOOKS=1).")
    env = prepare(s)
    print(f"Studio kernel gateway on {s.bind}:{s.port}: memory {s.memory_mb} MB per kernel, nice {s.nice}, "
          f"{s.kernels_per_user} kernels per user / {s.kernels_total} in total, idle stop {s.idle_minutes} min, "
          f"pip {s.pip_mode}, kernel volume {s.data_dir}", file=sys.stderr, flush=True)
    cmd = command(s)
    if not sys.platform.startswith("linux"):
        return subprocess.call(cmd, env=env)  # local tests (Windows / macOS)
    return _supervise(cmd, env)


def _non_dumpable() -> None:
    try:
        import ctypes

        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 0
    except Exception:  # noqa: BLE001 - not Linux / no libc: nothing to protect with
        pass


def _supervise(cmd: list[str], env: dict[str, str]) -> int:
    """PID 1 of the gateway container (instead of tini): non-dumpable, so a kernel (same OS user) can't read the
    auth token from ``/proc/1/environ``; forwards SIGTERM / SIGINT to the gateway and reaps orphaned processes (a
    kernel's leftover shell commands)."""
    import signal

    _non_dumpable()
    os.environ.pop("STUDIO_GATEWAY_AUTH_TOKEN", None)
    proc = subprocess.Popen(cmd, env=env)
    env.clear()

    def forward(signum: int, _frame: object) -> None:
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, forward)
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            return proc.returncode or 0
        except InterruptedError:
            continue
        if pid == proc.pid:
            return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
