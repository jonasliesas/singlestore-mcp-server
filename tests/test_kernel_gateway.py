"""Notebook kernels behind the kernel gateway (STUDIO_NOTEBOOKS=gateway, step 2a).

Unit tests (always): the kernel launcher's environment and limits, the settings, installs refused with
STUDIO_PIP_MODE=off.

End to end (marker ``gateway``; skipped unless a Jupyter Enterprise Gateway venv is given): a local Enterprise Gateway
process (``python -m singlestore_mcp.kernel_gateway``), the hub with real Studio processes and the fake SAS Logon, two
users running cells. Checks that a Studio reaches only its own kernels, that tokens arrive through the relay, that
interrupt / restart / limits / idle stop work, and that package installs go to the user's own folder. Needs:

    S2_TEST_EG_VENV       a venv with jupyter_enterprise_gateway (e.g. ``uv venv eg --python 3.11`` +
                          ``uv pip install --python eg jupyter_enterprise_gateway==3.3.0``)
    S2_TEST_KERNEL_PYTHON the kernels' Python (ipykernel, pandas, singlestoredb; default: the desktop notebook
                          environment ~/.singlestore-mcp/notebook-env, if there)

No cluster, no real SAS Logon, no passwords. Takes ~1-2 min.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from singlestore_mcp import kernel_launcher, server_mode

GATEWAY_TOKEN = "test-gateway-token-not-a-real-one-0123456789"  # noqa: S105 - only valid against the test gateway
MEMORY_MB = 1536


# ----------------------------------------------------------------------------- unit


def test_launcher_gives_kernels_a_clean_environment(tmp_path: Path) -> None:
    source = {"PATH": "/usr/bin", "EG_AUTH_TOKEN": "x" * 40, "STUDIO_GATEWAY_AUTH_TOKEN": "x" * 40, "STUDIO_MASTER_KEY": "m",
              "KERNEL_USERNAME": "alice", "KERNEL_ID": "k1", "JPY_PARENT_PID": "1", "KERNEL_EXTRA": "from the client",
              "SINGLESTORE_MCP_SECRET_KEY": "s", "HTTPS_PROXY": "http://proxy:8080"}
    dirs = kernel_launcher.user_dirs(str(tmp_path), "alice")
    env = kernel_launcher.kernel_env(source, "alice", dirs, "off", "", 5)
    assert not [k for k in env if k.startswith(("EG_", "STUDIO_", "SINGLESTORE_"))] and "KERNEL_EXTRA" not in env
    assert env["KERNEL_ID"] == "k1" and env["JPY_PARENT_PID"] == "1" and env["HTTPS_PROXY"] == "http://proxy:8080"
    assert env["HOME"] == dirs["files"] == str(tmp_path / "users" / "alice" / "files")
    assert env["PIP_TARGET"] == env["PYTHONPATH"] == dirs["pip"] and env["PIP_NO_INDEX"] == "1"
    assert env["S2_KERNEL_QUOTA_BYTES"] == str(5 * 1024 ** 3)
    mirror = kernel_launcher.kernel_env(source, "alice", dirs, "mirror", "https://mirror.example/simple", 5)
    assert mirror["PIP_INDEX_URL"] == "https://mirror.example/simple" and "PIP_NO_INDEX" not in mirror
    assert kernel_launcher.kernel_env(source, "alice", dirs, "pypi", "", 5)["PIP_INDEX_URL"] == kernel_launcher.PYPI


def test_launcher_refuses_a_bad_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("", "../etc", "Alice", "a/b", "x" * 81):
        monkeypatch.setenv("KERNEL_USERNAME", bad)
        assert kernel_launcher.main(["--data-dir", str(tmp_path), "-f", "c.json"]) == 2
    assert not (tmp_path / "users").exists()


def test_pip_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("STUDIO_PIP_MODE", "STUDIO_PIP_INDEX_URL", "SINGLESTORE_MCP_PIP_MODE", "SINGLESTORE_MCP_PIP_INDEX_URL"):
        monkeypatch.delenv(name, raising=False)
    assert server_mode.pip_settings() == ("off", "")
    monkeypatch.setenv("STUDIO_PIP_INDEX_URL", "https://mirror.example/simple")
    assert server_mode.pip_settings() == ("mirror", "https://mirror.example/simple")
    monkeypatch.setenv("STUDIO_PIP_MODE", "pypi")
    assert server_mode.pip_settings()[0] == "pypi"
    monkeypatch.setenv("STUDIO_PIP_MODE", "anything")
    with pytest.raises(SystemExit):
        server_mode.pip_settings()
    monkeypatch.setenv("STUDIO_PIP_MODE", "mirror")
    monkeypatch.delenv("STUDIO_PIP_INDEX_URL")
    with pytest.raises(SystemExit):
        server_mode.pip_settings()


def test_hub_config_for_the_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from singlestore_mcp.hub import config

    monkeypatch.setenv("STUDIO_VIYA_URL", "https://viya.example")
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STUDIO_MASTER_KEY", "k" * 40)
    monkeypatch.setenv("STUDIO_NOTEBOOKS", "gateway")
    monkeypatch.delenv("STUDIO_PIP_MODE", raising=False)
    monkeypatch.delenv("STUDIO_PIP_INDEX_URL", raising=False)
    with pytest.raises(SystemExit, match="STUDIO_GATEWAY_URL"):
        config.load()
    monkeypatch.setenv("STUDIO_GATEWAY_URL", "http://studio-gateway:8888/")
    monkeypatch.setenv("STUDIO_GATEWAY_AUTH_TOKEN", "short")
    with pytest.raises(SystemExit):
        config.load()
    monkeypatch.setenv("STUDIO_GATEWAY_AUTH_TOKEN", GATEWAY_TOKEN)
    cfg = config.load()
    assert cfg.gateway and cfg.notebooks and cfg.gateway_url == "http://studio-gateway:8888"
    assert (cfg.relay_port, cfg.kernels_per_user, cfg.kernels_total, cfg.pip_mode) == (8801, 3, 20, "off")
    monkeypatch.setenv("STUDIO_NOTEBOOKS", "local")
    cfg = config.load()
    assert cfg.notebooks and not cfg.gateway and cfg.notebooks_mode == "local"


def test_gateway_settings_and_kernelspec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from singlestore_mcp import kernel_gateway

    monkeypatch.delenv("STUDIO_GATEWAY_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        kernel_gateway.GatewaySettings.from_env()
    monkeypatch.setenv("STUDIO_GATEWAY_AUTH_TOKEN", GATEWAY_TOKEN)
    monkeypatch.setenv("STUDIO_GATEWAY_RUNTIME", str(tmp_path / "rt"))
    monkeypatch.setenv("STUDIO_KERNEL_DATA_DIR", str(tmp_path / "kernels"))
    monkeypatch.setenv("STUDIO_KERNEL_MEMORY_MB", "2048")
    monkeypatch.setenv("STUDIO_PIP_MODE", "off")
    monkeypatch.setenv("STUDIO_MASTER_KEY", "must-not-reach-the-gateway")
    s = kernel_gateway.GatewaySettings.from_env()
    env = kernel_gateway.prepare(s)
    spec = json.loads((tmp_path / "rt" / "kernels" / "studio-python" / "kernel.json").read_text("utf-8"))
    assert spec["argv"][1].endswith("kernel_launcher.py") and "2048" in spec["argv"] and spec["argv"][-1] == "{connection_file}"
    assert env["EG_AUTH_TOKEN"] == GATEWAY_TOKEN and "STUDIO_MASTER_KEY" not in env and "STUDIO_GATEWAY_AUTH_TOKEN" not in env
    assert "EG_AUTH_TOKEN" not in env["EG_INHERITED_ENVS"].split(",")
    cmd = " ".join(kernel_gateway.command(s))
    assert "cull_busy=False" in cmd and "whitelist=['studio-python']" in cmd and GATEWAY_TOKEN not in cmd


def test_studio_refuses_installs_when_pip_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from singlestore_mcp import notebook_kernel

    monkeypatch.setenv("SINGLESTORE_MCP_SERVER_MODE", "1")
    monkeypatch.setenv("SINGLESTORE_MCP_NOTEBOOKS_MODE", "gateway")
    monkeypatch.setenv("SINGLESTORE_MCP_PIP_MODE", "off")
    for name in ("STUDIO_PIP_MODE", "STUDIO_PIP_INDEX_URL", "SINGLESTORE_MCP_PIP_INDEX_URL"):
        monkeypatch.delenv(name, raising=False)
    server_mode.set_notebooks(True)
    try:
        st = notebook_kernel.environment_status()
        assert st["state"] == "ready" and st["kernels"] == "gateway" and st["pip_mode"] == "off"
        with pytest.raises(server_mode.FeatureOff, match="STUDIO_PIP_MODE=off"):
            notebook_kernel.install_packages(["requests"])
        assert notebook_kernel.setup()["state"] == "ready"  # nothing to install on the Studio side
    finally:
        server_mode.set_notebooks(None)


def test_websocket_client_against_a_tiny_server() -> None:
    """The Studio's own WebSocket client: RFC 6455 handshake, masked frames both ways, long messages, ping, close."""
    import base64
    import hashlib
    import struct

    from singlestore_mcp.notebook_gateway import WebSocket, _mask

    assert _mask(_mask(b"hello world", b"abcd"), b"abcd") == b"hello world"
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    crlf = b"\r\n"

    def read(conn: socket.socket, buf: bytes, n: int) -> bytes:
        while len(buf) < n:
            buf += conn.recv(65536)
        return buf

    def serve() -> None:
        conn, _ = srv.accept()
        head = b""
        while crlf * 2 not in head:
            head += conn.recv(4096)
        key = next(line.split(":", 1)[1].strip() for line in head.decode().split("\r\n")
                   if line.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     + f"Sec-WebSocket-Accept: {accept}".encode() + crlf * 2)
        conn.sendall(bytes([0x89, 0x00]))  # a ping first
        buf = b""
        while True:  # echo each client text frame back, unmasked
            buf = read(conn, buf, 2)
            n, off = buf[1] & 0x7F, 2
            if n == 126:
                buf = read(conn, buf, 4)
                n, off = struct.unpack(">H", buf[2:4])[0], 4
            elif n == 127:
                buf = read(conn, buf, 10)
                n, off = struct.unpack(">Q", buf[2:10])[0], 10
            buf = read(conn, buf, off + 4 + n)
            opcode, mask, payload = buf[0] & 0x0F, buf[off:off + 4], buf[off + 4:off + 4 + n]
            buf = buf[off + 4 + n:]
            if opcode == 0xA:
                continue  # the client's pong
            if opcode == 0x8:
                conn.close()
                return
            data = _mask(payload, mask)
            length = bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n)
            conn.sendall(bytes([0x81]) + length + data)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ws = WebSocket(f"http://127.0.0.1:{srv.getsockname()[1]}/x", {"X-Test": "1"})
    for text in ("short", "é" * 300, "x" * 70000):
        ws.send(text)
        assert ws.recv() == text
    ws.close()
    t.join(5)
    srv.close()


# ----------------------------------------------------------------------------- end to end


def _eg_executable() -> Path | None:
    venv = os.environ.get("S2_TEST_EG_VENV")
    if not venv:
        return None
    for rel in ("Scripts/jupyter-enterprisegateway.exe", "bin/jupyter-enterprisegateway"):
        if (Path(venv) / rel).exists():
            return Path(venv) / rel
    return None


def _kernel_python() -> Path | None:
    configured = os.environ.get("S2_TEST_KERNEL_PYTHON")
    if configured:
        return Path(configured) if Path(configured).exists() else None
    env = Path.home() / ".singlestore-mcp" / "notebook-env"
    exe = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.exists() else None


def _free_port() -> int:
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port not in (8800, 8801, 8851, 8852, 8853):
            return port


def _tiny_wheel(folder: Path) -> None:
    """A one-module wheel and a PEP 503 'simple' index for it (the package mirror of the test)."""
    name, version = "s2tinypkg", "0.1"
    dist = f"{name}-{version}.dist-info"
    files = {f"{name}/__init__.py": "VALUE = 42\n",
             f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: test package\n",
             f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    files[f"{dist}/RECORD"] = "".join(f"{p},,\n" for p in files) + f"{dist}/RECORD,,\n"
    (folder / "files").mkdir(parents=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p, text in files.items():
            z.writestr(p, text)
    whl = f"{name}-{version}-py3-none-any.whl"
    (folder / "files" / whl).write_bytes(buf.getvalue())
    (folder / "simple" / name).mkdir(parents=True)
    (folder / "simple" / name / "index.html").write_text(f'<a href="../../files/{whl}">{whl}</a>', encoding="utf-8")


@pytest.fixture(scope="module")
def mirror(tmp_path_factory: pytest.TempPathFactory):
    folder = tmp_path_factory.mktemp("mirror")
    _tiny_wheel(folder)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(folder)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/simple"
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def gateway(tmp_path_factory: pytest.TempPathFactory, mirror: str):
    eg, python = _eg_executable(), _kernel_python()
    if eg is None or python is None:
        pytest.skip("kernel gateway tests: set S2_TEST_EG_VENV (a venv with jupyter_enterprise_gateway) and have a "
                    "notebook environment (S2_TEST_KERNEL_PYTHON)")
    root = tmp_path_factory.mktemp("gateway")
    port = _free_port()
    keep = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA",
            "HOME", "USERPROFILE", "LANG", "USERNAME", "USER")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update({"STUDIO_GATEWAY_AUTH_TOKEN": GATEWAY_TOKEN, "STUDIO_GATEWAY_BIND": "127.0.0.1", "STUDIO_GATEWAY_PORT": str(port),
                "STUDIO_GATEWAY_EG": str(eg), "STUDIO_NOTEBOOK_PYTHON": str(python),
                "STUDIO_GATEWAY_RUNTIME": str(root / "runtime"), "STUDIO_KERNEL_DATA_DIR": str(root / "kernels"),
                "STUDIO_KERNEL_MEMORY_MB": str(MEMORY_MB), "STUDIO_KERNEL_NICE": "10", "STUDIO_KERNELS_PER_USER": "2",
                "STUDIO_KERNELS_TOTAL": "4", "STUDIO_PIP_MODE": "mirror", "STUDIO_PIP_INDEX_URL": mirror,
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                "STUDIO_MASTER_KEY": "must-not-reach-a-kernel",  # as if the ConfigMap/Secret leaked it to the gateway pod
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    log = open(root / "gateway.log", "wb")  # noqa: SIM115
    proc = subprocess.Popen([sys.executable, "-m", "singlestore_mcp.kernel_gateway"], env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{url}/api", headers={"Authorization": f"token {GATEWAY_TOKEN}"})
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3).read()
            break
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("the local kernel gateway didn't start: " + (root / "gateway.log").read_text("utf-8", "replace")[-2000:])
    if proc.poll() is not None:
        pytest.fail("the local kernel gateway stopped: " + (root / "gateway.log").read_text("utf-8", "replace")[-2000:])
    yield {"url": url, "root": root, "proc": proc}
    if os.name == "nt":  # the gateway and its kernels are children of kernel_gateway: stop the whole tree
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="module")
def logon():
    from fake_saslogon import FakeSasLogon

    fake = FakeSasLogon()
    yield fake
    fake.close()


@pytest.fixture(scope="module")
def hub(tmp_path_factory: pytest.TempPathFactory, logon: Any, gateway: dict[str, Any], mirror: str):
    from fake_saslogon import TEST_CLIENT_SECRET
    from test_hub import MASTER

    from singlestore_mcp.hub.app import Hub
    from singlestore_mcp.hub.audit import Audit
    from singlestore_mcp.hub.config import HubConfig

    data = tmp_path_factory.mktemp("hubdata")
    cfg = HubConfig(viya_url=logon.url, data_dir=data, master_key=MASTER, oauth_mode="redirect", client_id="studio-hub",
                    client_secret=TEST_CLIENT_SECRET, port=0, admins=("alice",), max_users=5, metrics=False,
                    viya_name="Zeus", notebooks=True, notebooks_mode="gateway", gateway_url=gateway["url"],
                    gateway_token=GATEWAY_TOKEN, kernels_per_user=2, kernels_total=4, relay_port=0,
                    pip_mode="mirror", pip_index=mirror, users_group="SpeedyStoreStudioUsers",
                    notebook_group="SpeedyStoreStudioNotebooks")
    h = Hub(cfg, Audit(data / "hub" / "audit.jsonl", echo=False))
    port = h.serve(block=False)
    cfg.public_url = f"http://127.0.0.1:{port}"
    logon.register_redirect("studio-hub", cfg.redirect_uri)
    logon.memberships.update(alice=["SpeedyStoreStudioNotebooks"], bob=["SpeedyStoreStudioNotebooks"])
    yield h
    h.close()


@pytest.fixture(scope="module")
def users(hub: Any, logon: Any) -> dict[str, Any]:
    from test_hub import sign_in

    return {"alice": sign_in(hub, logon, "alice"), "bob": sign_in(hub, logon, "bob")}


def call(b: Any, name: str, arguments: dict | None = None, ok: bool = True) -> dict:
    deadline = time.time() + 120
    while True:  # the Studio may still be starting
        r = b.call(name, arguments)
        if r.status != 503 or time.time() > deadline:
            break
        time.sleep(1)
    assert r.status == 200, r.text[:300]
    res = r.json()
    if ok:
        assert not res.get("isError"), res
    return res.get("structuredContent") or res


def wait_kernel(b: Any, nid: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while True:
        st = call(b, "notebook_kernel_state", {"notebook_id": nid})
        if st.get("state") in ("idle", "error", "dead") or time.time() > deadline:
            return st
        time.sleep(0.5)


def run(b: Any, nid: str, code: str, timeout: float = 120, on_start: Any = None) -> dict:
    run_id = call(b, "notebook_run", {"notebook_id": nid, "cell_type": "python", "source": code})["run_id"]
    if on_start:
        on_start()
    outputs: list = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = call(b, "notebook_poll", {"run_id": run_id, "after": len(outputs)})
        outputs += p["outputs"]
        if p["done"]:
            text = "".join(o.get("text", "") for o in outputs if o.get("output_type") == "stream")
            return {"status": p["status"], "outputs": outputs, "text": text,
                    "errors": [o for o in outputs if o.get("output_type") == "error"], "seconds": p["seconds"]}
        time.sleep(0.3)
    raise AssertionError(f"cell didn't finish in {timeout} s: {outputs}")


def last_json(cell: dict) -> Any:
    assert cell["status"] == "ok", cell
    return json.loads(cell["text"].strip().splitlines()[-1])


PROBE = r'''
import json, os, subprocess, sys
env = dict(os.environ)
out = subprocess.run(json.loads(os.environ["SINGLESTORE_MCP_SAS_TOKEN_CMD"]), capture_output=True, text=True, timeout=60)
print(json.dumps({"cwd": os.getcwd(), "home": os.environ.get("HOME"), "env": env, "sas_token": out.stdout.strip(),
                  "sas_rc": out.returncode, "relay": os.environ.get("SINGLESTORE_MCP_KERNEL_TOKENS")}))
'''


@pytest.mark.gateway
def test_kernels_run_in_their_users_folder_with_tokens_from_the_relay(users: dict[str, Any], hub: Any, logon: Any,
                                                                      gateway: dict[str, Any]) -> None:
    alice, bob = users["alice"], users["bob"]
    assert call(alice, "notebook_environment")["kernels"] == "gateway"
    for b, nid in ((alice, "nb-a"), (bob, "nb-b")):
        call(b, "notebook_kernel_start", {"notebook_id": nid})
    for b, nid in ((alice, "nb-a"), (bob, "nb-b")):
        st = wait_kernel(b, nid)
        assert st["state"] == "idle", st
    a, b_ = last_json(run(alice, "nb-a", PROBE)), last_json(run(bob, "nb-b", PROBE))
    kroot = gateway["root"] / "kernels" / "users"
    assert Path(a["cwd"]) == kroot / "alice" / "files" and Path(b_["cwd"]) == kroot / "bob" / "files"
    # Each kernel got its own user's Viya token, through the relay (the hub's), never the hub's or the gateway's secrets.
    assert a["sas_rc"] == 0 and logon.access[a["sas_token"]]["user"] == "alice"
    assert b_["sas_rc"] == 0 and logon.access[b_["sas_token"]]["user"] == "bob"
    assert a["relay"].startswith(hub.broker.relay_url + "/k/") and a["relay"] != b_["relay"]
    from test_hub import MASTER

    for probe in (a, b_):
        dump = json.dumps(probe["env"])
        assert not [k for k in probe["env"] if k.startswith(("EG_", "STUDIO_"))]
        for secret in (GATEWAY_TOKEN, MASTER.decode(), hub.cfg.user_key("alice"), hub.cfg.user_key("bob"),
                       "must-not-reach-a-kernel", *logon.refresh):
            assert secret not in dump
    assert a["sas_token"] not in json.dumps(b_["env"]) and b_["sas_token"] not in json.dumps(a["env"])
    # A wrong relay secret gets nothing.
    bad = a["relay"].rsplit("/", 1)[0] + "/not-the-secret/sas/Zeus"
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(bad, timeout=10)
    assert err.value.code == 404


@pytest.mark.gateway
def test_a_studio_reaches_only_its_own_kernels(users: dict[str, Any], hub: Any, gateway: dict[str, Any]) -> None:
    alice, bob = users["alice"], users["bob"]
    for b, nid in ((alice, "nb-a"), (bob, "nb-b")):
        call(b, "notebook_kernel_start", {"notebook_id": nid})
        assert wait_kernel(b, nid)["state"] == "idle"
    # Through the app: alice's Studio knows nothing of bob's notebook.
    assert call(alice, "notebook_kernel_state", {"notebook_id": "nb-b"})["state"] == "stopped"
    bobs = [k for k in hub.broker.kernels() if k.user == "bob"]
    assert bobs
    kid = bobs[0].id
    # Through the broker: another Studio's key (carol), or no / a wrong kernel key, gets the same 404 as no kernel.
    carol = hub.broker.register_studio("carol")
    host, port = hub.broker.url.split("//")[1].split(":")

    def broker(method: str, path: str, headers: dict[str, str]) -> int:
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        try:
            conn.request(method, path, body=b"{}" if method != "GET" else None, headers=headers)
            return conn.getresponse().status
        finally:
            conn.close()

    ws = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13"}
    for headers in ({"X-Studio-Key": carol}, {"X-Studio-Key": carol, "X-Kernel-Key": "guess"}):
        assert broker("GET", f"/kernels/{kid}", headers) == 404
        assert broker("POST", f"/kernels/{kid}/interrupt", headers) == 404
        assert broker("DELETE", f"/kernels/{kid}", headers) == 404
        assert broker("GET", f"/kernels/{kid}/channels", {**headers, **ws}) == 404
    assert broker("GET", f"/kernels/{kid}", {"X-Studio-Key": "not-a-studio"}) == 403
    with pytest.raises(Exception):  # noqa: B017 - bob's own user, but without the kernel's key
        hub.broker._kernel_for("bob", kid, "wrong")
    from singlestore_mcp.hub.gateway import GatewayError

    with pytest.raises(GatewayError) as err:  # carol has no notebook access (never signed in): no kernel at all
        hub.broker.start_kernel("carol", "")
    assert err.value.status == 403
    hub.broker.forget_studio("carol")
    # The gateway itself: nothing without the hub's token (REST and channels).
    gw = gateway["url"].split("//")[1]
    conn = http.client.HTTPConnection(*gw.split(":"), timeout=10)
    conn.request("GET", f"/api/kernels/{kid}")
    assert conn.getresponse().status == 401
    conn.close()
    conn = http.client.HTTPConnection(*gw.split(":"), timeout=10)
    conn.request("GET", f"/api/kernels/{kid}/channels", headers=ws)
    assert conn.getresponse().status in (401, 403)
    conn.close()
    assert hub.broker.gateway("GET", "/api/kernels")[0] == 403  # with the token: still no listing of kernels
    # bob's kernel still works.
    assert run(bob, "nb-b", "print(6 * 7)")["text"].strip() == "42"


@pytest.mark.gateway
def test_interrupt_and_restart(users: dict[str, Any]) -> None:
    alice = users["alice"]
    call(alice, "notebook_kernel_start", {"notebook_id": "nb-a"})
    assert wait_kernel(alice, "nb-a")["state"] == "idle"
    assert run(alice, "nb-a", "x_before = 1\nprint('set')")["text"].strip() == "set"

    def interrupt_soon() -> None:
        def go() -> None:
            time.sleep(2)
            call(alice, "notebook_kernel_control", {"notebook_id": "nb-a", "action": "interrupt"})

        threading.Thread(target=go, daemon=True).start()

    t0 = time.time()
    cell = run(alice, "nb-a", "import time\nfor _ in range(600):\n    time.sleep(0.1)\nprint('not interrupted')", on_start=interrupt_soon)
    assert time.time() - t0 < 30 and cell["status"] == "error" and cell["errors"][0]["ename"] == "KeyboardInterrupt", cell
    assert run(alice, "nb-a", "print(x_before)")["text"].strip() == "1"  # interrupted, not restarted
    call(alice, "notebook_kernel_control", {"notebook_id": "nb-a", "action": "restart"})
    time.sleep(1)
    assert wait_kernel(alice, "nb-a")["state"] == "idle"
    after = last_json(run(alice, "nb-a", "import json, os\nprint(json.dumps(['x_before' in dir(), "
                                         "bool(os.environ.get('SINGLESTORE_MCP_KERNEL_TOKENS')), 'pd' in dir()]))"))
    assert after == [False, True, True]  # fresh state; prelude and start-up code ran again


@pytest.mark.gateway
def test_limits_apply(users: dict[str, Any], hub: Any) -> None:
    from singlestore_mcp.hub.gateway import GatewayError

    alice = users["alice"]
    call(alice, "notebook_kernel_start", {"notebook_id": "nb-a"})
    assert wait_kernel(alice, "nb-a")["state"] == "idle"
    probe = last_json(run(alice, "nb-a", r'''
import json, os, sys
if os.name == "nt":
    import ctypes
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    prio = k.GetPriorityClass(ctypes.c_void_p(k.GetCurrentProcess()))
    low = prio == 0x4000
else:
    low = os.getpriority(os.PRIO_PROCESS, 0) >= 10
def rss():
    if sys.platform.startswith("linux"):
        return int([l for l in open("/proc/self/status") if l.startswith("VmRSS")][0].split()[1]) * 1024
    import ctypes
    from ctypes import wintypes
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("f", wintypes.DWORD), ("peak", ctypes.c_size_t), ("ws", ctypes.c_size_t)] + \
                   [(n, ctypes.c_size_t) for n in ("a", "b", "c", "d", "pf", "ppf")]
    pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentProcess()), ctypes.byref(pmc), pmc.cb)
    return pmc.ws
print(json.dumps({"low": low, "rss": rss(), "threads": os.environ.get("OPENBLAS_NUM_THREADS")}))
'''))
    assert probe["low"] is True and probe["threads"] == "2"
    print(f"\nmemory of one gateway kernel after start-up (pandas, singlestoredb, matplotlib): {probe['rss'] / 2**20:.0f} MB")
    big = run(alice, "nb-a", f"blob = bytearray({MEMORY_MB + 512} * 1024 * 1024)")
    assert big["status"] == "error" and big["errors"][0]["ename"] == "MemoryError", big
    assert run(alice, "nb-a", "print('still alive')")["text"].strip() == "still alive"
    # Kernels per user (2): the broker refuses a third; the Studio closes its oldest idle one first instead.
    call(alice, "notebook_kernel_start", {"notebook_id": "nb-a2"})
    assert wait_kernel(alice, "nb-a2")["state"] == "idle"
    assert len([k for k in hub.broker.kernels() if k.user == "alice"]) == 2
    with pytest.raises(GatewayError) as err:
        hub.broker.start_kernel("alice", "")
    assert err.value.status == 429 and "per user" in str(err.value)
    call(alice, "notebook_kernel_start", {"notebook_id": "nb-a3"})
    assert wait_kernel(alice, "nb-a3")["state"] == "idle"
    deadline = time.time() + 20
    while len([k for k in hub.broker.kernels() if k.user == "alice"]) > 2 and time.time() < deadline:
        time.sleep(0.5)
    assert len([k for k in hub.broker.kernels() if k.user == "alice"]) == 2
    assert call(alice, "notebook_kernel_state", {"notebook_id": "nb-a"})["state"] == "stopped"  # the oldest idle one
    call(alice, "notebook_kernel_control", {"notebook_id": "nb-a3", "action": "shutdown"})


@pytest.mark.gateway
def test_packages_go_to_the_users_own_folder(users: dict[str, Any], gateway: dict[str, Any]) -> None:
    alice, bob = users["alice"], users["bob"]
    call(alice, "notebook_packages_install", {"packages": "s2tinypkg"})
    deadline = time.time() + 180
    while True:
        st = call(alice, "notebook_environment")
        if st["state"] != "installing" or time.time() > deadline:
            break
        time.sleep(1)
    log = "\n".join(st["log"])
    assert st["state"] == "ready" and "Error" not in log, log
    assert "of 5.00 GB" in log and "Successfully installed s2tinypkg" in log
    assert (gateway["root"] / "kernels" / "users" / "alice" / "pip" / "s2tinypkg" / "__init__.py").exists()
    names = {p["name"] for p in call(alice, "notebook_packages")["packages"]}
    assert "s2tinypkg" in names and "pandas" in names
    for b, nid in ((alice, "nb-a2"), (bob, "nb-b")):
        call(b, "notebook_kernel_start", {"notebook_id": nid})
        assert wait_kernel(b, nid)["state"] == "idle"
    call(alice, "notebook_kernel_control", {"notebook_id": "nb-a2", "action": "restart"})
    time.sleep(1)
    assert wait_kernel(alice, "nb-a2")["state"] == "idle"
    assert run(alice, "nb-a2", "import s2tinypkg; print(s2tinypkg.VALUE)")["text"].strip() == "42"
    other = run(bob, "nb-b", "import s2tinypkg")
    assert other["status"] == "error" and other["errors"][0]["ename"] == "ModuleNotFoundError"
    # The image's environment can't be changed from a kernel: `pip install` goes to the user's own folder.
    where = run(alice, "nb-a2", "import os; print(os.environ['PIP_TARGET'])")["text"].strip()
    assert Path(where) == gateway["root"] / "kernels" / "users" / "alice" / "pip"


@pytest.mark.gateway
def test_admin_page_shows_kernels_and_the_warning(users: dict[str, Any]) -> None:
    page = users["alice"].get("/admin")
    assert page.status == 200
    assert "Notebook kernels" in page.text and "no isolation between users&#x27; kernels" in page.text
    assert "package installs: mirror" in page.text and "answers" in page.text


@pytest.mark.gateway
def test_idle_culled_and_signed_out_kernels_are_dropped(users: dict[str, Any], hub: Any, gateway: dict[str, Any]) -> None:
    alice, bob = users["alice"], users["bob"]
    call(alice, "notebook_kernel_start", {"notebook_id": "nb-a2"})
    assert wait_kernel(alice, "nb-a2")["state"] == "idle"
    k = next(k for k in hub.broker.kernels() if k.user == "alice")
    # The gateway culls it (idle): the broker forgets it and the Studio sees it gone, then starts a new one on use.
    status, _ = hub.broker.gateway("DELETE", f"/api/kernels/{k.id}")
    assert status == 204
    assert k.id in hub.broker.reap_once()
    deadline = time.time() + 40
    while call(alice, "notebook_kernel_state", {"notebook_id": "nb-a2"})["state"] not in ("dead", "stopped") and time.time() < deadline:
        time.sleep(0.5)
    assert run(alice, "nb-a2", "print('new kernel')")["text"].strip() == "new kernel"
    # Signing out stops bob's Studio and his kernels at the gateway.
    call(bob, "notebook_kernel_start", {"notebook_id": "nb-b"})
    assert wait_kernel(bob, "nb-b")["state"] == "idle"
    bobs = [k.id for k in hub.broker.kernels() if k.user == "bob"]
    account = bob.get("/account")
    bob.post_form("/logout", {"csrf": account.hidden("csrf")})
    deadline = time.time() + 30
    while time.time() < deadline and any(hub.broker.gateway("GET", f"/api/kernels/{kid}")[0] != 404 for kid in bobs):
        time.sleep(0.5)
    assert all(hub.broker.gateway("GET", f"/api/kernels/{kid}")[0] == 404 for kid in bobs)
    assert not [k for k in hub.broker.kernels() if k.user == "bob"]


@pytest.mark.gateway
def test_no_secrets_in_logs(users: dict[str, Any], hub: Any, logon: Any, gateway: dict[str, Any]) -> None:
    from test_hub import MASTER

    logs = [Path(hub.cfg.data_dir) / "hub" / "audit.jsonl", gateway["root"] / "gateway.log",
            *(Path(hub.cfg.data_dir) / "users").rglob("studio.log")]
    text = " ".join(p.read_text("utf-8", "replace") for p in logs if p.exists())
    for secret in [*logon.access, *logon.refresh, MASTER.decode(), GATEWAY_TOKEN]:
        assert secret not in text
