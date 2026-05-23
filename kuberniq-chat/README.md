# kuberniq-chat

An AI-powered Kubernetes assistant built with Streamlit and GPT-4o.  
Answers are grounded entirely in **live cluster data** fetched from [kuberniq-server](../kuberniq-server/README.md) — no hallucinated pod names, no stale state.

---

## Features

- **Natural language queries** — ask about pods, logs, events, deployments, jobs, HPA, and more
- **JWT authentication** — authenticates to kuberniq-server with a dedicated service account; token cached and refreshed transparently
- **Smart entity extraction** — identifies namespaces, pod names, app names, and container names from plain English
- **Intent routing** — maps each question to the right set of API endpoints automatically
- **Auto-troubleshoot mode** — fans out across events, logs, deployment state, HPA, and resource quotas in one turn
- **Container-aware logs** — fetch logs for a named container or all containers merged
- **App-name log resolution** — say "get logs from the devops-helper app" and it finds the matching pods automatically
- **YAML manifest analysis** — upload a Kubernetes YAML and get a security + misconfiguration review
- **Helm packaged** — deploy to any cluster with one command

---

## Prerequisites

- A running [kuberniq-server](../kuberniq-server/README.md) (`>= 1.1.0`) reachable from the chatbot
- An OpenAI API key
- A `viewer` service account on the server (see [Authentication](#authentication) below)

---

## Authentication

Since kuberniq-server `1.1.0`, all API endpoints require a JWT. The chatbot authenticates as a dedicated **viewer** service account — not admin credentials.

### 1. Create the service account on the server

```bash
# Get an admin token first
TOKEN=$(curl -s -X POST http://<server>/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<admin-password>"}' \
  | jq -r .accessToken)

# Create the viewer account the chatbot will use
curl -X POST http://<server>/auth/users \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"username":"kuberniq-chat","password":"<strong-password>","role":"viewer"}'
```

### 2. Store the credentials as a Kubernetes Secret

```bash
kubectl create secret generic kuberniq-chat-mcp-auth \
  --from-literal=username=kuberniq-chat \
  --from-literal=password=<strong-password> \
  -n kuberniq-chat
```

The Helm chart injects this Secret as `MCP_USERNAME` / `MCP_PASSWORD` environment variables. The chatbot logs in once at startup and silently refreshes the JWT when it expires — no login overhead on every request.

### Local dev (no auth)

If `MCP_PASSWORD` is not set, the chatbot skips authentication entirely. This is backward-compatible with a locally run server that has no auth configured.

---

## Quick Local Run

```bash
cd mcp-chatbot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (never committed):

```env
OPENAI_API_KEY=sk-...
MCP_SERVER_URL=http://localhost:8080
# Optional — omit if running the server locally without auth
MCP_USERNAME=kuberniq-chat
MCP_PASSWORD=<strong-password>
```

Run:

```bash
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

---

## Run with Docker

```bash
docker run -p 8501:8501 \
  -e OPENAI_API_KEY=sk-... \
  -e MCP_SERVER_URL=http://your-kuberniq-server:8080 \
  -e MCP_USERNAME=kuberniq-chat \
  -e MCP_PASSWORD=<strong-password> \
  elumole22/kuberniq-chat:1.0.1
```

---

## Deploy with Helm

### 1. Create secrets (once per namespace)

```bash
# OpenAI API key
kubectl create secret generic kuberniq-chat-secrets \
  --from-literal=OPENAI_API_KEY=sk-... \
  -n kuberniq-chat

# MCP Server credentials (viewer account created above)
kubectl create secret generic kuberniq-chat-mcp-auth \
  --from-literal=username=kuberniq-chat \
  --from-literal=password=<strong-password> \
  -n kuberniq-chat
```

### 2. Install the chart

```bash
git clone https://github.com/oluwaTG/kuberniq.git
cd kuberniq

helm upgrade --install kuberniq-chat helm/Application/kuberniq-chat \
  --namespace kuberniq-chat \
  --create-namespace \
  --set mcpServerUrl=http://kuberniq-server.kuberniq-server.svc.cluster.local:8080
```

### 3. Access the UI

```bash
kubectl port-forward svc/kuberniq-chat 8501:8501 -n kuberniq-chat
```

Then open `http://localhost:8501`.

### Key values

| Value | Default | Description |
|---|---|---|
| `image.tag` | `1.0.1` | Chatbot image version |
| `mcpServerUrl` | `http://kuberniq-server.kuberniq-server.svc.cluster.local:8080` | Internal kuberniq-server URL |
| `mcpAuth.secretName` | `kuberniq-chat-mcp-auth` | K8s Secret with `username` + `password` keys |
| `mcpAuth.usernameKey` | `username` | Key name for the username inside the Secret |
| `mcpAuth.passwordKey` | `password` | Key name for the password inside the Secret |
| `ingress.enabled` | `true` | Expose the UI via Ingress |
| `ingress.hosts[0].host` | `mcp-chatbot.local` | Ingress hostname |
| `resources.limits.memory` | `512Mi` | Memory limit for the pod |

See `helm/Application/kuberniq-chat/values.yaml` for the full list.
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
- [ ] Conversation memory with summarisation for long sessions
- [ ] Multi-cluster support (switch clusters mid-session)
- [ ] Streaming log tail (live follow mode)
