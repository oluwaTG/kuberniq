"use client";

import { useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import { login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError]       = useState("");
  const [loading, setLoading]   = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (!username || !password) { setError("Please enter username and password."); return; }
    setLoading(true);
    try {
      await login(username, password);
      // Tokens are stored in localStorage by login() in lib/api.ts.
      // Redirect to chat — same as the MCP server's post-login flow.
      router.push("/chat");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex h-screen w-full items-center justify-center" style={{ background: "var(--color-bg)" }}>
      <div
        className="w-full max-w-sm rounded-2xl p-8"
        style={{ background: "var(--color-surface)", border: "1px solid var(--color-border)" }}
      >
        {/* Logo */}
        <div className="mb-8 flex flex-col items-center gap-3">
          <img
            src="/kuberniq.png"
            alt="Kuberniq"
            width={56}
            height={56}
            style={{ display: "block", mixBlendMode: "screen" }}
          />
          <div className="text-center">
            <h1 className="text-lg font-semibold" style={{ color: "var(--color-text)" }}>
              Kuberniq Chat
            </h1>
            <p className="text-sm mt-0.5" style={{ color: "var(--color-muted)" }}>
              Kubernetes AI assistant — sign in to continue
            </p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <label className="text-xs font-medium" style={{ color: "var(--color-muted)" }}>
              Username
            </label>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              placeholder="admin"
              autoComplete="username"
              className="rounded-lg px-3 py-2.5 text-sm outline-none transition-colors"
              style={{
                background: "var(--color-surface2)",
                border: "1px solid var(--color-border)",
                color: "var(--color-text)",
              }}
              onFocus={e => (e.currentTarget.style.borderColor = "var(--color-accent)")}
              onBlur={e => (e.currentTarget.style.borderColor = "var(--color-border)")}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-xs font-medium" style={{ color: "var(--color-muted)" }}>
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="••••••••"
              autoComplete="current-password"
              className="rounded-lg px-3 py-2.5 text-sm outline-none transition-colors"
              style={{
                background: "var(--color-surface2)",
                border: "1px solid var(--color-border)",
                color: "var(--color-text)",
              }}
              onFocus={e => (e.currentTarget.style.borderColor = "var(--color-accent)")}
              onBlur={e => (e.currentTarget.style.borderColor = "var(--color-border)")}
            />
          </div>

          {error && (
            <p className="rounded-lg px-3 py-2 text-xs" style={{ background: "#f76f6f22", color: "var(--color-danger)", border: "1px solid #f76f6f44" }}>
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="mt-2 rounded-lg py-2.5 text-sm font-semibold transition-opacity disabled:opacity-50"
            style={{ background: "var(--color-accent)", color: "#fff" }}
          >
            {loading ? "Signing in…" : "Sign in →"}
          </button>
        </form>
      </div>
    </div>
  );
}
