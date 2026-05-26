# kuberniq-server

A lightweight .NET 10 minimal API that exposes Kubernetes cluster context over HTTP, secured with JWT authentication.  
Designed to run in-cluster and serve as the data layer for the Kuberniq dashboard and MCP RAG Chatbot.

---

## Features

- **JWT authentication** — all routes are protected; login via `POST /auth/login`, tokens stored in-browser
- **ArgoCD-style bootstrap** — first-run auto-creates an `admin` user with a random password stored in a K8s Secret; no setup wizard needed
- **Role-based access control** — three built-in roles: `admin`, `operator`, `viewer`
- **External OIDC authentication (Phase 1)** — accepts JWTs from any OIDC-compliant provider (Entra ID, AWS Cognito, Google, Okta); enabled via a single K8s Secret; disabled by default
- **Full SPA dashboard** at `/` — login page, collapsible sidebar navigation, namespace switcher, resource tables, log viewer, user management (admin)
- **Live cluster data** — 40+ endpoints covering every major Kubernetes resource type
- **Multi-cluster support** — register remote clusters via `POST /clusters`; all endpoints accept `?cluster=<name>`
- **Multi-container log support** — view logs per container or all containers merged in one call
- **Auto-reconnect** — recreates the Kubernetes client automatically on SSL/connection drops
- **Troubleshoot endpoint** — aggregates pods, events and logs for a service in one call
- **In-cluster & local** — uses in-cluster config when deployed, falls back to `~/.kube/config` locally
- **Helm packaged** — distributed as a Helm chart with fully overridable `values.yaml`
- **ArgoCD managed** — GitOps deployment via ArgoCD Application manifests
- **Multi-arch Docker image** — built for `linux/amd64` and `linux/arm64`

---

## Authentication

All API endpoints (except `GET /`, `GET /health`, `POST /auth/login`, `POST /auth/refresh`, and `POST /auth/logout`) require a valid JWT `Bearer` token.

### Roles

| Role | Permissions |
|------|-------------|
| `admin` | Full access — cluster reads, troubleshoot, user management |
| `operator` | Cluster reads + troubleshoot — no user management |
| `viewer` | Cluster reads only |

### First-run bootstrap

On first start, the server auto-creates an `admin` user and stores the random password in a K8s Secret:

```bash
# Get the namespace the server is running in (shown in server startup logs)
kubectl get secret kuberniq-admin-initial-password \
  -n <server-namespace> \
  -o jsonpath='{.data.password}' | base64 -d
```

> The login page hint automatically shows the exact command with the correct namespace.

Delete the secret after changing your password:
```bash
kubectl delete secret kuberniq-admin-initial-password -n <server-namespace>
```

### Token flow

| Step | Request |
|------|---------|
| Login | `POST /auth/login` → returns `accessToken` (1 hr) + `refreshToken` (30 days) |
| Refresh | `POST /auth/refresh` → rotates both tokens |
| Logout | `POST /auth/logout` → revokes the refresh token |

All tokens are stored in browser `localStorage`. Access tokens are sent as `Authorization: Bearer <token>` on every API call. Expired access tokens are silently refreshed.

### User management (admin only)

```bash
# List users
GET  /auth/users

# Create a user  (role: "admin", "operator", or "viewer")
POST /auth/users        {"username":"alice","password":"...","role":"viewer"}

# Delete a user
DELETE /auth/users/{username}

# Change your own password (any authenticated user)
POST /auth/change-password   {"currentPassword":"...","newPassword":"..."}
```

---

## External OIDC Authentication (Phase 1)

kuberniq-server can validate JWTs issued by an external OIDC provider alongside its own local tokens. This is **disabled by default** and requires no code changes — everything is configured via a K8s Secret.

**Supported providers:** Entra ID (Azure AD) · AWS Cognito · Google · Okta · any OIDC-compliant issuer

### How it works

When a request arrives with a `Bearer` token:
1. The server first tries to validate it as a local kuberniq JWT
2. If that fails **and** OIDC is enabled, it validates the token against the provider's JWKS
3. Group/role claims are mapped to kuberniq roles (`admin` / `operator` / `viewer`)
4. Local tokens continue to work unchanged — OIDC is purely additive

### Enable OIDC — create the config Secret

**Entra ID (Azure AD)**
```bash
kubectl create secret generic kuberniq-oidc-config \
  -n <server-namespace> \
  --from-literal=enabled=true \
  --from-literal=authority=https://login.microsoftonline.com/<tenantId>/v2.0 \
  --from-literal=clientId=<app-registration-client-id> \
  --from-literal=clientSecret=<client-secret> \
  --from-literal=roleClaimType=roles \
  --from-literal=adminValues=kuberniq-admins \
  --from-literal=operatorValues=kuberniq-operators \
  --from-literal=defaultRole=viewer
```

**AWS Cognito**
```bash
kubectl create secret generic kuberniq-oidc-config \
  -n <server-namespace> \
  --from-literal=enabled=true \
  --from-literal=authority=https://cognito-idp.<region>.amazonaws.com/<userPoolId> \
  --from-literal=clientId=<cognito-app-client-id> \
  --from-literal=roleClaimType=cognito:groups \
  --from-literal=adminValues=kuberniq-admins \
  --from-literal=defaultRole=viewer
```

**Google**
```bash
kubectl create secret generic kuberniq-oidc-config \
  -n <server-namespace> \
  --from-literal=enabled=true \
  --from-literal=authority=https://accounts.google.com \
  --from-literal=clientId=<client-id>.apps.googleusercontent.com \
  --from-literal=roleClaimType=groups \
  --from-literal=defaultRole=viewer
```

### Secret fields

| Field | Required | Description |
|-------|----------|-------------|
| `enabled` | ✅ | Set to `true` to activate OIDC |
| `authority` | ✅ | Issuer base URL — must expose `/.well-known/openid-configuration` |
| `clientId` | ✅ | App/client ID from your provider |
| `clientSecret` | Phase 2 only | Not required for token validation |
| `roleClaimType` | ✅ | JWT claim holding groups/roles (`roles`, `cognito:groups`, `groups`) |
| `adminValues` | Optional | Comma-separated group names → `admin` role |
| `operatorValues` | Optional | Comma-separated group names → `operator` role |
| `defaultRole` | Optional | Fallback role for unmatched users (default: `viewer`) |

### Restart and verify

```bash
# Restart the pod to pick up the new secret
kubectl rollout restart deployment/kuberniq-server -n <server-namespace>

# Verify OIDC is loaded
curl http://<host>/health
# → {"status":"ok","oidc":{"enabled":true,"authority":"https://..."}}
```

> **Phase 2** (browser login redirect + `/auth/oidc/login` callback) is on the roadmap — see below.

---

## Quick Local Run

1. Install [.NET 10 SDK](https://dotnet.microsoft.com/download)
2. Restore and run:
   ```bash
   cd kuberniq-server
   dotnet restore
   dotnet run
   ```
3. Open `http://localhost:8080` in your browser.

The service reads `~/.kube/config` when running locally, and uses in-cluster credentials when deployed.

---

## Deploy with Helm

The chart is published in this repository under `helm/Application/kuberniq-server/`.  
Anyone with `kubectl` access to a cluster can install it directly — no ArgoCD required.

### Install

```bash
helm upgrade --install kuberniq-server \
  oci://raw.githubusercontent.com/oluwaTG/kuberniq/main/helm/Application/kuberniq-server \
  --namespace kuberniq-server \
  --create-namespace
```

Or clone the repo and install from the local path:

```bash
git clone https://github.com/oluwaTG/kuberniq.git
cd kuberniq

helm upgrade --install kuberniq-server helm/Application/kuberniq-server \
  --namespace kuberniq-server \
  --create-namespace
```

### Install with custom values

Override any value inline or with your own values file:

```bash
# Override the ingress hostname
helm upgrade --install kuberniq-server helm/Application/kuberniq-server \
  --namespace kuberniq-server \
  --create-namespace \
  --set ingress.hosts[0].host=kuberniq-server.yourdomain.com

# Or use a custom values file
helm upgrade --install kuberniq-server helm/Application/kuberniq-server \
  --namespace kuberniq-server \
  --create-namespace \
  --values my-values.yaml
```

### Uninstall

```bash
helm uninstall kuberniq-server --namespace kuberniq-server
```

### Key values to override

| Value | Default | Description |
|-------|---------|-------------|
| `image.repository` | `elumole22/kuberniq-server` | Container image registry and name |
| `image.tag` | `1.1.0` | Image tag — update on every release |
| `ingress.enabled` | `true` | Enable/disable the ingress |
| `ingress.hosts[0].host` | `kuberniq-server.local` | Hostname for the dashboard |
| `ingress.className` | `nginx` | Ingress class (change if using Traefik, etc.) |
| `serviceAccount.create` | `true` | Create a dedicated service account |
| `rbac.create` | `true` | Create the ClusterRole and ClusterRoleBinding |
| `resources.requests.memory` | `64Mi` | Pod memory request |
| `resources.limits.memory` | `256Mi` | Pod memory limit |

See `helm/Application/kuberniq-server/values.yaml` for the full list of options with comments.

---

## Dashboard

The SPA dashboard is served at `/` and includes:

- **Overview** — cluster version, node count, pod count, not-ready pods, deployment health
- **Nodes** — CPU/memory capacity and allocatable per node
- **Pods** — ready count (`2/2`), phase, restarts, per-container status tooltip, log viewer
- **Deployments / StatefulSets / DaemonSets** — replica status
- **Jobs / CronJobs** — status badge, succeeded/failed counts, schedule and last run times
- **Autoscalers (HPA)** — min–max range, current vs desired replicas, "At Max" warning
- **Services / Ingresses / Network Policies** — networking resources
- **ConfigMaps / Secrets** — keys listed, secret values are redacted
- **PVCs / Storage Classes** — storage resources with capacity and binding mode
- **Events** — namespace-wide Warning/Normal events sorted by last seen
- **Resource Quotas** — used vs hard limit per resource

### Log viewer

Click **Logs** on any pod row to open the slide-up log panel:
- **Container selector** — for multi-container pods, pick a specific container or view all merged
- **Tail selector** — last 100 / 250 / 500 / 1000 lines
- **Filter** — real-time text filter across log lines
- **Colour coding** — errors (red), warnings (amber), info (blue)
- **Wrap toggle** — enable/disable line wrapping

---

## Endpoints

### Authentication (public)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | SPA dashboard (login page if unauthenticated) |
| GET | `/health` | Health probe — returns `{"status":"ok","ns":"<ns>","oidc":{"enabled":false}}` |
| POST | `/auth/login` | Login — body `{"username":"...","password":"..."}`, returns `accessToken` + `refreshToken` |
| POST | `/auth/refresh` | Rotate tokens — body `{"refreshToken":"..."}` |
| POST | `/auth/logout` | Revoke refresh token — body `{"refreshToken":"..."}` |

### Authentication (protected)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/auth/users` | List users (admin only) |
| POST | `/auth/users` | Create user (admin only) — body `{"username","password","role"}` |
| DELETE | `/auth/users/{username}` | Delete user (admin only) |
| POST | `/auth/change-password` | Change own password — body `{"currentPassword","newPassword"}` |

### Core

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | SPA dashboard |
| GET | `/health` | Health probe — returns `{"status":"ok"}` |
| GET | `/cluster/info` | Cluster version, node count, node ready status |
| GET | `/namespaces` | List all namespaces |

### Nodes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/nodes` | All nodes — name, roles, OS, kubelet version, CPU/memory |
| GET | `/nodes/{name}` | Full node detail — labels, taints, conditions, addresses, capacity, allocatable |
| GET | `/metrics/nodes` | Node CPU/memory capacity and allocatable |

### Pods

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/pods` | List pods with ready count (`2/2`), phase, restarts, per-container status |
| GET | `/namespaces/{ns}/pods/{pod}/events` | Events scoped to a single pod |
| GET | `/namespaces/{ns}/pods/{pod}/logs?tail=200` | Default container logs (last N lines) |
| GET | `/namespaces/{ns}/pods/{pod}/logs/all?tail=200` | All containers' logs as `{ containerName: logText }` |
| GET | `/namespaces/{ns}/pods/{pod}/containers` | List containers with image, ports, resource requests/limits, live status |
| GET | `/namespaces/{ns}/pods/{pod}/containers/{container}/logs?tail=200` | Logs for a specific container by name |

### Metrics

| Method | Path | Description |
|--------|------|-------------|
| GET | `/metrics/nodes` | CPU/memory capacity and allocatable per node |
| GET | `/metrics/pods` | Per-container CPU/memory requests and limits (all namespaces) |

### Deployments

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/deployments` | List deployments with replica status |
| GET | `/namespaces/{ns}/deployments/{name}` | Full deployment spec and status |
| GET | `/namespaces/{ns}/replicasets` | ReplicaSets with owning deployment name and image |

### StatefulSets & DaemonSets

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/statefulsets` | StatefulSets with replica status |
| GET | `/namespaces/{ns}/daemonsets` | DaemonSets with desired/ready/available counts |

### Jobs & CronJobs

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/jobs` | Jobs with succeeded/failed/active counts and start time |
| GET | `/namespaces/{ns}/jobs/{name}` | Full job detail including conditions and selector |
| GET | `/namespaces/{ns}/cronjobs` | CronJobs with schedule, suspend status, last run times |
| GET | `/namespaces/{ns}/cronjobs/{name}` | Full CronJob detail including history limits and active jobs |

### Autoscaling

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/hpa` | HPAs with target, min/max replicas, current vs desired, conditions |

### Services & Networking

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/services` | List services with type and ports |
| GET | `/namespaces/{ns}/services/{name}` | Full service config |
| GET | `/namespaces/{ns}/ingresses` | List ingresses with hosts and TLS |
| GET | `/namespaces/{ns}/ingresses/{name}` | Full ingress config |
| GET | `/namespaces/{ns}/networkpolicies` | Network policies with policy types and rule counts |

### Config

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/configmaps` | List configmaps (key names only) |
| GET | `/namespaces/{ns}/configmaps/{name}` | Full configmap including data values |
| GET | `/namespaces/{ns}/secrets` | List secrets (key names only — values are always redacted) |

### Storage

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/persistentvolumeclaims` | PVCs with status, capacity, storage class |
| GET | `/persistentvolumes` | Cluster-wide PVs with capacity, claim ref, reclaim policy |
| GET | `/storageclasses` | Storage classes with provisioner, binding mode, default flag |

### RBAC

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/roles` | Namespaced roles with rules |
| GET | `/namespaces/{ns}/rolebindings` | Namespaced role bindings |
| GET | `/namespaces/{ns}/serviceaccounts` | Service accounts in a namespace |
| GET | `/clusterroles` | Cluster-wide roles (system roles excluded) |
| GET | `/clusterrolebindings` | Cluster-wide role bindings |

### Namespace Observability

| Method | Path | Description |
|--------|------|-------------|
| GET | `/namespaces/{ns}/events` | All namespace events sorted by last seen (Warning and Normal) |
| GET | `/namespaces/{ns}/resourcequotas` | Resource quotas with hard limits vs current usage |
| GET | `/namespaces/{ns}/limitranges` | LimitRanges with min/max/default per resource |

### Troubleshoot

| Method | Path | Description |
|--------|------|-------------|
| GET | `/troubleshoot/service/{ns}/{name}` | Aggregated pods + events + last 200 log lines for a service or deployment name |

---

## Security Notes

- **JWT authentication** is enabled by default — all API routes (except login/refresh/logout) require a `Bearer` token.
- Access tokens expire after **1 hour**; refresh tokens after **30 days**. Tokens are rotated on refresh.
- Passwords are hashed with **bcrypt** (work factor 12) and stored as K8s Secrets.
- The JWT signing key is auto-generated on first run and stored as a K8s Secret.
- **External OIDC** tokens are validated against the provider's JWKS endpoint. Signing keys are cached for 24 hours and auto-refreshed on rotation. OIDC is disabled unless the `kuberniq-oidc-config` Secret exists with `enabled=true`.
- The ClusterRole is **read-only**. No write, delete, or exec permissions are granted.
- Secret values are never returned — only key names are exposed.
- Delete the `kuberniq-admin-initial-password` Secret after changing the admin password.

---

## Roadmap

- [x] Authentication (JWT with bcrypt + K8s Secret storage)
- [x] Role-based access control (`admin` / `operator` / `viewer`)
- [x] User management dashboard (admin-only UI page)
- [x] Collapsible sidebar navigation
- [x] External OIDC authentication — Phase 1: token validation (Entra ID, Cognito, Google, Okta)
- [x] Multi-arch Docker image (`amd64` + `arm64`)
- [ ] External OIDC — Phase 2: browser login redirect (`/auth/oidc/login` + callback)
- [ ] External OIDC — Phase 3: role sync / first-login provisioning
- [ ] Multi-cluster support (UI cluster switcher)
- [ ] Rate limiting and response caching
- [ ] Cluster-wide health summary endpoint (`/summary`)
- [ ] WebSocket log streaming (real-time instead of tail)
