"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { LogOut, KeyRound, Users, ChevronDown, Plus, Trash2, Settings, Pencil, Check, X } from "lucide-react";
import type { User, Model, ManagedUser } from "@/lib/types";
import { logout, changePassword, listUsers, createUser, deleteUser, updateUser } from "@/lib/api";

const ROLE_COLORS: Record<string, string> = {
  admin:    "var(--color-warn)",
  operator: "var(--color-accent2)",
  viewer:   "var(--color-muted)",
};

const PROVIDER_COLORS: Record<string, string> = {
  OpenAI:    "#10a37f",
  Anthropic: "#d4a27f",
  Google:    "#4285f4",
  Groq:      "#f55036",
  Ollama:    "var(--color-muted)",
};

interface Props {
  user: User;
  models: Model[];
  selectedModel: string;
  onModelChange: (id: string) => void;
  onNewChat: () => void;
}

// ── Change Password Modal ─────────────────────────────────────────────────────

function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const [cur, setCur]   = useState("");
  const [next, setNext] = useState("");
  const [conf, setConf] = useState("");
  const [err, setErr]   = useState("");
  const [ok, setOk]     = useState(false);
  const [loading, setLoading] = useState(false);

  async function submit() {
    setErr("");
    if (next !== conf) { setErr("New passwords do not match."); return; }
    if (next.length < 8) { setErr("Minimum 8 characters."); return; }
    setLoading(true);
    try {
      await changePassword(cur, next);
      setOk(true);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,.6)" }}>
      <div className="w-full max-w-sm rounded-2xl p-6" style={{ background: "var(--color-surface)", border: "1px solid var(--color-border)" }}>
        <h3 className="mb-4 font-semibold" style={{ color: "var(--color-text)" }}>Change Password</h3>
        {ok ? (
          <div className="text-center">
            <p className="text-sm mb-4" style={{ color: "var(--color-accent2)" }}>Password updated ✓</p>
            <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm" style={{ background: "var(--color-surface2)", color: "var(--color-text)", border: "1px solid var(--color-border)" }}>Close</button>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {(["Current password", "New password", "Confirm new password"] as const).map((label, i) => {
              const val  = [cur, next, conf][i];
              const setter = [setCur, setNext, setConf][i];
              return (
                <div key={label} className="flex flex-col gap-1">
                  <label className="text-xs" style={{ color: "var(--color-muted)" }}>{label}</label>
                  <input type="password" value={val} onChange={e => setter(e.target.value)}
                    className="rounded-lg px-3 py-2 text-sm outline-none"
                    style={{ background: "var(--color-surface2)", border: "1px solid var(--color-border)", color: "var(--color-text)" }} />
                </div>
              );
            })}
            {err && <p className="text-xs" style={{ color: "var(--color-danger)" }}>{err}</p>}
            <div className="flex gap-2 mt-1">
              <button onClick={onClose} className="flex-1 rounded-lg py-2 text-sm" style={{ background: "var(--color-surface2)", color: "var(--color-text)", border: "1px solid var(--color-border)" }}>Cancel</button>
              <button onClick={submit} disabled={loading} className="flex-1 rounded-lg py-2 text-sm font-semibold disabled:opacity-50" style={{ background: "var(--color-accent)", color: "#fff" }}>
                {loading ? "Saving…" : "Update"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── User Management Modal ─────────────────────────────────────────────────────

function UserMgmtModal({ onClose }: { onClose: () => void }) {
  const [users, setUsers]       = useState<ManagedUser[] | null>(null);
  const [loading, setLoading]   = useState(false);
  const [err, setErr]           = useState("");
  const [creating, setCreating] = useState(false);
  const [editingUser, setEditingUser] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ role: "", namespaces: "" });
  const [newUser, setNewUser]   = useState({ username: "", password: "", role: "viewer", namespaces: "" });

  async function load() {
    setLoading(true);
    try { setUsers(await listUsers()); } catch (e: unknown) { setErr(e instanceof Error ? e.message : "Failed"); }
    setLoading(false);
  }

  if (users === null && !loading && !err) { load(); }

  async function handleDelete(username: string) {
    if (!confirm(`Delete user '${username}'?`)) return;
    try { await deleteUser(username); setUsers(u => u ? u.filter(x => x.username !== username) : u); }
    catch (e: unknown) { setErr(e instanceof Error ? e.message : "Failed"); }
  }

  function startEdit(u: ManagedUser) {
    setEditingUser(u.username);
    setEditForm({ role: u.role, namespaces: (u.allowed_namespaces ?? []).join(", ") });
  }

  async function handleUpdate(username: string) {
    try {
      const ns = editForm.namespaces.split(",").map(s => s.trim()).filter(Boolean);
      await updateUser(username, editForm.role, ns);
      setEditingUser(null);
      load();
    } catch (e: unknown) { setErr(e instanceof Error ? e.message : "Failed"); }
  }

  async function handleCreate() {
    try {
      await createUser(newUser.username, newUser.password, newUser.role,
        newUser.namespaces.split(",").map(s => s.trim()).filter(Boolean));
      setCreating(false);
      setNewUser({ username: "", password: "", role: "viewer", namespaces: "" });
      load();
    } catch (e: unknown) { setErr(e instanceof Error ? e.message : "Failed"); }
  }

  const inputStyle = { background: "var(--color-surface)", border: "1px solid var(--color-border)", color: "var(--color-text)" };
  const selectStyle = { background: "var(--color-surface)", border: "1px solid var(--color-border)", color: "var(--color-text)" };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,.6)" }}>
      <div className="w-full max-w-lg rounded-2xl p-6" style={{ background: "var(--color-surface)", border: "1px solid var(--color-border)", maxHeight: "85vh", overflow: "auto" }}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="font-semibold" style={{ color: "var(--color-text)" }}>User Management</h3>
          <div className="flex gap-2">
            <button onClick={() => { setCreating(v => !v); setEditingUser(null); }}
              className="flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-medium"
              style={{ background: "var(--color-surface2)", color: "var(--color-accent)", border: "1px solid var(--color-border)" }}>
              <Plus size={12} /> New user
            </button>
            <button onClick={onClose} style={{ color: "var(--color-muted)" }}>✕</button>
          </div>
        </div>

        {err && <p className="mb-3 text-xs" style={{ color: "var(--color-danger)" }}>{err}</p>}

        {creating && (
          <div className="mb-4 rounded-xl p-4 flex flex-col gap-2" style={{ background: "var(--color-surface2)", border: "1px solid var(--color-border)" }}>
            <h4 className="text-xs font-semibold mb-1" style={{ color: "var(--color-muted)" }}>Create user</h4>
            {[["Username", "username"], ["Password", "password"]].map(([lbl, field]) => (
              <div key={field} className="flex flex-col gap-0.5">
                <label className="text-[10px]" style={{ color: "var(--color-muted)" }}>{lbl}</label>
                <input type={field === "password" ? "password" : "text"}
                  value={(newUser as Record<string, string>)[field]}
                  onChange={e => setNewUser(u => ({ ...u, [field]: e.target.value }))}
                  className="rounded-lg px-3 py-1.5 text-xs outline-none" style={inputStyle} />
              </div>
            ))}
            <div className="flex flex-col gap-0.5">
              <label className="text-[10px]" style={{ color: "var(--color-muted)" }}>Role</label>
              <select value={newUser.role} onChange={e => setNewUser(u => ({ ...u, role: e.target.value }))}
                className="rounded-lg px-3 py-1.5 text-xs outline-none" style={selectStyle}>
                {["admin", "operator", "viewer"].map(r => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            <div className="flex flex-col gap-0.5">
              <label className="text-[10px]" style={{ color: "var(--color-muted)" }}>
                Allowed namespaces <span style={{ color: "var(--color-accent)" }}>(comma-separated — leave blank for full access)</span>
              </label>
              <input type="text" placeholder="e.g. dev, kuberniq, staging"
                value={newUser.namespaces}
                onChange={e => setNewUser(u => ({ ...u, namespaces: e.target.value }))}
                className="rounded-lg px-3 py-1.5 text-xs outline-none" style={inputStyle} />
            </div>
            <div className="flex gap-2 mt-1">
              <button onClick={() => setCreating(false)} className="flex-1 rounded-lg py-1.5 text-xs"
                style={{ background: "var(--color-surface)", color: "var(--color-text)", border: "1px solid var(--color-border)" }}>Cancel</button>
              <button onClick={handleCreate} className="flex-1 rounded-lg py-1.5 text-xs font-semibold"
                style={{ background: "var(--color-accent)", color: "#fff" }}>Create</button>
            </div>
          </div>
        )}

        {loading ? (
          <p className="text-sm text-center py-4" style={{ color: "var(--color-muted)" }}>Loading…</p>
        ) : (
          <div className="flex flex-col gap-2">
            {(users ?? []).map(u => (
              <div key={u.username} className="rounded-xl px-3 py-2.5"
                style={{ background: "var(--color-surface2)", border: "1px solid var(--color-border)" }}>
                {editingUser === u.username ? (
                  /* ── Edit row ── */
                  <div className="flex flex-col gap-2">
                    <p className="text-sm font-medium" style={{ color: "var(--color-text)" }}>{u.username}</p>
                    <div className="flex gap-2">
                      <div className="flex flex-col gap-0.5 flex-1">
                        <label className="text-[10px]" style={{ color: "var(--color-muted)" }}>Role</label>
                        <select value={editForm.role} onChange={e => setEditForm(f => ({ ...f, role: e.target.value }))}
                          className="rounded-lg px-2 py-1 text-xs outline-none" style={selectStyle}>
                          {["admin", "operator", "viewer"].map(r => <option key={r} value={r}>{r}</option>)}
                        </select>
                      </div>
                      <div className="flex flex-col gap-0.5 flex-[2]">
                        <label className="text-[10px]" style={{ color: "var(--color-muted)" }}>Namespaces (comma-separated)</label>
                        <input type="text" placeholder="dev, kuberniq, staging"
                          value={editForm.namespaces}
                          onChange={e => setEditForm(f => ({ ...f, namespaces: e.target.value }))}
                          className="rounded-lg px-2 py-1 text-xs outline-none" style={inputStyle} />
                      </div>
                    </div>
                    <div className="flex gap-2 justify-end">
                      <button onClick={() => setEditingUser(null)} className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-xs"
                        style={{ color: "var(--color-muted)", border: "1px solid var(--color-border)" }}>
                        <X size={11} /> Cancel
                      </button>
                      <button onClick={() => handleUpdate(u.username)} className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-xs font-semibold"
                        style={{ background: "var(--color-accent)", color: "#fff" }}>
                        <Check size={11} /> Save
                      </button>
                    </div>
                  </div>
                ) : (
                  /* ── Display row ── */
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm font-medium" style={{ color: "var(--color-text)" }}>{u.username}</p>
                      <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                        <span className="text-[10px] rounded-full px-2 py-0.5"
                          style={{ background: ROLE_COLORS[u.role] + "22", color: ROLE_COLORS[u.role], border: `1px solid ${ROLE_COLORS[u.role]}44` }}>
                          {u.role}
                        </span>
                        {(u.allowed_namespaces ?? []).length > 0 ? (
                          <span className="text-[10px]" style={{ color: "var(--color-muted)" }}>
                            ns: {(u.allowed_namespaces ?? []).join(", ")}
                          </span>
                        ) : (
                          <span className="text-[10px]" style={{ color: "var(--color-muted)" }}>all namespaces</span>
                        )}
                      </div>
                    </div>
                    {u.username !== "admin" && (
                      <div className="flex items-center gap-2">
                        <button onClick={() => startEdit(u)} className="transition-colors" style={{ color: "var(--color-muted)" }}
                          onMouseEnter={e => (e.currentTarget.style.color = "var(--color-accent)")}
                          onMouseLeave={e => (e.currentTarget.style.color = "var(--color-muted)")}>
                          <Pencil size={13} />
                        </button>
                        <button onClick={() => handleDelete(u.username)} className="transition-colors" style={{ color: "var(--color-muted)" }}
                          onMouseEnter={e => (e.currentTarget.style.color = "var(--color-danger)")}
                          onMouseLeave={e => (e.currentTarget.style.color = "var(--color-muted)")}>
                          <Trash2 size={13} />
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Sidebar ───────────────────────────────────────────────────────────────────

export default function Sidebar({ user, models, selectedModel, onModelChange, onNewChat }: Props) {
  const router = useRouter();
  const [showChangePw, setShowChangePw]     = useState(false);
  const [showUserMgmt, setShowUserMgmt]     = useState(false);
  const [showModelMenu, setShowModelMenu]   = useState(false);
  const [loggingOut, setLoggingOut]         = useState(false);

  const roleColor = ROLE_COLORS[user.role] ?? "var(--color-muted)";
  const currentModel = models.find(m => m.id === selectedModel);

  async function handleLogout() {
    setLoggingOut(true);
    try { await logout(); } catch {}
    router.push("/login");
  }

  const grouped = models.reduce<Record<string, Model[]>>((acc, m) => {
    (acc[m.provider] ??= []).push(m);
    return acc;
  }, {});

  return (
    <>
      {showChangePw && <ChangePasswordModal onClose={() => setShowChangePw(false)} />}
      {showUserMgmt && <UserMgmtModal onClose={() => setShowUserMgmt(false)} />}

      <aside
        className="flex h-full w-64 shrink-0 flex-col"
        style={{ background: "var(--color-surface)", borderRight: "1px solid var(--color-border)" }}
      >
        {/* Logo */}
        <div className="flex items-center gap-2.5 px-4 py-4" style={{ borderBottom: "1px solid var(--color-border)" }}>
          <img
            src="/kuberniq.png"
            alt="Kuberniq"
            width={32}
            height={32}
            style={{ display: "block", mixBlendMode: "screen" }}
          />
          <div>
            <p className="text-sm font-semibold leading-tight" style={{ color: "var(--color-text)" }}>Kuberniq</p>
            <p className="text-[10px]" style={{ color: "var(--color-muted)" }}>Kubernetes AI assistant</p>
          </div>
        </div>

        {/* New chat */}
        <div className="px-3 pt-3">
          <button
            onClick={onNewChat}
            className="flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors"
            style={{ border: "1px solid var(--color-border)", color: "var(--color-text)" }}
            onMouseEnter={e => { e.currentTarget.style.background = "var(--color-surface2)"; }}
            onMouseLeave={e => { e.currentTarget.style.background = "transparent"; }}
          >
            <Plus size={15} style={{ color: "var(--color-accent)" }} />
            New chat
          </button>
        </div>

        {/* Spacer — conversation history goes here in a future iteration */}
        <div className="flex-1 px-3 py-2">
          <p className="px-2 pt-2 text-[10px] font-semibold uppercase tracking-widest" style={{ color: "var(--color-border)" }}>
            Conversations
          </p>
          <p className="px-2 py-1 text-xs" style={{ color: "var(--color-muted)" }}>
            Session-only — history is cleared on new chat
          </p>
        </div>

        {/* Model picker */}
        <div className="px-3 pb-2" style={{ borderTop: "1px solid var(--color-border)" }}>
          <p className="px-1 pt-3 pb-1.5 text-[10px] font-semibold uppercase tracking-widest" style={{ color: "var(--color-muted)" }}>
            Model
          </p>
          <div className="relative">
            <button
              onClick={() => setShowModelMenu(v => !v)}
              className="flex w-full items-center justify-between rounded-xl px-3 py-2.5 text-sm transition-colors"
              style={{ background: "var(--color-surface2)", border: "1px solid var(--color-border)", color: "var(--color-text)" }}
            >
              <div className="flex items-center gap-2">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: PROVIDER_COLORS[currentModel?.provider ?? ""] ?? "var(--color-muted)" }}
                />
                <span className="truncate">{currentModel?.name ?? selectedModel}</span>
              </div>
              <ChevronDown size={14} style={{ color: "var(--color-muted)", transform: showModelMenu ? "rotate(180deg)" : "none", transition: "transform .15s" }} />
            </button>

            {showModelMenu && (
              <div
                className="absolute bottom-full left-0 right-0 mb-1 rounded-xl py-1 shadow-xl z-10 overflow-y-auto"
                style={{ background: "var(--color-surface2)", border: "1px solid var(--color-border)", maxHeight: "min(360px, 60vh)" }}
              >
                {Object.entries(grouped).map(([provider, providerModels]) => (
                  <div key={provider}>
                    <p className="px-3 py-1 text-[10px] font-semibold uppercase tracking-widest" style={{ color: "var(--color-muted)" }}>
                      {provider}
                    </p>
                    {providerModels.map(m => (
                      <button
                        key={m.id}
                        onClick={() => { onModelChange(m.id); setShowModelMenu(false); }}
                        className="flex w-full items-center gap-2 px-3 py-2 text-sm transition-colors text-left"
                        style={{ color: m.id === selectedModel ? "var(--color-accent)" : "var(--color-text)" }}
                        onMouseEnter={e => { e.currentTarget.style.background = "var(--color-surface)"; }}
                        onMouseLeave={e => { e.currentTarget.style.background = "transparent"; }}
                      >
                        <span className="h-1.5 w-1.5 rounded-full flex-shrink-0" style={{ background: PROVIDER_COLORS[provider] ?? "var(--color-muted)" }} />
                        {m.name}
                        {m.id === selectedModel && <span className="ml-auto text-xs">✓</span>}
                      </button>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* User section */}
        <div className="px-3 pb-3" style={{ borderTop: "1px solid var(--color-border)" }}>
          <div className="flex items-center gap-2.5 px-1 pt-3 pb-2">
            <div className="flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold" style={{ background: roleColor + "22", color: roleColor, border: `1px solid ${roleColor}44` }}>
              {user.username[0].toUpperCase()}
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium leading-tight" style={{ color: "var(--color-text)" }}>{user.username}</p>
              <span className="text-[10px] rounded-full px-1.5 py-0.5" style={{ background: roleColor + "22", color: roleColor, border: `1px solid ${roleColor}44` }}>
                {user.role}
              </span>
            </div>
          </div>

          <div className="flex flex-col gap-0.5">
            <SidebarAction icon={<KeyRound size={13} />} label="Change password" onClick={() => setShowChangePw(true)} />
            {user.role === "admin" && (
              <SidebarAction icon={<Users size={13} />} label="User management" onClick={() => setShowUserMgmt(true)} />
            )}
            <SidebarAction
              icon={<LogOut size={13} />}
              label={loggingOut ? "Signing out…" : "Sign out"}
              onClick={handleLogout}
              danger
            />
          </div>
        </div>
      </aside>
    </>
  );
}

function SidebarAction({ icon, label, onClick, danger }: {
  icon: React.ReactNode; label: string; onClick: () => void; danger?: boolean;
}) {
  const color = danger ? "var(--color-danger)" : "var(--color-muted)";
  return (
    <button
      onClick={onClick}
      className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-xs transition-colors"
      style={{ color }}
      onMouseEnter={e => { e.currentTarget.style.background = "var(--color-surface2)"; e.currentTarget.style.color = danger ? "var(--color-danger)" : "var(--color-text)"; }}
      onMouseLeave={e => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = color; }}
    >
      {icon}
      {label}
    </button>
  );
}
