# Gemini Tutor Prompt — Expert (ai-tutor project)

Project-specific. **Companion to (not replacement for) `gemini-prompting-expert`** — read that first for universal Gemini rules. This skill captures how the ai-tutor codebase's Anthropic-shaped tutor prompt converts to Gemini-native form.

## When to use this skill

Use this skill whenever you are:
- Writing or modifying `apps/tutoring/prompts/gemini.py` (the Gemini tutor prompt builder)
- Adding a new dynamic-suffix block that needs to ship to both Anthropic and Gemini
- Debugging a Gemini-specific tutoring quality regression
- Migrating an additional purpose (judge, regen, generation) from Anthropic to Gemini

## Core conversions — Anthropic → Gemini

### 1. XML sections → markdown headers

Anthropic builder uses XML throughout: `<identity>`, `<rules>`, `<lesson_context>`, etc. Gemini 3 tolerates XML but is idiomatic with markdown headers.

```diff
- <identity>
- You are a friendly, encouraging tutor for secondary school students at
- {institution_name} ({locale_context}). Your name is {tutor_name}.
- </identity>
+ ## Role
+ Tutor for secondary school students at {institution_name} ({locale_context}).
+ Tutor name: {tutor_name}.
```

Strip the persona priming ("friendly, encouraging") — Gemini 3 underperforms with flowery role-play per the Gemini 3 prompting guide. Keep the role definition factual.

### 2. System message → `system_instruction` parameter

Anthropic: system prompt is the first text block in the messages array.

Gemini: `system_instruction` is a **top-level parameter on every `generate_content` call** — it does not live in `contents`. The builder must split:

- Stable prefix (role, rules, lesson context, tool specs, media catalog) → `system_instruction`
- Dynamic per-turn suffix (figure_facts, regen, bank_grade, scaffolding directive) → user-message preamble OR last user content block

Return signature:

```python
def build(self, ...) -> tuple[str, dict]:
    system_instruction = self._build_stable_prefix(...)
    user_preamble = self._build_dynamic_suffix(...)
    return (user_preamble, {"system_instruction": system_instruction})
```

The caller passes `system_instruction` via `extra_provider_kwargs` to `GeminiClient.generate()`.

### 3. Negative rules → positive rephrases

Google docs explicitly warn that Gemini 3 "over-indexes" on negative instructions and breaks arithmetic. **Convert every "do NOT" rule that's possible to phrase positively.**

| Current Anthropic phrasing | Gemini-native rephrase |
|---|---|
| "Do NOT re-recite verbatim" | "Adapt the script's phrasing for natural delivery while preserving structure and examples" |
| "Do NOT author a new question this turn" | "Give a text hint only — the in-flight question is visible in the artifact panel" |
| "Never say 'which of these'" | "Make every question complete and self-contained — include the choices in the question itself" |
| "Do NOT reveal the answer" | "When the student is wrong, point at the misconception with a hint they can act on" |

**Some negatives must stay** — safety rules, leak prevention, "do not impersonate the student". Keep those, but phrase as a single short clause rather than a multi-bullet list.

### 4. Query / instructions at END for long context

Gemini long-context rule (verbatim from docs): *"The model's performance will be better if you put your query / question at the end of the prompt."*

The tutor prompt is long-context (~50KB system + 10-50 turns history + per-turn dynamic blocks). The dynamic suffix order matters more for Gemini than for Anthropic. Place these LAST in the per-turn user message:

1. figure_facts (visual grounding for current step)
2. bank_grade (deterministic verdict the LLM must trust)
3. scaffolding_directive (per-turn behavior gate)
4. **Then the student's latest message**

Anchor the LLM with a closing phrase: `"Based on the rules and lesson context above, respond to the student's message below:"`

### 5. Temperature: leave at default 1.0

`ModelConfig.effective_temperature` clamps tutoring to [0.1, 0.3] for Anthropic stability. **Override this clamp for Gemini.** Google's Gemini 3 docs warn: *"keep the temperature parameter at its default value of 1.0... doesn't necessarily benefit from tuning."*

If a benchmark run shows lower temperature gives clearly better results for tutoring specifically, override with a documented justification — but don't carry over the Anthropic clamp by reflex.

### 6. Tool use forcing: `tool_config` not `tool_choice`

Anthropic:
```python
tool_choice = {"type": "tool", "name": "pose_question"}
```

Gemini:
```python
tool_config = {
    "function_calling_config": {
        "mode": "ANY",
        "allowed_function_names": ["pose_question", "pose_inline_question"],
    }
}
```

When the scaffolding gate fires (pose tools should NOT be available), use `mode: "NONE"`:

```python
tool_config = {"function_calling_config": {"mode": "NONE"}}
```

This is cleaner than the Anthropic pattern of removing the tool from the schema list entirely — the gate just toggles the mode.

### 7. Tool schemas: OpenAPI subset

Anthropic tool schema uses a JSON-Schema-ish format with `input_schema`. Gemini uses a **subset of OpenAPI** (`type: "OBJECT"`, `properties`, `required`). Conversions:

| Anthropic | Gemini |
|---|---|
| `"type": "object"` | `"type": "OBJECT"` |
| `"type": "string"` | `"type": "STRING"` |
| `"type": "integer"` | `"type": "INTEGER"` |
| `"type": "boolean"` | `"type": "BOOLEAN"` |
| `"input_schema": {...}` | `"parameters": {...}` |
| `name`, `description`, `input_schema` | `name`, `description`, `parameters` |

Unsupported in Gemini OpenAPI subset (silently dropped): `oneOf`, `$ref` cycles, `anyOf` with complex patterns. Keep tool schemas flat.

### 8. Caching: implicit only, instrument anyway

Anthropic uses explicit `cache_control: {"type": "ephemeral"}` on the stable prefix; the LLMResponse dataclass exposes `cache_read_tokens` / `cache_creation_tokens`.

Gemini uses **implicit caching** — silent, no markers, no API field reporting whether a hit happened. The `CACHE_BREAK_MARKER` is meaningless. Don't emit it.

Telemetry: the `[QuestionTool] llm_response` log line should still print cache fields for Gemini calls, populated from `usage_metadata.cached_content_token_count` when present (only populated for explicit caches you create via the Context Caching API — which we're not doing in Phase 1).

### 9. Multimodal ordering: image FIRST, text AFTER

When a figure is attached to a step, current Anthropic builder places text first then image. Gemini docs are explicit: *"place the text prompt after the image part in the contents array"* for single-image tasks.

For Gemini, the figure attachment block becomes:

```python
contents = [
    Part.from_bytes(figure_bytes, "image/png"),
    user_preamble_text + "\n\nDescribe what's in the figure above as part of your tutoring.",
]
```

### 10. Thinking mode: pin to `low` for chat

Gemini 3 has `thinkingLevel` parameter (`minimal | low | medium | high`). Production chat path should pin to `low` to keep p99 latency bounded. The docs warn against using `thinkingBudget` on Gemini 3 ("may result in unexpected performance"). Always use `thinkingLevel`.

## Section-by-section conversion map for THIS codebase

Use this table as the checklist when porting the tutor prompt:

| Anthropic section (`conversational_tutor.py:98+`) | Gemini destination | Conversion notes |
|---|---|---|
| `<identity>` | `system_instruction` | Strip persona priming; factual role only |
| `<rules>` | `system_instruction` | Convert each `Do NOT X` to positive form when possible |
| `<lesson_context>` | `system_instruction` | Markdown headers (## Lesson) |
| `<media_catalog>` | `system_instruction` | Plain numbered list — Gemini handles structure fine |
| `<mobile_response_format>` | `system_instruction` | Move into system_instruction (stable across turns) |
| `<final_reminder>` | End of user message | Long-context rule: critical instructions at END |
| `figure_facts` block | End of user message | Per-turn, attaches to current step |
| `regen` constraint block | End of user message | Per-turn, replaces or augments previous attempt |
| `math_eval` signal | End of user message | Last-mile verdict before student message |
| `time` awareness | End of user message | Per-turn |
| `bank_grade` signal | End of user message | Last block before scaffolding directive |
| `scaffolding_directive` | End of user message | LAST block before student input — highest salience |
| `working_analysis` | End of user message | Per-turn diagnostic |

## Tool schema conversions for THIS codebase

### `pose_question` tool

Anthropic shape (current):
```python
{
    "name": "pose_question",
    "description": "Pose a bank question to the student...",
    "input_schema": {
        "type": "object",
        "properties": {
            "slot": {"type": "integer", "description": "..."},
            "lead_in": {"type": "string", "description": "..."},
        },
        "required": ["slot"],
    },
}
```

Gemini shape:
```python
{
    "name": "pose_question",
    "description": "Pose a bank question to the student...",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "slot": {"type": "INTEGER", "description": "..."},
            "lead_in": {"type": "STRING", "description": "..."},
        },
        "required": ["slot"],
    },
}
```

Wrap in:
```python
tools = [Tool(function_declarations=[pose_question_decl, pose_inline_decl])]
```

### `pose_inline_question` tool

Same conversion. Note: Gemini's enum support is limited — the MCQ `correct` field that's currently restricted to "A"/"B"/"C"/"D" via Anthropic JSON Schema enum works in Gemini OpenAPI subset (single-level enum is supported).

## Anti-patterns specific to the ai-tutor + Gemini combination

- ❌ Don't carry over the Anthropic temperature clamp [0.1, 0.3]. Use Gemini default 1.0.
- ❌ Don't preserve `CACHE_BREAK_MARKER` placement — meaningless to Gemini, just clutter.
- ❌ Don't keep the multi-bullet "RULE_1 / RULE_2 / RULE_3" structure if it ends up with 6+ bullets of negatives — Gemini 3 over-indexes. Collapse to 2-3 short positive clauses.
- ❌ Don't pass the same prompt verbatim and expect parity — the **whole point** of the provider builder split is to give Gemini a prompt shaped for Gemini.
- ❌ Don't enable Gemini grounding (`google_search` tool) for tutoring — adds latency, no quality gain on curriculum-bounded content.

## Verification checklist before benchmarking

- [ ] `system_instruction` is non-empty and contains role + lesson context + rules
- [ ] User-message preamble contains per-turn blocks ending with student input
- [ ] Negative-rule count in `system_instruction` ≤ 5 (audit any over)
- [ ] Tool schemas use Gemini OpenAPI subset (`OBJECT`, `STRING`, `INTEGER`)
- [ ] `tool_config.function_calling_config.mode` set to `ANY` for normal turns, `NONE` for scaffolding
- [ ] `thinkingLevel = "low"` on the call config (not `thinkingBudget`)
- [ ] Temperature is 1.0 unless explicitly overridden with measured justification
- [ ] Figure attachments place image part BEFORE text part
- [ ] Cache metrics logged but not relied on for cost calculations (implicit caching is silent)

## Further context

- `gemini-prompting-expert` — universal Gemini rules (don't duplicate here)
- `prompting-fundamentals-expert` — eval-first iteration, CoT-vs-not, format sensitivity
- `tutoring-engine-expert` — codebase-specific tutor engine architecture
- `memory/provider_specific_prompt_system_plan.md` — overall migration plan
