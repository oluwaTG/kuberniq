import type { User, Model, ManagedUser } from "./types";

const BASE = typeof window !== "undefined" ? "" : (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000");

// ── Token storage — mirrors kuberniq-server Bearer token pattern ──────────────
// Tokens are kept in localStorage (no cookies).  Every request attaches the
// access token via  Authorization: Bearer <token>  — exactly as the MCP server
// enforces for its own protected endpoints.

const STORAGE_ACCESS  = "kuberniq_access_token";
const STORAGE_REFRESH = "kuberniq_refresh_token";

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(STORAGE_ACCESS);
}

export function storeTokens(accessToken: string, refreshToken: string): void {
  localStorage.setItem(STORAGE_ACCESS,  accessToken);
  localStorage.setItem(STORAGE_REFRESH, refreshToken);
}

export function clearTokens(): void {
  localStorage.removeItem(STORAGE_ACCESS);
  localStorage.removeItem(STORAGE_REFRESH);
}

function authHeaders(): Record<string, string> {
  const token = getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// ── Core fetch helper ─────────────────────────────────────────────────────────

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(opts?.headers ?? {}),
    },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error((err as { detail?: string }).detail ?? res.statusText);
  }
  return res.json() as Promise<T>;
}

// ── Auth ──────────────────────────────────────────────────────────────────────

interface TokenResponse {
  accessToken: string;
  refreshToken: string;
  expiresIn: number;
}

export async function login(username: string, password: string): Promise<TokenResponse> {
  const tokens = await req<TokenResponse>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  storeTokens(tokens.accessToken, tokens.refreshToken);
  return tokens;
}

export async function refreshTokens(): Promise<boolean> {
  const refresh_token = typeof window !== "undefined"
    ? localStorage.getItem(STORAGE_REFRESH)
    : null;
  if (!refresh_token) return false;
  try {
    const tokens = await req<TokenResponse>("/api/auth/refresh", {
      method: "POST",
      body: JSON.stringify({ refresh_token }),
    });
    storeTokens(tokens.accessToken, tokens.refreshToken);
    return true;
  } catch {
    clearTokens();
    return false;
  }
}

export async function logout(): Promise<void> {
  const refresh_token = typeof window !== "undefined"
    ? localStorage.getItem(STORAGE_REFRESH)
    : null;
  clearTokens();   // clear immediately — best-effort server revocation
  if (refresh_token) {
    await req("/api/auth/logout", {
      method: "POST",
      body: JSON.stringify({ refresh_token }),
    }).catch(() => {});
  }
}

export async function getMe(): Promise<User> {
  return req<User>("/api/auth/me");
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  await req("/api/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

// ── Users (admin) ─────────────────────────────────────────────────────────────

export async function listUsers(): Promise<ManagedUser[]> {
  return req<ManagedUser[]>("/api/users");
}

export async function createUser(
  username: string, password: string, role: string, allowed_namespaces: string[]
): Promise<void> {
  await req("/api/users", {
    method: "POST",
    body: JSON.stringify({ username, password, role, allowed_namespaces }),
  });
}

export async function deleteUser(username: string): Promise<void> {
  await req(`/api/users/${username}`, { method: "DELETE" });
}

export async function updateUser(
  username: string, role?: string, allowed_namespaces?: string[]
): Promise<void> {
  await req(`/api/users/${username}`, {
    method: "PATCH",
    body: JSON.stringify({ role: role ?? null, allowed_namespaces: allowed_namespaces ?? null }),
  });
}

// ── Models ────────────────────────────────────────────────────────────────────

export async function listModels(): Promise<Model[]> {
  return req<Model[]>("/api/models");
}

// ── Chat (streaming) ──────────────────────────────────────────────────────────

export interface StreamEvent {
  type: "meta" | "token" | "done" | "error";
  content?: string;
  endpoints?: string[];
  rawContext?: string;
  message?: string;
}

export async function* streamChat(
  message: string,
  history: { role: string; content: string }[],
  model: string,
  yamlContent?: string,
): AsyncGenerator<StreamEvent> {
  const res = await fetch(`${BASE}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
    },
    body: JSON.stringify({ message, history, model, yaml_content: yamlContent ?? null }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    yield { type: "error", message: (err as { detail?: string }).detail ?? "Request failed" };
    return;
  }

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const event = JSON.parse(trimmed) as StreamEvent;
        yield event;
        if (event.type === "done") return;  // don't wait for TCP close
      } catch {
        // ignore malformed lines
      }
    }
  }
}
