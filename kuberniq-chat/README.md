# Kuberniq Chat

An AI-powered Kubernetes assistant built with Streamlit and GPT-4o.  
Answers are grounded entirely in **live cluster data** fetched from the [Kuberniq Server](../kuberniq-server/README.md) — no hallucinated pod names, no stale state.

---

## Features

- **Chat authentication + RBAC** — login page with three roles (admin / operator / viewer); namespace scoping for viewers; admin user-management panel in the sidebar
- **Persistent sessions** — login state is stored in a signed JWT browser cookie; sessions survive pod restarts and page refreshes without requiring re-login; the signing key is persisted on the mounted PV (`KUBERNIQ_DATA_DIR/jwt_secret.txt`)
- **Container-aware pod queries** — every pod in the `[PODS]` context now includes a `CONTAINERS` column listing each container as `name(image)` (init containers marked `[init]`), so the LLM can answer questions like *"which pods have a Dapr sidecar?"* or *"what image version is each container running?"*
- **Multi-cluster support** — query any registered remote cluster by name; the LLM extracts the target cluster from natural language and routes all MCP calls with `?cluster=<name>`
- **Natural language queries** — ask about pods, logs, events, deployments, jobs, HPA, and more
- **MCP server authentication** — logs in with username/password, sends Bearer tokens, and auto-refreshes on 401
- **LLM entity extraction** — uses `gpt-4o-mini` to identify namespaces, pod names, app names, and containers from plain English, including follow-up references like "that pod" or "same namespace as before"
- **Conversation-aware context** — passes recent chat history so follow-up questions resolve correctly
- **Time-bounded log queries** — ask for logs "between 10am Monday and 11pm today" or "last 2 hours" in any format
- **Intent routing** — maps each question to only the relevant MCP endpoints
- **Auto-troubleshoot mode** — fans out across events, logs, deployment state, HPA, and resource quotas in one go
- **Container-aware logs** — fetch logs for a named container or all containers merged
- **YAML manifest analysis** — upload a Kubernetes YAML and get a security + misconfiguration review
- **Configurable model** — switch between `gpt-4o`, `gpt-4o-mini`, or any OpenAI model via env var
- **Debug mode** — set `DEBUG_MCP=true` locally to see the raw MCP data used for each answer
- **Helm packaged** — deploy to any cluster with one command

---

## Prerequisites

- A running [Kuberniq Server](../kuberniq-server/README.md) reachable from the chatbot
- An OpenAI API key
- MCP server credentials (username + password for the MCP server's own auth)

---

## Quick Local Run

```bash
cd kuberniq-chat
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (never commit this — it contains real credentials):

```env
OPENAI_API_KEY=sk-...
MCP_SERVER_URL=http://localhost:5165
MCP_USERNAME=admin
MCP_PASSWORD=your-mcp-server-password-here

# Optional — defaults to gpt-4o
OPENAI_MODEL=gpt-4o

# Optional — uncomment locally to see raw MCP data in the UI
# DEBUG_MCP=true
```

Run:

```bash
streamlit run app.py
```

Open `http://localhost:8501` in your browser (or `8502` if `8501` is already in use).

---

## Chat App Authentication (login page)

The chat app has its **own** login system, separate from the MCP server credentials.

### Getting the initial admin password

On first startup with no users, the app auto-creates an `admin` account with a random 24-character password and saves it to `KUBERNIQ_DATA_DIR/admin-initial-password.txt`.

**Locally:**
```bash
cat kuberniq-chat/data/admin-initial-password.txt
```

**In Docker:**
```bash
docker exec <container-name> cat /data/admin-initial-password.txt
```

**In Kubernetes:**
```bash
kubectl exec -n kuberniq-chat \
  $(kubectl get pod -n kuberniq-chat -l app=kuberniq-chat -o jsonpath='{.items[0].metadata.name}') \
  -- cat /data/admin-initial-password.txt
```

Or check the pod logs on first startup — look for the banner:
```bash
kubectl logs -n kuberniq-chat -l app=kuberniq-chat | grep -A6 "Initial Admin"
```

> ⚠️ **Change the admin password** from the sidebar immediately after first login, then delete `admin-initial-password.txt`.

### RBAC roles

| Permission | admin | operator | viewer |
|---|---|---|---|
| Pods / events / deployments / services | ✅ | ✅ | ✅ |
| Logs | ✅ | ✅ | ❌ |
| ConfigMaps / Nodes / Metrics / HPA | ✅ | ✅ | ❌ |
| Troubleshoot | ✅ | ✅ | ❌ |
| Secrets (key names only) | ✅ | ❌ | ❌ |
| Kubernetes RBAC | ✅ | ❌ | ❌ |
| User management | ✅ | ❌ | ❌ |
| Namespace scoping | all | all | assigned only |

Viewers can be restricted to specific namespaces — they cannot query outside their assigned list even if they know the name.

### Managing users (admin sidebar)

The **👥 User management** panel lets you create users, delete users, and assign namespaces to viewer accounts. All users can change their own password from the **🔑 Change password** panel.

### User data storage

User accounts are stored in `KUBERNIQ_DATA_DIR/users.json` (default: `./data/users.json`).  
The JWT session-cookie signing key is stored in `KUBERNIQ_DATA_DIR/jwt_secret.txt` (auto-generated on first boot).  
**In production, mount a persistent volume at `/data`** so accounts, passwords, and the signing key all survive pod restarts.

---

## MCP Server Connection

The chat app connects to kuberniq-server using a dedicated service account. This is **separate** from the chat login system above.

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | ✅ | — | OpenAI API key |
| `MCP_SERVER_URL` | ✅ | `http://mcp-server.local` | Base URL of the Kuberniq Server |
| `MCP_USERNAME` | ✅ | `admin` | MCP server username |
| `MCP_PASSWORD` | ✅ | — | MCP server password |
| `OPENAI_MODEL` | — | `gpt-4o` | OpenAI model used for answering |
| `DEBUG_MCP` | — | `false` | Set `true` to show raw MCP context in the UI (local dev only) |
| `KUBERNIQ_DATA_DIR` | — | `./data` | Path to chat data directory — stores `users.json`, `admin-initial-password.txt`, and `jwt_secret.txt` |

### MCP server JWT flow

1. On the first request, calls `POST /auth/login` with `MCP_USERNAME` + `MCP_PASSWORD`
2. The returned `accessToken` is sent as a `Bearer` header on every MCP API call
3. On `401`, automatically calls `POST /auth/refresh`; falls back to re-login if needed

No credentials are ever sent to OpenAI — only formatted cluster data.

---

## Time-bounded Log Queries

```
Check the logs between 10am Monday and 11pm today for any errors
Show me logs from the api-server in the prod namespace since yesterday 3pm
Get logs from the last 2 hours for the devops-helper app
Show logs on Tuesday between 09:00 and 17:00
```

The chatbot uses `gpt-4o-mini` to resolve relative references against the current UTC time and converts them to ISO-8601 timestamps passed to the MCP server as `?sinceTime=...`.

---

## Run with Docker

```bash
docker run -p 8501:8501 \
  -v kuberniq-chat-data:/data \
  -e OPENAI_API_KEY=sk-... \
  -e MCP_SERVER_URL=http://your-mcp-server:5165 \
  -e MCP_USERNAME=kuberniq-chat \
  -e MCP_PASSWORD=your-mcp-server-password \
  -e KUBERNIQ_DATA_DIR=/data \
  elumole22/kuberniq-chat:latest
```

Get the initial chat app admin password after first run:
```bash
docker exec <container-name> cat /data/admin-initial-password.txt
```

---

## Deploy with Helm

### 1. Create the MCP credentials secret (run once per namespace)

Both the MCP server username and password are stored together in one Kubernetes Secret:

```bash
kubectl create secret generic kuberniq-chat-mcp-auth \
  --from-literal=username=kuberniq-chat \
  --from-literal=password=your-mcp-server-password \
  -n kuberniq-chat
```

> This secret name matches `mcpAuth.secretName` in `values.yaml`. The keys `username` and `password` match `mcpAuth.usernameKey` and `mcpAuth.passwordKey`.

### 2. Create the OpenAI secret

```bash
kubectl create secret generic kuberniq-chat-openai \
  --from-literal=OPENAI_API_KEY=sk-... \
  -n kuberniq-chat
```

Then reference it in your `values.yaml`:
```yaml
env:
  - name: OPENAI_API_KEY
    valueFrom:
      secretKeyRef:
        name: kuberniq-chat-openai
        key: OPENAI_API_KEY
```

### 3. Install the chart

```bash
git clone https://github.com/oluwaTG/kuberniq.git
cd kuberniq

helm upgrade --install kuberniq-chat helm/Application/kuberniq-chat \
  --namespace kuberniq-chat \
  --create-namespace \
  --set mcpServerUrl=http://kuberniq-server.kuberniq-server.svc.cluster.local:5165
```

### 4. Get the initial chat app admin password

```bash
kubectl exec -n kuberniq-chat \
  $(kubectl get pod -n kuberniq-chat -l app=kuberniq-chat -o jsonpath='{.items[0].metadata.name}') \
  -- cat /data/admin-initial-password.txt
```

### 5. Access the UI

```bash
kubectl port-forward svc/kuberniq-chat 8501:8501 -n kuberniq-chat
```

Open `http://localhost:8501`, sign in with `admin` and the retrieved password, then **change it from the sidebar**.

### Key Helm values

| Value | Default | Description |
|---|---|---|
| `image.tag` | `latest` | Chatbot image version |
| `mcpServerUrl` | `http://kuberniq-server.kuberniq-server.svc.cluster.local:8080` | Internal MCP Server URL |
| `mcpAuth.secretName` | `kuberniq-chat-mcp-auth` | Secret containing MCP `username` and `password` |
| `mcpAuth.usernameKey` | `username` | Key name for the username inside the secret |
| `mcpAuth.passwordKey` | `password` | Key name for the password inside the secret |
| `openaiModel` | `gpt-4o` | OpenAI model to use |
| `debugMcp` | `false` | Enable raw MCP context panel in UI |
| `ingress.enabled` | `true` | Expose the UI via Ingress |
| `resources.limits.memory` | `512Mi` | Memory limit for the pod |

---

## Example Questions

```
What is going on in the dev namespace?
Fetch the logs from the devops-helper app in dev
Troubleshoot the movies-app service in staging
Which pods are crashing and why?
Show me all jobs in the payments namespace
Check the logs between 10am Monday and now for any errors
Are there any HPA scaling issues?
```

---

## Security Notes

- `OPENAI_API_KEY` and MCP credentials are **never baked into the image** — injected at runtime from Kubernetes Secrets
- Chat app user passwords are stored as **bcrypt hashes** (cost factor 12) — plaintext is never persisted
- Delete `admin-initial-password.txt` after changing the admin password
- The chatbot has **no direct Kubernetes permissions** — all cluster access is delegated to kuberniq-server
- MCP JWT access tokens expire after **1 hour**; refreshed automatically
- For external access, enable `ingress.enabled=true` and add TLS via cert-manager

---

## Architecture

```
Browser
  │  (chat login — auth.py / users.json)
  ▼
┌──────────────────────┐   JWT Bearer    ┌────────────────────────┐
│   kuberniq-chat      │────────────────▶│   kuberniq-server      │──▶ Kubernetes API
│  Streamlit + GPT-4o  │◀────JSON────────│  .NET 10 minimal API   │
└──────────────────────┘                 └────────────────────────┘
         │
         ▼ (OPENAI_API_KEY)
    OpenAI API (GPT-4o)
```

The chatbot never talks to the Kubernetes API directly. All cluster data flows through kuberniq-server.

---

## Roadmap

- [x] JWT authentication for kuberniq-server calls
- [x] Chat app login with local auth + RBAC (admin / operator / viewer)
- [x] Persistent sessions via signed JWT browser cookie (survives pod restarts)
- [x] Time-bounded log queries
- [x] Multi-cluster support (query any registered cluster by name)
- [x] Container-aware pod queries (container names + images in every pod response)
- [ ] OIDC / SSO login support
- [ ] Persistent volume Helm template for user data
- [ ] Streaming log tail (live follow mode)

---

## Features

- **Natural language queries** — ask about pods, logs, events, deployments, jobs, HPA, and more
- **MCP server authentication** — logs in with username/password, sends Bearer tokens, and auto-refreshes on 401
- **LLM entity extraction** — uses `gpt-4o-mini` to identify namespaces, pod names, app names, and containers from plain English, including follow-up references like "that pod" or "same namespace as before"
- **Conversation-aware context** — passes recent chat history to the entity extractor so follow-up questions resolve correctly without repeating the pod/namespace
- **Time-bounded log queries** — ask for logs "between 10am Monday and 11pm today" or "last 2 hours" in any format; the chatbot resolves the window and passes it to the MCP server
- **Intent routing** — maps each question to only the relevant MCP endpoints; doesn't fetch everything on every request
- **Auto-troubleshoot mode** — fans out across events, logs, deployment state, HPA, and resource quotas in one go when you ask to troubleshoot or debug a service
- **Container-aware logs** — fetch logs for a named container or all containers merged
- **App-name log resolution** — say "get logs from the devops-helper app" and it finds matching pods automatically across all namespaces
- **Structured context formatting** — pods, events, and deployments are rendered as compact markdown tables; errors are surfaced clearly; sections are capped at 8 000 chars to prevent context overflow
- **YAML manifest analysis** — upload a Kubernetes YAML and get a security + misconfiguration review
- **Dark dashboard theme** — matches the Kuberniq Server dashboard aesthetic
- **Configurable model** — switch between `gpt-4o`, `gpt-4o-mini`, or any OpenAI model via env var
- **Debug mode** — set `DEBUG_MCP=true` locally to see the raw MCP data used for each answer
- **Helm packaged** — deploy to any cluster with one command

---

## Prerequisites

- A running [Kuberniq Server](../kuberniq-server/README.md) reachable from the chatbot
- An OpenAI API key
- MCP server credentials (username + password)

---

## Quick Local Run

```bash
cd kuberniq-chat
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (never commit this — it contains real credentials):

```env
OPENAI_API_KEY=sk-...
MCP_SERVER_URL=http://localhost:5165
MCP_USERNAME=admin
MCP_PASSWORD=your-password-here

# Optional — defaults to gpt-4o
OPENAI_MODEL=gpt-4o

# Optional — uncomment locally to see raw MCP data in the UI
# DEBUG_MCP=true
```

Run:

```bash
streamlit run app.py
```

Open `http://localhost:8501` in your browser (or `8502` if `8501` is already in use).

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | ✅ | — | OpenAI API key |
| `MCP_SERVER_URL` | ✅ | `http://mcp-server.local` | Base URL of the Kuberniq Server |
| `MCP_USERNAME` | ✅ | `admin` | Username for MCP server login |
| `MCP_PASSWORD` | ✅ | — | Password for MCP server login |
| `OPENAI_MODEL` | — | `gpt-4o` | OpenAI model used for answering questions |
| `DEBUG_MCP` | — | `false` | Set to `true` to show raw MCP context in the UI (local dev only) |

---

## Authentication

The chatbot authenticates with the MCP server using JWT:

1. On the first request, it calls `POST /auth/login` with `MCP_USERNAME` + `MCP_PASSWORD`
2. The returned `accessToken` is sent as a `Bearer` header on every MCP API call
3. If a request returns `401`, the chatbot automatically calls `POST /auth/refresh` using the `refreshToken`
4. If the refresh also fails, it re-runs the full login flow

No credentials are ever sent to OpenAI — only the formatted cluster data.

---

## Time-bounded Log Queries

You can ask for logs scoped to a specific time window in any natural language format:

```
Check the logs between 10am Monday and 11pm today for any errors
Show me logs from the api-server in the prod namespace since yesterday 3pm
Get logs from the last 2 hours for the devops-helper app
Show logs on Tuesday between 09:00 and 17:00
```

The chatbot uses a fast `gpt-4o-mini` call to resolve relative references ("today", "last Monday", "now") against the current UTC time and converts them to ISO-8601 timestamps. These are passed to the MCP server as `?sinceTime=...`, which forwards them to the Kubernetes API. Kubernetes returns timestamped log lines; the LLM then filters to only lines within the requested window.

> **Note**: Kubernetes supports a start time (`sinceTime`) natively. End-time filtering is applied client-side from the timestamped lines returned.

---

## Run with Docker

```bash
docker run -p 8501:8501 \
  -e OPENAI_API_KEY=sk-... \
  -e MCP_SERVER_URL=http://your-mcp-server:5165 \
  -e MCP_USERNAME=admin \
  -e MCP_PASSWORD=your-password \
  elumole22/kuberniq-chat:latest
```

---

## Deploy with Helm

### 1. Create secrets (once per namespace)

```bash
kubectl create secret generic kuberniq-chat-secrets \
  --from-literal=OPENAI_API_KEY=sk-... \
  --from-literal=MCP_PASSWORD=your-password \
  -n kuberniq-chat
```

### 2. Install the chart

```bash
git clone https://github.com/oluwaTG/kuberniq.git
cd kuberniq

helm upgrade --install kuberniq-chat helm/Application/kuberniq-chat \
  --namespace kuberniq-chat \
  --create-namespace \
  --set mcpServerUrl=http://kuberniq-server.kuberniq-server.svc.cluster.local:5165 \
  --set mcpUsername=admin
```

### 3. Access the UI

```bash
kubectl port-forward svc/kuberniq-chat 8501:8501 -n kuberniq-chat
```

Then open `http://localhost:8501`.

### Key Helm values

| Value | Default | Description |
|---|---|---|
| `image.tag` | `latest` | Chatbot image version |
| `mcpServerUrl` | `http://kuberniq-server.kuberniq-server.svc.cluster.local:5165` | Internal MCP Server URL |
| `mcpUsername` | `admin` | MCP server username |
| `openaiModel` | `gpt-4o` | OpenAI model to use |
| `debugMcp` | `false` | Enable raw MCP context panel in UI |
| `secretName` | `kuberniq-chat-secrets` | Kubernetes Secret holding `OPENAI_API_KEY` and `MCP_PASSWORD` |
| `openaiSecretKey` | `OPENAI_API_KEY` | Key name inside the Secret |
| `ingress.enabled` | `false` | Expose the UI via Ingress |
| `ingress.tls` | `[]` | TLS config for external access |
| `resources.limits.memory` | `512Mi` | Memory limit for the pod |

---

## Example Questions

```
What is going on in the dev namespace?
Fetch the logs from the devops-helper app in dev
Troubleshoot the movies-app service in staging
Which pods are crashing and why?
Show me all jobs in the payments namespace
What events are happening in production?
Are there any HPA scaling issues?
```

---

## Security Notes

- `OPENAI_API_KEY` is **never baked into the image** — injected at runtime from a Kubernetes Secret
- `MCP_PASSWORD` is **never baked into the image** — injected from the `kuberniq-chat-mcp-auth` Secret
- The chatbot uses a **viewer** role — it cannot create, modify, or delete any cluster resources
- The chatbot has **no direct Kubernetes permissions** — all cluster access is delegated to kuberniq-server
- JWT access tokens expire after **1 hour**; the chatbot refreshes them automatically with no downtime
- For external access, enable `ingress.enabled=true` and add TLS via cert-manager

---

## Architecture

```
User (browser)
     │
     ▼
┌──────────────────────┐   JWT Bearer    ┌────────────────────────┐
│   kuberniq-chat      │────────────────▶│   kuberniq-server      │──▶ Kubernetes API
│  Streamlit + GPT-4o  │◀────JSON────────│  .NET 10 minimal API   │
└──────────────────────┘                 └────────────────────────┘
         │
         ▼ (OPENAI_API_KEY)
    OpenAI API (GPT-4o)
```

The chatbot never talks to the Kubernetes API directly.  
All live cluster data flows through kuberniq-server, which runs in-cluster with a scoped read-only ServiceAccount.  
The chatbot authenticates as a `viewer` — if the server credential is compromised, blast radius is read-only.

---

## Roadmap

- [x] JWT authentication for kuberniq-server service-to-service calls
- [x] Multi-cluster support (query any registered cluster by name)
- [x] Container-aware pod queries (container names + images exposed to the LLM)
- [ ] Conversation memory with summarisation for long sessions
- [ ] Streaming log tail (live follow mode)
