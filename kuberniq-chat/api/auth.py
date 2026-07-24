"""
Self-contained auth for Kuberniq Chat.

Mirrors the kuberniq-server Auth.cs pattern exactly but is fully independent:

  - Users stored as Kubernetes Secrets (labeled kuberniq.io/type=user)
  - JWT signing key in Secret  kuberniq-chat-jwt-signing-key
  - Refresh tokens stored as Secrets (labeled kuberniq.io/type=refresh-token)
  - Bootstrap admin → kuberniq-chat-admin-initial-password (ArgoCD-style)
  - Token shape:  { accessToken, refreshToken, expiresIn }
  - JWT:  issuer=kuberniq-chat, audience=kuberniq-chat-ui, HS256, 1 hr / 30 d

The chat UI has its own user store, completely decoupled from the MCP server.
Users can be granted chat access without needing an MCP account.
MCP is accessed via a single service account (MCP_USERNAME / MCP_PASSWORD).

DEV_MODE=true  (no K8s available):
  Falls back to a local JSON file store with the same bcrypt + JWT shape.
  No cluster needed — for local development and CI.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import string
import time
from base64 import b64decode, b64encode
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import bcrypt
import jwt as _jwt
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────

DEV_MODE   = os.getenv("DEV_MODE",   "false").lower() == "true"
DATA_DIR   = Path(os.getenv("KUBERNIQ_DATA_DIR", "./data"))  # dev fallback only

def _detect_namespace() -> str:
    """
    Return the namespace to use for K8s Secrets.
    Priority:
      1. KUBERNIQ_NAMESPACE env var (explicit override)
      2. In-cluster: /var/run/secrets/kubernetes.io/serviceaccount/namespace
      3. Fallback: "kuberniq"
    """
    explicit = os.getenv("KUBERNIQ_NAMESPACE", "").strip()
    if explicit:
        return explicit
    ns_file = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if ns_file.exists():
        ns = ns_file.read_text().strip()
        if ns:
            return ns
    return "kuberniq"

K8S_NS = _detect_namespace()


def _effective_dev_mode() -> bool:
    """Return True if running in dev mode OR if we can't reach K8s."""
    if DEV_MODE:
        return True
    try:
        _k8s_api()
        return False
    except Exception:
        return True

JWT_ISSUER    = "kuberniq-chat"
JWT_AUDIENCE  = "kuberniq-chat-ui"
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 60
REFRESH_TOKEN_DAYS   = 30

# Secret name constants — same naming convention as Auth.cs
_SIGNING_SECRET   = "kuberniq-chat-jwt-signing-key"
_BOOTSTRAP_SECRET = "kuberniq-chat-admin-initial-password"
_USER_LABEL       = "kuberniq.io/type=user"
_REFRESH_LABEL    = "kuberniq.io/type=refresh-token"

# ── RBAC ──────────────────────────────────────────────────────────────────────

ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin":    "Full access — cluster data, logs, secrets, RBAC, user management",
    "operator": "Broad access — pods, logs, events, deployments, metrics; no secrets or RBAC",
    "viewer":   "Read-only — pods, events, deployments, services in assigned namespaces only",
}

ROLE_ALLOWED_INTENTS: dict[str, Optional[set[str]]] = {
    "admin":    None,
    "operator": {
        "cluster", "registered_clusters", "namespaces", "pods", "logs", "events",
        "deployments", "replicasets", "statefulsets", "daemonsets", "jobs", "cronjobs",
        "services", "ingresses", "networkpolicies", "hpa", "nodes", "node_metrics",
        "pod_metrics", "resourcequotas", "limitranges", "storageclasses", "volumes",
        "serviceaccounts", "troubleshoot",
    },
    "viewer": {
        "cluster", "registered_clusters", "namespaces", "pods", "events",
        "deployments", "statefulsets", "daemonsets", "jobs", "cronjobs",
        "services", "ingresses",
    },
}

# ── Kubernetes helpers ────────────────────────────────────────────────────────

def _k8s_api():
    """Return a CoreV1Api client, preferring in-cluster config."""
    from kubernetes import client as _kc, config as _kcfg
    try:
        _kcfg.load_incluster_config()
    except Exception:
        _kcfg.load_kube_config()
    return _kc.CoreV1Api()


async def _k8s(fn, *args, **kwargs):
    """Run a synchronous kubernetes-client call in a thread pool."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _user_secret_name(username: str) -> str:
    return f"kuberniq-chat-user-{username.lower().replace(' ', '-')}"


def _secret_data(secret) -> dict[str, str]:
    """Decode a V1Secret's .data (base64 bytes) into a plain str→str dict."""
    if not secret.data:
        return {}
    return {k: b64decode(v).decode() for k, v in secret.data.items()}


# ── JWT signing key ───────────────────────────────────────────────────────────

_cached_signing_key: str | None = None


async def _get_signing_key() -> str:
    global _cached_signing_key
    if _cached_signing_key:
        return _cached_signing_key

    if _effective_dev_mode():
        # Dev: persist to a local file so the key survives server restarts
        key_file = DATA_DIR / "chat_jwt_secret.txt"
        if key_file.exists():
            _cached_signing_key = key_file.read_text().strip()
        else:
            _cached_signing_key = secrets.token_urlsafe(48)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            key_file.write_text(_cached_signing_key)
        return _cached_signing_key

    # Production: K8s Secret  kuberniq-chat-jwt-signing-key
    from kubernetes import client as _kc
    api = _k8s_api()
    try:
        secret = await _k8s(api.read_namespaced_secret, _SIGNING_SECRET, K8S_NS)
        _cached_signing_key = _secret_data(secret)["key"]
    except _kc.exceptions.ApiException as e:
        if e.status == 404:
            # First run — generate and persist
            key = secrets.token_urlsafe(48)
            body = _kc.V1Secret(
                metadata=_kc.V1ObjectMeta(
                    name=_SIGNING_SECRET,
                    namespace=K8S_NS,
                    labels={"kuberniq.io/type": "signing-key"},
                ),
                string_data={"key": key},
            )
            await _k8s(api.create_namespaced_secret, K8S_NS, body)
            _cached_signing_key = key
        else:
            raise
    return _cached_signing_key


# ── Token issuance ────────────────────────────────────────────────────────────

async def _issue_tokens(username: str, role: str, allowed_namespaces: list[str] | None = None) -> dict:
    """Issue { accessToken, refreshToken, expiresIn } — same shape as Auth.cs TokenResponse."""
    key = await _get_signing_key()
    now = datetime.now(tz=timezone.utc)

    access_payload = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": username,
        "unique_name": username,
        "role": role,
        "allowed_namespaces": allowed_namespaces or [],
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    access_token = _jwt.encode(access_payload, key, algorithm=JWT_ALGORITHM)

    # Refresh token: opaque random bytes in prod (stored as K8s Secret),
    # signed JWT in dev (stateless — no K8s needed).
    if _effective_dev_mode():
        refresh_payload = {
            "iss": JWT_ISSUER,
            "sub": username,
            "role": role,
            "allowed_namespaces": allowed_namespaces or [],
            "type": "refresh",
            "iat": now,
            "exp": now + timedelta(days=REFRESH_TOKEN_DAYS),
        }
        refresh_token = _jwt.encode(refresh_payload, key, algorithm=JWT_ALGORITHM)
    else:
        refresh_token = await _create_k8s_refresh_token(username, role)

    return {
        "accessToken":  access_token,
        "refreshToken": refresh_token,
        "expiresIn":    ACCESS_TOKEN_MINUTES * 60,
    }


async def _create_k8s_refresh_token(username: str, role: str) -> str:
    """Store a refresh token as a K8s Secret (same pattern as Auth.cs CreateRefreshTokenAsync)."""
    from kubernetes import client as _kc
    api = _k8s_api()
    raw       = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).digest()
    token_hash_b64 = b64encode(token_hash).decode()
    expires_at = (datetime.now(tz=timezone.utc) + timedelta(days=REFRESH_TOKEN_DAYS)).isoformat()
    secret_name = f"kuberniq-chat-rt-{secrets.token_hex(8)}"

    body = _kc.V1Secret(
        metadata=_kc.V1ObjectMeta(
            name=secret_name,
            namespace=K8S_NS,
            labels={
                "kuberniq.io/type":     "refresh-token",
                "kuberniq.io/username": username[:63],
            },
        ),
        string_data={
            "tokenHash": token_hash_b64,
            "username":  username,
            "role":      role,
            "expiresAt": expires_at,
        },
    )
    await _k8s(api.create_namespaced_secret, K8S_NS, body)
    return raw


# ── Bootstrap ─────────────────────────────────────────────────────────────────

async def bootstrap_admin() -> Optional[str]:
    """
    ArgoCD-style first-run bootstrap.
    If no users exist, create admin with a random password stored as a K8s Secret.
    Retrieve with:
      kubectl get secret kuberniq-chat-admin-initial-password -n <ns> \\
        -o jsonpath='{.data.password}' | base64 -d
    Returns the plaintext password if bootstrapped, None if admin already exists.
    """
    if _effective_dev_mode():
        return await _dev_bootstrap_admin()

    from kubernetes import client as _kc
    api = _k8s_api()
    # Check if any users exist
    try:
        existing = await _k8s(
            api.list_namespaced_secret, K8S_NS,
            label_selector=_USER_LABEL,
        )
        if existing.items:
            return None
    except _kc.exceptions.ApiException as e:
        if e.status == 404:
            raise RuntimeError(
                f"[Auth] Namespace '{K8S_NS}' does not exist. "
                f"Create it first:  kubectl create namespace {K8S_NS}"
            ) from e
        raise

    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(24))

    # Store the recoverable password Secret
    try:
        pw_secret = _kc.V1Secret(
            metadata=_kc.V1ObjectMeta(
                name=_BOOTSTRAP_SECRET,
                namespace=K8S_NS,
                labels={"kuberniq.io/type": "initial-admin-password"},
                annotations={"kuberniq.io/note": "Delete after changing the admin password."},
            ),
            string_data={"password": password},
        )
        await _k8s(api.create_namespaced_secret, K8S_NS, pw_secret)
    except _kc.exceptions.ApiException as e:
        if e.status == 409:
            # Secret already exists from a prior partial bootstrap — re-read it
            s = await _k8s(api.read_namespaced_secret, _BOOTSTRAP_SECRET, K8S_NS)
            password = _secret_data(s)["password"]

    ok, err = await create_user("admin", password, role="admin", _bypass_rbac=True)
    if ok:
        print(f"[Auth] Bootstrap complete. Retrieve admin password with:\n"
              f"  kubectl get secret {_BOOTSTRAP_SECRET} -n {K8S_NS} "
              f"-o jsonpath='{{.data.password}}' | base64 -d")
    return password if ok else None


# ── Credential validation ─────────────────────────────────────────────────────

async def validate_credentials(username: str, password: str) -> tuple[bool, dict]:
    if _effective_dev_mode():
        return _dev_validate_credentials(username, password)

    import json as _json
    from kubernetes import client as _kc
    api = _k8s_api()
    try:
        secret = await _k8s(api.read_namespaced_secret, _user_secret_name(username), K8S_NS)
        data = _secret_data(secret)
        if not bcrypt.checkpw(password.encode(), data["hash"].encode()):
            return False, {}
        raw_ns = data.get("allowed_namespaces", "[]")
        try:
            allowed_ns = _json.loads(raw_ns)
        except Exception:
            allowed_ns = []
        return True, {"username": username, "role": data.get("role", "viewer"), "allowed_namespaces": allowed_ns}
    except _kc.exceptions.ApiException as e:
        if e.status == 404:
            # Constant-time dummy check to prevent user enumeration
            bcrypt.checkpw(b"dummy", b"$2b$12$" + b"a" * 53)
        return False, {}


# ── Login / refresh / logout ──────────────────────────────────────────────────

async def login(username: str, password: str) -> tuple[bool, dict, str]:
    ok, user = await validate_credentials(username, password)
    if not ok:
        return False, {}, "Invalid username or password."
    tokens = await _issue_tokens(username, user["role"], user.get("allowed_namespaces", []))
    return True, tokens, ""


async def refresh(refresh_token: str) -> tuple[bool, dict, str]:
    if _effective_dev_mode():
        return await _dev_refresh(refresh_token)

    from kubernetes import client as _kc
    api = _k8s_api()
    token_hash = b64encode(hashlib.sha256(refresh_token.encode()).digest()).decode()
    try:
        all_rt = await _k8s(
            api.list_namespaced_secret, K8S_NS,
            label_selector=_REFRESH_LABEL,
        )
    except Exception:
        return False, {}, "Could not validate refresh token."

    found = None
    for s in all_rt.items:
        data = _secret_data(s)
        if data.get("tokenHash") == token_hash:
            found = (s, data)
            break

    if not found:
        return False, {}, "Invalid or expired refresh token. Please log in again."

    secret_obj, data = found
    expires_at = datetime.fromisoformat(data["expiresAt"])
    if datetime.now(tz=timezone.utc) > expires_at:
        await _k8s(api.delete_namespaced_secret, secret_obj.metadata.name, K8S_NS)
        return False, {}, "Refresh token has expired. Please log in again."

    # Rotate — delete old, issue new
    await _k8s(api.delete_namespaced_secret, secret_obj.metadata.name, K8S_NS)
    import json as _json
    raw_ns = data.get("allowed_namespaces", "[]")
    try:
        allowed_ns = _json.loads(raw_ns) if isinstance(raw_ns, str) else (raw_ns or [])
    except Exception:
        allowed_ns = []
    tokens = await _issue_tokens(data["username"], data.get("role", "viewer"), allowed_ns)
    return True, tokens, ""


async def logout(refresh_token: str) -> None:
    if _effective_dev_mode():
        return  # dev tokens are stateless

    from kubernetes import client as _kc
    api = _k8s_api()
    token_hash = b64encode(hashlib.sha256(refresh_token.encode()).digest()).decode()
    try:
        all_rt = await _k8s(
            api.list_namespaced_secret, K8S_NS,
            label_selector=_REFRESH_LABEL,
        )
        for s in all_rt.items:
            if _secret_data(s).get("tokenHash") == token_hash:
                await _k8s(api.delete_namespaced_secret, s.metadata.name, K8S_NS)
                return
    except Exception:
        pass


# ── User management ───────────────────────────────────────────────────────────

async def create_user(
    username: str, password: str,
    role: str = "viewer",
    allowed_namespaces: list[str] | None = None,
    _bypass_rbac: bool = False,
) -> tuple[bool, str]:
    if not _bypass_rbac:
        pass  # caller must be admin — enforced at the FastAPI layer
    username = username.strip().lower()
    if len(username) < 3:
        return False, "Username must be at least 3 characters."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if role not in ("admin", "operator", "viewer"):
        return False, "Role must be 'admin', 'operator', or 'viewer'."

    if _effective_dev_mode():
        return _dev_create_user(username, password, role, allowed_namespaces or [])

    import json as _json
    from kubernetes import client as _kc
    api = _k8s_api()
    secret_name = _user_secret_name(username)
    try:
        await _k8s(api.read_namespaced_secret, secret_name, K8S_NS)
        return False, f"User '{username}' already exists."
    except _kc.exceptions.ApiException as e:
        if e.status != 404:
            raise

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    body = _kc.V1Secret(
        metadata=_kc.V1ObjectMeta(
            name=secret_name,
            namespace=K8S_NS,
            labels={
                "kuberniq.io/type":     "user",
                "kuberniq.io/username": username,
                "kuberniq.io/role":     role,
            },
        ),
        string_data={
            "username":          username,
            "hash":              pw_hash,
            "role":              role,
            "allowed_namespaces": _json.dumps(allowed_namespaces or []),
        },
    )
    await _k8s(api.create_namespaced_secret, K8S_NS, body)
    return True, ""


async def delete_user(username: str) -> tuple[bool, str]:
    username = username.strip().lower()
    if username == "admin":
        return False, "The admin account cannot be deleted."

    if _effective_dev_mode():
        return _dev_delete_user(username)

    from kubernetes import client as _kc
    api = _k8s_api()
    try:
        await _k8s(api.delete_namespaced_secret, _user_secret_name(username), K8S_NS)
        # Clean up refresh tokens for this user
        all_rt = await _k8s(
            api.list_namespaced_secret, K8S_NS,
            label_selector=f"kuberniq.io/type=refresh-token,kuberniq.io/username={username}",
        )
        for s in all_rt.items:
            try:
                await _k8s(api.delete_namespaced_secret, s.metadata.name, K8S_NS)
            except Exception:
                pass
        return True, ""
    except _kc.exceptions.ApiException as e:
        if e.status == 404:
            return False, f"User '{username}' not found."
        raise


async def update_user(
    username: str,
    role: str | None = None,
    allowed_namespaces: list[str] | None = None,
) -> tuple[bool, str]:
    """Update a user's role and/or allowed_namespaces."""
    username = username.strip().lower()
    if role is not None and role not in ("admin", "operator", "viewer"):
        return False, "Role must be 'admin', 'operator', or 'viewer'."

    if _effective_dev_mode():
        return _dev_update_user(username, role, allowed_namespaces)

    import json as _json
    from kubernetes import client as _kc
    api = _k8s_api()
    secret_name = _user_secret_name(username)
    try:
        secret = await _k8s(api.read_namespaced_secret, secret_name, K8S_NS)
    except _kc.exceptions.ApiException as e:
        if e.status == 404:
            return False, f"User '{username}' not found."
        raise

    data = _secret_data(secret)
    new_role = role if role is not None else data.get("role", "viewer")
    raw_ns = data.get("allowed_namespaces", "[]")
    try:
        existing_ns = _json.loads(raw_ns)
    except Exception:
        existing_ns = []
    new_ns = allowed_namespaces if allowed_namespaces is not None else existing_ns

    patch_body = _kc.V1Secret(
        metadata=_kc.V1ObjectMeta(
            labels={
                "kuberniq.io/type":     "user",
                "kuberniq.io/username": username,
                "kuberniq.io/role":     new_role,
            }
        ),
        string_data={
            "username":          username,
            "hash":              data.get("hash", ""),
            "role":              new_role,
            "allowed_namespaces": _json.dumps(new_ns),
        },
    )
    await _k8s(api.patch_namespaced_secret, secret_name, K8S_NS, patch_body)
    return True, ""
    try:
        await _k8s(api.delete_namespaced_secret, _user_secret_name(username), K8S_NS)
        # Clean up refresh tokens for this user
        all_rt = await _k8s(
            api.list_namespaced_secret, K8S_NS,
            label_selector=f"kuberniq.io/type=refresh-token,kuberniq.io/username={username}",
        )
        for s in all_rt.items:
            try:
                await _k8s(api.delete_namespaced_secret, s.metadata.name, K8S_NS)
            except Exception:
                pass
        return True, ""
    except _kc.exceptions.ApiException as e:
        if e.status == 404:
            return False, f"User '{username}' not found."
        raise


async def list_users() -> list[dict]:
    if _effective_dev_mode():
        return _dev_list_users()

    api = _k8s_api()
    try:
        result = await _k8s(
            api.list_namespaced_secret, K8S_NS,
            label_selector=_USER_LABEL,
        )
        users = []
        for s in result.items:
            data = _secret_data(s)
            raw_ns = data.get("allowed_namespaces", "")
            try:
                import json as _json
                allowed_ns = _json.loads(raw_ns) if raw_ns else []
            except Exception:
                allowed_ns = [x.strip() for x in raw_ns.split(",") if x.strip()]
            users.append({
                "username":          s.metadata.labels.get("kuberniq.io/username", "?"),
                "role":              s.metadata.labels.get("kuberniq.io/role",     "viewer"),
                "allowed_namespaces": allowed_ns,
            })
        return users
    except Exception:
        return []


async def change_password(
    username: str, current_password: str, new_password: str
) -> tuple[bool, str]:
    ok, _ = await validate_credentials(username, current_password)
    if not ok:
        return False, "Current password is incorrect."
    if len(new_password) < 8:
        return False, "New password must be at least 8 characters."

    if _effective_dev_mode():
        return _dev_change_password(username, new_password)

    from kubernetes import client as _kc
    api = _k8s_api()
    secret_name = _user_secret_name(username)
    secret = await _k8s(api.read_namespaced_secret, secret_name, K8S_NS)
    data   = _secret_data(secret)
    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    secret.string_data = {"username": username, "hash": new_hash, "role": data.get("role", "viewer")}
    secret.data = None
    await _k8s(api.replace_namespaced_secret, secret_name, K8S_NS, secret)
    return True, ""


# ── JWT validation ────────────────────────────────────────────────────────────

def validate_access_token(token: str) -> Optional[dict]:
    """
    Validate a JWT access token.  Returns { username, role } or None.
    Sync — uses a cached signing key so no I/O on the hot path.
    """
    if not _cached_signing_key:
        # Key not loaded yet — decode without verification as a last resort
        try:
            payload = _jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": True},
                algorithms=[JWT_ALGORITHM],
            )
            return _extract_claims(payload)
        except Exception:
            return None
    try:
        payload = _jwt.decode(
            token, _cached_signing_key,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            leeway=30,
        )
        return _extract_claims(payload)
    except Exception:
        return None


def _extract_claims(payload: dict) -> dict:
    username = payload.get("unique_name") or payload.get("sub") or ""
    role = payload.get("role", "viewer")
    if isinstance(role, list):
        role = role[0] if role else "viewer"
    allowed_namespaces = payload.get("allowed_namespaces", [])
    if not isinstance(allowed_namespaces, list):
        allowed_namespaces = []
    return {"username": username, "role": role, "allowed_namespaces": allowed_namespaces}


# ── Dev-mode local file store ─────────────────────────────────────────────────
# Same bcrypt + JSON shape as the original Streamlit auth — no K8s needed.

def _dev_users_path() -> Path:
    return DATA_DIR / "users.json"


def _dev_load() -> dict:
    p = _dev_users_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _dev_save(users: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _dev_users_path().write_text(json.dumps(users, indent=2))


async def _dev_bootstrap_admin() -> Optional[str]:
    users = _dev_load()
    if users:
        return None
    alphabet = string.ascii_letters + string.digits
    password = os.getenv("DEV_ADMIN_PASSWORD") or "".join(secrets.choice(alphabet) for _ in range(16))
    pw_hash  = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    _dev_save({"admin": {"username": "admin", "hash": pw_hash, "role": "admin", "created_at": time.time()}})
    pw_file = DATA_DIR / "chat-admin-initial-password.txt"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pw_file.write_text(password)
    print(f"[Auth] Dev bootstrap — admin password: {password}  (saved to {pw_file})")
    return password


def _dev_validate_credentials(username: str, password: str) -> tuple[bool, dict]:
    users = _dev_load()
    user  = users.get(username.lower())
    if not user:
        bcrypt.checkpw(b"dummy", b"$2b$12$" + b"a" * 53)
        return False, {}
    if not bcrypt.checkpw(password.encode(), user["hash"].encode()):
        return False, {}
    return True, {
        "username":          username.lower(),
        "role":              user.get("role", "viewer"),
        "allowed_namespaces": user.get("allowed_namespaces", []),
    }


async def _dev_refresh(refresh_token: str) -> tuple[bool, dict, str]:
    key = await _get_signing_key()
    try:
        payload = _jwt.decode(
            refresh_token, key, algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER, options={"verify_aud": False},
        )
        if payload.get("type") != "refresh":
            return False, {}, "Invalid refresh token."
        return True, await _issue_tokens(payload["sub"], payload.get("role", "viewer")), ""
    except Exception:
        return False, {}, "Refresh token is invalid or expired."


def _dev_create_user(username: str, password: str, role: str, allowed_namespaces: list[str] | None = None) -> tuple[bool, str]:
    users = _dev_load()
    if username in users:
        return False, f"User '{username}' already exists."
    users[username] = {
        "username": username,
        "hash": bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode(),
        "role": role,
        "allowed_namespaces": allowed_namespaces or [],
        "created_at": time.time(),
    }
    _dev_save(users)
    return True, ""


def _dev_delete_user(username: str) -> tuple[bool, str]:
    users = _dev_load()
    if username not in users:
        return False, f"User '{username}' not found."
    del users[username]
    _dev_save(users)
    return True, ""


def _dev_update_user(
    username: str,
    role: str | None = None,
    allowed_namespaces: list[str] | None = None,
) -> tuple[bool, str]:
    users = _dev_load()
    if username not in users:
        return False, f"User '{username}' not found."
    if role is not None:
        users[username]["role"] = role
    if allowed_namespaces is not None:
        users[username]["allowed_namespaces"] = allowed_namespaces
    _dev_save(users)
    return True, ""


def _dev_list_users() -> list[dict]:
    return [
        {
            "username":          u["username"],
            "role":              u.get("role", "viewer"),
            "allowed_namespaces": u.get("allowed_namespaces", []),
        }
        for u in _dev_load().values()
    ]


def _dev_change_password(username: str, new_password: str) -> tuple[bool, str]:
    users = _dev_load()
    if username not in users:
        return False, "User not found."
    users[username]["hash"] = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(rounds=12)).decode()
    _dev_save(users)
    return True, ""


# ── RBAC helpers ──────────────────────────────────────────────────────────────

def filter_intents(intents: list[str], role: str) -> list[str]:
    allowed = ROLE_ALLOWED_INTENTS.get(role)
    if allowed is None:
        return intents
    return [i for i in intents if i in allowed]


def filter_namespaces(all_namespaces: list[str], user: dict) -> list[str]:
    role = user.get("role", "viewer")
    if role in ("admin", "operator"):
        return all_namespaces
    assigned = user.get("allowed_namespaces", [])
    if not assigned:
        return []   # viewer with no assigned namespaces sees nothing
    return [ns for ns in all_namespaces if ns in assigned]


def permission_denied_note(intent: str, role: str) -> str:
    return (
        f"[PERMISSION_DENIED] Your role ('{role}') does not allow access to '{intent}' data. "
        "Please ask an admin to upgrade your permissions if you need this."
    )
