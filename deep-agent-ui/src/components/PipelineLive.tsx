import { useEffect, useMemo, useState } from "react";
import { mapStoredMessagesToChatMessages, type StoredMessage as LcStored } from "@langchain/core/messages";
import { fetchAttempts, fetchLive, type AttemptRow, type LiveAttempt } from "../api";
import { Panel, TodoList } from "./TodoList";
import { Transcript } from "./Transcript";
import { SandboxPanel } from "./SandboxPanel";

const FLOW = ["PENDING", "PREPARING", "PLANNING", "IMPLEMENTING", "VALIDATING", "DELIVERING", "REPORTING", "COMPLETED"];
const BAD = new Set(["FAILED", "REJECTED", "BLOCKED", "CANCELLED"]);

export function PipelineLive({ attemptId, onClose }: { attemptId: string; onClose: () => void }) {
  const [live, setLive] = useState<LiveAttempt | null>(null);
  const [row, setRow] = useState<AttemptRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const [l, a] = await Promise.all([fetchLive(attemptId), fetchAttempts()]);
        if (stop) return;
        setLive(l);
        setRow(a.attempts.find((x) => x.attempt_id === attemptId) ?? null);
        setError(null);
        setTick((t) => t + 1);
        if (l && !l.terminal) timer = setTimeout(load, 3000); // poll while the worker runs
      } catch (e) {
        if (!stop) { setError(String(e)); timer = setTimeout(load, 5000); }
      }
    };
    void load();
    return () => { stop = true; if (timer) clearTimeout(timer); };
  }, [attemptId]);

  const phases = live?.phases ?? [];
  const current = phases.find((p) => p.thread_id === tab) ?? phases.at(-1);
  const messages = useMemo(
    () => (current ? mapStoredMessagesToChatMessages(current.messages as unknown as LcStored[]) : []),
    [current],
  );
  const a = live?.attempt;
  const running = !!live && !live.terminal;
  const step = a ? FLOW.indexOf(a.status) : -1;

  return (
    <>
      <aside className="min-h-0 space-y-3 overflow-auto">
        <Panel title="Jira pipeline (live)" right={running ? "● live · 3 s" : "finished"}>
          {a ? (
            <div className="space-y-2 text-xs">
              <div className="flex items-center justify-between">
                <span className="text-sm font-semibold">{a.issue} · attempt #{a.number}</span>
                <button onClick={onClose} className="rounded border border-zinc-300 px-2 py-0.5 dark:border-zinc-700">Close</button>
              </div>
              <div className="text-zinc-500">{a.repository} · repairs {a.repairs}</div>
              <ol className="space-y-1">
                {FLOW.map((s, i) => {
                  const done = BAD.has(a.status) ? false : i < step || a.status === "COMPLETED";
                  const now = s === a.status && running;
                  return (
                    <li key={s} className={`flex items-center gap-2 ${done ? "text-green-600" : now ? "font-semibold text-blue-600" : "text-zinc-400"}`}>
                      <span>{done ? "✓" : now ? "◉" : "○"}</span>{s.toLowerCase()}
                      {now && <span className="animate-pulse">…</span>}
                    </li>
                  );
                })}
                {BAD.has(a.status) && <li className="font-semibold text-red-600">✗ {a.status.toLowerCase()} {a.failure_code ? `(${a.failure_code})` : ""}</li>}
              </ol>
            </div>
          ) : <p className="text-xs text-zinc-500">{error ?? "Loading…"}</p>}
        </Panel>
        {row && (
          <Panel title="LLM cost" right={row.cost_usd == null ? "n/a" : `$${Number(row.cost_usd).toFixed(4)}`}>
            <div className="space-y-1 text-xs">
              {Object.entries(row.stage_costs).map(([s, c]) => <div key={s} className="flex justify-between"><span>{s}</span><span className="font-mono">${Number(c).toFixed(4)}</span></div>)}
              <div className="text-zinc-500">{row.calls} calls · in {row.tokens_in.toLocaleString()} / out {row.tokens_out.toLocaleString()}</div>
              {row.pr_url && <a href={row.pr_url} target="_blank" rel="noreferrer" className="block text-blue-600 underline">Draft PR ↗</a>}
            </div>
          </Panel>
        )}
        {current && <TodoList todos={current.todos} isLoading={current.running} />}
        {current?.result && (
          <Panel title={`${current.stage} result`}>
            <pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(current.result, null, 2)}</pre>
          </Panel>
        )}
      </aside>

      <section className="flex min-h-0 flex-col rounded-lg border border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950">
        <div className="flex gap-1 border-b border-zinc-200 p-2 dark:border-zinc-800">
          {phases.length === 0 && <span className="text-xs text-zinc-500">No agent activity yet (preparing workspace…)</span>}
          {phases.map((p) => (
            <button key={p.thread_id} onClick={() => setTab(p.thread_id)} className={`rounded px-2 py-1 text-xs ${current?.thread_id === p.thread_id ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900" : "bg-zinc-100 dark:bg-zinc-800"}`}>
              {p.stage}{p.running ? " ●" : ""}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-3">
          <Transcript messages={messages} toolCalls={[]} isLoading={!!current?.running} />
        </div>
        <div className="border-t border-zinc-200 px-3 py-1 text-[10px] text-zinc-500 dark:border-zinc-800">
          Read-only view of the worker's checkpoints; refreshed every 3 s while the attempt runs (step-level, not token streaming).
        </div>
      </section>

      <SandboxPanel threadId={null} prefix={`/studio/attempts/${attemptId}`} isLoading={running} tick={tick} title="Worker workspace" emptyText={running ? "appears while a phase runs" : "removed after the attempt"} />
    </>
  );
}
