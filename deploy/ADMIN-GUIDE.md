# SAS SpeedyStore Studio pilot - admin guide

For the Viya / Kubernetes admin of the Zeus deployment. Studio runs as **one pod in its own namespace** next to Viya:
a small hub (sign-in with SAS Logon, one Studio process per user, idle stop, audit log) behind your ingress. Users are
SAS colleagues; they sign in with their SAS user and password on SAS Logon. Nothing in the Viya namespace changes
except one OAuth client registration (step 2), which is optional.

What it needs, in short:

| # | What | Who | Required? |
|---|---|---|---|
| 1 | A namespace, registry, image | K8s admin | yes |
| 2 | An OAuth client in SAS Logon with the redirect URI | Viya admin | no - fallback below |
| 3 | DNS name + TLS certificate + Ingress | K8s / network admin | yes |
| 4 | PVC for users' folders | K8s admin | yes |
| 5 | Network access to SingleStore (3306) and Viya (443) | K8s / network admin | yes |
| 6 | Secrets (master key, client secret) | K8s admin | yes |
| 7 | SingleStore users for the colleagues | SingleStore admin | yes (they may exist) |
| 8 | Two SAS groups: who may use Studio, who may use notebooks | Viya admin | recommended (else everyone who can sign in) |
| 9 | Kernel gateway for notebooks (step 2a): second Deployment, PVC 50 Gi, Secret, NetworkPolicy, package egress | K8s admin | when notebooks go beyond the test users |

## 1. Namespace, registry, image

```bash
kubectl apply -f deploy/k8s/00-namespace.yaml          # namespace speedystore-studio (Pod Security: restricted)
docker build -f deploy/Dockerfile -t <registry>/speedystore-studio:<version> .
docker push <registry>/speedystore-studio:<version>
```

- Use the registry the Viya deployment pulls from (or another one the cluster can reach); add an `imagePullSecrets`
  entry in `40-deployment.yaml` if it needs credentials.
- The image runs as uid/gid 10001, non-root, read-only root filesystem, all capabilities dropped, no service account
  token. It writes only to `/data` (PVC) and `/tmp` (emptyDir).

## 2. OAuth client in SAS Logon (preferred sign-in)

The hub signs users in with OAuth 2.0 Authorization Code + PKCE. Register **one confidential client** for it on the
Zeus Viya. Values:

| Field | Value |
|---|---|
| `client_id` | `speedystore-studio` (any name; put it in `STUDIO_OAUTH_CLIENT_ID`) |
| `client_secret` | a long random string (goes into the Secret `studio-secrets` as `STUDIO_OAUTH_CLIENT_SECRET`) |
| `authorized_grant_types` | `["authorization_code", "refresh_token"]` |
| `redirect_uri` | `["https://studio.<internal-domain>/oauth/callback"]` - exactly `STUDIO_PUBLIC_URL` + `/oauth/callback` |
| `scope` | `["openid", "*"]` - the user's own group scopes, as for SAS's own clients, so Viya authorization works as for the user |
| `autoapprove` | `true` (no consent page) |
| `access_token_validity` | `3600` (1 h) |
| `refresh_token_validity` | e.g. `43200` (12 h) - how long a user stays signed in to SAS without signing in again |

Steps (as in the SAS Viya documentation "Register a client with SAS Logon"; check the exact form for your Viya release):

```bash
VIYA=https://<zeus-viya-host>
NS=<viya-namespace>

# a) The Consul token of the Viya deployment (shared infrastructure secret: don't store it anywhere)
CONSUL_TOKEN=$(kubectl -n $NS get secret sas-consul-client -o go-template='{{ .data.CONSUL_TOKEN | base64decode }}')

# b) A short-lived token that may register clients
REG_TOKEN=$(curl -sk -X POST "$VIYA/SASLogon/oauth/clients/consul?callback=false&serviceId=speedystore-studio" \
  -H "X-Consul-Token: $CONSUL_TOKEN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# c) The client
CLIENT_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
curl -sk -X POST "$VIYA/SASLogon/oauth/clients" -H "Content-Type: application/json" -H "Authorization: Bearer $REG_TOKEN" -d '{
  "client_id": "speedystore-studio",
  "client_secret": "'"$CLIENT_SECRET"'",
  "name": "SAS SpeedyStore Studio",
  "authorized_grant_types": ["authorization_code", "refresh_token"],
  "redirect_uri": ["https://studio.<internal-domain>/oauth/callback"],
  "scope": ["openid", "*"],
  "autoapprove": true,
  "access_token_validity": 3600,
  "refresh_token_validity": 43200
}'
# d) Put $CLIENT_SECRET into the Secret (step 6), then forget it here.
```

- Check: `curl -sk "$VIYA/SASLogon/oauth/clients/speedystore-studio" -H "Authorization: Bearer $REG_TOKEN"`.
- Change the redirect URI later: `PUT $VIYA/SASLogon/oauth/clients/speedystore-studio` with the full client JSON.
- Remove: `DELETE $VIYA/SASLogon/oauth/clients/speedystore-studio`.
- ConfigMap: `STUDIO_OAUTH_MODE=redirect`, `STUDIO_OAUTH_CLIENT_ID=speedystore-studio`.

### Fallback without a client registration

Set `STUDIO_OAUTH_MODE=paste` and leave `STUDIO_OAUTH_CLIENT_ID` empty. The hub then uses Viya's built-in public
client `vscode` (the one SAS's own VS Code extension uses): "Sign in with SAS" opens SAS Logon in a new tab, SAS
Logon shows a code after the sign-in, and the user pastes it into the hub's page. Everything else is the same (tokens
are renewed with the refresh token; no client secret). Downside: one extra copy-paste per sign-in.

## 3. DNS, TLS, Ingress

- A DNS name for users, e.g. `studio.<internal-domain>`, pointing at your ingress controller.
- A TLS certificate for it in the Secret `studio-tls` (internal CA, or cert-manager - see the annotation in
  `60-ingress.yaml`). The hub sets `Secure` / `__Host-` cookies and HSTS when `STUDIO_PUBLIC_URL` is `https://`.
- `kubectl apply -f deploy/k8s/50-service.yaml -f deploy/k8s/60-ingress.yaml` after filling in `ingressClassName` and
  the host. Timeouts of 600 s (long SQL) and a 4 MB body limit are set for ingress-nginx.
- **The SAS CA (required on SAS-internal Viya such as Zeus).** SAS Logon's certificate there is signed by *SAS SHA2
  Issuing CA01/CA02* under *SAS SHA2 Root CA*, which the image (Debian's public CAs) doesn't trust: without it every
  sign-in fails with `CERTIFICATE_VERIFY_FAILED` (checked in Docker, 2026-11-06). `SSL_CERT_FILE` replaces the default
  bundle, so give a file with the public CAs **and** the SAS ones (PyPI and other public hosts still need the public
  CAs), as a ConfigMap, and uncomment the `SSL_CERT_FILE` / `studio-ca` lines in `40-deployment.yaml` (and
  `81-gateway-deployment.yaml` with the gateway). The hub hands it to every Studio, the collector and the kernels.

  ```bash
  docker run --rm --entrypoint cat <registry>/speedystore-studio:<version> /etc/ssl/certs/ca-certificates.crt > ca.pem
  cat sas-sha2-root-ca.pem sas-sha2-issuing-ca01.pem sas-sha2-issuing-ca02.pem >> ca.pem   # PEM files from SAS IT / the Viya deployment's trust bundle
  kubectl -n speedystore-studio create configmap studio-ca --from-file=ca.pem
  ```

  `STUDIO_VIYA_VERIFY=0` instead is a last resort for a first test only.

## 4. Storage

`30-pvc.yaml`: one ReadWriteOnce volume (20 Gi to start) for `/data` - users' folders (`/data/users/<sas id>/`) and
the hub's audit log (`/data/hub/audit.jsonl`). Back it up like other app data; the saved passwords and Viya sign-ins in
it are encrypted with keys derived from `STUDIO_MASTER_KEY`, so keep that key **out** of the same backup.
`replicas: 1` and `strategy: Recreate` - sessions live in the hub's memory and the volume is RWO.

## 5. Network

`70-networkpolicy.yaml` (needs a CNI that enforces NetworkPolicy):
- **In:** only from the ingress controller's namespace, port 8800.
- **Out:** DNS; the SingleStore aggregator(s) on 3306; Viya's ingress on 443. Nothing else (no internet, no cloud
  metadata endpoint, no other namespaces).

Fill in the SingleStore address (or a namespaceSelector if the cluster runs in the same Kubernetes with the
SingleStore Operator) and the address the pod uses to reach Viya. The hub calls Viya at `STUDIO_VIYA_URL` (SAS Logon
token endpoint, `/identities`, and the users' Studios call Compute / CAS / Files); if that host name resolves to the
external load balancer, allow that address, or point `STUDIO_VIYA_URL` at an in-cluster route your admins prefer
(users' browsers still need to reach the same SAS Logon host for the sign-in page).

## 6. Secrets

```bash
kubectl -n speedystore-studio create secret generic studio-secrets \
  --from-literal=STUDIO_MASTER_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  --from-literal=STUDIO_OAUTH_CLIENT_SECRET="$CLIENT_SECRET" \
  --from-literal=STUDIO_COLLECTOR_PASSWORD="$COLLECTOR_PASSWORD"
# STUDIO_OAUTH_CLIENT_SECRET: only with the client from step 2. STUDIO_COLLECTOR_PASSWORD: the Studio repository (7b).
kubectl -n speedystore-studio create secret tls studio-tls --cert=<cert.pem> --key=<key.pem>
```

`20-secret.example.yaml` shows the shape; don't commit filled-in copies. Then the settings and the app:

```bash
kubectl apply -f deploy/k8s/10-configmap.yaml -f deploy/k8s/30-pvc.yaml -f deploy/k8s/40-deployment.yaml -f deploy/k8s/70-networkpolicy.yaml
kubectl -n speedystore-studio rollout status deploy/studio-hub
kubectl -n speedystore-studio logs deploy/studio-hub | grep '"event": "hub_start"'
```

## 7. SingleStore users

Studio connects to SingleStore **as each user** with their own database user and password (entered in Studio's
Connections, stored encrypted in their folder). Colleagues need a SingleStore user with the grants they should have;
Studio adds no rights. The default connection in the ConfigMap only pre-fills host/port/database (user name = SAS id).
Give each colleague their **own** SingleStore user rather than a shared admin: the Studio repository shows people
outside the monitor group the queries of the SingleStore user they sign in as, so a shared login shares that view.

## 7b. Studio repository (`SSSTUDIO`)

One **collector** in the hub pod watches the cluster for everyone: it samples the Cluster Monitor's metrics once a
minute, copies new query history events (`MV_TRACE_EVENTS`, which forgets) and keeps the plan cache - and stores it
all in a database on the cluster, so history survives pod restarts and lost volumes, and every Studio shows the same
history instead of each keeping (and sampling) its own. Users' Studios read it through the collector, never directly.

**Database and service account** (a DBA, once; Studio doesn't create users). The name has 8 characters, so it also
works as a SAS libref:

```sql
CREATE DATABASE IF NOT EXISTS SSSTUDIO;
CREATE USER 'svc_studio_collector'@'%' IDENTIFIED BY '<a long random password>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, CREATE VIEW, SHOW VIEW ON SSSTUDIO.* TO 'svc_studio_collector'@'%';
GRANT PROCESS, SHOW METADATA ON *.* TO 'svc_studio_collector'@'%';
```

`PROCESS` lets the collector see every user's queries (the trace, the plan cache, the process list) - only this
account gets it, no person does. `ALL PRIVILEGES` on the database doesn't work when the granting admin lacks some
privileges itself (REFERENCES, EVENT, TRIGGER...): the list above is all the collector needs. Users get **no grants**
on `SSSTUDIO`: their own logins can't read it. Put the password in the Secret as `STUDIO_COLLECTOR_PASSWORD` (step 6);
on Zeus it is kept in the owner's Windows Credential Manager (`speedystore-studio-collector` / `svc_studio_collector`).

**Who sees what** - a third SAS group, `SpeedyStoreStudioMonitor` (`STUDIO_MONITOR_GROUP`), checked at sign-in and at
each token renewal like the other two:

| | Monitor group members, Studio admins | Everyone else |
|---|---|---|
| Query history, trends, most used tables / active users | every user's queries | only the queries of the SingleStore user their connection signs in as |
| Plan cache (statement stats, kept plans) | yes | no (it has no user) |
| Cluster Monitor history, trend alerts | yes | yes |
| Alerts | their own; admins: everyone's, in the Alerts view and on `/admin` | their own |
| Workloads | shared by all users | shared by all users |

A user taken out of the monitor group gets the narrower view within a token lifetime (the Studio is handed a new
token). Their **live** views (Cluster > Queries, the live query history) still use their own login's rights.

**How it is kept:** the collector writes into SQLite files in `/data/collector` and, every 15 s, the new and changed
rows into `SSSTUDIO` (tables `runs`, `plans`, `plan_samples`, `metrics`; times are `DATETIME(6)`). The repository lives
in the cluster it watches, so while the cluster is down the rows wait in the SQLite outbox and go when it is back. A
collector that starts on an empty volume first copies `SSSTUDIO` back. Retention (hourly, both sides): query runs and
plans 90 days, plan-cache counter samples 30 days, metrics 30 days per minute and 1 year in 10-minute rows. A row a
column can't take is left out and counted (admin page) instead of holding up the rest.

**Alerts and workloads** stay in files on the volume, so Studios work the same with the cluster away, and the collector
mirrors them to `SSSTUDIO` once a minute (tables `alerts`, `alert_state`, `workloads`, `workload_runs`; each record whole
in `doc` as JSON, times in UTC). Deleted alerts and workloads are deleted there too; a file that is missing or being
written deletes nothing. On an empty volume the collector puts them back before Studios start: a user's rules, settings
and alerts, and the workloads with their runs. Existing files are never overwritten. The stores limit their size (300
alerts per user, 30 runs per workload), so these tables have no retention. The collector creates the tables itself with
the grants above.

**Check it:** the admin page's *repository* line (last write, rows waiting, last sample, errors); the audit log's
`collector_start` / `collector_exited`; `/data/collector/collector.log`. Without `STUDIO_COLLECTOR_PASSWORD` each
Studio keeps its own history as on the desktop (the admin page says "off"). The users' default connection
(`STUDIO_DEFAULT_S2_HOST` / `_PORT`) must name the same host:port as the collector's, or their Studio keeps its own.

## 8. SAS groups for access

Who may use Studio is decided by two **SAS groups** that the Viya admin creates in **SAS Environment Manager**
(Users > New custom group) - Studio doesn't create or change groups. Names agreed for the pilot:

| Group (id) | Members may | Setting |
|---|---|---|
| `SpeedyStoreStudioUsers` | use Studio | `STUDIO_USERS_GROUP` |
| `SpeedyStoreStudioNotebooks` | use notebooks (and so Studio), with `STUDIO_NOTEBOOKS=local` or `gateway` | `STUDIO_NOTEBOOK_GROUP` |
| `SpeedyStoreStudioMonitor` | see every user's queries in the Studio repository (7b) - not an access group | `STUDIO_MONITOR_GROUP` |

Add the colleagues to the groups **directly** (as members, not through another group): whether the identities
service's membership list includes nested groups isn't documented publicly and hasn't been checked on Zeus yet.
Membership can be delegated (e.g. to the Studio owner) in Environment Manager.

How it works: at sign-in the hub asks Viya's identities service which groups the user is in -
`GET /identities/users/@currentUser/memberships` (paged) with **the user's own token**, no service account. A group
matches by its id or its name. Not a member: sign-in refused with a readable page and a `login_denied` audit line
(reason "not in SpeedyStoreStudioUsers or SpeedyStoreStudioNotebooks"). Each Studio checks again whenever it renews the user's
Viya token (at least once per token lifetime, `access_token_validity`, 1 h), so someone taken out of the group is
signed out and their Studio stopped within that time (`access_revoked`); taken out of the notebook group, notebooks
go off at once. If the identities service doesn't answer, **new** sign-ins are refused ("couldn't check your SAS group
membership") but people already signed in carry on (`access_check_failed`). `STUDIO_ALLOWED_USERS` is an extra list
of SAS user ids that may use Studio without being in a group. Both groups empty: everyone who can sign in (or
`STUDIO_ALLOWED_USERS`), as before. The admin page shows per user whether they may use Studio and notebooks and which
group (or list) gave it.

## Operating it

- **Admin page:** `https://studio.<internal-domain>/admin` for the SAS user ids in `STUDIO_ADMINS` - who is signed in,
  running Studios with memory, stop one (also signs that user out), recent audit events.
- **Health:** `GET /healthz` (no sign-in) - used by the probes.
- **Logs:** `kubectl logs` shows the audit lines (`audit {...}`); each user's Studio writes `studio.log` in their state
  folder. No passwords, tokens, codes or cookies are logged.
- **Sizing:** measured ~145 MB per running Studio right after opening, plan ~200-300 MB in use (README, Sizing);
  idle ones stop after `STUDIO_IDLE_MINUTES`; at most `STUDIO_MAX_USERS` (an admin may start one more to reach /admin).
- **A crashed or hung Studio** is started again on the user's next request ("Your Studio is restarting"); one that
  crashes 3 times in 5 minutes waits a few minutes. Its `studio.log` says why.
- **Upgrade:** new image tag in the Deployment; the pod restarts and everyone signs in again (their folders stay).
- **Remove a user's data:** delete `/data/users/<sas id>/` (e.g. `kubectl exec` into the pod). Their queries in the
  Studio repository are the cluster's history, not their files: they age out with the retention.
- **Workloads** are shared: `/data/shared/workloads` (all users see, run, change and delete them).
- **Alerts:** each user's own rules and alerts; the admin page lists everyone's open ones.

## Notebooks: `STUDIO_NOTEBOOKS=local` (test version)

For the first deploy notebooks stay on for Jonas and one colleague: `STUDIO_NOTEBOOKS: "local"` in the ConfigMap and
both in `SpeedyStoreStudioNotebooks`. Each notebook user's Studio process runs their kernels in the pod, with the
image's read-only notebook environment (`/opt/notebook-env`), in the user's own folder, with the user's own tokens.
**Local notebooks: no isolation between users' kernels - test use with a few trusted users only; the kernel gateway (step 2a) replaces this.** The kernels run Python code as the pod's single OS user: one notebook user's code could read other users'
folders and running kernels' environments. Keep the notebook group to a few trusted people; plan ~210 MB per open
kernel (README, Sizing). Kernels reach the network like Studio does (the NetworkPolicy's egress applies). Set
`STUDIO_NOTEBOOKS: "off"` to switch them off for everyone (no image change).

## 9. Notebooks on the kernel gateway (`STUDIO_NOTEBOOKS=gateway`, step 2a)

Moves the notebook kernels out of the hub pod into their own pod (Jupyter Enterprise Gateway, same image), with
limits per kernel and their own egress rules. How it works and what it does and doesn't isolate:
[README](README.md#notebooks-on-the-kernel-gateway-studio_notebooksgateway-step-2a).

1. **Image:** built with `INSTALL_GATEWAY=1` and `INSTALL_NOTEBOOKS=1` (the defaults). Same tag for both Deployments.
2. **Secret** for the gateway's token (the hub and the gateway pod; never the users):
   ```bash
   kubectl -n speedystore-studio create secret generic studio-gateway      --from-literal=STUDIO_GATEWAY_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
   ```
3. **Package installs** - decide with Jonas, then set `STUDIO_PIP_MODE` in the ConfigMap:
   - `mirror` (preferred): is there an internal PyPI proxy (Artifactory / Nexus) the cluster can reach? Set
     `STUDIO_PIP_INDEX_URL` to its `/simple` index (and `SSL_CERT_FILE` in `81-gateway-deployment.yaml` if it uses an
     internal CA); egress only to it.
   - `pypi`: straight to pypi.org + files.pythonhosted.org - needs HTTPS egress to public addresses (or an egress proxy
     / your CNI's FQDN rule; see the comments in `83-gateway-networkpolicy-pypi.yaml`).
   - `off`: only the packages in the image.
4. **Fill in** `<storage-class>` (80), image/registry (81), and in the chosen `83-gateway-networkpolicy-<mode>.yaml`
   the SingleStore and Viya addresses (as in `70-networkpolicy.yaml`) and for `mirror` the mirror's address.
5. **Apply** (the hub's own policy `70-networkpolicy.yaml` now selects only the hub pod and allows hub -> gateway:8888
   and gateway -> hub:8801 - re-apply it too):
   ```bash
   kubectl apply -f deploy/k8s/10-configmap.yaml -f deploy/k8s/70-networkpolicy.yaml      -f deploy/k8s/80-gateway-pvc.yaml -f deploy/k8s/81-gateway-deployment.yaml -f deploy/k8s/82-gateway-service.yaml      -f deploy/k8s/83-gateway-networkpolicy-<mode>.yaml
   kubectl -n speedystore-studio rollout status deploy/studio-gateway
   ```
6. **Switch** `STUDIO_NOTEBOOKS: "gateway"` in the ConfigMap and restart the hub
   (`kubectl -n speedystore-studio rollout restart deploy/studio-hub`; everyone signs in again).
7. **Check:** the admin page shows "notebooks: gateway", "Kernel gateway ... answers", the package mode and the
   warning; a notebook user runs `import os; os.getcwd()` (-> `/kernels/users/<id>/files`) and a SAS cell. The gateway
   pod's log has one start line with the limits. `kubectl -n speedystore-studio logs deploy/studio-hub | grep kernel_`
   shows `kernel_start` / `kernel_stop` audit lines.

Sizing: ~130 MB per kernel after start, plan ~300-500 MB in use; the gateway pod requests 4 GiB, limit 12 GiB for 20
kernels (`STUDIO_KERNELS_TOTAL`); each kernel is capped at `STUDIO_KERNEL_MEMORY_MB` (3 GiB address space). Kernel
volume 50 Gi, 5 Gi per user (checked at installs from the app, not a filesystem quota).
**No isolation between users' kernels in 2a** (one pod, one OS user): keep the notebook group to colleagues. Step 2b
(one pod per kernel; the gateway then needs a ServiceAccount and a Role for pods) is described in the README.
Back to local notebooks or off: set `STUDIO_NOTEBOOKS` and restart the hub; the gateway Deployment can stay or go.

## Not in step 1

- Isolated notebooks: one pod per kernel (step 2b; step 2a above runs kernels in one shared gateway pod).
- Ask Claude (needs Claude Code in the image, `STUDIO_ASSISTANT=1` and an Anthropic or Microsoft Foundry API key
  from a Secret; who pays and per-user limits are open).
- Entra ID / SSO and per-user SingleStore JWT logins.
- More than one hub replica.
