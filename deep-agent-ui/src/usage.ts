import { AIMessage, type BaseMessage } from "@langchain/core/messages";
import type { CallUsage } from "./api";

/** Provider-reported usage per AI message. LangChain's input_tokens includes cache reads and
 * writes, so the uncached part is the remainder (no estimation). */
export function usageOf(messages: BaseMessage[], fallbackModel: string): CallUsage[] {
  const out: CallUsage[] = [];
  for (const m of messages) {
    if (!AIMessage.isInstance(m) || !m.usage_metadata) continue;
    const u = m.usage_metadata;
    const d = (u.input_token_details ?? {}) as Record<string, number | undefined>;
    const read = d.cache_read ?? 0;
    const w5 = d.ephemeral_5m_input_tokens;
    const w1 = d.ephemeral_1h_input_tokens;
    const created = d.cache_creation ?? 0;
    const write5 = w5 ?? (w1 === undefined ? created : 0);
    const write1 = w1 ?? 0;
    const model = (m.response_metadata?.model as string | undefined) ?? fallbackModel;
    out.push({
      model,
      input_tokens: Math.max(u.input_tokens - read - write5 - write1, 0),
      output_tokens: u.output_tokens,
      cache_write_5m_tokens: write5,
      cache_write_1h_tokens: write1,
      cache_read_tokens: read,
    });
  }
  return out;
}
