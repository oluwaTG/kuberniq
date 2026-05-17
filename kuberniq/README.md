# kuberniq

A CLI for registering and managing remote Kubernetes clusters with the [MCP server](../mcp-server/README.md).

Modelled after `argocd cluster add` — one command sets up everything in the target cluster and registers it with the MCP server so every endpoint gains `?cluster=<name>` routing.

---

## Installation

### macOS / Linux — one-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/oluwaTG/kuberniq/main/kuberniq/install.sh | bash
```

The script auto-detects your OS and architecture, downloads the right pre-built binary from GitHub Releases, and copies it to `/usr/local/bin`. **No .NET runtime required.**

To install a specific version:

```bash
curl -fsSL https://raw.githubusercontent.com/oluwaTG/kuberniq/main/kuberniq/install.sh | KUBEAI_VERSION=kuberniq/v1.0.0 bash
```

To install to a custom directory (e.g. `~/.local/bin`):

```bash
curl -fsSL https://raw.githubusercontent.com/oluwaTG/kuberniq/main/kuberniq/install.sh | KUBEAI_INSTALL_DIR=~/.local/bin bash
```

---

### Windows

Download `kuberniq-win-x64.exe` from the [latest GitHub Release](https://github.com/oluwaTG/kuberniq/releases), rename it to `kuberniq.exe`, and place it on your `PATH`.

---

### Build from source (developers)

Requires the [.NET 10 SDK](https://dotnet.microsoft.com/download).

```bash
cd kuberniq
make install          # auto-detects OS + arch, builds, copies to /usr/local/bin

# Or for a specific platform:
make macos-arm64      # → dist/osx-arm64/kuberniq
make linux-x64        # → dist/linux-x64/kuberniq
make windows          # → dist/win-x64/kuberniq.exe
make all-platforms    # builds all four at once
```

---

## Usage

### 1. Authenticate

Point `kuberniq` at your running MCP server:

```bash
kuberniq login http://mcp-server.example.com
```

This pings `/health`, then saves the server URL to `~/.kuberniq/config.json`.

---

### 2. Register a cluster

```bash
# Interactive — kuberniq shows a selection menu of your kubeconfig contexts
kuberniq cluster add prod

# Or supply the context directly
kuberniq cluster add prod --context prod-aks
```

What happens under the hood:
1. Connects to the target cluster using the chosen kubeconfig context
2. Creates a `ServiceAccount` named `kuberniq` in `kube-system`
3. Creates a `ClusterRole` (read-only, all resources the MCP server queries) + `ClusterRoleBinding`
4. Creates a `kubernetes.io/service-account-token` Secret for a permanent token
5. Waits for the token controller to issue the token
6. Extracts the server URL + CA certificate from your kubeconfig
7. Calls `POST /clusters` on the MCP server to register everything

After this, every MCP endpoint accepts `?cluster=prod`:

```
GET /namespaces?cluster=prod
GET /namespaces/default/pods?cluster=prod
GET /namespaces/kube-system/pods/coredns-xxx/logs?cluster=prod
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--context` | _interactive_ | Kubeconfig context for the target cluster |
| `--sa-name` | `kuberniq` | ServiceAccount name to create |
| `--sa-namespace` | `kube-system` | Namespace for the ServiceAccount |
| `--skip-rbac` | false | Skip SA/RBAC creation (use if already set up) |

---

### 3. List registered clusters

```bash
kuberniq cluster list
```

```
╭──────────┬────────────────────┬────────────────────────────╮
│ Name     │ Type               │ ?cluster= query param      │
├──────────┼────────────────────┼────────────────────────────┤
│ local    │ local (in-cluster) │ (omit for local)           │
│ prod     │ remote             │ ?cluster=prod              │
│ staging  │ remote             │ ?cluster=staging           │
╰──────────┴────────────────────┴────────────────────────────╯
```

---

### 4. Remove a cluster

```bash
kuberniq cluster remove prod
```

This calls `DELETE /clusters/prod` on the MCP server and removes the persisted Secret.  
The `ServiceAccount` and `ClusterRole` in the target cluster are **not** deleted — remove them manually if no longer needed:

```bash
kubectl --context prod-aks delete clusterrolebinding kuberniq-mcp-reader
kubectl --context prod-aks delete clusterrole       kuberniq-mcp-reader
kubectl --context prod-aks delete sa kuberniq -n kube-system
kubectl --context prod-aks delete secret kuberniq-kuberniq-token -n kube-system
```

---

### 5. Log out

```bash
kuberniq logout
```

Removes `~/.kuberniq/config.json`.

---

## How cluster credentials are persisted

Registered clusters are stored as Kubernetes Secrets in the MCP server's own namespace, labelled `mcp.io/cluster-type=remote`. The MCP server reads these at startup so clusters survive pod restarts — no re-registration needed after a rollout.

```
Secret: mcp-cluster-prod
  mcp.io/cluster-type: remote
  mcp.io/cluster-name: prod
Data:
  server: https://prod-api.example.com
  caData: <base64 CA cert>
  token:  <ServiceAccount token>
```

---

## Publishing a new release

```bash
# Bump the version in kuberniq.csproj, then:
git add kuberniq/
git commit -m "chore: release kuberniq v1.1.0"
git tag kuberniq/v1.1.0
git push origin main --tags
```

The GitHub Actions workflow (`.github/workflows/kuberniq-release.yml`) picks up the tag, builds binaries for all five platforms in parallel (macOS ARM64, macOS x64, Linux x64, Linux ARM64, Windows x64), and publishes them as a GitHub Release. Users then get the new version via the one-liner install script.
