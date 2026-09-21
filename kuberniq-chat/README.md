# Kuberniq Chat

An AI-powered Kubernetes assistant with a **Next.js** frontend and **FastAPI** backend.  
The retrieval pipeline supplies **live cluster data** from the [Kuberniq Server](../kuberniq-server/README.md) to the model. Generated answers still require verification.

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
│   ├── rag.py          #   Streaming answers + LLM orchestration
│   ├── rag_plan.py     #   Validated query plans + deterministic fallback
│   ├── rag_retrieval.py #   Authorized, bounded evidence retrieval
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
- **Persistent JWT sessions** — access + refresh tokens stored in browser `localStorage`, with Bearer authentication; signing key persisted in a K8s Secret (prod) or local file (dev). Tokens are accessible to JavaScript, so they do not have HttpOnly cookie protection. Refresh reloads the current user role and namespace assignments.
- **33 models across 8 providers** — OpenAI, Anthropic, Google, Groq, Mistral, DeepSeek, xAI, Ollama; switch per-conversation from the sidebar dropdown
- **Multi-cluster support** — query any registered remote cluster by name; the LLM extracts the target cluster and routes all MCP calls with `?cluster=<name>`
- **Natural language queries** — ask about pods, logs, events, deployments, services, ingresses, HPAs, resource quotas, RBAC, nodes, storage, and more
- **Namespace resolution** — searches accessible namespaces, resolves exact names, and asks for clarification when a name occurs in multiple namespaces
- **Structured query planning** — identifies resource types, names, clusters, namespaces, labels, containers and time windows in one model call, including follow-up references; deterministic matching provides a fallback
- **Conversation memory** — passes recent chat history so follow-up questions resolve correctly
- **Time-bounded log queries** — relative durations or timestamp ranges with timezone; log lines are filtered in code
- **Troubleshooting** — correlates pod details, container states, events, workload state, HPA, quotas and sampled logs; supports previous-container and init-container logs
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
pip install --require-hashes -r api/requirements.txt

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
| `CORS_ORIGINS` | No | Comma-separated CORS origins (default: `http://localhost:3000`) |

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

Create the `kuberniq-chat-secrets` Secret with an `OPENAI_API_KEY` key and the
`kuberniq-chat-mcp-auth` Secret with `username` and `password` keys in the release
namespace first. The MCP credentials must belong to an existing server account.
The command below replaces the chart's placeholder `env` list with a Secret reference.

```bash
helm upgrade --install kuberniq-chat helm/Application/kuberniq-chat \
  --namespace kuberniq \
  --create-namespace \
  --set mcpServerUrl=http://kuberniq-server.kuberniq-server.svc.cluster.local:8080 \
  --set mcpAuth.secretName=kuberniq-chat-mcp-auth \
  --set-json 'env=[{"name":"OPENAI_API_KEY","valueFrom":{"secretKeyRef":{"name":"kuberniq-chat-secrets","key":"OPENAI_API_KEY"}}}]'
```

See [`helm/Application/kuberniq-chat/values.yaml`](../helm/Application/kuberniq-chat/values.yaml) for all options.

---

## CI / CD

The GitHub Actions workflow (`.github/workflows/kuberniq-chat.yml`) triggers on any push to `main` that changes files under `kuberniq-chat/**`. It reads the version from `kuberniq-chat/VERSION`, pushes a `chat/vX.Y.Z` git tag, and builds + pushes a multi-arch Docker image (`elumole22/kuberniq-chat`) to Docker Hub.

To release a new version, bump `kuberniq-chat/VERSION` and push.


## Development checks and dependency updates

Run the API regression suite without a cluster or LLM credentials:

```bash
python -m unittest discover -s kuberniq-chat/tests -v
```

`api/requirements.in` lists direct dependency constraints. `api/requirements.txt`
locks direct and transitive packages with hashes for Python 3.12 and newer.
Docker and CI install this lock with `--require-hashes`. To intentionally update it,
run from the repository root with uv 0.12.17:

```bash
uv pip compile kuberniq-chat/api/requirements.in --python-version 3.12 --universal \
  --generate-hashes --upgrade --output-file kuberniq-chat/api/requirements.txt
```

Review the lock diff and rerun the tests after dependency updates. The root
`checks.yml` workflow runs API tests, frontend type checking/build, server
HTTP authorization tests, and the CLI build on pull requests and pushes to main.
Helm linting runs in its existing workflow. These checks are separate from release
workflows and run even when a version has not changed.

Access tokens retain their issued permissions until expiry (one hour by default).
Refresh picks up changes from the user store and rejects deleted users. Development
refresh tokens remain stateless; production refresh tokens are rotated Kubernetes
Secrets.


## RAG retrieval behavior

The planner selects capabilities from an explicit catalog; it cannot create arbitrary
URLs or execute commands. The executor checks the current role, registered cluster
names, and namespace assignments before fetching resources. All namespaced resource
lists exposed by the server support discovery across permitted namespaces. Explicit
cluster and namespace comparisons preserve separate source paths and evidence sections.

Named resources use exact matches. Services and deployments use their selectors to
find pods; workload-name prefixes are a fallback when selectors are absent. An app name
can match `app` / `app.kubernetes.io/name` labels or a pod-name prefix. Ambiguous names
ask for a namespace instead of silently selecting the first match. When no pod is named,
log and troubleshooting requests inspect a bounded sample prioritizing unhealthy pods
and restarts. Labels filter pod selection; other resource lists remain scope-wide.

Each answer receives source paths, endpoint record counts, failed-read notices,
retrieval timestamps and explicit coverage limits. Completed evidence survives the
overall deadline. Resource limits, node capacity and allocatable values are not measured
usage; the current API does not provide live CPU/memory utilization or historical metrics.
CRDs, PDBs and EndpointSlices are also outside the current retrieval catalog.

Default retrieval limits are 3 clusters, 20 namespaces per cluster, 80 HTTP requests,
6 concurrent requests, 12 resource detail reads and logs from 6 pods. Endpoint calls have
an 8-second timeout and retrieval has a 35-second deadline. Log tails default to 200 lines
per container (up to 1,000 requested through the planner). Context is capped at 48,000
characters, with section/row truncation marked explicitly. Narrow broad queries when
coverage limits are reported. Namespace, cluster-list and cluster-info metadata may be
cached for 60 seconds; resources and logs are fetched per request.

`FAST_LLM_MODEL` optionally selects a separate planning model. If unset, planning uses
the selected answer model, avoiding an implicit requirement for an OpenAI key when using
another provider. Model errors or malformed plans fall back to deterministic parsing;
complex references may need a more explicit question in that mode. YAML review takes
precedence over development mode and does not claim to compare against live state.

Example questions:

- “Compare deployment api in namespaces staging and production on cluster west.”
- “Why is pod api-123 in namespace payments restarting?”
- “Show its previous logs for the last 30 minutes.”
- “List cronjobs across all namespaces on cluster west.”
- “Show logs for service checkout in namespace payments.”
- “Compare pods in namespace payments across clusters local and west.”

Deploy the updated server alongside the chat app to enable timestamped,
previous-container and init-container logs. Older server versions cannot provide all
of these guarantees. The NDJSON frontend contract (`meta`, `token`, `error`, `done`)
is unchanged. Tests use mocked cluster responses and model streams; live model quality
and cluster compatibility still need deployment validation.
