# Kuberniq Chat

An AI-powered Kubernetes assistant with a **Next.js** frontend and **FastAPI** backend.  
Answers are grounded entirely in **live cluster data** fetched from the [Kuberniq Server](../kuberniq-server/README.md) — no hallucinated pod names, no stale state.

---

## Architecture

```
kuberniq-chat/
├── frontend/           # Next.js 15 (App Router, static export)
│   ├── app/            #   Pages: /, /login, /chat
│   ├── components/     #   Sidebar, ChatWindow, InputBar
│   └── lib/            #   API client, types
├── api/                # FastAPI backend (Python 3.12)
│   ├── main.py         #   REST + SSE endpoints
│   ├── rag.py          #   RAG pipeline + LLM orchestration
│   ├── auth.py         #   JWT auth, RBAC, K8s Secrets / file-based users
│   ├── mcp_client.py   #   MCP server client (auth, caching, retry)
│   └── models.py       #   Pydantic request/response models
└── Dockerfile          # Multi-stage: Next.js build → Python runtime
```

The frontend is compiled to a static export (`out/`) and served directly by the FastAPI process via `StaticFiles`. A single Docker image ships both.

---

## Features

- **Modern chat UI** — dark-themed Next.js interface with streaming token output, model selector, and collapsible raw-context drawer
- **Chat authentication + RBAC** — login page with three roles (admin / operator / viewer); namespace scoping for viewers; admin user-management panel in the sidebar
- **Persistent JWT sessions** — access + refresh token pair stored as `httponly` cookies; survive pod restarts without re-login; signing key persisted in a K8s Secret (prod) or local file (dev)
- **33 models across 8 providers** — OpenAI, Anthropic, Google, Groq, Mistral, DeepSeek, xAI, Ollama; switch per-conversation from the sidebar dropdown
- **Multi-cluster support** — query any registered remote cluster by name; the LLM extracts the target cluster and routes all MCP calls with `?cluster=<name>`
- **Natural language queries** — ask about pods, logs, events, deployments, services, ingresses, HPAs, resource quotas, RBAC, nodes, storage, and more
- **Smart namespace resolution** — when a resource name is given without a namespace, the RAG searches all namespaces and returns the first match
- **LLM entity extraction** — identifies namespaces, pod names, app names, containers, and time windows from plain English, including follow-up references
- **Conversation memory** — passes recent chat history so follow-up questions resolve correctly
- **Time-bounded log queries** — natural language time windows ("last 2 hours", "between Monday 10am and now")
- **Auto-troubleshoot mode** — fans out across events, logs, deployment state, HPA, and resource quotas in one response
- **YAML manifest analysis** — paste or upload a Kubernetes YAML for security + misconfiguration review
- **User management** — admins can create, edit (role + namespace access), and delete users from the sidebar
- **Dev mode** — `DEV_MODE=true` + a local `users.json` file; no K8s required for local development
- **Helm packaged** — deploy to any cluster with one command

---

## Prerequisites

- A running [Kuberniq Server](../kuberniq-server/README.md) reachable from the chatbot
- An LLM API key (OpenAI, Anthropic, Groq, etc.)
- MCP server credentials (username + password)

---

## Quick Local Run

```bash
cd kuberniq-chat

# 1. Backend
python -m venv .venv && source .venv/bin/activate
pip install -r api/requirements.txt

cp api/.env.example api/.env   # then fill in your values

cd api && uvicorn main:app --port 8000 --reload &

# 2. Frontend
cd ../frontend
npm install
npm run dev        # http://localhost:3000
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes* | OpenAI key (*or the key for whichever provider/model you choose) |
| `MCP_SERVER_URL` | Yes | e.g. `http://localhost:5165` |
| `MCP_USERNAME` | Yes | MCP server admin username |
| `MCP_PASSWORD` | Yes | MCP server admin password |
| `KUBERNIQ_NAMESPACE` | No | K8s namespace for user Secrets (default: `kuberniq`) |
| `DEV_MODE` | No | `true` — use local `users.json` instead of K8s Secrets |
| `ALLOWED_ORIGINS` | No | Comma-separated CORS origins (default: `http://localhost:3000`) |

---

## Docker

```bash
# Build
docker build -t kuberniq-chat:latest .

# Run
docker run -p 8000:8000 \
  -v "$KUBECONFIG:/root/.kube/config:ro" \
  -e OPENAI_API_KEY=sk-... \
  -e MCP_SERVER_URL=http://your-mcp-server \
  -e MCP_USERNAME=admin \
  -e MCP_PASSWORD=your-password \
  kuberniq-chat:latest
```

Open `http://localhost:8000` in your browser.

---

## Authentication

The chat app has its **own** login system, separate from the MCP server credentials.

### Getting the initial admin password

**Kubernetes (Helm deployment):**
```bash
kubectl get secret kuberniq-chat-admin-initial-password \
  -n kuberniq \
  -o jsonpath='{.data.password}' | base64 -d && echo
```

**Dev mode (local file):**
```bash
cat kuberniq-chat/data/admin-initial-password.txt
```

> ⚠️ Change the admin password from the sidebar immediately after first login.

---

## User Roles

| Role | Access |
|---|---|
| `admin` | Full access — cluster data, logs, secrets, RBAC, user management |
| `operator` | Broad access — pods, logs, events, deployments, metrics; no secrets or RBAC |
| `viewer` | Read-only — pods, events, deployments, services in **assigned namespaces only** |

Admins assign namespace access per viewer via the **User Management** panel in the sidebar.

---

## Helm Deployment

```bash
helm upgrade --install kuberniq-chat helm/Application/kuberniq-chat \
  --namespace kuberniq \
  --create-namespace \
  --set env.OPENAI_API_KEY=sk-... \
  --set env.MCP_SERVER_URL=http://kuberniq-server.kuberniq-server.svc.cluster.local:8080 \
  --set env.MCP_USERNAME=admin \
  --set env.MCP_PASSWORD=your-password
```

See [`helm/Application/kuberniq-chat/values.yaml`](../helm/Application/kuberniq-chat/values.yaml) for all options.

---

## CI / CD

The GitHub Actions workflow (`.github/workflows/kuberniq-chat.yml`) triggers on any push to `main` that changes files under `kuberniq-chat/**`. It reads the version from `kuberniq-chat/VERSION`, pushes a `chat/vX.Y.Z` git tag, and builds + pushes a multi-arch Docker image (`elumole22/kuberniq-chat`) to Docker Hub.

To release a new version, bump `kuberniq-chat/VERSION` and push.
