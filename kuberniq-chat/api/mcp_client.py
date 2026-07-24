"""MCP server client — auth + GET helper.

Uses synchronous `requests` (same as the original Streamlit app.py) wrapped in
asyncio.to_thread so it works in the FastAPI async context.  This is important
on macOS where httpx's async DNS resolver does NOT check /etc/hosts for .local
domains (routes them through mDNS instead), whereas Python's socket.getaddrinfo
used by `requests` correctly honours /etc/hosts.
"""
from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests as _requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# Dedicated small thread pool for MCP calls so they can never starve the
# main asyncio thread pool used by FastAPI.
_MCP_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="mcp")

# Semaphore limits how many MCP calls run concurrently.
# Must match max_workers so asyncio.gather() can't queue more futures than
# there are threads — otherwise a cancelled wait_for() leaves orphan threads
# that block the pool for the next request.
_MCP_SEM: asyncio.Semaphore | None = None  # created lazily after event loop starts


def _get_sem() -> asyncio.Semaphore:
    global _MCP_SEM
    if _MCP_SEM is None:
        _MCP_SEM = asyncio.Semaphore(6)
    return _MCP_SEM

MCP_URL      = os.getenv("MCP_SERVER_URL", "http://mcp-server.local")
MCP_USERNAME = os.getenv("MCP_USERNAME",   "admin")
MCP_PASSWORD = os.getenv("MCP_PASSWORD",   "")
MCP_TIMEOUT  = float(os.getenv("MCP_TIMEOUT", "5"))   # kept short so threads clear fast

# Shared token cache
_tokens: dict[str, str | None] = {"access": None, "refresh": None}

# ── Short-lived cache for the 3 "always-fetched" MCP calls ───────────────────
# Namespaces, cluster list, and cluster info rarely change within a session.
# Caching them for 60s means follow-up questions skip the slow initial fetch.
_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, Any]] = {}   # path → (expires_at, value)


def _cached_get(path: str, text: bool = False) -> Any | None:
    entry = _cache.get(path)
    if entry and time.time() < entry[0]:
        return entry[1]
    return None


def _cache_set(path: str, value: Any) -> None:
    _cache[path] = (time.time() + _CACHE_TTL, value)


def _login() -> bool:
    if not MCP_PASSWORD:
        return False
    try:
        r = _requests.post(
            f"{MCP_URL}/auth/login",
            json={"username": MCP_USERNAME, "password": MCP_PASSWORD},
            timeout=MCP_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            _tokens["access"]  = d.get("accessToken")
            _tokens["refresh"] = d.get("refreshToken")
            return bool(_tokens["access"])
    except Exception:
        pass
    return False


def _refresh() -> bool:
    if not _tokens["refresh"]:
        return False
    try:
        r = _requests.post(
            f"{MCP_URL}/auth/refresh",
            json={"refreshToken": _tokens["refresh"]},
            timeout=MCP_TIMEOUT,
        )
        if r.status_code == 200:
            d = r.json()
            _tokens["access"]  = d.get("accessToken")
            _tokens["refresh"] = d.get("refreshToken")
            return bool(_tokens["access"])
    except Exception:
        pass
    return False


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_tokens['access']}"} if _tokens["access"] else {}


def _mcp_get_sync(path: str, text: bool = False) -> Any:
    """Synchronous MCP GET with auto login/refresh on 401 and short-lived cache."""
    # Return cached value for stable endpoints (namespaces, clusters, cluster/info)
    cached = _cached_get(path)
    if cached is not None:
        return cached

    if _tokens["access"] is None:
        _login()

    def _do():
        return _requests.get(
            f"{MCP_URL}{path}", headers=_auth_headers(), timeout=MCP_TIMEOUT
        )

    try:
        r = _do()
        if r.status_code == 401:
            if not (_refresh() or _login()):
                return {"error": "Authentication failed"}
            r = _do()
        r.raise_for_status()
        result = r.text if text else r.json()
        # Cache stable read-only endpoints
        if path in ("/namespaces", "/clusters", "/cluster/info"):
            _cache_set(path, result)
        return result
    except Exception as e:
        return {"error": str(e)}


async def mcp_get(path: str, text: bool = False) -> Any:
    """Async wrapper — semaphore-bounded so gather() can't exhaust the thread pool."""
    async with _get_sem():
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_MCP_EXECUTOR, _mcp_get_sync, path, text)


async def get_all_namespaces() -> list[str]:
    result = await mcp_get("/namespaces")
    return result if isinstance(result, list) else []
