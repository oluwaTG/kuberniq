# Kuberniq CLI

A cross-platform .NET 10 command-line tool for registering and managing remote Kubernetes clusters with the Kuberniq MCP server.

---

## Features

- **Login / logout** — authenticate against the MCP server; connection saved to `~/.kuberniq/config.json`
- **Cluster add** — creates a read-only `ServiceAccount` and `ClusterRoleBinding` in the target cluster, then registers it with the MCP server via `POST /clusters`
- **Cluster list** — show all registered clusters with type, server URL and default flag
- **Cluster show** — detailed view of a registered cluster: version, node count, namespaces, reachability
- **Cluster ping** — latency and connectivity check for any registered cluster
- **Cluster set-default** — nominate a cluster used when no `?cluster=` flag is supplied
- **Cluster remove** — unregister a cluster from the MCP server
- **Interactive context selector** — when `--context` is omitted, displays a menu of all kubeconfig contexts
- **Spinner + progress feedback** — Spectre.Console animated status while long-running operations complete
- **Coloured tables and panels** — structured output via Spectre.Console

---

## Prerequisites

- [.NET 10 SDK](https://dotnet.microsoft.com/download) (build from source) or the pre-built binary
- A running instance of **kuberniq-server** (see `kuberniq-server/README.md`)
- A valid `~/.kube/config` with at least one context when using `cluster add`

---

## Installation

### From source

```bash
git clone https://github.com/oluwaTG/kuberniq.git
cd kuberniq/kuberniq
dotnet build -c Release
dotnet run -- --help
```

### Publish a self-contained binary

```bash
# macOS ARM
dotnet publish -c Release -r osx-arm64 --self-contained true -o out/

# Linux x64
dotnet publish -c Release -r linux-x64 --self-contained true -o out/

# Windows x64
dotnet publish -c Release -r win-x64 --self-contained true -o out/
```

Copy `out/kuberniq` (or `kuberniq.exe` on Windows) to a directory in your `$PATH`.

---

## Authentication

Before running any command you must log in to the MCP server:

```bash
kuberniq login http://kuberniq-server.example.com
```

What this does:
1. Sends `GET /health` to the server to verify it is reachable.
2. Saves `{ "serverUrl": "..." }` to **`~/.kuberniq/config.json`**.

All subsequent commands (`cluster add`, `cluster list`, etc.) call `LoadOrFail()`, which reads `~/.kuberniq/config.json` and throws an error if the file is missing — so always run `login` first.

To remove the saved connection:

```bash
kuberniq logout
```

---

## Config file

`~/.kuberniq/config.json`

```json
{
  "serverUrl": "http://kuberniq-server.example.com",
  "defaultCluster": "prod",
  "clusters": [
    { "name": "prod",    "isLocal": false, "server": "https://prod-api.example.com" },
    { "name": "staging", "isLocal": false, "server": "https://staging-api.example.com" }
  ]
}
```

| Field | Description |
|-------|-------------|
| `serverUrl` | MCP server base URL — set by `kuberniq login` |
| `defaultCluster` | Cluster used when `?cluster=` is omitted — set by `cluster set-default` |
| `clusters` | Local cache of registered clusters — updated by `cluster add` / `cluster remove` |

---

## Commands

### `kuberniq login <server-url>`

Authenticate with the MCP server and save the connection.

```bash
kuberniq login http://kuberniq-server.example.com
```

Saves the server URL to `~/.kuberniq/config.json`. If the server cannot be reached the command exits with code 1.

---

### `kuberniq logout`

Remove the saved MCP server connection.

```bash
kuberniq logout
```

Deletes `~/.kuberniq/config.json`. Subsequent commands will require `login` to be run again.

---

### `kuberniq cluster add <name>`

Register a remote cluster with the MCP server.

```bash
# Use a specific kubeconfig context
kuberniq cluster add prod --context prod-aks

# Pick a context interactively
kuberniq cluster add staging

# Skip ServiceAccount / RBAC creation if it already exists
kuberniq cluster add prod --context prod-aks --skip-rbac
```

#### What it does

1. Resolves the kubeconfig context (interactive selector if `--context` is omitted).
2. Creates a `Namespace`, `ServiceAccount`, `ClusterRole` (read-only) and `ClusterRoleBinding` in the target cluster using the resolved context (unless `--skip-rbac` is set).
3. Retrieves a long-lived bearer token for the ServiceAccount.
4. Calls `POST /clusters` on the MCP server with `{ name, server, caData, token }`.
5. Saves the cluster entry to the local `~/.kuberniq/config.json`.

After registration every MCP server endpoint accepts `?cluster=<name>`.

#### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--context <name>` | interactive | kubeconfig context for the target cluster |
| `--sa-name <name>` | `kuberniq` | ServiceAccount name created in the target cluster |
| `--sa-namespace <ns>` | `kuberniq-server` | Namespace for the ServiceAccount (created if absent) |
| `--skip-rbac` | false | Skip SA and RBAC creation |

---

### `kuberniq cluster list`

List all clusters registered with the MCP server.

```bash
kuberniq cluster list
```

Shows a table with name, type (local in-cluster / remote), server URL and the default cluster marker.

---

### `kuberniq cluster show <name>`

Show detailed information about a registered cluster.

```bash
kuberniq cluster show prod
```

Fetches cluster version, node count, namespace list and reachability status from the MCP server in parallel.

---

### `kuberniq cluster ping <name>`

Check latency and reachability of a registered cluster.

```bash
kuberniq cluster ping prod
```

---

### `kuberniq cluster set-default <name>`

Set the default cluster used when no `?cluster=` flag is supplied.

```bash
kuberniq cluster set-default prod
```

Updates `defaultCluster` in `~/.kuberniq/config.json`.

---

### `kuberniq cluster remove <name>`

Unregister a cluster from the MCP server.

```bash
kuberniq cluster remove staging
```

Calls `DELETE /clusters/<name>` on the MCP server and removes the entry from the local config.

---

## Error handling

All commands surface errors as coloured messages:

| Exit code | Meaning |
|-----------|---------|
| 0 | Success |
| 1 | Error — message printed in red |

If `~/.kuberniq/config.json` is absent when a command that requires authentication is run, you will see:

```
✗ Not logged in. Run kuberniq login <server-url> first.
```

---

## Building a release

```bash
cd kuberniq/kuberniq
dotnet build -c Release
```

The binary appears at `bin/Release/net10.0/kuberniq`.

---

## Roadmap

- [x] Login / logout with config persistence at `~/.kuberniq/config.json`
- [x] Cluster add — automated ServiceAccount + RBAC setup, interactive context selector
- [x] Cluster list / show / ping / set-default / remove
- [ ] Bearer-token forwarding to MCP server when the server has JWT auth enabled
- [ ] `kuberniq status` — quick health summary of all registered clusters
- [ ] Homebrew / Scoop distribution
