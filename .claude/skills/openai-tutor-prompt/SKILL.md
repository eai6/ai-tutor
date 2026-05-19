# OpenAI Tutor Prompt — Expert (ai-tutor project)

Project-specific. **Companion to (not replacement for) `openai-prompting-expert`** — read that first for universal OpenAI rules. This skill captures how the ai-tutor codebase's Anthropic-shaped tutor prompt converts to OpenAI-native form (GPT-5 family + o-series).

## When to use this skill

- Writing or modifying `apps/tutoring/prompts/openai.py` (the OpenAI tutor prompt builder)
- Adding a new dynamic-suffix block that needs to ship to all three providers
- Debugging an OpenAI-specific tutoring quality regression
- Picking which OpenAI variant to use per task (GPT-5.2 Instant vs Thinking vs o-series)

## Core conversions — Anthropic → OpenAI

### 1. System message → `developer` role

OpenAI's Responses API distinguishes three message roles:

| Role | Use | Authority |
|---|---|---|
| `developer` | Application-level instructions (the tutor system prompt) | Highest — overrides user |
| `user` | Student input / dynamic context | Mid |
| `assistant` | Tutor responses | N/A (model output) |

Convert the Anthropic system text into a `developer` role message. Don't use `system` role — that's the legacy name; `developer` is the current convention and has stronger override semantics in the instruction hierarchy.

```python
messages = [
    {"role": "developer", "content": stable_prefix_text},
    *conversation_history,
    {"role": "user", "content": dynamic_suffix + student_input},
]
```

### 2. XML sections → markdown headers

OpenAI models (especially GPT-5+) handle markdown structure naturally. Convert:

```diff
- <identity>
- You are a friendly, encouraging tutor for secondary school students at
- {institution_name} ({locale_context}). Your name is {tutor_name}.
- </identity>
+ ## Role
+ You are a tutor for secondary school students at {institution_name}
+ ({locale_context}). Your name is {tutor_name}.
```

OpenAI tolerates XML too — but markdown is cleaner and the structure is what matters. Don't mix the two within one prompt.

### 3. Auto-caching: no markers needed

Anthropic uses explicit `cache_control: {"type": "ephemeral"}` markers.

**OpenAI uses automatic prompt caching** — no marker required. The platform caches stable prefixes ≥1024 tokens automatically. The `developer` message will be cached if reused across calls.

To maximize cache hits:
- Keep the `developer` message **byte-identical** across turns within a session
- Put all per-turn dynamic content in the trailing `user` message
- Don't interpolate timestamps or session-specific IDs into the `developer` message

This means the dynamic-block layout differs from Anthropic:

| Block | Anthropic placement | OpenAI placement |
|---|---|---|
| Identity / rules / lesson context | System prefix, above CACHE_BREAK_MARKER | `developer` message (cached automatically) |
| figure_facts (per-step) | Dynamic suffix | Last `user` message preamble |
| bank_grade / scaffolding / math_eval | Dynamic suffix | Last `user` message preamble |
| Student input | Last user message | Last `user` message (after dynamic preamble) |

### 4. Tool schemas: Structured Outputs JSON Schema

OpenAI tools use **Structured Outputs**, a strict subset of JSON Schema enforced at decode time. Conversions:

```python
# Anthropic
{
    "name": "pose_question",
    "description": "...",
    "input_schema": {
        "type": "object",
        "properties": {...},
        "required": [...],
    },
}

# OpenAI
{
    "type": "function",
    "function": {
        "name": "pose_question",
        "description": "...",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {...},
            "required": [...],
            "additionalProperties": False,
        },
    },
}
```

**Two critical extras** that don't exist in the Anthropic schema:
- `"strict": True` — opt into constrained-decoding enforcement
- `"additionalProperties": False` — required when strict is true; OpenAI rejects schemas without it

### 5. Forcing tool use: `tool_choice`

```python
# Force any tool from a specific list (no current Anthropic equivalent of this exact shape)
tool_choice = {"type": "function", "function": {"name": "pose_question"}}

# Force ANY tool to be called
tool_choice = "required"

# Auto (default)
tool_choice = "auto"

# Disable tools entirely (use this for scaffolding gate)
tool_choice = "none"
```

For our scaffolding gate, use `tool_choice = "none"` rather than removing tools from the array — cleaner intent.

### 6. o-series models: strip CoT scaffolding

If benchmarking includes o3, o4-mini, or any o-series model, **remove all CoT prompting from the developer message**. These models reason internally; CoT prompting hurts them.

Specifically strip:
- "Let's think step by step"
- "First, ... Second, ... Third, ..."
- Multi-step reasoning examples (few-shot CoT)
- "Show your reasoning"

Keep:
- Role definition
- Output format requirements
- Tool descriptions
- Hard rules (safety, leak prevention)

For GPT-5+ (non-thinking variants), CoT prompting is neutral-to-helpful — keep current structure.

### 7. Reasoning effort parameter

GPT-5.2 Thinking and o-series expose `reasoning_effort`:

| Value | When to use |
|---|---|
| `low` | Default for conversational tutoring (cost-sensitive, latency-bounded) |
| `medium` | Math-heavy lessons, multi-step misconception diagnosis |
| `high` | Reserve for hardest math problems; expect $/latency cost |

Pin to `low` for the production tutor path. Bump per-step to `medium` for math steps with explicit difficulty marker. Never use `high` in production.

### 8. Temperature

Unlike Gemini 3's "leave at default", OpenAI temperature is tunable for tutoring without warnings. The current Anthropic clamp [0.1, 0.3] is reasonable for OpenAI too:

- `0.0` — too rigid, fails to vary scaffolding hints
- `0.2-0.3` — sweet spot for tutoring (current Anthropic value, transferable)
- `0.7+` — too creative, model invents off-curriculum tangents

Recommended default: `0.2` for GPT-5.2 Instant, leave at default (1.0) for GPT-5.2 Thinking (reasoning models prefer default temp).

### 9. Verbosity control (GPT-5+)

GPT-5 introduced a `verbosity` parameter:

```python
response = client.responses.create(
    model="gpt-5.2",
    verbosity="medium",  # low | medium | high
    ...
)
```

Tutoring uses `medium` — `low` truncates explanations; `high` produces info-dumps that violate the implicit "deliver concise scaffolding" pedagogy.

### 10. Multimodal: image-text ordering flexible

Unlike Gemini's strict "image FIRST, text AFTER" rule, OpenAI handles both orderings well. Current Anthropic placement (text describing the figure first, then image) can stay.

## Section-by-section conversion map for THIS codebase

| Anthropic section | OpenAI destination | Notes |
|---|---|---|
| `<identity>` | `developer` message ## Role | Keep persona but tighten |
| `<rules>` | `developer` message ## Rules | Markdown bullets; positive framing helpful but not required |
| `<lesson_context>` | `developer` message ## Lesson | Keep structure |
| `<media_catalog>` | `developer` message ## Media | Numbered list |
| `<mobile_response_format>` | `developer` message ## Output | Stable across turns |
| `<final_reminder>` | End of last `user` message | Instruction recency still helps |
| `figure_facts` | Last `user` preamble | Per-turn |
| `regen` constraint | Last `user` preamble | Per-turn |
| `math_eval` signal | Last `user` preamble | Per-turn — high salience |
| `time` awareness | Last `user` preamble | Per-turn |
| `bank_grade` signal | Last `user` preamble | Per-turn |
| `scaffolding_directive` | Last `user` preamble | LAST before student input |
| `working_analysis` | Last `user` preamble | Per-turn diagnostic |

## OpenAI variant picker for THIS codebase

| Variant | Use when | Avoid when |
|---|---|---|
| **GPT-5.2 Instant** | Default conversational tutoring (humanities, geography, conceptual science) | Multi-step math with show-working |
| **GPT-5.2 Thinking** | Math lessons; misconception diagnosis on multi-step problems | Bare-answer turns (overkill latency) |
| **GPT-5.2 Thinking + reasoning_effort=medium** | Hardest math problems on a struggling student's session | Default — too expensive |
| **o3 / o4-mini** | Don't pick for tutoring in 2026. GPT-5 supersedes per OpenAI announcement. | Always (now) |
| **GPT-4.1 / 4o** | Legacy, fallback only | New deploys |

## Tool schema conversions for THIS codebase

### `pose_question` tool

```python
{
    "type": "function",
    "function": {
        "name": "pose_question",
        "description": (
            "Pose a bank question to the student by referencing one of the "
            "numbered slots in the catalog above. The student will see the "
            "question rendered verbatim in the chat + artifact panel."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "integer",
                    "description": "The slot number (1-indexed) of the bank question to pose.",
                },
                "lead_in": {
                    "type": "string",
                    "description": "Short transition phrase before the question (NOT a question itself).",
                },
            },
            "required": ["slot", "lead_in"],
            "additionalProperties": False,
        },
    },
}
```

Note `lead_in` is now required (OpenAI strict mode requires all properties be in `required` OR explicitly nullable). Pass an empty string `""` when no lead-in is needed.

### `pose_inline_question` tool

Same pattern. MCQ enum on `correct` field:

```python
"correct": {
    "type": "string",
    "enum": ["A", "B", "C", "D"],
    "description": "The letter of the correct option.",
}
```

OpenAI strict mode supports single-level enum well.

## Anti-patterns specific to the ai-tutor + OpenAI combination

- ❌ Don't use `system` role — it's legacy. Use `developer` for the tutor prompt.
- ❌ Don't put per-turn dynamic content in `developer` message — breaks auto-caching.
- ❌ Don't omit `additionalProperties: false` in strict tool schemas — OpenAI rejects.
- ❌ Don't carry CoT scaffolding to o-series models — strip it.
- ❌ Don't enable `verbosity: high` — info-dumps in student chat.
- ❌ Don't use GPT-4o / GPT-4.1 for new tutor deploys — GPT-5 supersedes per OpenAI's 2026 announcement.
- ❌ Don't enable function-calling parallel calls — pose_question + pose_inline_question are mutually exclusive per turn.

## Verification checklist before benchmarking

- [ ] `developer` message contains role + rules + lesson context + tool docs + media catalog
- [ ] `developer` message is byte-identical across turns in a session (cache hit)
- [ ] Last `user` message contains per-turn dynamic blocks ending with student input
- [ ] All tools have `strict: True` + `additionalProperties: False`
- [ ] All required fields present in tool schemas
- [ ] `tool_choice` correct per turn: `"required"` for normal, `"none"` for scaffolding
- [ ] `reasoning_effort` set per variant (Thinking variants) or omitted (Instant)
- [ ] `verbosity = "medium"` (don't omit — default may have changed across GPT-5 minor versions)
- [ ] CoT scaffolding stripped if o-series model used
- [ ] Temperature 0.2 for Instant, 1.0 (default) for Thinking
- [ ] First call per session warms cache (~1024+ tokens in developer message)

## Further context

- `openai-prompting-expert` — universal OpenAI rules (Structured Outputs, instruction hierarchy, o-series specifics)
- `prompting-fundamentals-expert` — eval-first iteration, format sensitivity
- `tutoring-engine-expert` — codebase-specific tutor engine architecture
- `memory/provider_specific_prompt_system_plan.md` — overall migration plan
