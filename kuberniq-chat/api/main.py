"""FastAPI application — Kuberniq Chat backend."""
from __future__ import annotations

import os
from typing import Optional

from pathlib import Path as _Path
from dotenv import load_dotenv
# Use __file__ so the .env is always found next to main.py regardless of CWD
load_dotenv(dotenv_path=_Path(__file__).parent / ".env")

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import auth as _auth
from models import (
    LoginRequest, RefreshRequest, ChatRequest,
    ChangePasswordRequest, CreateUserRequest, UpdateUserRequest,
)
from rag import stream_chat_response


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bootstrap admin on first run (K8s Secret in prod, local file in dev)
    await _auth.bootstrap_admin()
    # Warm up the signing key so validate_access_token is fully sync on first request
    await _auth._get_signing_key()
    yield

app = FastAPI(title="Kuberniq Chat API", lifespan=lifespan)

# Allow Next.js dev server (port 3000) during local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth dependency — mirrors kuberniq-server middleware (Bearer token) ────────
#
# The MCP server enforces:  Authorization: Bearer <accessToken>
# We use exactly the same pattern — no cookies, no session files.

def _extract_bearer(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]
    return None


def get_current_user(request: Request) -> dict:
    token = _extract_bearer(request)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in via POST /api/auth/login.",
        )
    user = _auth.validate_access_token(token)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token. Please log in again.",
        )
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Auth endpoints — same shape as kuberniq-server /auth/* ───────────────────

@app.post("/api/auth/login")
async def login(body: LoginRequest):
    """Proxy to MCP server /auth/login → returns { accessToken, refreshToken, expiresIn }."""
    ok, tokens, err = await _auth.login(body.username, body.password)
    if not ok:
        raise HTTPException(status_code=401, detail=err)
    return tokens   # pass through the MCP server's TokenResponse as-is


@app.post("/api/auth/refresh")
async def refresh_token(body: RefreshRequest):
    """Proxy to MCP server /auth/refresh — rotates the token pair."""
    ok, tokens, err = await _auth.refresh(body.refresh_token)
    if not ok:
        raise HTTPException(status_code=401, detail=err)
    return tokens


@app.post("/api/auth/logout")
async def logout(body: RefreshRequest):
    """Proxy to MCP server /auth/logout — revokes the refresh token."""
    await _auth.logout(body.refresh_token)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {
        "username": user["username"],
        "role": user["role"],
        "allowed_namespaces": user.get("allowed_namespaces", []),
        "role_description": _auth.ROLE_DESCRIPTIONS.get(user["role"], ""),
    }


@app.post("/api/auth/change-password")
async def change_password(body: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    ok, err = await _auth.change_password(
        user["username"], body.current_password, body.new_password,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True}


# ── User management (admin only) — proxy to MCP /auth/users/* ────────────────

@app.get("/api/users")
async def list_users(_: dict = Depends(require_admin)):
    return await _auth.list_users()


@app.post("/api/users", status_code=201)
async def create_user(body: CreateUserRequest, _: dict = Depends(require_admin)):
    ok, err = await _auth.create_user(body.username, body.password, body.role, body.allowed_namespaces)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True}


@app.delete("/api/users/{username}")
async def delete_user(username: str, _: dict = Depends(require_admin)):
    ok, err = await _auth.delete_user(username)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True}


@app.patch("/api/users/{username}")
async def update_user(username: str, body: UpdateUserRequest, _: dict = Depends(require_admin)):
    ok, err = await _auth.update_user(username, role=body.role, allowed_namespaces=body.allowed_namespaces)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True}


# ── Chat ──────────────────────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat(body: ChatRequest, user: dict = Depends(get_current_user)):
    history = [{"role": m.role, "content": m.content} for m in body.history]

    async def generate():
        async for line in stream_chat_response(
            message=body.message,
            history=history,
            user=user,
            model=body.model,
            yaml_content=body.yaml_content,
        ):
            yield line

    return StreamingResponse(generate(), media_type="text/plain")


# ── Models list ───────────────────────────────────────────────────────────────

@app.get("/api/models")
async def list_models(_: dict = Depends(get_current_user)):
    """Return the list of LLM models available in this deployment."""
    return [
        # OpenAI
        {"id": "gpt-5.6-sol",      "name": "GPT-5.6 Sol",      "provider": "OpenAI"},
        {"id": "gpt-5.6-terra",    "name": "GPT-5.6 Terra",    "provider": "OpenAI"},
        {"id": "gpt-5.6-luna",     "name": "GPT-5.6 Luna",     "provider": "OpenAI"},
        {"id": "gpt-5.5",          "name": "GPT-5.5",          "provider": "OpenAI"},
        {"id": "gpt-5.5-pro",      "name": "GPT-5.5 Pro",      "provider": "OpenAI"},
        {"id": "gpt-5.4",          "name": "GPT-5.4",          "provider": "OpenAI"},
        {"id": "gpt-5.4-pro",      "name": "GPT-5.4 Pro",      "provider": "OpenAI"},
        {"id": "gpt-5.4-mini",     "name": "GPT-5.4 mini",     "provider": "OpenAI"},
        {"id": "gpt-5.4-nano",     "name": "GPT-5.4 nano",     "provider": "OpenAI"},
        {"id": "gpt-5.1",          "name": "GPT-5.1",          "provider": "OpenAI"},
        {"id": "gpt-5",            "name": "GPT-5",            "provider": "OpenAI"},
        {"id": "gpt-5-mini",       "name": "GPT-5 mini",       "provider": "OpenAI"},
        {"id": "gpt-5-nano",       "name": "GPT-5 nano",       "provider": "OpenAI"},
        {"id": "gpt-5-pro",        "name": "GPT-5 Pro",        "provider": "OpenAI"},
        {"id": "gpt-5.3-codex",    "name": "GPT-5.3 Codex",    "provider": "OpenAI"},
        {"id": "o3",               "name": "o3",              "provider": "OpenAI"},
        {"id": "o3-pro",           "name": "o3 Pro",          "provider": "OpenAI"},
        {"id": "o4-mini",          "name": "o4-mini",         "provider": "OpenAI"},
        {"id": "gpt-4.1",          "name": "GPT-4.1",         "provider": "OpenAI"},
        {"id": "gpt-4.1-mini",     "name": "GPT-4.1 mini",    "provider": "OpenAI"},
        {"id": "gpt-4o-mini",      "name": "GPT-4o mini",     "provider": "OpenAI"},
        # Anthropic
        {"id": "claude-opus-4-5",                     "name": "Claude Opus 4.5",          "provider": "Anthropic"},
        {"id": "claude-sonnet-4-5",                   "name": "Claude Sonnet 4.5",        "provider": "Anthropic"},
        {"id": "claude-3-5-sonnet-20241022",          "name": "Claude Sonnet 3.5",        "provider": "Anthropic"},
        {"id": "claude-3-5-haiku-20241022",           "name": "Claude Haiku 3.5",         "provider": "Anthropic"},
        {"id": "claude-3-opus-20240229",              "name": "Claude Opus 3",            "provider": "Anthropic"},
        # Google
        {"id": "gemini/gemini-2.5-pro",               "name": "Gemini 2.5 Pro",           "provider": "Google"},
        {"id": "gemini/gemini-2.5-flash",             "name": "Gemini 2.5 Flash",         "provider": "Google"},
        {"id": "gemini/gemini-2.0-flash",             "name": "Gemini 2.0 Flash",         "provider": "Google"},
        {"id": "gemini/gemini-2.0-flash-lite",        "name": "Gemini 2.0 Flash Lite",    "provider": "Google"},
        {"id": "gemini/gemini-1.5-pro",               "name": "Gemini 1.5 Pro",           "provider": "Google"},
        {"id": "gemini/gemini-1.5-flash",             "name": "Gemini 1.5 Flash",         "provider": "Google"},
        # Groq
        {"id": "groq/llama-3.3-70b-versatile",        "name": "Llama 3.3 70B",            "provider": "Groq"},
        {"id": "groq/llama-3.1-8b-instant",           "name": "Llama 3.1 8B",             "provider": "Groq"},
        {"id": "groq/mixtral-8x7b-32768",             "name": "Mixtral 8x7B",             "provider": "Groq"},
        {"id": "groq/gemma2-9b-it",                   "name": "Gemma 2 9B",               "provider": "Groq"},
        # Mistral
        {"id": "mistral/mistral-large-latest",        "name": "Mistral Large",            "provider": "Mistral"},
        {"id": "mistral/mistral-small-latest",        "name": "Mistral Small",            "provider": "Mistral"},
        {"id": "mistral/codestral-latest",            "name": "Codestral",                "provider": "Mistral"},
        # Cohere
        {"id": "cohere/command-r-plus",               "name": "Command R+",               "provider": "Cohere"},
        {"id": "cohere/command-r",                    "name": "Command R",                "provider": "Cohere"},
        # Perplexity
        {"id": "perplexity/llama-3.1-sonar-large-128k-online", "name": "Sonar Large (online)", "provider": "Perplexity"},
        {"id": "perplexity/llama-3.1-sonar-small-128k-online", "name": "Sonar Small (online)", "provider": "Perplexity"},
        # DeepSeek
        {"id": "deepseek/deepseek-chat",              "name": "DeepSeek Chat",            "provider": "DeepSeek"},
        {"id": "deepseek/deepseek-coder",             "name": "DeepSeek Coder",           "provider": "DeepSeek"},
        # Ollama (local)
        {"id": "ollama/llama3.1",                     "name": "Llama 3.1 (local)",        "provider": "Ollama"},
        {"id": "ollama/llama3.2",                     "name": "Llama 3.2 (local)",        "provider": "Ollama"},
        {"id": "ollama/mistral",                      "name": "Mistral (local)",          "provider": "Ollama"},
        {"id": "ollama/codellama",                    "name": "CodeLlama (local)",        "provider": "Ollama"},
        {"id": "ollama/gemma2",                       "name": "Gemma 2 (local)",          "provider": "Ollama"},
        {"id": "ollama/phi3",                         "name": "Phi-3 (local)",            "provider": "Ollama"},
        {"id": "ollama/qwen2.5-coder",                "name": "Qwen 2.5 Coder (local)",   "provider": "Ollama"},
    ]


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ── Serve Next.js build in production ────────────────────────────────────────
# (the Dockerfile copies the Next.js `out/` folder to /app/static)
_STATIC = "/app/static"
if os.path.isdir(_STATIC):
    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")


