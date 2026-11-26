import { useEffect, useMemo, useState } from "react";
import { useStream } from "@langchain/react";
import { fetchInfo, priceCalls, type Cost, type Info } from "./api";
import { API_URL, ASSISTANTS, type AgentState, type AssistantId, type Todo } from "./config";
import { usageOf } from "./usage";
import { Panel, TodoList } from "./components/TodoList";
import { SubagentPanel } from "./components/Subagents";
import { Transcript } from "./components/Transcript";
import { SandboxPanel } from "./components/SandboxPanel";
import { HistoryPanel } from "./components/History";
import { PipelineLive } from "./components/PipelineLive";

const EXAMPLE = "Add a farewell(name) function next to greet() that returns 'Goodbye, <name>!', with a pytest test. Track your steps with write_todos, then run python -m pytest -q.";

function readHash(): { assistant: AssistantId; thread: string | null } {
  const p = new URLSearchParams(location.hash.slice(1));
  const a = p.get("assistant");
  return { assistant: a && a in ASSISTANTS ? (a as AssistantId) : "coding_agent", thread: p.get("thread") };
}

export default function App() {
  const initial = readHash();
  const [assistant, setAssistant] = useState<AssistantId>(initial.assistant);
  const [threadId, setThreadId] = useState<string | null>(initial.thread);
  const [session, setSession] = useState(0); // remount only for a new thread or assistant
  useEffect(() => {
    location.hash = new URLSearchParams({ assistant, ...(threadId ? { thread: threadId } : {}) }).toString();
  }, [assistant, threadId]);
  return (
    <Studio
      key={`${assistant}:${session}`}
      assistant={assistant}
      threadId={threadId}
      onThread={setThreadId}
      onNewThread={() => { setThreadId(null); setSession((s) => s + 1); }}
      onAssistant={(a) => { setThreadId(null); setAssistant(a); setSession((s) => s + 1); }}
      onOpen={(a, t) => { if (a in ASSISTANTS) { setAssistant(a as AssistantId); setThreadId(t); setSession((s) => s + 1); } }}
    />
  );
}

function Studio({ assistant, threadId, onThread, onNewThread, onAssistant, onOpen }: { assistant: AssistantId; threadId: string | null; onThread: (t: string) => void; onNewThread: () => void; onAssistant: (a: AssistantId) => void; onOpen: (assistant: string, thread: string) => void }) {
  const [showHistory, setShowHistory] = useState(() => window.innerWidth >= 1280);
  const [wide, setWide] = useState(() => window.innerWidth >= 1536);
  useEffect(() => {
    const onResize = () => setWide(window.innerWidth >= 1536);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  const [watch, setWatch] = useState<string | null>(null);
  // threadId from the URL is read once (rejoin after reload); later ids come from onThreadId.
  const [initialThread] = useState(threadId);
  const stream = useStream<AgentState>({ apiUrl: API_URL, assistantId: assistant, threadId: initialThread, onThreadId: onThread });
  const liveThread = stream.threadId ?? threadId;
  const [input, setInput] = useState(EXAMPLE);
  const [info, setInfo] = useState<Info | null>(null);
  const [cost, setCost] = useState<Cost | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => { fetchInfo().then(setInfo).catch(() => setInfo(null)); }, []);
  const todos = (stream.values?.todos ?? []) as Todo[];
  const result = stream.values?.structured_response;
  const calls = useMemo(() => usageOf(stream.messages, info?.model ?? "unknown"), [stream.messages, info]);
  const totals = calls.reduce((a, c) => ({ in: a.in + c.input_tokens + c.cache_read_tokens + c.cache_write_5m_tokens + c.cache_write_1h_tokens, out: a.out + c.output_tokens }), { in: 0, out: 0 });

  useEffect(() => {
    if (calls.length === 0) { setCost(null); return; }
    const t = setTimeout(() => priceCalls(calls).then(setCost).catch(() => setCost(null)), 400);
    return () => clearTimeout(t);
  }, [calls]);
  useEffect(() => { if (!stream.isLoading) setTick((x) => x + 1); }, [stream.isLoading]);

  const subagentCount = stream.subagents.size;
  const hasInfo = todos.length > 0 || stream.isLoading || subagentCount > 0 || !!result || stream.error != null;
  const infoPanels = (
    <>
      {(todos.length > 0 || stream.isLoading) && <TodoList todos={todos} isLoading={stream.isLoading} />}
      {subagentCount > 0 ? <SubagentPanel stream={stream} /> : wide && (
        <p className="px-1 text-[11px] text-zinc-500">No subagents: this project runs one Deep Agent (the <code>task</code> tool is disabled).</p>
      )}
      {result && (wide ? (
        <Panel title="Structured result">
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(result, null, 2)}</pre>
        </Panel>
      ) : (
        <details className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs dark:border-zinc-800 dark:bg-zinc-900">
          <summary className="cursor-pointer font-semibold uppercase tracking-wide text-zinc-500">
            Structured result{typeof result.outcome === "string" ? ` · ${result.outcome}` : ""}
          </summary>
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap">{JSON.stringify(result, null, 2)}</pre>
        </details>
      ))}
      {stream.error != null && (
        <Panel title="Error"><p className="text-xs text-red-600">{String((stream.error as Error)?.message ?? stream.error)}</p></Panel>
      )}
    </>
  );

  const submit = () => {
    const text = input.trim();
    if (!text || stream.isLoading) return;
    void stream.submit({ messages: [{ type: "human", content: text }] }, { config: { recursion_limit: 250 } });
    setInput("");
  };

  return (
    <div className="flex h-screen min-w-0 flex-col overflow-hidden">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-zinc-200 bg-white px-4 py-2 dark:border-zinc-800 dark:bg-zinc-900">
        <h1 className="font-semibold">Deep Agent Studio</h1>
        <select value={assistant} onChange={(e) => onAssistant(e.target.value as AssistantId)} className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700">
          {Object.entries(ASSISTANTS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
        <span className="text-xs text-zinc-500">{info ? `${info.provider} · ${info.model} · seed ${info.seed}` : "agent server offline?"}</span>
        <span className={`rounded px-2 py-0.5 text-xs ${stream.isLoading ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300" : "bg-zinc-100 dark:bg-zinc-800"}`}>{stream.isLoading ? "running" : "idle"}</span>
        <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
          <span title="provider-reported tokens">tokens in {totals.in.toLocaleString()} · out {totals.out.toLocaleString()}</span>
          <span title={cost ? `${cost.calls} model calls, ${cost.pricing_version}` : ""} className="rounded bg-emerald-100 px-2 py-0.5 font-mono text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300">
            LLM ${cost ? Number(cost.total_usd).toFixed(4) : "0.0000"}{cost && cost.unpriced_calls > 0 ? "+?" : ""}
          </span>
          {liveThread && <span className="font-mono text-zinc-400">{liveThread.slice(0, 8)}</span>}
          <button onClick={() => setShowHistory(!showHistory)} className="rounded border border-zinc-300 px-2 py-1 dark:border-zinc-700">{showHistory ? "Hide history" : "History"}</button>
          <button onClick={onNewThread} className="rounded border border-zinc-300 px-2 py-1 dark:border-zinc-700">New thread</button>
        </div>
      </header>

      <main className={`grid min-h-0 min-w-0 flex-1 gap-3 p-3 ${gridCols(showHistory, wide, !!watch)}`}>
        {showHistory && <HistoryPanel currentThread={liveThread} refreshKey={tick} onOpen={onOpen} onWatch={setWatch} watching={watch} />}
        {watch ? <PipelineLive key={watch} attemptId={watch} onClose={() => setWatch(null)} /> : <>
        {wide && <aside className="min-h-0 space-y-3 overflow-auto">{infoPanels}</aside>}

        <section className="flex min-h-0 min-w-0 flex-col rounded-lg border border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950">
          <div className="min-h-0 flex-1 overflow-auto p-3">
            {!wide && hasInfo && <div className="mb-3 space-y-2">{infoPanels}</div>}
            {stream.messages.length === 0 ? (
              <p className="text-sm text-zinc-500">Describe a change. The agent works inside an offline Docker sandbox seeded with the sample service; files update live on the right.</p>
            ) : (
              <Transcript messages={stream.messages} toolCalls={stream.toolCalls} isLoading={stream.isLoading} />
            )}
          </div>
          <div className="flex gap-2 border-t border-zinc-200 p-2 dark:border-zinc-800">
            <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }} rows={3} className="flex-1 resize-none rounded border border-zinc-300 bg-white p-2 text-sm dark:border-zinc-700 dark:bg-zinc-900" placeholder="Describe the change (⌘/Ctrl+Enter)" />
            {stream.isLoading ? (
              <button onClick={() => void stream.stop()} className="rounded bg-red-600 px-4 text-sm text-white">Stop</button>
            ) : (
              <button onClick={submit} disabled={!input.trim()} className="rounded bg-blue-600 px-4 text-sm text-white disabled:opacity-40">Run</button>
            )}
          </div>
        </section>

        <SandboxPanel threadId={liveThread} isLoading={stream.isLoading} tick={tick} />
        </>}
      </main>
    </div>
  );
}

/** Column layout: history | info | transcript | sandbox. Narrow screens fold the info column
 * into the transcript column (`StudioInfo` renders above the messages). */
function gridCols(history: boolean, wide: boolean, watching: boolean): string {
  if (watching) return history ? "grid-cols-[15rem_16rem_minmax(0,1fr)_minmax(0,1fr)]" : "grid-cols-[17rem_minmax(0,1fr)_minmax(0,1fr)]";
  if (history) return wide ? "grid-cols-[16rem_16rem_minmax(0,1fr)_minmax(0,1fr)]" : "grid-cols-[15rem_minmax(0,1.2fr)_minmax(0,1fr)]";
  return wide ? "grid-cols-[18rem_minmax(0,1fr)_minmax(0,1.1fr)]" : "grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]";
}
