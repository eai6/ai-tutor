---
name: gemini-prompting-expert
description: Gemini-specific prompt engineering — system_instructions as a separate parameter, native multimodal prompting (image/video/audio/PDF in contents array), the 1M-token context window with query-at-end structure, function calling with ANY/AUTO/NONE modes, Google Search grounding, thinkingBudget vs thinkingLevel (Gemini 2.5 vs 3), Pydantic response_schema for structured output, and Vertex AI vs Generative Language API auth. Auto-loads when designing or optimizing prompts for the Gemini API. Strongly opinionated about Gemini 3 — direct task statements beat persona priming, negative instructions over-index, use context caching aggressively.
---

# Gemini API Prompt Engineering — Expert

Gemini-specific tactics. **Read `prompting-fundamentals-expert` first for universal principles.** This skill covers what's distinctive to Gemini.

## TL;DR — Gemini-specific rules

1. **`system_instruction` is a top-level parameter**, not a chat message. Per-call config; you must re-send it every `generateContent` request.
2. **Multimodal is native — image FIRST, text AFTER** for single-image tasks. Don't OCR + text yourself; let Gemini see the image directly.
3. **Long context: query at the END, anchored** with phrases like "Based on the entire document above..." Use the 1M window before reaching for RAG.
4. **Enable context caching aggressively** — ~4× cheaper on cached tokens. The biggest single cost win.
5. **Gemini 3 dislikes flowery prompts.** Direct task statements beat "you are the world's foremost...". Negative instructions ("do not guess") *over-index* and hurt arithmetic/logic — use positive framing.

## Google's core guidance

The canonical references: [Prompt design strategies](https://ai.google.dev/gemini-api/docs/prompting-strategies) + [Prompt design intro](https://ai.google.dev/gemini-api/docs/prompting-intro) + [Gemini 3 prompting guide (Vertex)](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/gemini-3-prompting-guide).

**Four input archetypes** — pick one rather than blending:
- **question** — "What is X?"
- **task** — "Do X."
- **entity** — extract structured info about an entity
- **completion** — finish a partial pattern

**Few-shot is recommended by default**: *"We recommend to always include few-shot examples in your prompts. Prompts without few-shot examples are likely to be less effective."* Use 2-5 varied examples with **consistent formatting** — Gemini follows the format exactly, including punctuation quirks.

**Gemini 3 verbosity/tone rules**: *"State your goal clearly and concisely. Avoid unnecessary or overly persuasive language."*

**Pitfall.** Copying Claude/OpenAI prompt habits onto Gemini 3 — flowery persona priming and aggressive emphasis underperform vs direct task statements.

## System instructions — separate parameter, not a message

In Gemini, system instructions are a **separate top-level field**, not a role in `contents`.

```python
from google import genai
from google.genai import types

client.models.generate_content(
    model="gemini-3-flash-preview",
    config=types.GenerateContentConfig(
        system_instruction="You are a math tutor. Use one paragraph."
    ),
    contents="Hello",
)
```

REST equivalent: `"system_instruction": {"parts":[{"text":"..."}]}` alongside `contents`.

**Critical: per-call configuration, not server-side state.** Every `generateContent` request must re-send the system instruction. The SDK's `chats.create()` helper holds them across turns, but under the hood each call re-sends.

**Pitfall.** Treating system instructions like a persistent persona that "sticks" between sessions. You must persist and re-send them yourself.

## Multimodal — native input ordering

Gemini is **natively multimodal**. Image, video, audio, and PDF live in `contents` as ordered `parts` ([Vision docs](https://ai.google.dev/gemini-api/docs/vision)).

**Ordering rule (from the docs):** *"place the text prompt after the image part in the contents array"* — for single-image tasks.

For multi-image comparison, interleave with labels:

```python
contents = [
    "Image 1:",
    Part.from_bytes(img1_bytes, "image/png"),
    "Image 2:",
    Part.from_bytes(img2_bytes, "image/png"),
    "Compare the two — which has more dimensions labeled?",
]
```

**Token costs for images:**
- ≤384px on both sides: 258 tokens flat
- Larger: tiled into 768×768 chunks at 258 tokens each
- Up to **3,600 images per request**

**Files API** for reuse or large payloads. Use it when:
- Reusing media across calls
- Total payload >20 MB
- Otherwise inline base64 is fine

**Pitfall.** Vague image prompts ("describe this"). Gemini's vision is precise when constrained — ask for structured extraction: `"List every dimension labeled on this schematic in {label: value_with_unit} JSON"`.

## Long context — the 1M-token window

Gemini's signature advantage: **1M tokens across the 2.5 and 3 lines** (~50k LOC, ~8 novels). [Long context docs](https://ai.google.dev/gemini-api/docs/long-context).

**Two structural rules (verbatim from the docs):**

1. **Query at the end.** *"The model's performance will be better if you put your query / question at the end of the prompt (after all the other context)."*
2. **Anchor the question.** Use phrases like *"Based on the entire document above..."* — repeated verbatim in the Gemini 3 guide.

**Needle-in-haystack accuracy.** ~99% for a single needle. **Degrades with multiple needles** — for 100 retrievals at 99% accuracy you'd want 100 separate calls.

**Context caching** ([docs](https://ai.google.dev/gemini-api/docs/caching)) cuts cached-token costs **~4×**. Use it whenever the same big preamble is reused (system instructions + tools + reference document).

**When to apply.** Prefer long context over RAG when:
- The corpus fits in 1M tokens
- Queries vary across the same corpus
- Caching can amortize the upload

Prefer RAG when retrieval is highly selective or the corpus is >1M tokens.

**Pitfall.** **Under-using long context.** Many teams reach for RAG by reflex when stuffing the whole document into the prompt + caching would be cheaper and more accurate.

## Function calling

Function declarations use a **JSON-subset of OpenAPI schema** with `name`, `description`, `parameters` ([Function calling docs](https://ai.google.dev/gemini-api/docs/function-calling)).

**`tool_config.function_calling_config.mode`:**

| Mode | Effect |
|---|---|
| `AUTO` (default) | Model decides |
| `ANY` | Force a function call; combine with `allowed_function_names=[...]` to constrain |
| `NONE` | Disable tool use |
| `VALIDATED` | Default when multiple tools combined; enforces schema adherence |

**Parallel calling** supported in Gemini 3. Response may contain multiple `functionCall` parts; you respond with matching `functionResponse` parts keyed by `id`.

**Python SDK automatic function calling.** Pass a type-hinted Python function as a `tool` and the SDK generates the schema and executes the call.

**Comparison with OpenAI / Anthropic:**

| Gemini | OpenAI | Anthropic |
|---|---|---|
| `ANY` | `tool_choice: "required"` | `tool_choice: {"type": "any"}` |
| `AUTO` | `tool_choice: "auto"` | `tool_choice: {"type": "auto"}` |
| `ANY` + `allowed_function_names` | `tool_choice: {function: {name: "..."}}` | `tool_choice: {type: "tool", name: "..."}` |

**Pitfall.** Forgetting that descriptions are load-bearing — the model decides whether to call based on `description`, so a thin description means missed or wrong tool selection.

## Search grounding

Built-in **Google Search grounding** as a tool ([grounding docs](https://ai.google.dev/gemini-api/docs/grounding)):

```python
config = GenerateContentConfig(
    tools=[Tool(google_search=GoogleSearch())]
)
```

Response carries `groundingMetadata`:

| Field | Purpose |
|---|---|
| `webSearchQueries` | Queries the model issued |
| `groundingChunks` | `{uri, title}` per source |
| `groundingSupports` | `startIndex`/`endIndex` text spans linked to chunk indices |
| `searchEntryPoint` | HTML widget you're **required to display** when surfacing grounded content |

**Citation rendering.** Walk `groundingSupports` in **reverse order** and insert links at byte offsets (going forward would shift subsequent offsets).

**Billing.** On Gemini 3 it's **per search query the model executes**; on 2.5 and older it's per prompt.

**When to apply.** Factual questions about recent events, niche entities, or anything time-sensitive.

**Pitfalls.**
- Forgetting Google's display-attribution requirement.
- Enabling grounding for creative writing, code, classification — adds latency for no quality gain.

## Thinking mode (Gemini 2.5+)

Reasoning is exposed differently across versions:

| Version | Parameter | Range |
|---|---|---|
| Gemini 2.5 Pro | `thinkingBudget` | 128–32768 tokens |
| Gemini 2.5 Flash | `thinkingBudget` | 0–24576 (0 disables) |
| Gemini 2.5 Flash-Lite | `thinkingBudget` | 512–24576 |
| Gemini 3 | `thinkingLevel` | `minimal | low | medium | high` |

`thinkingBudget = -1` = dynamic (Gemini 2.5).

**Important: don't use `thinkingBudget` on Gemini 3** — the doc warns: *"While thinkingBudget is accepted for backwards compatibility, using it with Gemini 3 Pro may result in unexpected performance."* Use `thinkingLevel`.

Enable `includeThoughts=True` to get summarized reasoning back in the response.

**When longer thinking pays off:** math, multi-step coding, planning, compositional tool use.

**When it hurts:** chat, classification, formatting transformations. Use `low`/`minimal` for latency-sensitive paths.

**Pitfall.** Leaving thinking on `dynamic` in a hot chat path and being surprised by p99 latency. **Pin to `low` for production chat.**

## Structured output

Set `response_mime_type="application/json"` and `response_schema=<schema>` in `GenerateContentConfig` ([structured output docs](https://ai.google.dev/gemini-api/docs/structured-output)).

**Pydantic `BaseModel` accepted directly** by the SDK:

```python
from pydantic import BaseModel

class Recipe(BaseModel):
    name: str
    ingredients: list[str]

config = GenerateContentConfig(
    response_mime_type="application/json",
    response_schema=Recipe,
)
```

**Supported schema features:**
- `enum`
- `format: date-time`
- `minimum`/`maximum`, `minItems`/`maxItems`
- `prefixItems` for tuples
- Streaming emits valid partial JSON

**Version differences:**
- Gemini 2.0 — requires explicit `propertyOrdering` list
- Gemini 2.5+ — infers from declaration order

**Pitfalls.**
- Very deep/wide schemas may be silently truncated.
- Unsupported JSON-Schema keywords (`oneOf` patterns, `$ref` cycles) are silently dropped.
- **Always validate parsed output in app code** — schema adherence is not semantic correctness.

## Few-shot prompting in Gemini

Format: conventional `Input: ... / Output: ...` blocks, XML, or Markdown — whatever you choose, **be consistent**. Gemini follows the format exactly, including punctuation quirks.

The docs claim: *"Well-constructed examples can even replace lengthy instructions."*

**Sweet spot.** 2-5 examples. Cover edge cases. Add the "negative" shape only if you've observed the model failing there.

**When examples earn their keep:** structured extraction, tone matching, classification with non-obvious labels.

**Pitfall.** Examples that all look the same — Gemini learns the surface pattern (e.g., all examples being 3-word answers) and over-generalizes.

## Anti-patterns specific to Gemini

| Anti-pattern | Why wrong | Fix |
|---|---|---|
| **Open-ended negatives** ("do not guess", "do not infer") | Google warns these cause the model to *"over-index and fail to perform basic logic or arithmetic"* | Positive framing: "Use only the provided document; if it doesn't say, reply 'Not stated.'" |
| **Treating Gemini like Claude** — flowery role-play, "you are the world's foremost..." | Underperforms vs direct task statements on Gemini 3 | Direct: "Summarize the document below into 3 bullets." |
| **Under-using the 1M window** | Chunking + RAG'ing a 200k-token doc that would fit whole | Stuff the doc + use context caching |
| **Disabling grounding for factual queries** | Fluent fabrication on recent events | Enable `google_search` tool |
| **Vague image prompts** ("what's in this?") | Wastes Gemini's vision precision | Ask for structured extraction |
| **Tuning temperature on Gemini 3** | The doc says keep default 1.0; varying hurts more than helps | Leave at default |
| **Re-sending huge prompts without caching** | Leaves ~75% cost savings on the table | Enable context caching |
| **`thinkingBudget` on Gemini 3** | Backwards-compat only; "unexpected performance" warning | Use `thinkingLevel` |
| **Leaving thinking on dynamic in chat** | p99 latency blows up | Pin to `low` for production chat |

## Vertex AI vs Generative Language API

**Two surfaces, one SDK** (`google-genai`). [Migration docs](https://ai.google.dev/gemini-api/docs/migrate-to-cloud).

| Surface | Auth | When |
|---|---|---|
| **Generative Language API** (ai.google.dev) | API-key | Prototype, consumer apps, fastest path from zero. *"Most developers should use the Gemini Developer API unless there is a need for specific enterprise controls."* |
| **Vertex AI / Gemini Enterprise** | IAM via Application Default Credentials, service accounts | Regulated industries, EU/UK data residency, audit logs, VPC-SC, CMEK, batch quotas |

**Client init differs by one flag:**

```python
client = genai.Client()                                          # Developer API
client = genai.Client(vertexai=True, project="p", location="l")  # Vertex
```

**Vertex regional endpoints:** `us-central1`, `europe-west4`, etc., plus a `global` endpoint.

**Important migration note (May 2026):** the legacy `vertexai` Python SDK is being sunset. *"Vertex AI SDK releases after June 2026 won't support Gemini, and new Gemini features are only available in the Gen AI SDK."* **Port any old `vertexai.preview.generative_models` code to `google-genai` now.**

**Pitfall.** Assuming an API key works on Vertex (it doesn't — needs ADC) or that ADC works on the Developer API (it doesn't — needs an API key).

## Safety rules

❌ **Don't** use open-ended negatives ("do not guess") on Gemini 3 — over-indexes.
❌ **Don't** apply Claude/OpenAI flowery persona priming to Gemini 3 — underperforms.
❌ **Don't** chunk + RAG a doc that fits in 1M tokens — use the window + caching.
❌ **Don't** use `thinkingBudget` on Gemini 3 — use `thinkingLevel`.
❌ **Don't** leave thinking on dynamic for hot chat paths — p99 latency.
❌ **Don't** skip context caching — leaves 75% cost savings on the table.
❌ **Don't** mix API-key auth and Vertex (or ADC and Developer API).
❌ **Don't** forget to re-send `system_instruction` on every call — it's per-call, not server-state.

✅ **Do** put image FIRST and text AFTER for single-image tasks.
✅ **Do** place query AT THE END for long context, with an anchor phrase.
✅ **Do** enable `google_search` grounding for time-sensitive factual queries.
✅ **Do** display the `searchEntryPoint` widget when surfacing grounded content (required).
✅ **Do** use `response_schema` with Pydantic for structured output.
✅ **Do** validate parsed output in app code — schema adherence ≠ semantic correctness.
✅ **Do** consistent formatting across few-shot examples — Gemini copies inconsistency.
✅ **Do** port legacy `vertexai` SDK code to `google-genai` before June 2026.

## Key sources

**Core docs:**
- [Prompt design strategies](https://ai.google.dev/gemini-api/docs/prompting-strategies)
- [Prompt design intro](https://ai.google.dev/gemini-api/docs/prompting-intro)
- [Gemini 3 prompting guide (Vertex)](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/gemini-3-prompting-guide)
- [Text generation / system_instruction](https://ai.google.dev/gemini-api/docs/text-generation)

**Specific features:**
- [Vision / multimodal](https://ai.google.dev/gemini-api/docs/vision)
- [Long context](https://ai.google.dev/gemini-api/docs/long-context)
- [Context caching](https://ai.google.dev/gemini-api/docs/caching)
- [Function calling](https://ai.google.dev/gemini-api/docs/function-calling)
- [Grounding with Google Search](https://ai.google.dev/gemini-api/docs/grounding)
- [Thinking mode](https://ai.google.dev/gemini-api/docs/thinking)
- [Structured output](https://ai.google.dev/gemini-api/docs/structured-output)

**Models & migration:**
- [Models lineup](https://ai.google.dev/gemini-api/docs/models)
- [Developer API vs Enterprise / Vertex](https://ai.google.dev/gemini-api/docs/migrate-to-cloud)
- [Gemini API Cookbook](https://github.com/google-gemini/cookbook)

## Further context

- `prompting-fundamentals-expert` — universal principles (CoT, few-shot, eval-driven iteration, injection defense)
- `agent-orchestration-expert` — when to use multi-agent vs single-call
- `codebase-architecture-expert` — how this project's `BaseLLMClient` wraps Gemini uniformly alongside Anthropic / OpenAI
