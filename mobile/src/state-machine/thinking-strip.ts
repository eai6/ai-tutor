// Qwen 3 / Qwen 3.5 emit <think>...</think> blocks where the model
// "thinks out loud" before answering. Great for benchmarks; useless
// for a tutor chat. Strip those blocks from the response before any
// downstream processing (praise filter, media parser, UI).
//
// Also handles partial / unclosed thinking tags — if the model gets
// truncated mid-think, just drop everything from <think> on.

const THINK_BLOCK_RE = /<think>[\s\S]*?<\/think>/gi;
const UNCLOSED_THINK_RE = /<think>[\s\S]*$/i;

export function stripThinkingBlocks(text: string): string {
  if (!text) return text;
  let out = text.replace(THINK_BLOCK_RE, '');
  out = out.replace(UNCLOSED_THINK_RE, '');
  return out.trim();
}
