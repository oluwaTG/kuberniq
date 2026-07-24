"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import ChatWindow from "@/components/ChatWindow";
import InputBar from "@/components/InputBar";
import { getMe, listModels, streamChat } from "@/lib/api";
import type { User, Model, ChatMessage } from "@/lib/types";

function nanoid() {
  return Math.random().toString(36).slice(2);
}

const DEFAULT_MODEL = "gpt-4o";

export default function ChatPage() {
  const router = useRouter();
  const [user,    setUser]    = useState<User | null>(null);
  const [models,  setModels]  = useState<Model[]>([]);
  const [model,   setModel]   = useState(DEFAULT_MODEL);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef  = useRef<boolean>(false);

  // Auth guard
  useEffect(() => {
    getMe()
      .then(u => setUser(u))
      .catch(() => router.push("/login"));
  }, [router]);

  // Load models
  useEffect(() => {
    listModels().then(setModels).catch(() => {});
  }, []);

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = useCallback(async (message: string, yamlContent?: string) => {
    if (streaming || (!message.trim() && !yamlContent)) return;
    abortRef.current = false;

    const userMsg: ChatMessage = {
      id: nanoid(),
      role: "user",
      content: message || (yamlContent ? "📎 Attached YAML manifest for review" : ""),
    };
    const assistantMsg: ChatMessage = {
      id: nanoid(),
      role: "assistant",
      content: "",
      isStreaming: true,
    };

    setMessages(prev => [...prev, userMsg, assistantMsg]);
    setStreaming(true);

    // Build history for LLM (exclude the streaming placeholder)
    const history = messages.map(m => ({ role: m.role, content: m.content }));

    let content = "";
    let endpoints: string[] = [];
    let rawContext: string | undefined;

    try {
      for await (const event of streamChat(message, history, model, yamlContent)) {
        if (abortRef.current) break;
        if (event.type === "meta") {
          endpoints  = event.endpoints ?? [];
          rawContext = event.rawContext;
        } else if (event.type === "token") {
          content += event.content ?? "";
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantMsg.id ? { ...m, content, isStreaming: true } : m
            )
          );
        } else if (event.type === "error") {
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantMsg.id
                ? { ...m, content: "", error: event.message, isStreaming: false }
                : m
            )
          );
          break;
        } else if (event.type === "done") {
          break;
        }
      }
    } catch (err) {
      setMessages(prev =>
        prev.map(m =>
          m.id === assistantMsg.id
            ? { ...m, content: "", error: err instanceof Error ? err.message : "Request failed", isStreaming: false }
            : m
        )
      );
    } finally {
      setMessages(prev =>
        prev.map(m =>
          m.id === assistantMsg.id
            ? { ...m, content, isStreaming: false, endpoints, rawContext }
            : m
        )
      );
      setStreaming(false);
    }
  }, [messages, model, streaming]);

  const handleNewChat = () => {
    abortRef.current = true;
    setMessages([]);
    setStreaming(false);
  };

  if (!user) {
    return (
      <div className="flex h-screen items-center justify-center" style={{ background: "var(--color-bg)" }}>
        <div className="flex items-center gap-2" style={{ color: "var(--color-muted)" }}>
          <span className="animate-spin">⎈</span>
          <span className="text-sm">Loading…</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden" style={{ background: "var(--color-bg)" }}>
      {/* Sidebar */}
      <Sidebar
        user={user}
        models={models.length > 0 ? models : [{ id: DEFAULT_MODEL, name: "GPT-4o", provider: "OpenAI" }]}
        selectedModel={model}
        onModelChange={setModel}
        onNewChat={handleNewChat}
      />

      {/* Main */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Header */}
        <header
          className="flex items-center justify-between px-6 py-3 shrink-0"
          style={{ borderBottom: "1px solid var(--color-border)" }}
        >
          <div className="flex items-center gap-2">
            <h1 className="text-sm font-semibold" style={{ color: "var(--color-text)" }}>
              Chat
            </h1>
            {streaming && (
              <span className="text-xs px-2 py-0.5 rounded-full animate-pulse"
                style={{ background: "var(--color-accent)22", color: "var(--color-accent)", border: "1px solid var(--color-accent)44" }}>
                Thinking…
              </span>
            )}
          </div>
        </header>

        {/* Messages */}
        <ChatWindow
          messages={messages}
          onExampleClick={handleSend}
        />

        {/* Input */}
        <InputBar onSend={handleSend} disabled={streaming} />
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
