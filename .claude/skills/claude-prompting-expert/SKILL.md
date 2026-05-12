---
name: claude-prompting-expert
description: Claude-specific prompt engineering — XML tags as structural delimiters, system prompts on Opus 4.5/4.6/4.7, prompt caching (5-min and 1-hour TTL), tool use with strict mode, structured outputs via constrained decoding, long context (200K/1M), adaptive thinking, and agentic Claude patterns from Anthropic's 2025-2026 guidance. Auto-loads when designing or optimizing prompts for the Claude API. Strongly opinionated on what changed in Claude 4.x — prefill is gone, CRITICAL-shouting now overtriggers, "literal instruction following" requires explicit asks. Pairs with prompting-fundamentals-expert.
---

# Claude API Prompt Engineering — Expert

Claude-specific tactics. **Read `prompting-fundamentals-expert` first for universal principles.** This skill covers what's distinctive to Claude.

## TL;DR — Claude-specific rules

1. **Use XML tags.** Claude was trained heavily on XML-delimited prompts. `<instructions>`, `<context>`, `<example>`, `<input>` outperform Markdown headings for structuring complex prompts.
2. **Cache aggressively.** Prompt caching gives 90% off cached input tokens. Place tools + system + few-shot examples in the static prefix; user-specific content last.
3. **Place documents first, query last.** Anthropic measures ~30% quality improvement on multi-doc inputs when the question goes at the bottom.
4. **Drop the CRITICAL-shouting on Claude 4.x.** Opus 4.5+ now overtriggers on aggressive caps. Use neutral imperatives.
5. **Migrate off assistant-prefill on 4.6+.** It's a 400 error. Use structured outputs instead.

## Anthropic's core six techniques

The canonical reference is now [Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices). The recommended order:

1. **Be clear and direct.** *"Show your prompt to a colleague with minimal context on the task. If they'd be confused, Claude will be too."*
2. **Add context to instructions.** State the purpose, audience, and why each rule exists.
3. **Use examples (multishot).** Especially valuable for non-reasoning models.
4. **Structure with XML tags.** §2 below.
5. **Give Claude a role via system prompt.** Even one sentence makes a measurable difference.
6. **Let Claude think (adaptive thinking).** §8 below.
7. **Chain prompts** only when you need to inspect intermediate state.

**On Opus 4.7, instructions are followed literally.** Don't expect silent "above and beyond" generalization — explicitly request it ("Go beyond the basics to create a fully-featured implementation").

## XML tags — Claude's distinctive structural device

**Why XML.** Markdown headings are stylistic and ambiguous; JSON inside prompts is brittle to escape and is treated as data. XML tags are unambiguously *structural delimiters*. Tag names themselves carry semantic weight Claude honors (`<sensitive_data>`, `<untrusted_input>`).

**Common tags:**

| Tag | Use |
|---|---|
| `<instructions>` | The core task |
| `<context>` | Background info Claude needs |
| `<example>` / `<examples>` | Few-shot demonstrations |
| `<input>` | The variable input for this call |
| `<document>` with `<source>` and `<document_content>` | Multi-doc inputs |
| `<thinking>` and `<answer>` | Manual CoT separation (less needed on adaptive-thinking models) |
| `<output_format>` | Format constraints |
| Custom tags | Anthropic's own samples use bespoke names freely (`<frontend_aesthetics>`, `<use_parallel_tool_calls>`) |

**Nesting pattern for multi-doc:**

```xml
<documents>
  <document index="1">
    <source>memo_2025_q3.pdf</source>
    <document_content>...</document_content>
  </document>
  <document index="2">
    <source>memo_2025_q4.pdf</source>
    <document_content>...</document_content>
  </document>
</documents>
```

**Pitfalls.**
- Inventing one-off tag names per prompt — pick consistent vocabulary, reuse it.
- Mixing XML with Markdown headings randomly — pick one structural device per prompt.

## System prompts on Claude 4.x

The `system` parameter sets persistent role, tone, constraints, and policy.

**What belongs in system.** Stable, long-lived instructions: role, tone, formatting rules, safety boundaries, tool-use policy.

**What belongs in user.** Task-specific, turn-specific content. Per-call inputs.

**Claude 4.x changes.**
- **System is more responsive** than on 3.x — over-emphatic language ("CRITICAL: You MUST...") now causes overtriggering. Dial down to neutral phrasing ("Use this tool when...").
- **Assistant-prefill is removed on 4.6+.** The old `{"role": "assistant", "content": "{"}` pattern returns a 400. Migrate to structured outputs.

**Pitfall: dynamic system content.** Stuffing user data or retrieved docs into `system` breaks prompt caching on every turn. Keep `system` immutable per conversation.

## Prompt caching — the single biggest cost win

**Numbers.** Cache hits cost **0.1× base input** (90% savings). Writes cost 1.25× (5-min TTL) or 2× (1-hour TTL). [Prompt caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

**Two modes:**

| Mode | Use |
|---|---|
| **Automatic** | `cache_control={"type":"ephemeral"}` at top level — breakpoint advances with conversation. Best for multi-turn chat. |
| **Explicit** | Attach `cache_control` to specific content blocks. Up to **4 breakpoints**. Use for layered prompts (tools → static instructions → large doc → user query). |

**TTL options.**
- Default 5-min — refreshed on hit at no cost.
- `"ttl": "1h"` — 2× write cost. For infrequent reuse. Must appear *before* 5-minute entries in the same request.

**Minimum cacheable prompt length.**
- Opus 4.5+/4.7, Haiku 4.5: 4,096 tokens
- Sonnet 4.6: 2,048 tokens
- Older: 1,024 tokens

Below the minimum, caching silently no-ops. Check `usage.cache_read_input_tokens` to confirm hits.

**Lookback window: 20 blocks** from the breakpoint. Longer conversations need additional breakpoints.

**Pre-warming.** Send a `max_tokens: 0` request to populate the cache before the first user-facing call eliminates first-request latency.

**Critical pitfalls:**
- Placing `cache_control` on content that changes every request (timestamps, user IDs in system message) — the hash changes, so every request becomes a cache write.
- Forgetting `cache_read_input_tokens` is the verification signal — assume "I'm caching" without checking it.

## Tool use / function calling

**Schema.** Tools live in the `tools` array with `name`, `description`, `input_schema` (JSON Schema). [Tool use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview).

**Routing via `tool_choice`:**

| Value | Effect |
|---|---|
| `auto` (default) | Model decides |
| `any` | Force *some* tool call |
| `tool` (with `name`) | Force a specific tool |
| `none` | Disable tools for this call |

**Strict mode.** Add `strict: true` for **guaranteed schema conformance** via constrained decoding. [Strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use).

**Parallel calls on Claude 4.x.** Excellent native support. Push to ~100% parallel rate with:

```xml
<use_parallel_tool_calls>
If you intend to call multiple tools and there are no dependencies between
them, make all of the independent tool calls in parallel.
</use_parallel_tool_calls>
```

**Description quality matters more than parameter quality.** Tool descriptions deserve the same prompt-engineering rigor as the main prompt ([Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)).

**Pitfalls.**
- Asking Claude to "suggest changes" instead of "make changes" — it will refuse to call write tools.
- On Opus 4.7, tool use *decreased* by default in favor of more reasoning. Raise `effort` to `high`/`xhigh` if you want more tool use.
- Assistant-prefilled JSON tricks (`{"role":"assistant","content":"{"}`) return 400 on Claude 4.6+. Migrate.

## Structured output

**Current best path: `output_config.format`** with a JSON schema (constrained decoding). Schema compliance is **guaranteed** — no retry loop needed. [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).

Pair with `client.messages.parse()` and a Pydantic model for type-safe extraction.

**Fallback: XML extraction.** When constrained decoding isn't available, ask Claude to wrap the answer in `<answer>...</answer>` and parse with a regex. This is also the documented "ground responses in quotes first" pattern — ask Claude to extract relevant `<quotes>` from documents, then produce `<info>` based on them.

**Limits of `output_config.format`:**
- No `minLength`/`maxLength`/`minimum`/`maximum`
- No recursion or external `$ref`
- Max 20 strict tools per request, 24 optional parameters total
- First request compiles the grammar (latency hit); cached 24h after

**Pitfall.** The old assistant-prefill trick (prefilling `{` to force JSON) is **no longer supported on Claude 4.6+**. Migrate to `output_config.format`.

## Long context (200K → 1M)

**Place long documents FIRST, query LAST.** Anthropic reports up to **30% quality improvement** for complex multi-document inputs when queries appear at the end. [Long context tips](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/long-context-tips).

**Wrap each doc** in the structured pattern shown in §2.

**Quote-then-answer pattern.** For tasks involving the docs, ask Claude to **extract relevant passages first** into `<quotes>` tags before producing the answer. Anchors response in source material; cuts hallucination.

**Position-of-information bias.** Claude attends best to start-of-context and end-of-context (the "lost in the middle" effect — less pronounced on Claude 4.x but still real). Mitigate by putting the *instruction* at the end since it's read just before generation begins.

**Pitfalls.**
- Burying instructions before 100K tokens of context — attenuated.
- Repeating documents across turns instead of using prompt caching to keep them resident.

## Adaptive thinking (Claude 4.6 / Opus 4.7)

**The shift.** Manual `extended_thinking` with `budget_tokens` is **deprecated** in favor of **adaptive thinking**: `thinking={"type":"adaptive"}` combined with `output_config={"effort": "high"}` (or `max`, `xhigh`, `medium`, `low`). [Extended thinking docs](https://platform.claude.com/docs/en/build-with-claude/extended-thinking).

**When it helps.** Multi-step math/logic, agentic loops, code review, research synthesis, debugging.

**When it hurts.** Simple lookups, chat, latency-sensitive UX. Adaptive thinking auto-skips on easy queries, but a low `effort` setting gives a hard floor for hot paths.

**Cost.** Billed for full thinking tokens even when `display: "omitted"` hides them. Budgets above ~32K show diminishing returns.

**With tool use.** You **must pass thinking blocks back** in subsequent turns to preserve reasoning continuity.

**Pitfall on Opus 4.7.** At `low`/`medium` effort the model now respects the floor strictly and may under-think. Raise `effort` *before* adding "think harder" language to the prompt.

## Anti-patterns specific to Claude 4.x

| Anti-pattern | Why it's wrong | Fix |
|---|---|---|
| **CRITICAL-shouting** ("CRITICAL: You MUST...") | Overtriggers on Claude 4.5+ | Neutral imperatives ("Use this tool when...") |
| **Assistant-prefill on 4.6+** | Returns 400 | Use `output_config.format` |
| **Hardcoded "use this tool aggressively"** | Causes overuse on 4.5+ | Let model decide, raise `effort` if needed |
| **Negative-only instructions** ("don't use markdown") | Weaker than positive framing | "Write in flowing prose paragraphs" |
| **Polite filler in prompts** | Dilutes signal, wastes tokens | Direct imperatives |
| **Conflicting instructions** across system/user | Model picks one, usually the last seen | Reconcile explicitly |
| **Prompt drift across turns** | Slowly adding contradictory rules corrupts behavior | Reset via system prompt or fresh context |
| **Burying task at top of 50K-token prompt** | Recency bias attenuates it | Place at bottom |

## Agentic Claude — 2024-2026 patterns

Anthropic's [Building Effective AI Agents](https://www.anthropic.com/research/building-effective-agents) is the canonical reference. Core guidance: *"Do the simplest thing that works."* Don't reach for multi-agent until single Claude + good tools demonstrably fails.

**Modern playbook** ([Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), [Effective harnesses](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)):

- **Compaction over scrolling context** — summarize older turns rather than truncating.
- **Memory tools** for state that outlives the window.
- **Context awareness** — the model tracks its own token budget on Claude 4.5+.
- **Subagent orchestration** native on Opus 4.6/4.7. Scope explicitly: *"Do not spawn a subagent for work you can complete directly."*
- **Verification tools** (Playwright, computer use) so long-horizon agents can self-check.

**Key Claude 4.7-era prompt patterns:**

- `<default_to_action>` system prompts for autonomous coding agents.
- `<do_not_act_before_instructions>` for conservative assistants.
- Explicitly scope subagent use.

Reference architecture: [Building agents with the Claude Agent SDK](https://claude.com/blog/building-agents-with-the-claude-agent-sdk).

**Pitfall.** Assuming more agents = better. Cemri et al. (2025) measured 17× error amplification in unstructured multi-agent setups. Anthropic's own framing: start with one Claude + good tools + clean loop; decompose only when you can name a *specific* failure decomposition fixes.

## Safety rules

❌ **Don't** prefill assistant turns on Claude 4.6+ — 400 error.
❌ **Don't** use CRITICAL / MUST / NEVER caps on Claude 4.5+ — overtriggers.
❌ **Don't** put dynamic content (timestamps, user IDs) in the system message — breaks cache.
❌ **Don't** add `cache_control` to changing content — every request becomes a cache write.
❌ **Don't** bury instructions before long context — attenuation.
❌ **Don't** use `extended_thinking` with `budget_tokens` on 4.6+ — use `adaptive`.
❌ **Don't** reach for multi-agent before a single-Claude baseline with evals.

✅ **Do** use XML tags as structural delimiters.
✅ **Do** cache aggressively — tools + system + few-shot in static prefix.
✅ **Do** verify cache hits via `cache_read_input_tokens`.
✅ **Do** place documents first, query last for long context.
✅ **Do** use `output_config.format` with strict schemas for guaranteed shape.
✅ **Do** use `<use_parallel_tool_calls>` to push parallel rate to ~100%.
✅ **Do** raise `effort` to coax more tool use on Opus 4.7.

## Key sources

- [Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
- [Long context tips](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/long-context-tips)
- [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Extended / adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
- [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Tool use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)
- [Strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use)
- [Reduce hallucinations](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)
- [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)
- [Context engineering for agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Building agents with the Claude Agent SDK](https://claude.com/blog/building-agents-with-the-claude-agent-sdk)

## Further context

- `prompting-fundamentals-expert` — universal principles (CoT, few-shot, eval-driven iteration, injection defense)
- `claude-api` — Anthropic SDK mechanics (tools, caching API specifics, model migration)
- `agent-orchestration-expert` — when to use multi-agent vs single-call
- `codebase-architecture-expert` — how this project's `BaseLLMClient` wraps Anthropic / OpenAI / Gemini uniformly
