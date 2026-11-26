import { API_URL } from "./config";

export type TreeEntry = {
  path: string;
  name: string;
  type: "file" | "directory";
  size: number;
  change?: "added" | "modified";
};
export type Tree = { entries: TreeEntry[]; deleted: string[]; sandbox: string };
export type FileView = { path: string; content: string | null; original: string | null; diff: string };
export type Info = { provider: string; model: string; seed: string; image: string; pricing_version: string };
export type CallUsage = {
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_write_5m_tokens: number;
  cache_write_1h_tokens: number;
  cache_read_tokens: number;
};
export type Cost = { total_usd: string; calls: number; unpriced_calls: number; pricing_version: string };

async function get<T>(path: string): Promise<T | null> {
  const res = await fetch(API_URL + path);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return (await res.json()) as T;
}

export const fetchInfo = () => get<Info>("/studio/info");
/** prefix: `/sandbox/<thread>` (studio) or `/studio/attempts/<id>` (worker attempt). */
export const fetchTree = (prefix: string) => get<Tree>(`${prefix}/tree`);
export const fetchFile = (prefix: string, path: string) =>
  get<FileView>(`${prefix}/file?path=${encodeURIComponent(path)}`);

export type StoredMessage = { type: string; data: Record<string, unknown> };
export type LivePhase = {
  thread_id: string; phase: string; stage: string; repairs: number; messages: StoredMessage[];
  todos: { content: string; status: "pending" | "in_progress" | "completed" }[];
  result: Record<string, unknown> | null; running: boolean; updated_at: string | null;
};
export type LiveAttempt = {
  attempt: { attempt_id: string; issue: string; number: number; status: string; failure_code: string | null; repairs: number; repository: string; created_at: string; updated_at: string };
  phases: LivePhase[];
  terminal: boolean;
};
export const fetchLive = (id: string) => get<LiveAttempt>(`/studio/attempts/${id}/live`);

export type ThreadRow = {
  thread_id: string; assistant: string | null; status: string; created_at: string; updated_at: string;
  title: string; outcome: string | null; todos_done: number; todos_total: number; calls: number;
  tokens_in: number; tokens_out: number; cost_usd: string; unpriced_calls: number;
};
export type AttemptRow = {
  attempt_id: string; issue: string; number: number; repository: string; status: string;
  failure_code: string | null; repairs: number; created_at: string; updated_at: string; calls: number;
  unknown_usage_calls: number; tokens_in: number; tokens_out: number; cost_usd: string | null;
  stage_costs: Record<string, string>; models: string[]; pr_url: string | null;
};
export const fetchThreads = async () => (await get<{ threads: ThreadRow[] }>("/studio/threads")) ?? { threads: [] };
export const fetchAttempts = async () =>
  (await get<{ attempts: AttemptRow[]; error?: string }>("/studio/attempts")) ?? { attempts: [] };

export async function priceCalls(calls: CallUsage[]): Promise<Cost> {
  const res = await fetch(API_URL + "/studio/cost", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ calls }),
  });
  if (!res.ok) throw new Error(`cost ${res.status}`);
  return (await res.json()) as Cost;
}
