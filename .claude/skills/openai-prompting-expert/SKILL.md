---
name: openai-prompting-expert
description: OpenAI-specific prompt engineering — system/developer/user role hierarchy, function calling with strict mode, JSON mode vs Structured Outputs, prompting for reasoning models (o1/o3/o4-mini), GPT-4.1 literal-following patterns, automatic prompt caching, batch/predicted-outputs cost optimization, and the Agents SDK. Auto-loads when designing or optimizing prompts for the OpenAI API. Strongly opinionated about the o-series shift — strip CoT scaffolding, drop few-shot, use developer role not system.
---

# OpenAI API Prompt Engineering — Expert

OpenAI-specific tactics. **Read `prompting-fundamentals-expert` first for universal principles.** This skill covers what's distinctive to OpenAI's API surface.

## TL;DR — OpenAI-specific rules

1. **Use `developer` role for high-priority instructions on GPT-4.1+ and o-series.** `system` is legacy. For o1 specifically, `system` is silently dropped — use `developer`.
2. **Strip CoT scaffolding when porting to o-series.** "Think step by step" and few-shot examples *hurt* reasoning models. State problem + format. Stop.
3. **Always use `strict: true` for function-calling shape guarantees.** Pair with `parallel_tool_calls: false` — strict + parallel is not compatible.
4. **Use `response_format: {type: "json_schema", strict: true}` over `json_object` for structured outputs.** Schema adherence is guaranteed via constrained decoding.
5. **Prompt caching is automatic.** No breakpoint markers. Put stable content (developer message, tools, few-shot) first; dynamic content last.

## OpenAI's six strategies — the core guide

The canonical reference: [Prompt Engineering Guide](https://platform.openai.com/docs/guides/prompt-engineering). Six strategies:

1. **Write clear instructions** — details, persona, delimiters (XML, triple-backticks, markdown), specify required steps, supply examples, specify output length.
2. **Provide reference text** — ground answers in supplied passages; instruct the model to cite them.
3. **Split complex tasks into simpler subtasks** — prompt chaining; one task per call.
4. **Give the model time to think** — for non-reasoning models (GPT-4, GPT-4.1). NOT for o-series.
5. **Use external tools** — function calling, code interpreter, retrieval.
6. **Test changes systematically with evals** — most important; without evals you're optimizing on vibes.

OpenAI Academy's app-builder framing simplifies: outline the task, give helpful context, describe the ideal output, break big tasks into smaller steps ([OpenAI Academy](https://academy.openai.com/public/clubs/work-users-ynjqu/resources/prompting)).

**Pitfall.** Treating the six strategies as a checklist. OpenAI's own advice: prompts are software, refined via evals ([Receipt Inspection eval-driven example](https://cookbook.openai.com/examples/partners/eval_driven_system_design/receipt_inspection)).

## The role hierarchy: system / developer / user

| Role | Priority | When to use |
|---|---|---|
| `developer` | Highest (GPT-4.1+, o-series) | Application-builder policy, role, tool descriptions. Ranks above user when conflicts arise. |
| `system` | Legacy | Same role as `developer` on older models. Not supported by o1 at all. |
| `user` | Mid | End-user input. Dynamic per-turn content. |
| `assistant` | (output) | Model output and prior tool calls. |

**On o1:** `system` is silently dropped or errors. Use `developer`. To re-enable markdown output, prepend the literal string `Formatting re-enabled` to the developer message ([Reasoning guide](https://platform.openai.com/docs/guides/reasoning)).

**Pitfall.** Stuffing per-call dynamic data into the system/developer message — kills prompt-cache hits (see §8). Keep developer message immutable per session; put dynamic content in user.

## Function calling / tools

Tools live in the `tools` array with JSON Schema parameter definitions. The model emits `tool_calls` with `name` and JSON `arguments`. [Function calling guide](https://developers.openai.com/api/docs/guides/function-calling).

**Routing via `tool_choice`:**

| Value | Effect |
|---|---|
| `"auto"` (default) | Model decides |
| `"required"` | Must call some tool |
| `"none"` | No tool calls |
| `{"type": "function", "function": {"name": "..."}}` | Force a specific call |

**Always declare tools via the `tools` field — not in prose.** The GPT-4.1 prompting guide is explicit about this ([cookbook](https://cookbook.openai.com/examples/gpt4-1_prompting_guide)). Tool descriptions deserve the same prompt-engineering rigor as the main prompt.

**Strict mode for guaranteed-valid arguments:**

```json
{
  "type": "function",
  "function": {
    "name": "...",
    "strict": true,
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "required": ["all_fields_must_be_listed_here"],
      "properties": {
        "optional_field": {"type": ["string", "null"]}
      }
    }
  }
}
```

Strict mode requires:
- `additionalProperties: false` on every object
- Every property listed in `required` (optional fields use `["type", "null"]`)

**Strict + parallel = not compatible.** Set `parallel_tool_calls: false` when you need strict guarantees ([community discussion](https://community.openai.com/t/strict-mode-does-not-enforce-the-json-schema/1104630)).

## JSON mode vs Structured Outputs

Two mechanisms exist; **prefer the newer one**.

**Structured Outputs** (`response_format: {type: "json_schema", json_schema: {...}, strict: true}`) provides a **100% guarantee** of schema-conforming output via constrained decoding ([announcement](https://openai.com/index/introducing-structured-outputs-in-the-api/)). Supported on `gpt-4o-2024-08-06`+, GPT-4.1 family, and o-series.

Python / Node SDKs accept Pydantic / Zod classes directly:

```python
class Recipe(BaseModel):
    name: str
    ingredients: list[str]

response = client.beta.chat.completions.parse(
    model="gpt-4.1",
    messages=[...],
    response_format=Recipe,
)
recipe = response.choices[0].message.parsed
```

**JSON mode** (`response_format: {type: "json_object"}`) is older — guarantees *syntactically* valid JSON but not shape. Requires the word "JSON" in the prompt.

**Pitfall.** Using `json_object` without describing the shape in prose. The model invents key names. Always use `json_schema` strict mode for new code.

## Reasoning models (o1, o3, o4-mini, etc.)

The o-series performs chain-of-thought *internally* before emitting tokens. **Inverts familiar patterns.**

[OpenAI's reasoning guide](https://platform.openai.com/docs/guides/reasoning) — explicit rules:

| Don't | Do |
|---|---|
| ❌ Add "think step by step" | Just state the problem |
| ❌ Request planning | Describe the goal |
| ❌ Pad with CoT scaffolding | Minimal, goal-directed prompts |
| ❌ Start with few-shot examples | Start zero-shot; add examples only if evals fail |
| ❌ Use `system` role on o1 | Use `developer` |

**`reasoning_effort` parameter.** `low | medium | high` (some models add `minimal` or `xhigh`):
- `low` — matches a tier below in capability
- `high` — "thinks harder" at cost of latency and reasoning tokens

For o3 and o4-mini, reasoning items adjacent to function calls are **preserved in context across turns**, improving agentic loops ([o3/o4-mini cookbook](https://cookbook.openai.com/examples/o-series/o3o4-mini_prompting_guide)).

**Pitfall.** Porting a verbose GPT-4 prompt directly to o3 — strip the CoT scaffolding first. Pasting a 2000-token GPT-4o prompt onto o3 typically hurts performance.

## GPT-4.1 literal-following

GPT-4.1 follows instructions **more literally** than GPT-4o. Vague intent will not be filled in for you ([GPT-4.1 cookbook](https://cookbook.openai.com/examples/gpt4-1_prompting_guide)).

**Three short agentic instructions that lifted internal SWE-bench Verified by ~20%:**

1. **Persistence** — *"keep going until the query is completely resolved... only terminate when the problem is solved"*
2. **Tool-calling** — *"use your tools to read files... do NOT guess"*
3. **Planning** — *"plan extensively before each function call and reflect on outcomes"*

**Long context (up to 1M tokens):** place instructions **both above AND below** the provided context — beats either alone. If you must pick one, **above-context wins** for GPT-4.1 specifically (contrast with Claude where below wins).

**Few-shot placement.** Put examples under an explicit `# Examples` heading, not inside tool descriptions.

**Pitfall.** Assuming GPT-4o prompts work unchanged on GPT-4.1. The literal-following surfaces every loose phrasing as a surprising output.

## Few-shot examples

OpenAI recommends few-shot when the task is hard to describe abstractly or when you need a specific output style ([best practices](https://help.openai.com/en/articles/6654000-best-practices-for-prompt-engineering-with-the-openai-api)).

**Count.** 2-5 diverse examples is typical. Cover edge cases you care about, not near-duplicates.

**Formats:**

```text
### Example 1
Input: ...
Output: ...

### Example 2
Input: ...
Output: ...
```

Or XML:

```xml
<example>
  <input>...</input>
  <output>...</output>
</example>
```

Or JSON for code contexts.

**For reasoning models (o-series): start zero-shot.** Examples often hurt because they constrain the internal reasoning trace ([Reasoning guide](https://platform.openai.com/docs/guides/reasoning)).

Few-shot examples in the static prefix count toward prompt-cache hits.

**Pitfall.** Examples that subtly conflict with the prose instructions. The model follows examples over prose — then you wonder why "the prompt says X but output is Y."

## Cost optimization

**Prompt caching is automatic.** No breakpoint markers, no flags ([announcement](https://openai.com/index/api-prompt-caching/)).

| Threshold | Effect |
|---|---|
| ≥1024 tokens | API caches the static prefix |
| Subsequent identical-prefix requests | 50% discount on cached input tokens, up to 80% latency reduction |

**Maximize cache hits.** Put **stable content (developer message, tool definitions, few-shot examples) first** and dynamic per-request data last.

**Batch API** offers 50% off both input AND output for async workloads that complete within 24 hours ([batch guide](https://developers.openai.com/api/docs/guides/batch)).

**Flex processing** (`service_tier: "flex"`) — same 50% discount on synchronous Responses API calls at variable latency.

**Predicted Outputs** — speed up regeneration when most output is known. Pass the existing file as the `prediction` parameter; ~3× speedup for code edits. Supported on GPT-4o, GPT-4o-mini, GPT-4.1 family ([predicted outputs](https://developers.openai.com/api/docs/guides/predicted-outputs)). Rejected prediction tokens are still billed.

**Pitfall.** Interleaving dynamic timestamps or user IDs into the system/developer message — breaks caching on every call.

## Anti-patterns specific to OpenAI

| Anti-pattern | Why wrong | Fix |
|---|---|---|
| **"Think step by step" on GPT-4/GPT-4.1** | Already chains reason when warranted; bloats output | Skip the phrase; let the model decide |
| **"Think step by step" on o-series** | Actively harmful — duplicates internal CoT | Strip it |
| **Over-stuffed system/developer messages** (>2000 tokens) | Retrieval degrades, contradictions multiply | Split into focused subtasks |
| **JSON mode without schema described** | Guarantees syntax but not shape — model invents keys | Use `json_schema` strict mode |
| **Manual tool descriptions in prose** | Bypasses strict mode | Always use `tools` field |
| **Strict + parallel** | Schemas may be violated on parallel calls | Set `parallel_tool_calls: false` |
| **Re-prompting o1 with system role** | Silently dropped | Use `developer` |
| **Pasting GPT-4o prompts to GPT-4.1 unchanged** | Literal-following surfaces every loose phrasing | Audit + tighten before porting |
| **Few-shot on reasoning models** | Constrains internal reasoning trace | Zero-shot first; add examples only if evals fail |

## Recent thinking (2024-2026): evals-driven + Agents SDK

### Evals-driven development

The dominant shift is from "prompt craft" to **eval-driven development** — building scoped tests *before* iterating, so improvements are measurable, not vibes-based ([eval best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)).

- `openai/evals` open-source framework ([github](https://github.com/openai/evals)) — YAML-driven custom evals + benchmark registry
- Platform Evals UI ([evals.openai.com](https://evals.openai.com/)) — store datasets, run prompt-version comparisons

### Agents SDK

The [Agents SDK](https://github.com/openai/openai-agents-python) codifies orchestration patterns into first-class primitives:

| Primitive | What it does |
|---|---|
| **Tools** | Standard function-calling, integrated |
| **Handoffs** | One-way delegation to a specialist agent |
| **Guardrails** | Concurrent validators that can abort generation |
| **Tracing** | Built-in observability |

**Manager vs decentralized patterns.** OpenAI recommends a "manager" (central LLM delegating via tool calls) over decentralized handoffs when you need a single point of synthesis. Use decentralized handoffs for triage routing.

The [Practical Guide to Building AI Agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/) emphasizes: keep prompts composable, write evals first, start single-agent before multi-agent.

**Pitfall.** Adopting Agents SDK orchestration before having a single-agent baseline with evals — you compound errors with no signal to debug.

## Safety rules

❌ **Don't** use `system` role on o1 — silently dropped. Use `developer`.
❌ **Don't** add CoT scaffolding on o-series — it duplicates internal reasoning.
❌ **Don't** mix `strict: true` with `parallel_tool_calls: true` — schemas may be violated.
❌ **Don't** use `json_object` without describing the shape — use `json_schema` strict mode.
❌ **Don't** describe tools in prose — always use the `tools` field.
❌ **Don't** put dynamic content (timestamps, user IDs) in developer/system — breaks caching.
❌ **Don't** paste GPT-4o prompts onto GPT-4.1 unchanged — audit literal-following first.
❌ **Don't** adopt Agents SDK before a single-agent baseline with evals.

✅ **Do** use `developer` role for app-builder instructions on GPT-4.1+ and o-series.
✅ **Do** use `response_format: {type: "json_schema", strict: true}` for guaranteed shape.
✅ **Do** put stable content (developer, tools, few-shot) first for cache hits.
✅ **Do** verify cache hits via `usage.prompt_tokens_details.cached_tokens`.
✅ **Do** use Batch API for non-realtime workloads — 50% off both input and output.
✅ **Do** apply GPT-4.1's "Persistence / Tool-calling / Planning" three-liner for agentic tasks.
✅ **Do** strip CoT scaffolding when porting to o-series.
✅ **Do** write evals before iterating.

## Key sources

**Core docs:**
- [Prompt engineering guide](https://platform.openai.com/docs/guides/prompt-engineering)
- [Text generation / roles](https://platform.openai.com/docs/guides/text-generation)
- [Reasoning models](https://platform.openai.com/docs/guides/reasoning)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)

**Cookbook:**
- [GPT-4.1 prompting guide](https://cookbook.openai.com/examples/gpt4-1_prompting_guide)
- [o3/o4-mini prompting guide](https://cookbook.openai.com/examples/o-series/o3o4-mini_prompting_guide)
- [Structured outputs intro](https://developers.openai.com/cookbook/examples/structured_outputs_intro)
- [Receipt Inspection — eval-driven](https://cookbook.openai.com/examples/partners/eval_driven_system_design/receipt_inspection)

**Cost & operations:**
- [Prompt caching](https://openai.com/index/api-prompt-caching/)
- [Batch API](https://developers.openai.com/api/docs/guides/batch)
- [Predicted outputs](https://developers.openai.com/api/docs/guides/predicted-outputs)
- [Structured outputs announcement](https://openai.com/index/introducing-structured-outputs-in-the-api/)

**Evaluation & agents:**
- [Eval best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- [OpenAI evals framework (GitHub)](https://github.com/openai/evals)
- [Agents SDK (GitHub)](https://github.com/openai/openai-agents-python)
- [Practical Guide to Building AI Agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/)

## Further context

- `prompting-fundamentals-expert` — universal principles (CoT, few-shot, eval-driven iteration, injection defense)
- `agent-orchestration-expert` — when to use multi-agent vs single-call
- `codebase-architecture-expert` — how this project's `BaseLLMClient` wraps OpenAI uniformly alongside Anthropic / Gemini
