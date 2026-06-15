"""
Local authentication and RBAC for Kuberniq Chat.

Users are stored as bcrypt-hashed records in a JSON file at DATA_DIR/users.json.

Bootstrap: on first run with no users an 'admin' account is created with a random
24-char alphanumeric password. The password is:
  • printed to stdout / container logs
  • written to DATA_DIR/admin-initial-password.txt

Retrieve it from a running container with:
  docker exec <container> cat /data/admin-initial-password.txt
  # or in k8s:
  kubectl exec -n kuberniq-chat <pod> -- cat /data/admin-initial-password.txt

RBAC permission matrix
────────────────────────────────────────────────────────────────────────────────
 Permission              │ admin │ operator │ viewer
─────────────────────────┼───────┼──────────┼──────────────────────────────────
 Namespaces queried      │  all  │   all    │ assigned only (empty = all)
 Pods / phase / restarts │   ✓   │    ✓     │ ✓
 Events (Warning/Normal) │   ✓   │    ✓     │ ✓
 Deployments / replicas  │   ✓   │    ✓     │ ✓
 StatefulSets / DS / Jobs│   ✓   │    ✓     │ ✓
 Services / Ingresses    │   ✓   │    ✓     │ ✓
 Logs                    │   ✓   │    ✓     │ ✗
 ConfigMaps              │   ✓   │    ✓     │ ✗
 Nodes / Metrics / HPA   │   ✓   │    ✓     │ ✗
 Storage / PV / PVC      │   ✓   │    ✓     │ ✗
 Troubleshoot            │   ✓   │    ✓     │ ✗
 Secrets (key names)     │   ✓   │    ✗     │ ✗
 RBAC / ClusterRoles     │   ✓   │    ✗     │ ✗
 User management         │   ✓   │    ✗     │ ✗
────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
import secrets
import string
import time
from pathlib import Path
from typing import Optional

import bcrypt

# ── Storage paths ─────────────────────────────────────────────────────────────
DATA_DIR              = Path(os.getenv("KUBERNIQ_DATA_DIR", "./data"))
USERS_FILE            = DATA_DIR / "users.json"
INITIAL_PASSWORD_FILE = DATA_DIR / "admin-initial-password.txt"

# ── Role definitions ──────────────────────────────────────────────────────────
VALID_ROLES = {"admin", "operator", "viewer"}

# Maps role → set of allowed intent keys produced by classify_intent().
# None means unrestricted (all intents pass through).
ROLE_ALLOWED_INTENTS: dict[str, Optional[set[str]]] = {
    "admin": None,
    "operator": {
        "cluster", "namespaces",
        "pods", "logs", "events",
        "deployments", "replicasets", "statefulsets", "daemonsets",
        "jobs", "cronjobs",
        "services", "ingresses", "networkpolicies",
        "configmaps",
        "nodes", "node_metrics", "pod_metrics",
        "hpa", "resourcequotas", "limitranges",
        "storageclasses", "volumes",
        "serviceaccounts",
        "troubleshoot",
    },
    "viewer": {
        "cluster", "namespaces",
        "pods", "events",
        "deployments", "statefulsets", "daemonsets",
        "jobs", "cronjobs",
        "services", "ingresses",
    },
}

# Human-readable description of what each role can do (shown in UI)
ROLE_DESCRIPTIONS = {
    "admin":    "Full access — cluster data, logs, secrets, RBAC, user management",
    "operator": "Broad access — pods, logs, events, deployments, metrics; no secrets or RBAC",
    "viewer":   "Read-only — pods, events, deployments, services in assigned namespaces only",
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_users() -> dict:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text())
    except Exception:
        return {}


def _save_users(users: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2))


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def bootstrap_admin() -> Optional[str]:
    """
    If no users exist, create an 'admin' account with a random 24-char password.
    Returns the plaintext password if bootstrapped, None if users already exist.
    Safe to call on every app startup — returns None immediately once a user exists.
    """
    if _load_users():
        return None

    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(24))

    users: dict = {}
    users["admin"] = {
        "username": "admin",
        "hash":     bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode(),
        "role":     "admin",
        "allowed_namespaces": [],   # empty = all namespaces for admin
        "created_at": time.time(),
    }
    _save_users(users)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INITIAL_PASSWORD_FILE.write_text(password)

    banner = (
        f"\n{'='*62}\n"
        f"  Kuberniq Chat  —  Initial Admin Credentials\n"
        f"  Username : admin\n"
        f"  Password : {password}\n"
        f"  Saved to : {INITIAL_PASSWORD_FILE.resolve()}\n"
        f"  ⚠  Change your password after first login!\n"
        f"{'='*62}\n"
    )
    print(banner, flush=True)
    return password


# ── Authentication ────────────────────────────────────────────────────────────

def validate_credentials(username: str, password: str) -> tuple[bool, dict]:
    """
    Validate username/password.
    Returns (True, user_record) on success or (False, {}) on failure.
    Always takes a constant-time code path to resist timing attacks.
    """
    users = _load_users()
    user  = users.get(username.lower())

    if not user:
        # Dummy verify to keep timing consistent
        bcrypt.checkpw(b"dummy", b"$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        return False, {}

    try:
        if bcrypt.checkpw(password.encode(), user["hash"].encode()):
            return True, dict(user)
    except Exception:
        pass

    return False, {}


# ── User management (admin only) ──────────────────────────────────────────────

def create_user(
    admin: dict,
    username: str,
    password: str,
    role: str = "viewer",
    allowed_namespaces: list[str] | None = None,
) -> tuple[bool, str]:
    if admin.get("role") != "admin":
        return False, "Only admins can create users."
    username = username.strip().lower()
    if len(username) < 3:
        return False, "Username must be at least 3 characters."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if role not in VALID_ROLES:
        return False, f"Role must be one of: {', '.join(sorted(VALID_ROLES))}."

    users = _load_users()
    if username in users:
        return False, f"User '{username}' already exists."

    users[username] = {
        "username": username,
        "hash":     bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode(),
        "role":     role,
        "allowed_namespaces": allowed_namespaces or [],
        "created_at": time.time(),
    }
    _save_users(users)
    return True, ""


def delete_user(admin: dict, username: str) -> tuple[bool, str]:
    if admin.get("role") != "admin":
        return False, "Only admins can delete users."
    username = username.strip().lower()
    if username == "admin":
        return False, "The admin account cannot be deleted."

    users = _load_users()
    if username not in users:
        return False, f"User '{username}' not found."

    del users[username]
    _save_users(users)
    return True, ""


def update_user_namespaces(
    admin: dict, username: str, allowed_namespaces: list[str]
) -> tuple[bool, str]:
    if admin.get("role") != "admin":
        return False, "Only admins can update user permissions."
    username = username.strip().lower()

    users = _load_users()
    if username not in users:
        return False, f"User '{username}' not found."

    users[username]["allowed_namespaces"] = allowed_namespaces
    _save_users(users)
    return True, ""


def change_password(
    user: dict, current_password: str, new_password: str
) -> tuple[bool, str]:
    ok, _ = validate_credentials(user["username"], current_password)
    if not ok:
        return False, "Current password is incorrect."
    if len(new_password) < 8:
        return False, "New password must be at least 8 characters."

    users = _load_users()
    uname = user["username"].lower()
    if uname not in users:
        return False, "User not found."

    users[uname]["hash"] = bcrypt.hashpw(
        new_password.encode(), bcrypt.gensalt(rounds=12)
    ).decode()
    _save_users(users)
    return True, ""


def list_users(admin: dict) -> tuple[bool, list[dict]]:
    if admin.get("role") != "admin":
        return False, []

    return True, [
        {
            "username":          u["username"],
            "role":              u["role"],
            "allowed_namespaces": u.get("allowed_namespaces", []),
        }
        for u in _load_users().values()
    ]


# ── RBAC helpers used by app.py ───────────────────────────────────────────────

def filter_intents(intents: list[str], role: str) -> list[str]:
    """
    Strip intents the user's role is not permitted to act on.
    Called by fetch_mcp_context() before any MCP endpoint is hit.
    """
    allowed = ROLE_ALLOWED_INTENTS.get(role)
    if allowed is None:          # admin — no restriction
        return intents
    return [i for i in intents if i in allowed]


def filter_namespaces(all_namespaces: list[str], user: dict) -> list[str]:
    """
    Return the namespaces this user may query.
      • admin / operator  → all namespaces
      • viewer            → user['allowed_namespaces'] (empty list = all, permissive default)
    """
    role = user.get("role", "viewer")
    if role in ("admin", "operator"):
        return all_namespaces
    assigned = user.get("allowed_namespaces", [])
    if not assigned:
        return all_namespaces   # viewer with no restriction = all (permissive default)
    return [ns for ns in all_namespaces if ns in assigned]


def permission_denied_note(intent: str, role: str) -> str:
    """
    Return a human-readable note explaining why an intent was blocked.
    Injected into the RAG context so the LLM can explain the denial.
    """
    return (
        f"[PERMISSION_DENIED] Your role ('{role}') does not allow access to '{intent}' data. "
        "Please ask an admin to upgrade your permissions if you need this."
    )
