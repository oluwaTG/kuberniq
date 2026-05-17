# GitHub Workflows

All pipelines are triggered automatically on a **push to `main`** based on which folder changed. There are no manual tags or `workflow_dispatch` triggers needed for normal releases.

---

## Workflow Overview

| Workflow | Trigger | What it does |
|---|---|---|
| `kuberniq-server.yml` | Push to `main` touching `kuberniq-server/**` | Bumps version, builds & pushes Docker image, updates Helm chart |
| `kuberniq-chat.yml` | Push to `main` touching `kuberniq-chat/**` | Bumps version, builds & pushes Docker image, updates Helm chart |
| `kuberniq-cli.yml` | Push to `main` touching `kuberniq/**` | Bumps version, builds 4-platform binaries, creates GitHub Release |
| `helm.yml` | Called by server/chat workflows (or manual) | Lints chart, patches `values.yaml` with new image tag, commits |

---

## Release Flow

```
Push to main
  │
  ├── kuberniq-server/** changed
  │     ├── Auto-bump git semver tag  (server-v1.0.7 → server-v1.0.8)
  │     ├── Push elumole22/kuberniq-server:1.0.8
  │     ├── Push elumole22/kuberniq-server:latest
  │     └── Update helm/Application/kuberniq-server/values.yaml → tag: "1.0.8"
  │
  ├── kuberniq-chat/** changed
  │     ├── Auto-bump git semver tag  (chat-v1.0.0 → chat-v1.0.1)
  │     ├── Push elumole22/kuberniq-chat:1.0.1
  │     ├── Push elumole22/kuberniq-chat:latest
  │     └── Update helm/Application/kuberniq-chat/values.yaml → tag: "1.0.1"
  │
  └── kuberniq/** changed
        ├── Auto-bump git semver tag  (cli-v1.0.0 → cli-v1.0.1)
        ├── Build kuberniq-macos-arm64
        ├── Build kuberniq-macos-x64
        ├── Build kuberniq-linux-x64
        ├── Build kuberniq-win-x64.exe
        └── Create GitHub Release with all binaries attached
```

---

## Semver — Conventional Commits

Version bumps are driven entirely by your **commit message prefix**. You do not set versions manually.

| Commit message | Bump type | Example |
|---|---|---|
| `fix: resolve crash on empty namespace` | **patch** | `1.0.7` → `1.0.8` |
| `feat: add multi-cluster support` | **minor** | `1.0.7` → `1.1.0` |
| `feat!: ...` or body contains `BREAKING CHANGE` | **major** | `1.0.7` → `2.0.0` |
| anything else (no prefix) | **patch** (default) | `1.0.7` → `1.0.8` |

### Commit message format

```
<type>(<scope>): <short description>

[optional body]

[optional footer — put BREAKING CHANGE here for a major bump]
```

### Common types

| Type | Use for |
|---|---|
| `feat` | A new feature |
| `fix` | A bug fix |
| `chore` | Build process, dependency updates, no production code change |
| `docs` | Documentation only |
| `refactor` | Code restructure with no behaviour change |
| `perf` | Performance improvement |
| `test` | Adding or updating tests |
| `ci` | Changes to CI/CD workflows |

### Examples

```bash
# Patch bump (1.0.7 → 1.0.8)
git commit -m "fix: handle nil pointer when cluster unreachable"

# Minor bump (1.0.7 → 1.1.0)
git commit -m "feat: add HPA metrics to troubleshoot endpoint"

# Major bump (1.0.7 → 2.0.0)
git commit -m "feat!: redesign cluster registration API

BREAKING CHANGE: kuberniq cluster add now requires --context flag"
```

---

## Git Tag Prefixes

Each component has its own tag namespace so they can be versioned independently.

| Component | Tag prefix | Example tag |
|---|---|---|
| Server | `server-v` | `server-v1.0.8` |
| Chat | `chat-v` | `chat-v1.0.1` |
| CLI | `cli-v` | `cli-v1.1.0` |

---

## Required Secrets

Set these in **GitHub → Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub username (`elumole22`) |
| `DOCKERHUB_TOKEN` | Docker Hub access token (not your password — generate one at hub.docker.com → Account Settings → Security) |

`GITHUB_TOKEN` is provided automatically by GitHub — no setup needed.

---

## Docker Images

Both images are available on Docker Hub with two tag types:

```bash
# Always get the latest build
docker pull elumole22/kuberniq-server:latest
docker pull elumole22/kuberniq-chat:latest

# Pin to a specific version
docker pull elumole22/kuberniq-server:1.0.8
docker pull elumole22/kuberniq-chat:1.0.1
```
