import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { AIMessage, HumanMessage, ToolMessage, type BaseMessage } from "@langchain/core/messages";
import type { AssembledToolCall } from "@langchain/react";

type ToolCallLike = { id?: string; name: string; args: unknown };

export function Transcript({ messages, toolCalls, isLoading }: { messages: BaseMessage[]; toolCalls: AssembledToolCall[]; isLoading: boolean }) {
  const results = new Map<string, ToolMessage>();
  for (const m of messages) if (ToolMessage.isInstance(m)) results.set(m.tool_call_id, m);
  const live = new Map(toolCalls.map((t) => [t.callId, t]));
  return (
    <div className="space-y-3">
      {messages.map((m, i) => {
        if (HumanMessage.isInstance(m)) {
          return (
            <div key={m.id ?? i} className="ml-auto max-w-[85%] rounded-lg bg-blue-600 px-3 py-2 text-sm whitespace-pre-wrap text-white">
              {m.text}
            </div>
          );
        }
        if (AIMessage.isInstance(m)) {
          const calls = (m.tool_calls ?? []) as ToolCallLike[];
          return (
            <div key={m.id ?? i} className="space-y-2">
              {m.text.trim() && (
                <div className="prose-msg max-w-[95%] rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-900">
                  <ReactMarkdown>{m.text}</ReactMarkdown>
                </div>
              )}
              {calls.map((c, j) => (
                <ToolCard key={c.id ?? j} call={c} result={c.id ? results.get(c.id) : undefined} live={c.id ? live.get(c.id) : undefined} />
              ))}
              {m.usage_metadata && (
                <div className="text-[10px] text-zinc-400">
                  in {m.usage_metadata.input_tokens.toLocaleString()} · out {m.usage_metadata.output_tokens.toLocaleString()} tokens
                </div>
              )}
            </div>
          );
        }
        return null;
      })}
      {isLoading && <div className="text-xs text-zinc-500"><span className="animate-pulse">●</span> streaming…</div>}
    </div>
  );
}

const ICON: Record<string, string> = {
  ls: "📁", read_file: "📄", write_file: "✏️", edit_file: "✏️", glob: "🔎", grep: "🔎",
  execute: "▶", write_todos: "☑", fetch_reference: "🌐", task: "🧑‍🔧",
  ImplementationPlan: "🗺", ImplementationReport: "📋",
};

function summary(call: ToolCallLike): string {
  const a = (call.args ?? {}) as Record<string, unknown>;
  const v = a.command ?? a.file_path ?? a.path ?? a.pattern ?? a.url ?? a.summary ?? "";
  return String(v).slice(0, 120);
}

function ToolCard({ call, result, live }: { call: ToolCallLike; result?: ToolMessage; live?: AssembledToolCall }) {
  const [open, setOpen] = useState(false);
  const status = result ? ((result as { status?: string }).status === "error" ? "error" : "finished") : live?.status ?? "running";
  const color = status === "error" ? "border-red-300 dark:border-red-900" : status === "finished" ? "border-zinc-200 dark:border-zinc-800" : "border-blue-300 dark:border-blue-800";
  const out = result ? (typeof result.content === "string" ? result.content : JSON.stringify(result.content, null, 2)) : live?.output != null ? JSON.stringify(live.output, null, 2) : "";
  return (
    <div className={`max-w-[95%] rounded-md border bg-zinc-50 text-xs dark:bg-zinc-900/60 ${color}`}>
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 px-2 py-1.5 text-left">
        <span>{ICON[call.name] ?? "🔧"}</span>
        <span className="font-mono font-semibold">{call.name}</span>
        <span className="truncate font-mono text-zinc-500">{summary(call)}</span>
        <span className="ml-auto shrink-0 text-zinc-400">{status === "running" ? "⟳" : status === "error" ? "✗" : "✓"}</span>
      </button>
      {open && (
        <div className="space-y-1 border-t border-zinc-200 p-2 dark:border-zinc-800">
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap text-zinc-600 dark:text-zinc-400">{JSON.stringify(call.args, null, 2)}</pre>
          {out && <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded bg-zinc-900 p-2 text-zinc-100">{out.slice(0, 20000)}</pre>}
        </div>
      )}
    </div>
  );
}
