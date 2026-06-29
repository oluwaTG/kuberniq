# Contributing to Kuberniq

Thank you for your interest in contributing! This document explains the repository layout, how to set up each component for local development, the branching strategy, commit message conventions, and the release process.

---

## Table of Contents

- [Repository Layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Development Setup](#development-setup)
  - [kuberniq-server (.NET)](#kuberniq-server-net)
  - [kuberniq-chat (Python / Streamlit)](#kuberniq-chat-python--streamlit)
  - [kuberniq CLI (.NET)](#kuberniq-cli-net)
- [Branching Strategy](#branching-strategy)
- [Commit Message Format](#commit-message-format)
- [Version Bumping](#version-bumping)
- [Pull Request Guidelines](#pull-request-guidelines)
- [Release Tags](#release-tags)
- [Code Style](#code-style)

---

## Repository Layout

```
kuberniq/
├── kuberniq-server/          # .NET 10 minimal API — read-only Kubernetes REST backend
├── kuberniq-chat/            # Python / Streamlit — AI chat frontend (GPT-4o + RAG)
├── kuberniq/                 # .NET CLI tool — register clusters, manage server config
├── helm/
│   └── Application/
│       ├── kuberniq-server/  # Helm chart for the MCP server
│       └── kuberniq-chat/    # Helm chart for the chatbot
├── CONTRIBUTING.md           # This file
└── README.md
```

| Component | Language | Description |
|---|---|---|
| `kuberniq-server` | C# / .NET 10 | JWT-authed REST API; exposes 40+ Kubernetes resource endpoints; multi-cluster support |
| `kuberniq-chat` | Python 3.12 / Streamlit | RAG chatbot; local auth + RBAC; JWT session cookies; LLM entity extraction |
| `kuberniq` (CLI) | C# / .NET 10 | `kuberniq` binary — cluster registration, server health, config management |

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| [.NET SDK](https://dotnet.microsoft.com/download) | ≥ 10.0 | Build `kuberniq-server` and the CLI |
| [Python](https://www.python.org/downloads/) | ≥ 3.12 | Run `kuberniq-chat` |
| [Docker](https://www.docker.com/) | any recent | Build and test container images |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | any recent | Point at a local or remote cluster |
| [Helm](https://helm.sh/docs/intro/install/) | ≥ 3.12 | Install / test Helm charts |
| A Kubernetes cluster | — | [kind](https://kind.sigs.k8s.io/) or [minikube](https://minikube.sigs.k8s.io/) work well locally |

---

## Development Setup

### kuberniq-server (.NET)

```bash
cd kuberniq-server
dotnet restore
dotnet run
```

The server reads `~/.kube/config` when running locally and uses in-cluster credentials when deployed.  
Default address: `http://localhost:5165`.

To run against a specific kubeconfig context:

```bash
KUBECONFIG=~/.kube/config dotnet run
```

### kuberniq-chat (Python / Streamlit)

```bash
cd kuberniq-chat

# Create and activate a virtual environment
python -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

Create a `.env` file (never commit this):

```env
OPENAI_API_KEY=sk-...
MCP_SERVER_URL=http://localhost:5165
MCP_USERNAME=admin
MCP_PASSWORD=your-mcp-server-password-here

# Optional — defaults to gpt-4o
OPENAI_MODEL=gpt-4o

# Optional — set true to see raw MCP context in the UI
# DEBUG_MCP=true
```

Run:

```bash
streamlit run app.py
```

Open `http://localhost:8501` (or `8502` if already in use). On first run, `auth.py` creates an admin account with a random password saved to `data/admin-initial-password.txt`.

### kuberniq CLI (.NET)

```bash
cd kuberniq
dotnet restore
dotnet run -- --help
```

Or build a self-contained binary:

```bash
dotnet publish -c Release -r osx-arm64 --self-contained
# binary is at bin/Release/net10.0/osx-arm64/publish/kuberniq
```

---

## Branching Strategy

All work branches off `main`. Open a pull request back into `main` when the feature or fix is ready.

| Branch prefix | Purpose | Example |
|---|---|---|
| `feature/<description>` | New capability or enhancement | `feature/chat-auth` |
| `fix/<description>` | Bug fix | `fix/server-security` |
| `hotfix/<description>` | Critical production patch (direct to main) | `hotfix/token-expiry` |
| `chore/<description>` | Housekeeping with no user-facing change | `chore/update-deps` |
| `docs/<description>` | Documentation only | `docs/add-contributing` |

**Rules:**
- Never commit directly to `main` (except trivial version/docs bumps).
- One concern per branch — avoid mixing unrelated features.
- Keep branches short-lived; merge or rebase frequently.
- Delete branches after merging.

---

## Commit Message Format

This project follows **[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)**.

### Structure

```
<type>(<scope>): <short imperative summary>

[optional body — explain the *why*, not the what]

[optional footer — e.g. BREAKING CHANGE: ..., Closes #123]
```

### Types

| Type | Use when |
|---|---|
| `feat` | Adding a new feature |
| `fix` | Fixing a bug |
| `chore` | Routine tasks: version bumps, dependency updates, CI |
| `docs` | Documentation only — no code change |
| `refactor` | Code restructuring with no behaviour change |
| `perf` | Performance improvement |
| `test` | Adding or fixing tests |
| `style` | Formatting, whitespace — no logic change |
| `revert` | Reverting a previous commit |

### Scopes

Use the component name in parentheses when the change is scoped to one component. Omit scope for changes that touch multiple components.

| Scope | Applies to |
|---|---|
| `chat` | `kuberniq-chat/` |
| `server` | `kuberniq-server/` |
| `cli` | `kuberniq/` |
| `helm` | `helm/` |

### Examples

```
feat(chat): add JWT cookie session persistence — survives pod restarts

fix(server): wire MCP credentials from Secret in Helm deployment

chore: bump versions — chat v1.1.4, server v1.0.11

docs(server): update pod endpoint table for container detail

feat(chat): multi-cluster RAG support via ?cluster= routing

fix(cli): login prompts for credentials, stores JWT, auto-refreshes

refactor(server): extract cluster routing into middleware

feat: session persistence, container detail in pod queries
```

### Summary line rules

- Use the **imperative mood** ("add", "fix", "expose" — not "added" or "fixes")
- Keep it under **72 characters**
- Do **not** end with a period
- Be specific — include the affected endpoint, feature, or file if it helps

### Breaking changes

Append `!` after the scope and add a `BREAKING CHANGE:` footer:

```
feat(server)!: change /pods response shape — containers now from Spec

BREAKING CHANGE: containers[] is now sourced from Spec.Containers instead
of Status.ContainerStatuses. The image field is always present even for
pending containers. Clients reading restartCount must use restarts instead.
```

---

## Version Bumping

Bump versions **in the same commit** as the code change (or a follow-up `chore:` commit). Always bump all four locations for the relevant component.

### kuberniq-chat

| File | Field | Example |
|---|---|---|
| `kuberniq-chat/VERSION` | plain semver | `1.1.4` |
| `helm/Application/kuberniq-chat/Chart.yaml` | `version:` (Helm chart version) | `1.1.1` |
| `helm/Application/kuberniq-chat/Chart.yaml` | `appVersion:` (app image tag) | `"1.1.4"` |

### kuberniq-server

| File | Field | Example |
|---|---|---|
| `kuberniq-server/kuberniq-server.csproj` | `<Version>` | `1.0.11` |
| `helm/Application/kuberniq-server/Chart.yaml` | `version:` | `1.0.2` |
| `helm/Application/kuberniq-server/Chart.yaml` | `appVersion:` | `"1.0.11"` |

### kuberniq CLI

| File | Field | Example |
|---|---|---|
| `kuberniq/kuberniq.csproj` | `<Version>` | `1.0.8` |

### Semver rules

- **Patch** (`x.x.+1`) — bug fixes, documentation, refactoring, dependency bumps
- **Minor** (`x.+1.0`) — new backward-compatible features, new endpoints
- **Major** (`+1.0.0`) — breaking changes to the API, auth, or Helm chart schema

The Helm chart `version` follows its own patch/minor cadence independent of `appVersion`. Bump it whenever the chart templates or values change.

---

## Pull Request Guidelines

1. **Branch from `main`** and keep the branch focused on one change.
2. **Verify locally before opening a PR:**
   - Server: `dotnet build -c Debug --nologo` → 0 errors
   - Chat: `python -c "import py_compile; py_compile.compile('app.py', doraise=True)"`
   - CLI: `dotnet build -c Debug --nologo` → 0 errors
3. **Bump versions** for every component you changed (see [Version Bumping](#version-bumping)).
4. **Update the relevant README** (`kuberniq-chat/README.md`, `kuberniq-server/README.md`) if you add a feature or change behaviour. Update the Roadmap checkboxes.
5. **Write a clear PR title** using the same Conventional Commits format as the commit message.
6. **PR description should cover:**
   - What changed and why
   - How to test it
   - Any migration steps for existing deployments
7. Squash or rebase messy WIP commits before requesting review.

---

## Release Tags

Tags follow the format `<component>/v<semver>` and are applied to the merge commit on `main`.

```bash
# After merging to main:
git tag chat/v1.1.4
git tag server/v1.0.11
git tag cli/v1.0.8

git push origin chat/v1.1.4 server/v1.0.11 cli/v1.0.8
```

CI/CD (if configured) triggers Docker image builds on matching tag pushes:

| Tag pattern | Image pushed |
|---|---|
| `chat/v*` | `elumole22/kuberniq-chat:<version>` |
| `server/v*` | `elumole22/kuberniq-server:<version>` |
| `cli/v*` | GitHub release with binary artifacts |

---

## Code Style

### C# (kuberniq-server, kuberniq CLI)

- Follow standard .NET conventions — PascalCase for types/methods, camelCase for locals.
- Use `var` for local variables when the type is obvious from the right-hand side.
- Prefer LINQ over explicit loops for collection projections.
- Async all the way down — no `.Result` or `.Wait()` except at the top-level startup block.
- Keep endpoint handlers thin — extract helpers for anything more than ~20 lines.
- No `Console.WriteLine` in production paths — use the injected `ILogger`.

### Python (kuberniq-chat)

- Follow [PEP 8](https://peps.python.org/pep-0008/) — 4-space indent, max line length 100.
- Use type annotations on all new functions.
- `from __future__ import annotations` at the top of every module.
- Prefer f-strings over `.format()` or `%`.
- Keep `app.py` functions focused — if a helper grows beyond ~30 lines, extract it.
- Never store plaintext secrets — bcrypt all passwords, never log tokens.
- Add a docstring to every public function in `auth.py`.

### Helm charts

- Keep `values.yaml` fully documented with inline comments.
- Use `_helpers.tpl` for any repeated label/selector logic.
- Template names must use the chart helper prefix (e.g. `{{ include "kuberniq-chat.fullname" . }}`).
- Test with `helm template` before committing chart changes.

---

## Questions?

Open a [GitHub Discussion](https://github.com/oluwaTG/kuberniq/discussions) or file an issue. PRs are always welcome.
