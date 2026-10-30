# SAS SpeedyStore Studio on a server (pilot: step 1 + notebooks step 2a)

Studio (the Workspace of the SingleStore MCP server) as a web app for colleagues: sign in with your SAS user on
SAS Logon, get your own Studio, connect to SingleStore with your own database user. No Claude, no install on the
laptop. For the Kubernetes / Viya admin steps see [ADMIN-GUIDE.md](ADMIN-GUIDE.md).

```
browser ──TLS──> Ingress ──> hub (:8800) ── /studio/* ──> Studio process of THIS user (127.0.0.1:<random port>/<random token>/)
                              │  sign-in: SAS Logon (Authorization Code + PKCE)          │
                              │  session cookie, CSRF checks, idle stop, admin, audit     ├─> SingleStore (the user's own DB user)
                              └─ one process per user, own folder under /data/users/<id> └─> SAS Viya (the user's own token)
```

## What it does

- **Sign-in:** "Sign in with SAS" sends the browser to SAS Logon on the configured Viya (Zeus). The password is
  typed into SAS Logon only; the hub gets an authorization code (PKCE S256), exchanges it, and asks Viya who
  signed in (`/identities/users/@currentUser`). Two ways:
  - **redirect** (preferred): the hub's own OAuth client, registered once by the Viya admin with the redirect URI
    `https://<studio host>/oauth/callback`.
  - **paste** (fallback, nothing to register): Viya's built-in `vscode` client; SAS Logon shows a code that the user
    pastes into the hub's page - the same flow as "Sign in" under SAS Viya in Connections today.
- **One Studio per user:** `python -m singlestore_mcp.studio_server` in server mode, started at sign-in (or on the
  first request after an idle stop), with its own folders:
  - `/data/users/<id>/state` - `SINGLESTORE_MCP_HOME`: connections, encrypted secrets, editor tabs, history, workloads, `studio.log`
  - `/data/users/<id>/files` - `HOME`: the file browser's **My files** (SQL folder inside); nothing else on the server is browsable.
- **SAS as the user:** the user's Viya token (and refresh token) is pre-loaded into their Studio as the SAS Viya
  connection, so SAS Compute / CAS / Content run as that user. Tokens are renewed with the refresh token (with the
  client secret for the hub's own client).
- **SingleStore as the user:** each user adds their own connection in Connections (password stored encrypted in their
  folder). An optional default connection (host/port/database, user name = SAS user id, no password) is pre-filled.
- **Secrets:** no OS keyring on the server. Each user's `secrets.enc` (Fernet) is encrypted with a key derived from
  `STUDIO_MASTER_KEY` and that user's id; the key is handed to the process over stdin and never written to the volume.
- **Notebooks** (`STUDIO_NOTEBOOKS`): `off` (default), `gateway` (kernels on the kernel gateway, step 2a - see
  "Notebooks on the kernel gateway" below) or `local` - the user's own Studio process runs the notebook
  kernels itself, as on the desktop (no kernel gateway yet), only for users with notebook access (member of
  `SpeedyStoreStudioNotebooks`, or listed in `STUDIO_ALLOWED_USERS` when no group is set; never "everyone"). The
  kernels use the shared, read-only notebook environment `STUDIO_NOTEBOOK_PYTHON` (in the image: `/opt/notebook-env`;
  installing packages from the app is refused), start in the user's own folder (My files), and get the user's own
  SingleStore / SAS Viya tokens from their Studio (a token service on 127.0.0.1 under a random secret) - never the hub's
  key, the secrets file's key or anything of other users. The existing limits stay: 8 kernels per Studio, idle kernels
  stop after 30 min. Interrupt works on Linux (SIGINT). **Local notebooks: no isolation between users' kernels - test use with a few trusted users only; the kernel gateway (step 2a) replaces this.** The admin page says so too.
- **Off:** Ask Claude - hidden in the UI, refused by the server. Browser
  sign-ins that open a browser on the server (Helios SSO, Entra ID) are refused; use a password or a pasted JWT.
- **Idle stop:** a Studio with no use (the open pages' 30 s heartbeat doesn't count) for `STUDIO_IDLE_MINUTES` is
  stopped; its page says "Studio stopped" and starts it again on click. Sign-out stops it at once.
- **Access by SAS group:** members of `STUDIO_USERS_GROUP` (pilot: `SpeedyStoreStudioUsers`) may use Studio, members
  of `STUDIO_NOTEBOOK_GROUP` (`SpeedyStoreStudioNotebooks`) also notebooks - when `STUDIO_NOTEBOOKS=local`.
  The hub reads the user's groups at sign-in with the user's own token (`/identities/users/@currentUser/memberships`);
  each Studio checks again whenever it renews the Viya token, so leaving the group signs the user out within the token
  lifetime. The notebook flag is per user and enforced by Studio's notebook tools (not only hidden). Identities down:
  new sign-ins are refused, running ones carry on. The Viya admin creates the groups (ADMIN-GUIDE §8).
- **Admin page** (`/admin`, users in `STUDIO_ADMINS`): who is signed in, running Studios with memory and each user's Studio / notebook access (and which group gave it), stop one
  (also signs that user out), recent audit events. **Health:** `/healthz`.
- **Audit log:** `/data/hub/audit.jsonl` + stderr: hub start/stop, login (user, address, mode), failed/denied
  logins (`login_denied` with the reason, e.g. not in the group), logout, Studio start/stop (reason: idle / logout /
  admin / exited / hung / access removed), access changes and re-check failures, rate-limited sign-ins, links from
  other sites, admin actions, blocked CSRF. Never
  passwords, codes, tokens, cookies or keys.

### Security model (pilot)

- The browser only ever talks to the hub. The user's Studio is found from the session cookie - no user name, port or
  token is in any URL - so a user can't address someone else's Studio. Studios listen on 127.0.0.1 under a random path
  token that only the hub knows, and check Host and Origin.
- Session cookie: random id, `HttpOnly`, `SameSite=Lax`, `Secure` and `__Host-` prefixed behind HTTPS; new id at
  every sign-in; absolute lifetime `STUDIO_SESSION_HOURS`. The OAuth `state` is bound to a separate short-lived
  cookie (no login CSRF); one try per code.
- CSRF: every POST must carry the hub's own `Origin` (or `Sec-Fetch-Site: same-origin`); the hub's own forms also
  carry a per-session token. Cookies and `Authorization` are never forwarded to a Studio, and a Studio can't set cookies.
  A link from another site (`Sec-Fetch-Site: cross-site`) opens plain Studio: it can't choose the tool and arguments
  that the Studio page runs when it opens. The Studio's hub-only routes (`hub/seed`) can't be reached through `/studio/`.
- Proxy: only a short list of request headers goes to a Studio (no hop-by-hop headers, no websocket upgrade); requests
  with `Transfer-Encoding` or two `Content-Length` headers are refused and a GET's body is read (no request smuggling
  behind the ingress); 2 MB body limit; 120 s per read from the browser.
- Sign-in rate limit: `STUDIO_LOGIN_RATE` sign-in starts / code exchanges per client address and minute (429 after).
  Behind the ingress the client address is the **last** `X-Forwarded-For` hop (the one the ingress adds).
- TLS file paths of a user's SingleStore connection (CA, client certificate/key) must be in that user's own folder.
- Headers: CSP (`frame-ancestors 'none'`, no scripts on hub pages), `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: same-origin`, HSTS behind HTTPS.
- **Known limit:** all Studios run as the same OS user in one container. Isolation between users is the app's
  (routing, own folders, own encryption keys), not the OS's. That is acceptable in step 1 because no user code runs
  on the server - **except with `STUDIO_NOTEBOOKS=local`**: then the notebook users' Python code runs in the pod as that
  same OS user and can read other users' folders, kernels' environments (their tokens) and the hub's files. **Local notebooks: no isolation between users' kernels - test use with a few trusted users only; the kernel gateway (step 2a) replaces this.**
  With `STUDIO_NOTEBOOKS=gateway` users' code runs in the kernel gateway pod instead (no access to the hub pod, its
  volume or the Studios), but kernels still share that pod and OS user; one pod per kernel (step 2b) removes that.

### When a Studio crashes or hangs

The hub pings every Studio every 30 s. A Studio that exited on its own, or that misses 3 pings in a row (or a call
times out after `STUDIO_PROXY_TIMEOUT` and its ping fails too), is dropped and started again at once: the open page
shows "Reconnecting to your Studio…" and carries on (a call that was running when it crashed says it may or may not
have finished); a page load shows "Your Studio is restarting", which reloads itself. Tabs, connections and files are
on disk, so nothing is lost but unsaved work in that process. A Studio that crashes 3 times in 5 minutes isn't
restarted for a while (the page says so; its `studio.log` says why). The idle stop never stops a Studio with a call
in flight. Stopping the hub (`SIGTERM`, Ctrl+C) stops all Studios at once within ~10 s.

## Notebooks on the kernel gateway (`STUDIO_NOTEBOOKS=gateway`, step 2a)

Kernels run in their own pod behind **Jupyter Enterprise Gateway** instead of inside the users' Studio processes.
Studio stays in the hub pod (it runs only our code); only the kernels move.

```
hub pod                                                          kernel gateway pod (same image)
 hub ── kernel broker (127.0.0.1) ─────── REST + WebSocket ───────> Enterprise Gateway :8888 (token only the hub has)
  │      one key per Studio, one per kernel                            │ kernelspec studio-python:
  │                                                                    │   kernel_launcher.py -> ipykernel, per kernel:
  ├─ Studio (alice) ── its kernels only, through the broker            │   clean env, RLIMIT_AS, nice, own folder + pip dir
  ├─ Studio (bob)                                                      │
  └─ token relay :8801 <──── a kernel asks for ITS user's token ───────┘   /kernels/users/<id>/{files,pip,tmp}
        (per-kernel secret) -> that user's Studio's token service
```

- **Starting a kernel:** the notebook asks its Studio; the Studio asks the hub's kernel broker (`hub/gateway.py`,
  127.0.0.1 inside the hub pod, URL + a key for this Studio over its stdin); the broker checks the user's notebook
  access and the limits, starts a kernel on the gateway with the gateway's token (`KERNEL_USERNAME` = the user's
  folder id is all it sends), and gives the Studio the kernel's id and its own random **kernel key**. Cells, output,
  interrupt and restart then go Studio -> broker -> gateway: REST for interrupt / restart / shutdown, the kernel's
  WebSocket channels relayed byte for byte (`notebook_gateway.py` speaks the Jupyter protocol with a small stdlib
  WebSocket client and reuses the bridge's start-up code and output handling, so notebooks behave as on the desktop).
- **Isolation between Studios:** a Studio has no gateway token. Every broker request carries the Studio's key (which
  user) and, for a kernel, the kernel key; a kernel answers only to its own key from its own user's Studio - anything
  else gets the same 404 as an unknown kernel. A restarted Studio gets a new key (the old one stops working); a
  stopped Studio's kernels are shut down. The gateway is reachable only from the hub pod (NetworkPolicy) and needs
  its token; it doesn't list kernels.
- **Tokens:** the kernel gets no password or token in its environment. Its prelude (sent over its own channel) sets
  the active connections' non-secret settings and token commands that ask the hub's **token relay**
  (`http://studio-hub-relay:8801/k/<kernel secret>/sas|s2/<connection>`) with a random per-kernel secret; the relay
  finds the kernel's user and asks that user's Studio's token service, which holds the secrets. A kernel can only
  ever get its own user's tokens; a wrong secret gets 404. Never in a kernel: the gateway token, the hub's keys, the
  users' secrets keys, `STUDIO_*` / `EG_*` settings or another user's anything.
- **Limits** (gateway pod settings, baked into the kernelspec; neither the hub nor a user can change them):
  `STUDIO_KERNEL_MEMORY_MB` address-space cap per kernel (`RLIMIT_AS`; a cell that needs more gets `MemoryError`,
  the kernel lives on), `STUDIO_KERNEL_NICE` lower CPU priority, BLAS / OpenMP threads limited to 2,
  `STUDIO_KERNELS_PER_USER` (the Studio closes its oldest idle kernel first) and `STUDIO_KERNELS_TOTAL` (the broker
  refuses with a readable message; the gateway's own max as a backstop), idle kernels stopped by the gateway after
  `STUDIO_KERNEL_IDLE_MINUTES` (busy ones stay) and by the Studio after 30 idle minutes; a Studio notices a kernel the
  gateway dropped within 10 s and starts a new one on the next cell.
- **Folders and packages:** each user has `/kernels/users/<id>/files` (the kernel's working folder and HOME),
  `.../pip` and `.../tmp` on the kernel volume (`studio-kernels`, 50 GiB). The image's notebook environment
  (`/opt/notebook-env`) is read-only; **Install packages** in the Notebook app (and `pip install` in a cell) installs
  with `pip install --target` into the user's own `pip` folder, which comes first on `sys.path` (restart the kernel to
  use new packages). `STUDIO_PIP_MODE`: `mirror` (`STUDIO_PIP_INDEX_URL`, an internal PyPI proxy; default when it's set),
  `pypi` (pypi.org + files.pythonhosted.org) or `off` (installs refused by the app; `pip` in a cell finds no index). The
  quota `STUDIO_KERNEL_QUOTA_GB` (5) is checked before each install from the app and reported after it ("x GB of 5 GB
  used"); it isn't a filesystem quota - a kernel writing files directly is only stopped by the volume's size. Each
  mode has its own NetworkPolicy (`k8s/83-gateway-networkpolicy-<mode>.yaml`). My files (the user's Studio folder in
  the hub pod) are not visible to gateway kernels - read and write data through SingleStore / SAS Viya.
- **No isolation between users' kernels in 2a.** All kernels run as one OS user in one pod: one user's code can read
  another kernel's files, environment and connection file (and so run code in it as that user), and a kernel that
  uses all the CPU or fills the volume slows down or stops the others. The memory cap keeps one kernel from taking the
  pod down by itself; many large ones together still can (the pod's limit). Notebooks are for a named group of
  colleagues (`SpeedyStoreStudioNotebooks`). The admin page says so, shows the gateway's state, every kernel (user,
  started, last activity) and the package mode. Step 2b (below) isolates them.
- **The gateway's token in its own pod:** it comes from its own Secret (`studio-gateway`), is taken out of the
  gateway process's environment before any kernel starts and is never passed to kernels; `kernel_gateway` is PID 1
  instead of tini and both it and the gateway process are non-dumpable, so a kernel (same OS user) can't read it from
  `/proc/<pid>/environ` or memory.
- **Image:** Enterprise Gateway is in the **same image** (build arg `INSTALL_GATEWAY=1`, own environment
  `/opt/gateway-env` with Python 3.11 - Enterprise Gateway 3.3 needs pyzmq < 25, which has no 3.12 wheels; pins in
  `deploy/gateway-constraints.txt`). Why one image: the kernels need the notebook environment and `kernel_launcher.py`
  that are already in it, and the hub's broker and the launcher change together; one image to build, scan, push and
  version (the Zeus admin keeps one registry entry); the gateway Deployment only changes the command. Cost: ~60 MB
  more image for the hub.

### Step 2b (not built): one pod per kernel

When customers / partners use it, or an audit must show who did what: switch the gateway to its **Kubernetes process
proxy** - a deploy change; Studio, the broker and the relay stay as they are.

- Kernelspec `studio-python` gets `"metadata": {"process_proxy": {"class_name":
  "enterprise_gateway.services.processproxies.k8s.KubernetesProcessProxy", "config": {"image_name":
  "<registry>/speedystore-studio-kernel:<version>"}}}` and Enterprise Gateway's `launch_kubernetes.py` + kernel pod
  template; the kernel image = the notebook environment + Enterprise Gateway's kernel launcher (`launch_ipykernel.py`)
  + the steps of `kernel_launcher.py` (clean environment, pip target, user folder) - limits become pod `resources`
  (`KERNEL_CPUS` / `KERNEL_MEMORY` caps set by the gateway, not the user) instead of `RLIMIT_AS` / `nice`.
- RBAC: the gateway gets a ServiceAccount and a Role in a kernels namespace (pods, services, configmaps: create, get,
  list, watch, delete; `EG_NAMESPACE`, `EG_SHARED_NAMESPACE=True` or a namespace per kernel), and
  `automountServiceAccountToken: true` on the gateway only. Kernel pods: no service account token.
- Kernel pods: non-root, read-only root filesystem, all capabilities dropped, `seccompProfile: RuntimeDefault`, their
  own NetworkPolicy (in: only the gateway pod on the kernel's ZMQ ports; the gateway's response port
  `EG_RESPONSE_PORT` 8877 for the launcher's call back; out: DNS, SingleStore, Viya, the relay, the package mode's
  egress), the user's folder as a volume `subPath` `users/<id>` (only that user's), `/tmp` as emptyDir.
- Then each kernel has its own OS-level box: no reading other kernels' files or connection files, a runaway kernel
  only hits its own pod limit, and pod events + the audit log show which user's kernel did what. Cost: a pod start per
  kernel (~5-15 s with the image cached) and ~130 MB + pod overhead each.

## Sizing (measured)

Measured 2026-10-30 on Windows 11 / Python 3.12 with real Studio processes against the fake SAS Logon (no cluster):
each user signed in and opened Studio (Workspace page, opening call, editor state, Connections), 5 s settle time.
Working set (RSS):

| Users | Hub process | Each Studio | All Studios |
|---|---|---|---|
| 1 | ~36 MB | ~145 MB | ~145 MB |
| 3 | ~36 MB | ~145 MB | ~435 MB |
| 5 | ~36 MB | ~145 MB | ~730 MB |

**Notebook kernels** (`STUDIO_NOTEBOOKS=local`): one idle kernel (bridge + ipykernel with pandas, matplotlib,
singlestoredb loaded) adds **~210 MB** to its user's Studio (measured: Studio 146 MB, with one kernel 355 MB); data in
the kernel comes on top. Up to 8 kernels per Studio; idle ones stop after 30 min. The admin page's memory includes them.

**Gateway kernels** (`STUDIO_NOTEBOOKS=gateway`, measured 2026-10-30 on Windows with the local gateway test): one
kernel after its start-up code (pandas, matplotlib, singlestoredb loaded) uses **~130 MB** in the gateway pod - no bridge
process and nothing in the user's Studio (on Linux the launcher becomes the kernel; on Windows it stays as a small
parent). Plan ~300-500 MB per kernel in use; `STUDIO_KERNEL_MEMORY_MB` (3 GiB) caps one. The gateway itself ~60-80 MB.
`k8s/81-gateway-deployment.yaml`: request 4 GiB, limit 12 GiB for `STUDIO_KERNELS_TOTAL=20`. Re-measure on Linux.

The hub stays flat; memory grows ~145 MB per running Studio (on Linux without the Windows venv launcher ~9 MB less).
These are idle-after-open numbers: query results, the Cluster Monitor sampler (`STUDIO_METRICS`) and long sessions add
to them, so plan **~200-300 MB per running Studio**. For `STUDIO_MAX_USERS=20`: request 3 GiB, limit 8 GiB (the
values in `k8s/40-deployment.yaml`); raise the limit before raising `STUDIO_MAX_USERS`. Studios stopped by the idle
stop take nothing. The admin page shows each Studio's memory. Re-measure in the pilot on the cluster.

## Run it locally (Windows, this repo)

```powershell
cd "C:\Users\norjni\claude code\singlestore-mcp-server"     # or the worktree
uv sync                                                     # once
.\scripts\studio-hub-local.ps1                              # Viya + SingleStore host from your own saved connections
# or: .\scripts\studio-hub-local.ps1 -ViyaUrl https://<viya> -Port 8800 -DataDir $HOME\.studio-hub-pilot
```

Open <http://127.0.0.1:8800/>, click **Sign in with SAS**, sign in on SAS Logon (new tab), paste the code. Your
Studio opens on Connections; the SingleStore connection is pre-filled - edit it, enter your database password,
Test, Save. A second user: another browser profile (or a colleague over the network with `STUDIO_HUB_BIND=0.0.0.0`
and `STUDIO_PUBLIC_URL=http://<your-pc>:8800`). `Ctrl+C` stops the hub and every Studio.

Without the script (any OS):

```bash
export STUDIO_VIYA_URL=https://<viya>  STUDIO_DATA_DIR=~/.studio-hub-pilot  STUDIO_ADMINS=<your SAS user id>
uv run --no-sync python -m singlestore_mcp.hub          # http://127.0.0.1:8800/
```

One user without the hub (e.g. behind your own proxy): `python -m singlestore_mcp.studio_server` prints its URL
(`SINGLESTORE_STUDIO_TOKEN`, `SINGLESTORE_STUDIO_BIND`, `SINGLESTORE_STUDIO_PORT`, `SINGLESTORE_STUDIO_ALLOWED_HOSTS` /
`_ORIGINS`, `SINGLESTORE_MCP_SECRET_KEY` - see the module docstring).

## Container

```bash
docker build -f deploy/Dockerfile -t speedystore-studio:pilot .
docker run --rm -p 8800:8800 -v studio-data:/data --read-only --tmpfs /tmp \
  -e STUDIO_VIYA_URL=https://<viya> -e STUDIO_PUBLIC_URL=http://127.0.0.1:8800 \
  -e STUDIO_MASTER_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  -e STUDIO_ADMINS=<sas-user-id> speedystore-studio:pilot
```

Python 3.12 slim, the app installed with `uv sync --locked --no-dev`, the notebook environment in `/opt/notebook-env`
(build arg `INSTALL_NOTEBOOKS=0` leaves it out), Jupyter Enterprise Gateway in `/opt/gateway-env` for the kernel gateway
pod (`INSTALL_GATEWAY=0` leaves it out), `tini` as PID 1 (forwards SIGTERM, reaps orphaned kernels), uid 10001, read-only
root filesystem (writes only `/data` and `/tmp`), health check on `/healthz`. `--build-arg INSTALL_CLAUDE=1` adds
Claude Code for a later "Ask Claude" switch. Kubernetes manifests: [k8s/](k8s/).

## Settings (environment)

| Variable | Default | What |
|---|---|---|
| `STUDIO_VIYA_URL` | **required** | The Viya whose SAS Logon signs users in (and that SAS work runs on) |
| `STUDIO_PUBLIC_URL` | `http://127.0.0.1:<port>` | The address users open; `https://` makes cookies `Secure`/`__Host-`. Redirect URI = this + `/oauth/callback` |
| `STUDIO_HUB_BIND` / `STUDIO_HUB_PORT` | `127.0.0.1` / `8800` | Listen address (`0.0.0.0` in the container) |
| `STUDIO_DATA_DIR` | `/data` | Users' folders and the hub's folder (audit log) |
| `STUDIO_MASTER_KEY` | a key file in `<data>/hub` | **Secret.** Derives each user's secrets key. Set it on a server (keep it out of volume backups) |
| `STUDIO_OAUTH_MODE` | `redirect` if a client id is set, else `paste` | Sign-in flow |
| `STUDIO_OAUTH_CLIENT_ID` / `_SECRET` | `vscode` / none | The hub's SAS Logon client (secret from a Secret) |
| `STUDIO_OAUTH_SCOPE` | empty | Scopes to ask for; empty = the client's registered scopes |
| `STUDIO_VIYA_NAME` | first part of the host | Name of the pre-loaded SAS Viya connection |
| `STUDIO_VIYA_VERIFY` | `1` | Check Viya's TLS certificate (`SSL_CERT_FILE` for an internal CA) |
| `STUDIO_VIYA_COMPUTE_CONTEXT` | SAS Studio compute context | Compute context of the pre-loaded connection |
| `STUDIO_ADMINS` | none | SAS user ids allowed on `/admin` |
| `STUDIO_USERS_GROUP` | none | SAS group whose members may use Studio (pilot: `SpeedyStoreStudioUsers`) |
| `STUDIO_NOTEBOOK_GROUP` | none | SAS group whose members may use notebooks and Studio (pilot: `SpeedyStoreStudioNotebooks`); notebooks also need `STUDIO_NOTEBOOKS=local` |
| `STUDIO_ALLOWED_USERS` | everyone who can sign in | SAS user ids allowed to use Studio (with a group set: allowed besides the group's members) |
| `STUDIO_MAX_USERS` | `20` | Studios running at once (more: "Studio is full") |
| `STUDIO_IDLE_MINUTES` | `30` | Idle stop |
| `STUDIO_SESSION_HOURS` | `10` | Sign-in lifetime |
| `STUDIO_DEFAULT_S2_NAME/HOST/PORT/DATABASE` | none | Pre-filled SingleStore connection (no password) |
| `STUDIO_NOTEBOOKS` | `off` | `off`, `local` (kernels in each notebook user's own Studio process) or `gateway` (kernels on the kernel gateway, step 2a). Neither isolates users' kernels from each other - a named group of colleagues only |
| `STUDIO_GATEWAY_URL` | none | gateway: the kernel gateway (`http://studio-gateway:8888`) |
| `STUDIO_GATEWAY_AUTH_TOKEN` | none | **Secret** (`studio-gateway`), 32+ characters: hub and gateway pod; never a Studio or kernel |
| `STUDIO_GATEWAY_VERIFY` | `1` | Check the gateway's TLS certificate (if `STUDIO_GATEWAY_URL` is `https://`) |
| `STUDIO_KERNELS_PER_USER` / `STUDIO_KERNELS_TOTAL` | `3` / `20` | gateway: notebook kernels per user / in all (+ one package-install kernel per user) |
| `STUDIO_KERNEL_IDLE_MINUTES` | `30` | gateway pod: stop kernels idle this long (busy ones stay) |
| `STUDIO_KERNEL_MEMORY_MB` / `STUDIO_KERNEL_NICE` | `3072` / `10` | gateway pod: address-space cap and CPU priority per kernel |
| `STUDIO_KERNEL_DATA_DIR` / `STUDIO_KERNEL_QUOTA_GB` | `/kernels` / `5` | gateway pod: the kernel volume; per-user quota (checked at installs) |
| `STUDIO_PIP_MODE` / `STUDIO_PIP_INDEX_URL` | `mirror` if an index URL is set, else `off` | gateway: package installs `mirror` / `pypi` / `off` (hub and gateway pod; apply the matching NetworkPolicy) |
| `STUDIO_RELAY_BIND` / `_PORT` / `_URL` | `127.0.0.1` / `8801` / `http://<bind>:<port>` | gateway: the hub's token relay and the address kernels use for it (`0.0.0.0`, `http://studio-hub-relay:8801` in K8s) |
| `STUDIO_GATEWAY_BIND` / `_PORT` / `_EG` / `_RUNTIME` | `0.0.0.0` / `8888` / `/opt/gateway-env/bin/jupyter-enterprisegateway` / `/tmp/studio-gateway` | gateway pod: where it listens, the Enterprise Gateway program, its runtime folder |
| `STUDIO_NOTEBOOK_PYTHON` | `/opt/notebook-env/bin/python` in the image | The shared notebook environment's Python (local: your desktop one, `-Notebooks` in the script; gateway: the kernels' Python in the gateway pod) |
| `STUDIO_ASSISTANT` | `0` | Ask Claude also needs Claude Code in the image and `ANTHROPIC_API_KEY` (or `CLAUDE_CODE_USE_FOUNDRY=1` + `ANTHROPIC_FOUNDRY_*`) - passed to Studios only when on |
| `STUDIO_METRICS` | `1` | Cluster Monitor history sampler in each Studio (N users = N samplers; turn off if that's too much) |
| `STUDIO_TRUST_FORWARDED` | `0` (`1` in the image) | Take the client address for the audit log from `X-Forwarded-For` |
| `STUDIO_START_TIMEOUT` | `90` | Seconds a Studio may take to start |
| `STUDIO_PROXY_TIMEOUT` | `600` | Seconds one Studio call may take (keep the ingress's `proxy-read-timeout` the same) |
| `STUDIO_LOGIN_RATE` | `20` | Sign-in starts / code exchanges per client address and minute |

## Tests

```powershell
.venv\Scripts\python.exe -m pytest tests/test_hub.py tests/test_server_mode.py tests/test_hub_e2e.py
```

Offline, with a fake SAS Logon (`tests/fake_saslogon.py`): sign-in (both flows), cookies, CSRF, per-user routing and
isolation, idle stop, limits, admin, audit without secrets (`test_hub.py`); server-mode switches, encrypted secrets
with the hub's key, seed (`test_server_mode.py`); two real Studio processes with separate state, Viya token renewal
with the client secret, sign-out (`test_hub_e2e.py`). They are part of the offline subset (`-m "not cluster and not ui"`).

Kernel gateway (`tests/test_kernel_gateway.py`): unit tests always (launcher environment, settings, kernelspec,
installs refused with `STUDIO_PIP_MODE=off`, the WebSocket client); end to end (marker `gateway`) with a local
Enterprise Gateway process, the hub, real Studios and the fake SAS Logon for two users: kernels in their own folders
with their own Viya token through the relay, no secrets in a kernel's environment, a Studio can't reach another
user's kernel (broker 404s, gateway 401 without the token), interrupt and restart, memory cap / CPU priority /
kernels-per-user, idle-culled and signed-out kernels dropped, a package installed from a local mirror lands in that
user's own folder only, admin page. They skip unless a gateway venv is given:

```powershell
uv venv $env:TEMP\eg-venv --python 3.11; uv pip install --python $env:TEMP\eg-venv -r deploy\gateway-constraints.txt
$env:S2_TEST_EG_VENV = "$env:TEMP\eg-venv"     # + a notebook environment (S2_TEST_KERNEL_PYTHON, default the desktop one)
.venv\Scripts\python.exe -m pytest tests/test_kernel_gateway.py
```

Try it by hand: `.\scripts\studio-hub-local.ps1 -Gateway -GatewayVenv $env:TEMP\eg-venv -NotebookUsers <you>`.
