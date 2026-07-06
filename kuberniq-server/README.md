# Kuberniq Server

A lightweight .NET 10 minimal API that exposes read-only Kubernetes cluster context over HTTP.  
Designed to run in-cluster and serve as the data layer for the Kuberniq Chat and the `kuberniq` CLI.

---

## Features

- **JWT authentication** — all routes are protected; login via `POST /auth/login`, tokens stored in-browser
- **ArgoCD-style bootstrap** — first-run auto-creates an `admin` user with a random password stored in a K8s Secret; no setup wizard needed
- **Role-based access control** — three built-in roles: `admin`, `operator`, `viewer`
- **External OIDC authentication (Phase 1)** — accepts JWTs from any OIDC-compliant provider (Entra ID, AWS Cognito, Google, Okta); enabled via a single K8s Secret; disabled by default
- **Full SPA dashboard** at `/` — login page, collapsible sidebar navigation, namespace switcher, resource tables, pod detail drawer, log viewer, user management (admin)
- **Live cluster data** — 40+ endpoints covering every major Kubernetes resource type
- **JWT authentication** — login endpoint issues access + refresh tokens; all API endpoints require a valid Bearer token
- **Auto-refreshing dashboard** — optional 10-second live refresh with namespace re-sync and last-updated status
- **Time-bounded log queries** — pass `?sinceTime=` (ISO-8601 UTC) or `?sinceSeconds=N` to fetch logs from a specific point in time; timestamps are automatically prepended to every line when a time window is requested
- **Rich pod container detail** — pod list response includes every container's `name`, `image`, `ready`, `restarts`, and `state` sourced from `Spec.Containers` (so pending or crash-looping containers always appear); init containers include image and state; pod `labels` and `nodeName` are included for sidecar-detection queries
- **Multi-cluster support** — register remote clusters via `POST /clusters`; all endpoints gain `?cluster=<name>` routing
- **Helm packaged** — distributed as a Helm chart with fully overridable `values.yaml`
- **Multi-container log support** — view logs per container or all containers merged in one call
- **Troubleshoot endpoint** — aggregates pods, events and logs for a service in one call
- **In-cluster & local** — uses in-cluster config when deployed, falls back to `~/.kube/config` locally

---

## Quick Local Run

1. Install [.NET 10 SDK](https://dotnet.microsoft.com/download)
2. Restore and run:
   ```bash
   cd kuberniq-server
   dotnet restore
   dotnet run
   ```
3. Open `http://localhost:5165` in your browser.

The service reads `~/.kube/config` when running locally, and uses in-cluster credentials when deployed.

---

## Authentication

All API endpoints (except `GET /health` and the SPA at `GET /`) require a valid Bearer token.

### Login

```
POST /auth/login
Content-Type: application/json

{ "username": "admin", "password": "your-password" }
```

Response:

```json
{
  "accessToken": "eyJ...",
  "refreshToken": "eyJ..."
}
```

### Refresh

```
POST /auth/refresh
Content-Type: application/json

{ "refreshToken": "eyJ..." }
```

Returns a new `accessToken` and `refreshToken`. Use this when the access token expires (response `401`) instead of re-entering credentials.

### Using the token

Pass the `accessToken` as a Bearer header on every request:

```
Authorization: Bearer eyJ...
```

---

## Deploy with Helm

The chart is published in this repository under `helm/Application/kuberniq-server/`.

### Install

```bash
git clone https://github.com/oluwaTG/kuberniq.git
cd kuberniq

helm upgrade --install kuberniq-server helm/Application/kuberniq-server \
  --namespace kuberniq-server \
  --create-namespace
```

### Install with custom values

```bash
helm upgrade --install kuberniq-server helm/Application/kuberniq-server \
  --namespace kuberniq-server \
  --create-namespace \
  --set ingress.hosts[0].host=kuberniq-server.yourdomain.com
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
- **Pods** — ready count (`2/2`), phase, restarts, per-container status tooltip, compact detail drawer, log viewer
- **Deployments / StatefulSets / DaemonSets** — replica status
- **Jobs / CronJobs** — status badge, succeeded/failed counts, schedule and last run times
- **Autoscalers (HPA)** — min–max range, current vs desired replicas, "At Max" warning
- **Services / Ingresses / Network Policies** — networking resources
- **ConfigMaps / Secrets** — keys listed, secret values are redacted
- **PVCs / Storage Classes** — storage resources with capacity and binding mode
- **Events** — namespace-wide Warning/Normal events sorted by last seen
- **Resource Quotas** — used vs hard limit per resource

The top bar includes an **Auto: On / Off** toggle. When enabled, the current view refreshes every 10 seconds without showing a full loading state. Namespace changes are re-synced during refresh, so deleted namespaces no longer leave the dashboard pointed at stale state.

### Pod detail drawer

Click **Details** on any pod row to open a right-side drawer with compact operational context:
- phase, node, ready containers, restart count, service account, owner, and start time
- important labels
- init container and container image/state/probe/resource summary
- attention conditions
- important Secret, ConfigMap, PVC, and HostPath volumes
- warning events, or recent events when no warnings exist

### Log viewer

Click **Logs** on any pod row to open the slide-up log panel:
- **Container selector** — for multi-container pods, pick a specific container or view all merged
- **Tail selector** — last 100 / 250 / 500 / 1000 lines
- **Filter** — real-time text filter across log lines
- **Colour coding** — errors (red), warnings (amber), info (blue)
- **Wrap toggle** — enable/disable line wrapping

---

## Endpoints

### Auth

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/login` | Login with `{username, password}` — returns `{accessToken, refreshToken}` |
| POST | `/auth/refresh` | Refresh with `{refreshToken}` — returns a new token pair |

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
| GET | `/namespaces/{ns}/pods` | List pods — name, phase, ready, restarts, nodeName, labels, and per-container detail (name, image, state) for all containers including init containers |
| GET | `/namespaces/{ns}/pods/{pod}` | Pod detail summary — metadata, owner, conditions, containers, important volumes, and recent/warning events |
| GET | `/namespaces/{ns}/pods/{pod}/events` | Events scoped to a single pod |
| GET | `/namespaces/{ns}/pods/{pod}/logs` | Default container logs — supports `?tail=N`, `?sinceTime=`, `?sinceSeconds=N` |
| GET | `/namespaces/{ns}/pods/{pod}/logs/all` | All containers' logs as `{ containerName: logText }` — same time params supported |
| GET | `/namespaces/{ns}/pods/{pod}/containers` | List containers with image, ports, resource requests/limits, live status |
| GET | `/namespaces/{ns}/pods/{pod}/containers/{container}/logs` | Logs for a specific container — same time params supported |

#### Log query parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `tail` | int | Lines to return (default 200). Ignored when `sinceTime` or `sinceSeconds` is set — 5 000 lines are fetched instead. |
| `sinceTime` | ISO-8601 UTC string | Return only logs on or after this timestamp, e.g. `2026-05-26T10:00:00Z`. Timestamps are prepended to every returned line. |
| `sinceSeconds` | int | Return only logs from the last N seconds. Takes precedence over `tail`. |

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

### Multi-cluster

| Method | Path | Description |
|--------|------|-------------|
| GET | `/clusters` | List all registered clusters |
| GET | `/clusters/{name}` | Details for a single registered cluster |
| POST | `/clusters` | Register a remote cluster `{name, server, caData, token}` |
| DELETE | `/clusters/{name}` | Unregister a cluster and delete its stored Secret |

All resource endpoints accept `?cluster=<name>` to target a registered remote cluster.

### Troubleshoot

| Method | Path | Description |
|--------|------|-------------|
| GET | `/troubleshoot/service/{ns}/{name}` | Aggregated pods + events + last 200 log lines for a service or deployment |

---

## Security Notes

- **JWT authentication** is enabled by default — all API routes (except login/refresh/logout) require a `Bearer` token.
- Access tokens expire after **1 hour**; refresh tokens after **30 days**. Tokens are rotated on refresh.
- Passwords are hashed with **bcrypt** (work factor 12) and stored as K8s Secrets.
- The JWT signing key is auto-generated on first run and stored as a K8s Secret.
- **External OIDC** tokens are validated against the provider's JWKS endpoint. Signing keys are cached for 24 hours and auto-refreshed on rotation. OIDC is disabled unless the `kuberniq-oidc-config` Secret exists with `enabled=true`.
- The ClusterRole is **read-only**. No write, delete, or exec permissions are granted.
- Secret values are never returned — only key names are exposed.
- All endpoints except `/health` and the dashboard require a valid JWT Bearer token.
- Sanitize logs before forwarding to any external LLM API to avoid leaking sensitive data.

---

## Roadmap

- [x] JWT authentication (login + refresh)
- [x] Multi-cluster support
- [x] Time-bounded log queries (`sinceTime`, `sinceSeconds`, `timestamps`)
- [ ] Rate limiting and response caching
- [ ] Cluster-wide health summary endpoint (`/summary`)
- [ ] WebSocket log streaming (real-time instead of tail)
