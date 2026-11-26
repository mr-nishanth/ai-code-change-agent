import { useState } from "react";
import { AIMessage } from "@langchain/core/messages";
import { useMessages, useToolCalls, type AnyStream, type SubagentDiscoverySnapshot } from "@langchain/react";
import { Panel } from "./TodoList";

const BADGE = {
  running: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  complete: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  error: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
} as const;

export function SubagentPanel({ stream }: { stream: AnyStream }) {
  const subagents = [...stream.subagents.values()];
  if (subagents.length === 0) {
    return (
      <Panel title="Subagents">
        <p className="text-xs text-zinc-500">
          None. This project runs one Deep Agent; the <code>task</code> tool is disabled by design. Cards appear here if a graph delegates to subagents.
        </p>
      </Panel>
    );
  }
  const done = subagents.filter((s) => s.status === "complete").length;
  return (
    <Panel title="Subagents" right={`${done}/${subagents.length} complete`}>
      <div className="mb-2 h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
        <div className="h-full bg-blue-500 transition-all" style={{ width: `${Math.round((done / subagents.length) * 100)}%` }} />
      </div>
      <div className="space-y-2">
        {subagents.map((s) => <SubagentCard key={s.id} stream={stream} subagent={s} />)}
      </div>
    </Panel>
  );
}

export function SubagentCard({ stream, subagent }: { stream: AnyStream; subagent: SubagentDiscoverySnapshot }) {
  const [open, setOpen] = useState(subagent.status === "running");
  const messages = useMessages(stream, subagent);
  const tools = useToolCalls(stream, subagent);
  const last = messages.filter((m) => AIMessage.isInstance(m)).at(-1);
  const text = last?.text || (typeof subagent.output === "string" ? subagent.output : "");
  return (
    <div className="rounded-md border border-zinc-200 dark:border-zinc-800">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center justify-between px-3 py-2 text-left">
        <div>
          <div className="text-sm font-medium capitalize">{subagent.name}</div>
          <div className="text-xs text-zinc-500">{tools.length} tool call{tools.length === 1 ? "" : "s"}</div>
        </div>
        <span className={`rounded px-2 py-0.5 text-xs ${BADGE[subagent.status]}`}>{subagent.status}</span>
      </button>
      {open && (
        <div className="border-t border-zinc-200 px-3 py-2 text-xs dark:border-zinc-800">
          {subagent.taskInput && <p className="mb-1 text-zinc-500">Task: {subagent.taskInput}</p>}
          {subagent.error ? <p className="text-red-600">{subagent.error}</p> : <p className="line-clamp-6 whitespace-pre-wrap">{text}{subagent.status === "running" && <span className="ml-0.5 inline-block h-3 w-1 animate-pulse bg-blue-500" />}</p>}
        </div>
      )}
    </div>
  );
}
