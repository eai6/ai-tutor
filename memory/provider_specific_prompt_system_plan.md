# Provider-Specific Tutor Prompt System — Plan (2026-05-19)

## Problem

The tutor system prompt (`TUTOR_SYSTEM_PROMPT_TEMPLATE`, ~50KB XML at `apps/tutoring/conversational_tutor.py:98`) is Anthropic-shaped: XML tags throughout, hedged-negative instructions ("do NOT re-recite"), temperature clamped to [0.1, 0.3], persona priming, explicit `cache_control` markers via `CACHE_BREAK_MARKER`. This shape was tuned for Sonnet then Opus 4.7.

Production tutor is **Opus 4.7 @ temp 0.0**. Cost analysis (task #220) showed tutor ≈ 45% of session cost. Gemini 3 Pro/Flash and GPT-5 lineup launched after this prompt was written and could plausibly match or beat Opus at lower cost — but the prompt is the wrong shape for them:

- **Gemini 3**: dislikes XML-heavy / flowery / negative-only prompts; expects `system_instruction` as a top-level parameter (re-sent every call); temp default 1.0 with explicit warning against tuning; `tool_config.function_calling_config.mode=ANY` for forcing tool use; image FIRST text AFTER for vision.
- **GPT-5 / GPT-5.2**: `developer` role for system-level instructions (above user); Structured Outputs API for tool schemas; o-series strip CoT; auto caching (no explicit markers); reasoning-effort parameter for o-series.

Swapping the tutor model without converting the prompt would test the model unfairly. Converting the prompt for one provider without preserving Anthropic compatibility would break production. Solution: extract prompt assembly into a provider dispatcher.

## Current state (from audit)

- **Template**: `apps/tutoring/conversational_tutor.py:98-660ish` — single `TUTOR_SYSTEM_PROMPT_TEMPLATE` with `{placeholder}` interpolation
- **Builder**: `_build_system_prompt(self)` at `apps/tutoring/conversational_tutor.py:5825` — interpolates the template + appends dynamic suffix blocks
- **Cache split**: `CACHE_BREAK_MARKER` (Anthropic-specific) injected at `apps/tutoring/conversational_tutor.py:6019` to split stable prefix from per-turn dynamic suffix
- **Dynamic suffix blocks** (per-turn): figure_facts → regen → math_eval → time → bank_grade → bare_answer → working_analysis → scaffolding_directive (commit 102608e)
- **Tool schemas**: `_build_question_tool` + `_build_inline_question_tool` use Anthropic tool-call shape
- **ModelConfig**: `apps/llm/models.py::ModelConfig` already supports per-purpose provider switching. `tutoring` purpose currently points at `anthropic/claude-opus-4-7`
- **BaseLLMClient**: `apps/llm/client.py:38` — ABC with `AnthropicClient`, `GeminiClient`, `OpenAIClient`, `OllamaClient` concrete classes
- **Existing benchmark**: `memory/eval_benchmark_v2_simplified.md` — 50 frozen items, 30 labels, 19 failure categories
- **Existing skills**: `.claude/skills/` has generic `claude-prompting-expert`, `gemini-prompting-expert`, `openai-prompting-expert` — but no project-specific *tutor* prompt skills

## Target design

```
apps/tutoring/prompts/
    __init__.py                  # registry + get_prompt_builder(provider) dispatcher
    base.py                      # TutorPromptBuilder ABC + shared section assembly
    anthropic.py                 # AnthropicTutorPromptBuilder (current behavior preserved)
    gemini.py                    # GeminiTutorPromptBuilder
    openai.py                    # OpenAITutorPromptBuilder

apps/tutoring/tool_schemas/
    __init__.py                  # get_tool_schemas(provider, tools_to_include, mode) dispatcher
    anthropic.py                 # Current pose_question / pose_inline_question shape
    gemini.py                    # OpenAPI-subset schemas + ToolConfig assembly
    openai.py                    # Function-calling JSON Schema + tool_choice
```

`conversational_tutor.py` becomes a dispatcher:

```python
def _build_system_prompt(self) -> tuple[str, dict]:
    provider = self.tutor_model_config.provider
    builder = get_prompt_builder(provider)
    return builder.build(
        identity=self.identity_context,
        lesson=self.lesson_context,
        rules=self.rule_set,
        dynamic_blocks=self._collect_dynamic_blocks(),  # provider-agnostic dicts
    )
```

Each builder returns `(system_prompt_text, extra_kwargs_for_generate_call)`:
- Anthropic: returns prompt with `CACHE_BREAK_MARKER` in-place, `extra_kwargs = {}`
- Gemini: returns the suffix text only (system_instruction is passed separately at call site), `extra_kwargs = {"system_instruction": prefix_text}`
- OpenAI: returns user-message text only, `extra_kwargs = {"developer_message": prefix_text}` — caller assembles into messages array

The `BaseLLMClient.generate()` signature gets a new `extra_provider_kwargs: dict | None` parameter that gets forwarded to provider-specific machinery.

## Provider-specific deltas (shipped as skills, see below)

### Anthropic builder (zero behavior change)
- Preserve `CACHE_BREAK_MARKER` placement
- Preserve XML tags
- Preserve current negative phrasings
- Preserve tool_choice forcing

### Gemini builder
- Convert XML sections → markdown headers (## SECTION)
- Move identity / persona / rules into `system_instruction` (top-level param)
- Strip persona priming ("you are a friendly tutor at..." → "Tutor for secondary school students at <institution>")
- Convert negative-only rules to positive framing
- Place dynamic per-turn blocks AT END (Gemini long-context query-last rule)
- Tool schema: OpenAPI subset, `tool_config.function_calling_config.mode = "ANY"` + `allowed_function_names` for forcing
- Vision: image FIRST text AFTER for figure-attached turns
- Temperature: **leave at Gemini default 1.0** unless benchmark says otherwise (override `ModelConfig.effective_temperature` clamp for Gemini tutoring)
- No `CACHE_BREAK_MARKER` (implicit caching only — silent)
- `thinking_level = "low"` for production chat (pinned, not dynamic)

### OpenAI builder
- Convert system block → `developer` role message
- Convert XML sections → markdown headers
- Tool schema: Structured Outputs JSON Schema, `tool_choice: {"type": "function", "function": {"name": "pose_question"}}` for forcing
- Strip CoT scaffolding if using o-series models (o3, o4-mini)
- `reasoning_effort = "medium"` for GPT-5 thinking variants
- No explicit caching markers (auto caching applies)

## Data model changes

None required for Phase 1. ModelConfig already supports per-purpose provider dispatch. Phase 2 may add:

- `ModelConfig.provider_settings` JSONField for per-provider knobs (Gemini `thinking_level`, OpenAI `reasoning_effort`, Anthropic `cache_control_strategy`)
- Not blocking — can pass via env vars or constants until justified

## Backend changes

### Phase 1 — extract builder (no behavior change)
- Create `apps/tutoring/prompts/` package
- Move `TUTOR_SYSTEM_PROMPT_TEMPLATE` + dynamic-block builders into `anthropic.py`
- `conversational_tutor.py::_build_system_prompt` calls dispatcher
- All tests still pass; production behavior identical
- ~300 LOC moved, ~50 LOC new dispatcher

### Phase 2 — add Gemini builder
- `gemini.py` builder with markdown sections, positive phrasing, `system_instruction` separation
- Extend `GeminiClient.generate()` to accept `system_instruction` kwarg
- New tool schema converter
- Add `ModelConfig` row for `tutoring/google/gemini-3-pro` (and variants)
- ~400 LOC

### Phase 3 — add OpenAI builder
- `openai.py` builder
- Extend `OpenAIClient.generate()` to assemble developer/user messages from builder output
- New tool schema converter (Structured Outputs)
- Add `ModelConfig` row for `tutoring/openai/gpt-5.2` (and variants)
- ~300 LOC

### Phase 4 — benchmark all variants
- Run `scripts/run_meta_judge.py` or equivalent against eval_benchmark_v2 for each model:
  - Anthropic: Opus 4.7 (control)
  - Google: Gemini 3 Pro, Gemini 3 Thinking, Gemini 3 Flash, Gemini 3.1 Flash-Lite
  - OpenAI: GPT-5.2, GPT-5.2 Thinking
- Score on the 30-label rubric. Report cost per session + quality per session.
- Output: `memory/tutor_model_benchmark_results.md`

### Phase 5 — staged rollout (only if benchmark passes)
- Add `TUTOR_MODEL` env var supporting `<provider>:<model_name>`
- 10% rollout → 50% → 100%
- Kill switch documented

## Frontend/mobile changes

None. The frontend doesn't know which model produced the response.

## Out of scope

- **Judges**: unified judge stays on Gemini 2.5 Flash → Haiku 4.5 fallback (already settled, commit `1784c48` era)
- **Content generation**: stays on Claude (project memory `project_model_routing.md`)
- **Image gen**: stays on OpenAI gpt-image-2
- **Mobile**: deferred; on-device pivot is paused
- **Multi-agent decomposition**: explicitly not now (CLAUDE.md Rule of Three; awaiting benchmark evidence)
- **Per-institution model overrides**: future, not Phase 1-5
- **Adaptive model routing** (cheap model first, escalate on hard turns): future

## Phased delivery

| Phase | Work | Touches prod? | Solo-dev days |
|---|---|---|---|
| 0 | Plan + skills (this file + 2 SKILL.md) | No | 0.25 |
| 1 | Extract Anthropic builder; dispatcher in place | No (behavior preserved) | 1.0 |
| 2 | Gemini builder + GeminiClient extensions + ModelConfigs | No (not wired) | 1.5 |
| 3 | OpenAI builder + OpenAIClient extensions + ModelConfigs | No (not wired) | 1.0 |
| 4 | Benchmark all 7 candidates on eval_benchmark_v2 | No | 1.0 |
| 5 | Staged rollout behind `TUTOR_MODEL` env var | YES | 0.5 |
| **Total** | | | **5.25** |

## Risks

1. **Tool-use compliance**. Haiku 4.5 was rejected for tutoring because it didn't reliably use the `pose_question` tool (auto-memory). Gemini Flash and Flash-Lite are at similar capability tiers — likely same failure mode. **Mitigation**: benchmark Phase 4 specifically tracks `tool_use_count` per turn; reject any model with <90% tool-use compliance on bank-eligible turns.

2. **Just-shipped tutor fixes assume Opus**. Commits `121051f` (BLOCKED render), `3281e70` (info_dump removal), `102608e` (scaffolding directive), `8212522` (hold-gate escape valve) were tuned by reading Opus traces. Some may need re-tuning for Gemini/GPT-5 turn shape. **Mitigation**: re-run a small e2e session under each candidate before scoring; flag any obvious regressions.

3. **Prompt-caching architectural change**. Anthropic explicit `cache_control` is wired through the LLMResponse dataclass (commit `226` era). Gemini uses implicit caching (silent). OpenAI uses auto caching. The cache metrics dashboard will show zeros for non-Anthropic — not a bug, but worth flagging in telemetry.

4. **Cost during benchmark**. Running all 7 models × 50 items × 9 dimensions = ~3,150 judge calls + 350 tutor calls. Estimate $50-150 in API spend. **Mitigation**: budget visible in `memory/deepmind_cost_analysis.md` patterns — fits.

5. **Production tutor regression**. Phase 5 rollout could degrade student experience if the chosen model has subtle failure modes the benchmark missed. **Mitigation**: kill switch via env var; rollback = single env var change + container restart; monitor `[TurnSummary] validator_issues` rates per provider for the first 48h.

## Open questions

1. **Override `effective_temperature` clamp for Gemini?** Currently tutoring is clamped to [0.1, 0.3]. Gemini 3 docs warn against tuning from default 1.0. Recommend: **add per-provider override in ModelConfig**, default 1.0 for Gemini, keep clamp for Anthropic. Confirm before Phase 2.

2. **Gemini Flash-Lite in benchmark?** It's likely below the tool-use compliance threshold. Recommend: **skip Flash-Lite from full benchmark**, document why. User wanted all 4 — but spending the benchmark budget on a model likely to fail tool-use is wasteful.

3. **Which OpenAI variants?** Recommend: **GPT-5.2 Instant (conversational) + GPT-5.2 Thinking (math-heavy)**. Skip o3 (older, GPT-5 supersedes per OpenAI docs). Skip Mini variants for first pass.

4. **Run benchmark on Edward's labels or LLM-judge labels?** v1 had Edward's labels; v2 is LLM-judge. Recommend: **use v2 LLM-judge** so it can rerun cheaply per model. Sample 5 disagreement cases per model for Edward to spot-check.

## Skills to ship (Phase 0, this PR)

Two new skills, placed alongside existing prompting skills:

- `.claude/skills/gemini-tutor-prompt/SKILL.md` — project-specific Gemini conversions for THIS codebase. Companion to (not replacement for) the generic `gemini-prompting-expert`.
- `.claude/skills/openai-tutor-prompt/SKILL.md` — same for OpenAI.

These document the conversion patterns concretely (e.g., "this XML tag → this markdown header"; "this Anthropic tool schema → this Gemini tool_config"; "this negative rule → this positive rephrase") so future Claude bootstraps the conversion without re-deriving the rules.

## Next step

User picks one of:

- **(a)** Approve plan + ship Phase 0 (this file + 2 skills) tonight. Phase 1+ tomorrow.
- **(b)** Approve plan + ship Phase 0 + Phase 1 (extract Anthropic builder, no behavior change) tonight.
- **(c)** Revise the plan — flag what to change.

Refs: auto-memory/project_tutor_model_choice.md, auto-memory/project_model_routing.md, memory/eval_benchmark_v2_simplified.md
