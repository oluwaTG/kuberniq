# Kuberniq Chat

An AI-powered Kubernetes assistant built with Streamlit and GPT-4o.  
Answers are grounded entirely in **live cluster data** fetched from the [Kuberniq Server](../kuberniq-server/README.md) — no hallucinated pod names, no stale state.

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

### 1. Create the secret (once per namespace)

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

- `OPENAI_API_KEY` is **never baked into the image** — it is injected at runtime from a Kubernetes Secret
- The chatbot has **no Kubernetes permissions** of its own — all cluster access is delegated to the MCP Server
- For external access, enable `ingress.enabled=true` and populate `ingress.tls` with a cert-manager or pre-provisioned secret
- Internal-only deployments (default) communicate over HTTP within the cluster network

---

## Architecture

```
User (browser)
     │
     ▼
┌─────────────────┐        ┌─────────────────┐
│   MCP Chatbot   │──────▶│   MCP Server    │──────▶ Kubernetes API
│  Streamlit/GPT  │  HTTP  │  .NET 10 API    │
└─────────────────┘        └─────────────────┘
```

The chatbot never talks to the Kubernetes API directly.  
All live data flows through the MCP Server, which runs in-cluster with a scoped read-only ServiceAccount.

---

## Roadmap

- [ ] Conversation memory with summarisation for long sessions
- [ ] Multi-cluster support (switch MCP Server URL mid-session)
- [ ] Auth proxy support for multi-tenant deployments
- [ ] Streaming log tail (live follow mode)
