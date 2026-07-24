"use client";

import { useState, useRef, KeyboardEvent, ChangeEvent } from "react";
import { Paperclip, Send, X } from "lucide-react";

interface Props {
  onSend: (message: string, yamlContent?: string) => void;
  disabled?: boolean;
}

export default function InputBar({ onSend, disabled }: Props) {
  const [text, setText] = useState("");
  const [yaml, setYaml] = useState<{ name: string; content: string } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef     = useRef<HTMLInputElement>(null);

  function autoResize() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }

  function handleKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function submit() {
    const msg = text.trim();
    if (!msg && !yaml) return;
    onSend(msg, yaml?.content);
    setText("");
    setYaml(null);
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }

  function handleFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = ev => {
      setYaml({ name: file.name, content: ev.target?.result as string });
    };
    reader.readAsText(file);
    e.target.value = "";
  }

  const canSend = (text.trim().length > 0 || yaml !== null) && !disabled;

  return (
    <div className="px-4 pb-4 md:px-8">
      <div className="mx-auto max-w-3xl">
        {/* YAML attachment pill */}
        {yaml && (
          <div className="mb-2 flex items-center gap-2">
            <span
              className="flex items-center gap-1.5 rounded-full px-3 py-1 text-xs"
              style={{ background: "var(--color-surface2)", border: "1px solid var(--color-border)", color: "var(--color-accent2)" }}
            >
              <Paperclip size={11} />
              {yaml.name}
            </span>
            <button onClick={() => setYaml(null)} style={{ color: "var(--color-muted)" }}>
              <X size={14} />
            </button>
          </div>
        )}

        <div
          className="flex items-end gap-2 rounded-2xl px-4 py-3"
          style={{ background: "var(--color-surface)", border: "1px solid var(--color-border)" }}
        >
          {/* File attach */}
          <input
            ref={fileRef}
            type="file"
            accept=".yaml,.yml"
            className="hidden"
            onChange={handleFile}
          />
          <button
            onClick={() => fileRef.current?.click()}
            className="mb-0.5 transition-colors"
            title="Attach YAML manifest"
            style={{ color: disabled ? "var(--color-border)" : "var(--color-muted)" }}
            onMouseEnter={e => { if (!disabled) e.currentTarget.style.color = "var(--color-accent)"; }}
            onMouseLeave={e => { e.currentTarget.style.color = disabled ? "var(--color-border)" : "var(--color-muted)"; }}
          >
            <Paperclip size={18} />
          </button>

          {/* Textarea */}
          <textarea
            ref={textareaRef}
            value={text}
            onChange={e => { setText(e.target.value); autoResize(); }}
            onKeyDown={handleKey}
            placeholder={yaml ? "Add a message about this manifest…" : "Ask about your cluster…"}
            rows={1}
            disabled={disabled}
            className="flex-1 resize-none bg-transparent text-sm outline-none placeholder:opacity-40"
            style={{ color: "var(--color-text)", maxHeight: "200px", lineHeight: "1.5" }}
          />

          {/* Send */}
          <button
            onClick={submit}
            disabled={!canSend}
            className="mb-0.5 flex h-8 w-8 items-center justify-center rounded-lg transition-all disabled:opacity-30"
            style={{
              background: canSend ? "var(--color-accent)" : "var(--color-surface2)",
              color: canSend ? "#fff" : "var(--color-muted)",
            }}
          >
            <Send size={15} />
          </button>
        </div>

        <p className="mt-1.5 text-center text-[10px]" style={{ color: "var(--color-muted)" }}>
          Shift+Enter for new line · attach .yaml for manifest review
        </p>
      </div>
    </div>
  );
}
