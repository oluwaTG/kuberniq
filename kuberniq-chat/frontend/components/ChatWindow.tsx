"use client";

import { useState, useRef, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.css";
import type { ChatMessage } from "@/lib/types";

interface Props {
  messages: ChatMessage[];
  onRetry?: () => void;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <button
      onClick={copy}
      className="absolute right-2 top-2 rounded px-2 py-0.5 text-xs transition-colors"
      style={{ background: "var(--color-surface)", color: copied ? "var(--color-accent2)" : "var(--color-muted)" }}
    >
      {copied ? "Copied!" : "Copy"}
    </button>
  );
}

function EndpointBadges({ endpoints }: { endpoints: string[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {endpoints.map(ep => (
        <span
          key={ep}
          className="rounded px-1.5 py-0.5 font-mono text-[10px]"
          style={{ background: "var(--color-surface2)", color: "var(--color-muted)", border: "1px solid var(--color-border)" }}
        >
          {ep}
        </span>
      ))}
    </div>
  );
}

function AssistantMessage({ msg }: { msg: ChatMessage }) {
  const [showRaw, setShowRaw] = useState(false);

  return (
    <div className="flex gap-3 py-4 px-2 group">
      {/* Avatar */}
      <div
        className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-sm"
        style={{ background: "var(--color-surface2)", border: "1px solid var(--color-border)", color: "var(--color-accent)" }}
      >
        ⎈
      </div>

      <div className="min-w-0 flex-1">
        {msg.isStreaming && !msg.content ? (
          /* Typing indicator */
          <div className="flex items-center gap-1 py-1">
            {[0, 150, 300].map(d => (
              <span
                key={d}
                className="h-1.5 w-1.5 rounded-full animate-bounce"
                style={{ background: "var(--color-muted)", animationDelay: `${d}ms` }}
              />
            ))}
          </div>
        ) : msg.error ? (
          <p className="text-sm" style={{ color: "var(--color-danger)" }}>⚠ {msg.error}</p>
        ) : (
          <div className="prose-chat">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeHighlight]}
              components={{
                pre({ children }) {
                  const text = typeof children === "string" ? children : "";
                  return (
                    <div className="relative">
                      <pre>{children}</pre>
                      <CopyButton text={text} />
                    </div>
                  );
                },
              }}
            >
              {msg.content}
            </ReactMarkdown>
          </div>
        )}

        {/* Endpoints + raw context toggle */}
        {!msg.isStreaming && msg.endpoints && msg.endpoints.length > 0 && (
          <div className="mt-2">
            <EndpointBadges endpoints={msg.endpoints} />
            {msg.rawContext && (
              <button
                onClick={() => setShowRaw(v => !v)}
                className="mt-1.5 text-xs transition-colors"
                style={{ color: "var(--color-muted)" }}
              >
                {showRaw ? "▲ Hide raw context" : "▼ Show raw MCP context"}
              </button>
            )}
            {showRaw && msg.rawContext && (
              <pre
                className="mt-2 overflow-auto rounded-lg p-3 text-xs"
                style={{ background: "#0a0d14", border: "1px solid var(--color-border)", color: "var(--color-muted)", maxHeight: "20rem" }}
              >
                {msg.rawContext}
              </pre>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function UserMessage({ msg }: { msg: ChatMessage }) {
  return (
    <div className="flex justify-end py-3 px-2">
      <div
        className="max-w-[75%] rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm"
        style={{ background: "var(--color-accent)", color: "#fff" }}
      >
        <p className="whitespace-pre-wrap leading-relaxed">{msg.content}</p>
      </div>
    </div>
  );
}

const EXAMPLE_PROMPTS = [
  "What is going on in the dev namespace?",
  "List all pods that have a Dapr sidecar container",
  "Troubleshoot the api-gateway service in staging",
  "Show me logs from the payments service in the last 2 hours",
  "Which deployments have fewer ready replicas than desired?",
  "Are there any HPA scaling issues across all namespaces?",
];

export default function ChatWindow({ messages, onExampleClick }: Props & { onExampleClick: (p: string) => void }) {
  const bottomRef = useRef<HTMLDivElement>(null);

  const isEmpty = messages.length === 0;

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex-1 overflow-y-auto px-4 md:px-8">
        <div className="mx-auto max-w-3xl">
          {isEmpty ? (
            /* Empty state */
            <div className="flex flex-col items-center justify-center py-24 gap-8">
              <div className="flex flex-col items-center gap-3">
                <div
                  className="flex h-16 w-16 items-center justify-center rounded-2xl text-4xl"
                  style={{ background: "var(--color-surface)", border: "1px solid var(--color-border)" }}
                >
                  ⎈
                </div>
                <div className="text-center">
                  <h2 className="text-xl font-semibold" style={{ color: "var(--color-text)" }}>
                    Kuberniq Chat
                  </h2>
                  <p className="mt-1 text-sm" style={{ color: "var(--color-muted)" }}>
                    Ask anything about your Kubernetes clusters
                  </p>
                </div>
              </div>

              <div className="grid w-full grid-cols-1 gap-2 sm:grid-cols-2">
                {EXAMPLE_PROMPTS.map(p => (
                  <button
                    key={p}
                    onClick={() => onExampleClick(p)}
                    className="rounded-xl px-4 py-3 text-left text-sm transition-colors"
                    style={{
                      background: "var(--color-surface)",
                      border: "1px solid var(--color-border)",
                      color: "var(--color-text)",
                    }}
                    onMouseEnter={e => (e.currentTarget.style.borderColor = "var(--color-accent)")}
                    onMouseLeave={e => (e.currentTarget.style.borderColor = "var(--color-border)")}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="py-6">
              {messages.map(msg =>
                msg.role === "user"
                  ? <UserMessage key={msg.id} msg={msg} />
                  : <AssistantMessage key={msg.id} msg={msg} />
              )}
              <div ref={bottomRef} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
