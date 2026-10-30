# SAS SpeedyStore Studio on a server (pilot, step 1)

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
- **Off in step 1:** notebooks (Python kernels) and Ask Claude - hidden in the UI, refused by the server. Browser
  sign-ins that open a browser on the server (Helios SSO, Entra ID) are refused; use a password or a pasted JWT.
- **Idle stop:** a Studio with no use (the open pages' 30 s heartbeat doesn't count) for `STUDIO_IDLE_MINUTES` is
  stopped; its page says "Studio stopped" and starts it again on click. Sign-out stops it at once.
- **Access by SAS group:** members of `STUDIO_USERS_GROUP` (pilot: `SpeedyStoreStudioUsers`) may use Studio, members
  of `STUDIO_NOTEBOOK_GROUP` (`SpeedyStoreStudioNotebooks`) also notebooks - once `STUDIO_NOTEBOOKS=1` (not in step 1).
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
  on the server (notebooks and Ask Claude are off); step 2 (notebooks) needs one pod or OS user per Studio.

### When a Studio crashes or hangs

The hub pings every Studio every 30 s. A Studio that exited on its own, or that misses 3 pings in a row (or a call
times out after `STUDIO_PROXY_TIMEOUT` and its ping fails too), is dropped and started again at once: the open page
shows "Reconnecting to your Studio…" and carries on (a call that was running when it crashed says it may or may not
have finished); a page load shows "Your Studio is restarting", which reloads itself. Tabs, connections and files are
on disk, so nothing is lost but unsaved work in that process. A Studio that crashes 3 times in 5 minutes isn't
restarted for a while (the page says so; its `studio.log` says why). The idle stop never stops a Studio with a call
in flight. Stopping the hub (`SIGTERM`, Ctrl+C) stops all Studios at once within ~10 s.

## Sizing (measured)

Measured 2026-10-30 on Windows 11 / Python 3.12 with real Studio processes against the fake SAS Logon (no cluster):
each user signed in and opened Studio (Workspace page, opening call, editor state, Connections), 5 s settle time.
Working set (RSS):

| Users | Hub process | Each Studio | All Studios |
|---|---|---|---|
| 1 | ~36 MB | ~145 MB | ~145 MB |
| 3 | ~36 MB | ~145 MB | ~435 MB |
| 5 | ~36 MB | ~145 MB | ~730 MB |

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

Python 3.12 slim, the app installed with `uv sync --locked --no-dev` (no notebook environment), uid 10001, read-only
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
| `STUDIO_NOTEBOOK_GROUP` | none | SAS group whose members may use notebooks and Studio (pilot: `SpeedyStoreStudioNotebooks`); notebooks also need `STUDIO_NOTEBOOKS=1` |
| `STUDIO_ALLOWED_USERS` | everyone who can sign in | SAS user ids allowed to use Studio (with a group set: allowed besides the group's members) |
| `STUDIO_MAX_USERS` | `20` | Studios running at once (more: "Studio is full") |
| `STUDIO_IDLE_MINUTES` | `30` | Idle stop |
| `STUDIO_SESSION_HOURS` | `10` | Sign-in lifetime |
| `STUDIO_DEFAULT_S2_NAME/HOST/PORT/DATABASE` | none | Pre-filled SingleStore connection (no password) |
| `STUDIO_NOTEBOOKS` / `STUDIO_ASSISTANT` | `0` / `0` | Step 1: off. Ask Claude also needs Claude Code in the image and `ANTHROPIC_API_KEY` (or `CLAUDE_CODE_USE_FOUNDRY=1` + `ANTHROPIC_FOUNDRY_*`) - passed to Studios only when on |
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
