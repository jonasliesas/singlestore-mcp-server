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
- If SAS Logon uses a certificate from an internal CA that the image doesn't trust, mount the CA bundle and set
  `SSL_CERT_FILE` (commented lines in `40-deployment.yaml`), or as a last resort `STUDIO_VIYA_VERIFY=0`.

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
  --from-literal=STUDIO_OAUTH_CLIENT_SECRET="$CLIENT_SECRET"     # only with the client from step 2
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

## Operating it

- **Admin page:** `https://studio.<internal-domain>/admin` for the SAS user ids in `STUDIO_ADMINS` - who is signed in,
  running Studios with memory, stop one (also signs that user out), recent audit events.
- **Health:** `GET /healthz` (no sign-in) - used by the probes.
- **Logs:** `kubectl logs` shows the audit lines (`audit {...}`); each user's Studio writes `studio.log` in their state
  folder. No passwords, tokens, codes or cookies are logged.
- **Sizing:** ~150-300 MB per running Studio; idle ones stop after `STUDIO_IDLE_MINUTES`; at most `STUDIO_MAX_USERS`.
- **Upgrade:** new image tag in the Deployment; the pod restarts and everyone signs in again (their folders stay).
- **Remove a user's data:** delete `/data/users/<sas id>/` (e.g. `kubectl exec` into the pod).

## Not in step 1

- Notebooks (Python kernels run user code: need one pod / OS user per Studio, egress limits, a PyPI mirror).
- Ask Claude (needs Claude Code in the image, `STUDIO_ASSISTANT=1` and an Anthropic or Microsoft Foundry API key
  from a Secret; who pays and per-user limits are open).
- Entra ID / SSO and per-user SingleStore JWT logins.
- More than one hub replica.
