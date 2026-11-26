import { useEffect, useState } from "react";
import { fetchAttempts, fetchThreads, type AttemptRow, type ThreadRow } from "../api";

const STATUS: Record<string, string> = {
  COMPLETED: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  FAILED: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  REJECTED: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  BLOCKED: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  error: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  busy: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  implemented: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  blocked: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  plan: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
};
const badge = (s: string | null | undefined) =>
  s ? <span className={`rounded px-1.5 py-0.5 text-[10px] ${STATUS[s] ?? "bg-zinc-100 dark:bg-zinc-800"}`}>{s}</span> : null;
const when = (iso: string) => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const usd = (v: string | null) => (v == null ? "n/a" : "$" + Number(v).toFixed(4));
const costCls = (v: string | null) => (v == null ? "text-zinc-400" : "text-emerald-700 dark:text-emerald-400");

export function HistoryPanel({ currentThread, refreshKey, onOpen, onWatch, watching }: { currentThread: string | null; refreshKey: number; onOpen: (assistant: string, thread: string) => void; onWatch: (attemptId: string) => void; watching: string | null }) {
  const [tab, setTab] = useState<"studio" | "pipeline">("studio");
  const [threads, setThreads] = useState<ThreadRow[] | null>(null);
  const [attempts, setAttempts] = useState<AttemptRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    fetchThreads().then((d) => setThreads(d.threads)).catch((e) => setError(String(e)));
    fetchAttempts().then((d) => { setAttempts(d.attempts); if (d.error) setError(d.error); }).catch((e) => setError(String(e)));
  }, [refreshKey]);

  useEffect(() => { // new Jira Stories appear without a reload
    if (tab !== "pipeline") return;
    const id = setInterval(() => { fetchAttempts().then((d) => setAttempts(d.attempts)).catch(() => {}); }, 5000);
    return () => clearInterval(id);
  }, [tab]);

  const rows: { cost_usd: string | null }[] = tab === "studio" ? threads ?? [] : attempts ?? [];
  const total = rows.reduce((a, r) => a + Number(r.cost_usd ?? 0), 0);
  const list = tab === "studio" ? threads : attempts;
  return (
    <div className="flex h-full min-h-0 flex-col rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex gap-1 border-b border-zinc-200 p-2 dark:border-zinc-800">
        {(["studio", "pipeline"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`flex-1 rounded px-2 py-1 text-xs ${tab === t ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900" : "bg-zinc-100 dark:bg-zinc-800"}`}>
            {t === "studio" ? `Studio runs (${threads?.length ?? "…"})` : `Jira pipeline (${attempts?.length ?? "…"})`}
          </button>
        ))}
      </div>
      <div className="flex justify-between px-3 py-1 text-[11px] text-zinc-500">
        <span>{tab === "studio" ? "langgraph dev threads" : "worker attempts (database)"}</span>
        <span>total {usd(total.toFixed(6))}</span>
      </div>
      {error && <p className="px-3 text-[11px] text-red-600">{error}</p>}
      <ul className="min-h-0 flex-1 space-y-1 overflow-auto p-2">
        {list == null && <li className="text-xs text-zinc-500">Loading…</li>}
        {list?.length === 0 && <li className="text-xs text-zinc-500">No runs yet.</li>}
        {tab === "studio" && threads?.map((t) => (
          <li key={t.thread_id}>
            <button onClick={() => t.assistant && onOpen(t.assistant, t.thread_id)} className={`w-full rounded-md border p-2 text-left text-xs ${t.thread_id === currentThread ? "border-blue-400 bg-blue-50 dark:bg-blue-950/40" : "border-zinc-200 hover:bg-zinc-50 dark:border-zinc-800 dark:hover:bg-zinc-800"}`}>
              <div className="flex items-center gap-1.5">
                <span className="font-medium">{t.assistant === "coding_planner" ? "Plan" : "Implement"}</span>
                {badge(t.status === "idle" ? t.outcome : t.status)}
                <span className="ml-auto font-mono text-emerald-700 dark:text-emerald-400">{usd(t.cost_usd)}{t.unpriced_calls ? "+?" : ""}</span>
              </div>
              <p className="mt-1 line-clamp-2 text-zinc-600 dark:text-zinc-400">{t.title || (t.status === "error" ? "Failed before the first model call" : "(empty thread)")}</p>
              <div className="mt-1 flex gap-2 whitespace-nowrap text-[10px] text-zinc-400">
                <span>{when(t.updated_at)}</span><span>{t.calls} calls</span>
                {t.todos_total > 0 && <span>todos {t.todos_done}/{t.todos_total}</span>}
                <span className="ml-auto font-mono">{t.thread_id.slice(0, 8)}</span>
              </div>
            </button>
          </li>
        ))}
        {tab === "pipeline" && attempts?.map((a) => (
          <li key={a.attempt_id} className={`rounded-md border text-xs ${watching === a.attempt_id ? "border-blue-400" : "border-zinc-200 dark:border-zinc-800"}`}>
            <button onClick={() => setOpen(open === a.attempt_id ? null : a.attempt_id)} className="w-full p-2 text-left">
              <div className="flex items-center gap-1.5">
                <span className="font-medium">{a.issue} · #{a.number}</span>
                {badge(a.status)}
                <span title={a.cost_usd == null ? "ran before cost tracking existed" : ""} className={`ml-auto font-mono ${costCls(a.cost_usd)}`}>{usd(a.cost_usd)}{a.unknown_usage_calls ? "+?" : ""}</span>
              </div>
              <div className="mt-1 flex gap-2 whitespace-nowrap text-[10px] text-zinc-400">
                <span>{when(a.created_at)}</span><span className="truncate">{a.repository}</span>
              </div>
              {a.failure_code && <div className="mt-0.5 break-all font-mono text-[10px] text-red-500">{a.failure_code}</div>}
            </button>
            {open === a.attempt_id && (
              <div className="space-y-1 border-t border-zinc-200 p-2 dark:border-zinc-800">
                <button onClick={() => onWatch(a.attempt_id)} className="w-full rounded bg-blue-600 px-2 py-1 text-white">{["COMPLETED", "FAILED", "REJECTED", "BLOCKED", "CANCELLED"].includes(a.status) ? "Open transcript" : "Watch live"}</button>
                {a.pr_url && <a href={a.pr_url} target="_blank" rel="noreferrer" className="block text-blue-600 underline">Draft PR ↗</a>}
                {a.cost_usd == null ? (
                  <p className="text-zinc-500">No cost data: this attempt ran before usage tracking existed.</p>
                ) : (
                  <>
                    {Object.entries(a.stage_costs).map(([s, c]) => <div key={s} className="flex justify-between"><span>{s}</span><span className="font-mono">{usd(c)}</span></div>)}
                    <div className="flex justify-between text-zinc-500"><span>{a.calls} calls · {a.models.join(", ")}</span><span>in {a.tokens_in.toLocaleString()} / out {a.tokens_out.toLocaleString()}</span></div>
                  </>
                )}
                <p className="font-mono text-[10px] text-zinc-400">{a.attempt_id} · repairs {a.repairs}</p>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
