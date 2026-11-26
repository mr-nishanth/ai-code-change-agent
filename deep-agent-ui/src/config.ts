export const API_URL = import.meta.env.VITE_AGENT_URL ?? "http://127.0.0.1:2024";

export const ASSISTANTS = {
  coding_agent: "Implementation (writable sandbox, todos)",
  coding_planner: "Planning (read-only sandbox)",
} as const;
export type AssistantId = keyof typeof ASSISTANTS;

export type Todo = { content: string; status: "pending" | "in_progress" | "completed" };

export interface AgentState {
  messages: unknown[];
  todos?: Todo[];
  structured_response?: Record<string, unknown>;
}
