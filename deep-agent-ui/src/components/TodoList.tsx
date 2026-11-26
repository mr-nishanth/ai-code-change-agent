import type { Todo } from "../config";

const STYLE = {
  pending: { icon: "○", text: "text-zinc-600 dark:text-zinc-400", box: "bg-zinc-50 dark:bg-zinc-900", iconCls: "text-zinc-400" },
  in_progress: { icon: "◉", text: "text-amber-800 dark:text-amber-300", box: "bg-amber-50 border-amber-200 dark:bg-amber-950/40 dark:border-amber-900", iconCls: "text-amber-500 animate-pulse" },
  completed: { icon: "✓", text: "text-green-800 line-through dark:text-green-300", box: "bg-green-50 border-green-200 dark:bg-green-950/40 dark:border-green-900", iconCls: "text-green-500" },
} as const;

export function TodoList({ todos, isLoading }: { todos: Todo[]; isLoading: boolean }) {
  if (todos.length === 0) {
    return (
      <Panel title="Todos">
        <p className="text-xs text-zinc-500">
          {isLoading ? "⟳ Agent is working; todos appear when it calls write_todos." : "No plan yet. The implementation assistant writes todos; planning returns a structured plan instead."}
        </p>
      </Panel>
    );
  }
  const done = todos.filter((t) => t.status === "completed").length;
  const pct = Math.round((done / todos.length) * 100);
  let pulsed = false;
  return (
    <Panel title="Todos" right={`${done}/${todos.length}`}>
      <div className="h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
        <div className="h-full rounded-full bg-green-500 transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
      <ul className="mt-3 space-y-1.5">
        {todos.map((t, i) => {
          const s = STYLE[t.status] ?? STYLE.pending;
          const pulse = t.status === "in_progress" && !pulsed;
          if (pulse) pulsed = true;
          return (
            <li key={i} className={`flex items-start gap-2 rounded-md border border-transparent px-2 py-1.5 transition-colors ${s.box}`}>
              <span className={`leading-5 ${pulse ? s.iconCls : s.iconCls.replace("animate-pulse", "")}`}>{s.icon}</span>
              <span className={`text-sm ${s.text}`}>{t.content}</span>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

export function Panel({ title, right, children }: { title: string; right?: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-3 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">{title}</h2>
        {right && <span className="text-xs text-zinc-500">{right}</span>}
      </div>
      {children}
    </section>
  );
}
