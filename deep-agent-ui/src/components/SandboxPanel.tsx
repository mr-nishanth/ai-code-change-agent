import { useEffect, useState } from "react";
import { fetchFile, fetchTree, type FileView, type Tree } from "../api";

export function SandboxPanel({ threadId, isLoading, tick, prefix: prefixProp, title = "Sandbox /workspace", emptyText = "starts on first run" }: { threadId: string | null; isLoading: boolean; tick: number; prefix?: string; title?: string; emptyText?: string }) {
  const prefix = prefixProp ?? (threadId ? `/sandbox/${threadId}` : null);
  const [tree, setTree] = useState<Tree | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<FileView | null>(null);
  const [mode, setMode] = useState<"code" | "diff">("code");

  useEffect(() => {
    if (!prefix) { setTree(null); return; }
    let stop = false;
    const load = () => fetchTree(prefix).then((t) => !stop && setTree(t)).catch(() => {});
    load();
    const id = isLoading ? setInterval(load, 1500) : undefined;
    return () => { stop = true; if (id) clearInterval(id); };
  }, [prefix, isLoading, tick]);

  useEffect(() => {
    if (!prefix || !selected) { setFile(null); return; }
    let stop = false;
    fetchFile(prefix, selected).then((f) => !stop && setFile(f)).catch(() => setFile(null));
    return () => { stop = true; };
  }, [prefix, selected, tree]);

  const files = tree?.entries.filter((e) => e.type === "file") ?? [];
  const changed = files.filter((f) => f.change).length + (tree?.deleted.length ?? 0);
  return (
    <div className="flex h-full min-h-0 flex-col rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-center justify-between border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">{title}</h2>
        <span className="text-xs text-zinc-500">{tree ? `${files.length} files · ${changed} changed` : emptyText}</span>
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,2fr)_minmax(0,5fr)]">
        <ul className="min-h-0 overflow-auto border-r border-zinc-200 p-1 font-mono text-xs dark:border-zinc-800">
          {tree?.entries.map((e) => (
            <li key={e.path}>
              <button
                disabled={e.type !== "file"}
                onClick={() => setSelected(e.path)}
                style={{ paddingLeft: `${e.path.split("/").length * 0.6}rem` }}
                className={`flex w-full items-center gap-1 rounded py-0.5 pr-1 text-left ${selected === e.path ? "bg-blue-100 dark:bg-blue-950" : "hover:bg-zinc-100 dark:hover:bg-zinc-800"}`}
              >
                <span>{e.type === "directory" ? "▸" : " "}</span>
                <span className={e.change === "added" ? "text-green-600" : e.change === "modified" ? "text-amber-600" : ""}>{e.name}</span>
                {e.change && <span className="ml-auto text-[10px]">{e.change === "added" ? "A" : "M"}</span>}
              </button>
            </li>
          ))}
          {tree?.deleted.map((p) => <li key={p} className="px-1 text-red-500 line-through">{p}</li>)}
        </ul>
        <div className="flex min-h-0 flex-col">
          {file ? (
            <>
              <div className="flex items-center gap-2 border-b border-zinc-200 px-2 py-1 text-xs dark:border-zinc-800">
                <span className="truncate font-mono">{file.path}</span>
                <div className="ml-auto flex gap-1">
                  {(["code", "diff"] as const).map((m) => (
                    <button key={m} onClick={() => setMode(m)} className={`rounded px-2 py-0.5 ${mode === m ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900" : "bg-zinc-100 dark:bg-zinc-800"}`}>{m}</button>
                  ))}
                </div>
              </div>
              <pre className="min-h-0 flex-1 overflow-auto p-2 font-mono text-xs leading-5">
                {mode === "code" ? (file.content ?? "(deleted)") : file.diff ? file.diff.split("\n").map((l, i) => (
                  <div key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "bg-green-100 text-green-900 dark:bg-green-950 dark:text-green-200" : l.startsWith("-") && !l.startsWith("---") ? "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200" : l.startsWith("@@") ? "text-blue-600" : ""}>{l || " "}</div>
                )) : "(no changes)"}
              </pre>
            </>
          ) : (
            <p className="p-3 text-xs text-zinc-500">Select a file. Green = added, amber = modified (vs. the seed baseline).</p>
          )}
        </div>
      </div>
    </div>
  );
}
