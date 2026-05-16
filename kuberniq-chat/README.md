# MCP Chatbot

An AI-powered Kubernetes assistant built with Streamlit and GPT-4o.  
Answers are grounded entirely in **live cluster data** fetched from the [MCP Server](../mcp-server/README.md) — no hallucinated pod names, no stale state.

---

## Features

- **Natural language queries** — ask about pods, logs, events, deployments, jobs, HPA, and more
- **Smart entity extraction** — identifies namespaces, pod names, app names, and container names from plain English
- **Intent routing** — maps each question to the right set of MCP endpoints automatically
- **Auto-troubleshoot mode** — fans out across events, logs, deployment state, HPA, and resource quotas in one go
- **Container-aware logs** — fetch logs for a named container or all containers merged
- **App-name log resolution** — say "get logs from the devops-helper app" and it finds the matching pods automatically
- **YAML manifest analysis** — upload a Kubernetes YAML and get a security + misconfiguration review
- **Helm packaged** — deploy to any cluster with one command

---

## Prerequisites

- A running [MCP Server](../mcp-server/README.md) reachable from the chatbot
- An OpenAI API key

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
  -e MCP_SERVER_URL=http://your-mcp-server:8080 \
  elumole22/mcp-chatbot:1.0.0
```

---

## Deploy with Helm

### 1. Create the secret (once per namespace)

```bash
kubectl create secret generic mcp-chatbot-secrets \
  --from-literal=OPENAI_API_KEY=sk-... \
  -n mcp-chatbot
```

### 2. Install the chart

```bash
git clone https://github.com/oluwaTG/azure-devops-scripts.git
cd azure-devops-scripts

helm upgrade --install mcp-chatbot helm/Application/mcp-chatbot \
  --namespace mcp-chatbot \
  --create-namespace \
  --set mcpServerUrl=http://mcp-server.mcp-server.svc.cluster.local:8080
```

### 3. Access the UI

```bash
kubectl port-forward svc/mcp-chatbot 8501:8501 -n mcp-chatbot
```

Then open `http://localhost:8501`.

### Custom values

| Value | Default | Description |
|---|---|---|
| `image.tag` | `1.0.0` | Chatbot image version |
| `mcpServerUrl` | `http://mcp-server.mcp-server.svc.cluster.local:8080` | Internal MCP Server URL |
| `openaiSecretName` | `mcp-chatbot-secrets` | Name of the Kubernetes Secret holding the API key |
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
