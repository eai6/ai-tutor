# Provider/model picker for bulk review + bulk image gen (#215 follow-up)

## Problem

Today the bulk-AI-review button always uses the active
`ModelConfig.get_for('content_judge_*')` (with cross-provider fallback
per `apps/curriculum/content_judges/_providers.py`). Bulk image gen
uses whatever the image_generation ModelConfig points at (currently
gpt-image-2).

Teachers want to compare model quality without DB edits — "let me
review this batch with Opus instead of the default judge model" or
"generate these images with Gemini this time, OpenAI last time was
bad". Per-run override, not a global flip.

## Current state (audit)

- **Judge chain builder** — `get_judge_provider_chain(judge_purpose,
  exclude_provider)` in `apps/curriculum/content_judges/_providers.py:69`.
  Returns a list of `JudgeProvider` (name, model_name, client, config).
  Built from active `ModelConfig` rows for the purpose + fallback
  purposes. Adding a `force_model_config` kwarg here propagates the
  override to every judge without touching the call sites.
- **Per-judge entries** — `run_factual_step_judge`,
  `run_pedagogy_step_judge`, `run_safety_content_judge`,
  `run_exit_question_judge`, `run_figure_alignment_judge`. Each calls
  `get_judge_provider_chain(...)` internally. Need a `force_model_config`
  passthrough kwarg on each.
- **Orchestrators** — `_run_content_judges_for_steps`,
  `_run_exit_question_judge_for_mcqs` in
  `apps/curriculum/content_generator.py`. Same passthrough pattern.
- **Bulk helper** — `review_unreviewed_content_async` in
  `apps/dashboard/background_tasks.py`. Accepts a config_id; resolves
  to ModelConfig; passes through.
- **Image service** — `ImageGenerationService.get_or_generate_image`
  already accepts `model_override='openai'|'gemini'` (per-call). The
  bulk image helpers (`generate_media_async`, `generate_media_for_lessons`)
  need to accept + pass that param.

## Target design

### Form (course detail)

Add a `<select name="judge_model">` next to the "Run AI review" button
with these options:
- "Default (per ModelConfig)" → empty value, no override
- "Claude Opus 4.7" → `anthropic:claude-opus-4-7`
- "Claude Sonnet 4.6" → `anthropic:claude-sonnet-4-6`
- "Claude Haiku 4.5" → `anthropic:claude-haiku-4-5-20251001`
- "OpenAI GPT-4o" → `openai:gpt-4o`
- "Gemini 3 Pro" → `google:gemini-3-pro-preview`
- "Gemini 3 Flash" → `google:gemini-3-flash-preview`

Add a `<select name="image_provider">` next to "Generate pending images":
- "Default" → empty
- "OpenAI gpt-image-2" → `openai`
- "Gemini Flash Image" → `gemini`

(Curated hardcoded list — teachers don't manage ModelConfig.)

### Backend plumbing

1. New helper `apps/llm/models.py::ModelConfig.resolve_runtime(provider, model_name)`:
   - Looks for an existing active `ModelConfig` row matching the
     `(provider, model_name)` pair (any institution / any purpose) —
     reuse credentials + api_key_env_var.
   - If none found, builds an in-memory `ModelConfig` (NOT saved) with
     conservative defaults (max_tokens=2048, temperature=0.0) and
     api_key_env_var inferred from provider (`ANTHROPIC_API_KEY`,
     `OPENAI_API_KEY`, `GOOGLE_API_KEY`).
   - Returns the config or `None` if provider unknown.

2. `get_judge_provider_chain(...)` — accept
   `force_model_config: ModelConfig | None = None`. When set, build a
   one-item chain from it (skipping the purpose lookup + the
   exclude_provider filter).

3. Each judge `run_*_judge(...)` — accept and pass through
   `force_model_config`.

4. `_run_content_judges_for_steps(lesson, steps, *, force_model_config=None)`
   and `_run_exit_question_judge_for_mcqs(lesson, mcqs, *, force_model_config=None)`
   — accept and pass.

5. `review_unreviewed_content_async(course_id, upload_id, *, judge_provider=None, judge_model=None)`
   — resolve to a ModelConfig at the top, pass to both orchestrators.

6. `generate_media_async(course_id, upload_id, *, force_regenerate=False, image_provider=None)`
   and the inner `generate_media_for_lessons` — pass
   `model_override=image_provider` to each `get_or_generate_image` call.

### View layer

- `course_review_unreviewed` view reads `judge_model` POST field,
  splits on `:` into `provider, model_name`, passes to async helper.
- `course_generate_media` view reads `image_provider` POST field,
  passes to async helper.

### Display

Log the selected provider/model into the CurriculumUpload's
processing_log at the start of the run so teachers see what was
actually used.

## Files to touch

| File | Change |
|---|---|
| `apps/llm/models.py` | + `ModelConfig.resolve_runtime(provider, model_name)` helper. |
| `apps/curriculum/content_judges/_providers.py` | + `force_model_config` kwarg on `get_judge_provider_chain`; short-circuit to single-item chain when set. |
| `apps/curriculum/content_judges/factual_step.py` | passthrough kwarg. |
| `apps/curriculum/content_judges/pedagogy_step.py` | passthrough kwarg. |
| `apps/curriculum/content_judges/safety_content.py` | passthrough kwarg. |
| `apps/curriculum/content_judges/exit_question.py` | passthrough kwarg. |
| `apps/curriculum/content_judges/figure_alignment.py` | passthrough kwarg. |
| `apps/curriculum/content_generator.py` | `_run_content_judges_for_steps` + `_run_exit_question_judge_for_mcqs` accept + propagate. |
| `apps/dashboard/background_tasks.py` | `review_unreviewed_content_async` + `generate_media_async` accept + propagate. |
| `apps/dashboard/views.py` | views read POST + resolve + pass. |
| `templates/dashboard/curriculum/course_detail.html` | two `<select>` dropdowns inline next to buttons. |

## Out of scope

- No per-judge picker (e.g. "use Opus for factual but Gemini for
  pedagogy") — single model for the whole run.
- No persistence of last-used model on the course — every run is
  independent.
- No cost estimate / preview ("this will hit Anthropic ~N times at $X
  each") — could be a follow-up.

## Risks

- **Cost surprise**: a teacher clicks "Run AI review" with Opus
  selected → ~6-10× cost vs Haiku. Mitigation: render the model name
  in the confirm() dialog so they see what they picked.
- **Auth gaps**: if the picked provider has no API key in env, the
  judge fails fast and logs to processing_log. Already fail-soft per
  the existing chain logic.

## Phased delivery

| Phase | Work | Estimate |
|---|---|---|
| 1 | `resolve_runtime` + chain override (1 LOC paths) | 0.25 day |
| 2 | Per-judge passthrough (5 judges, mechanical) | 0.25 day |
| 3 | Orchestrator + background helper passthrough | 0.25 day |
| 4 | View + template wiring + confirm dialog | 0.25 day |
| 5 | Smoke test both flows via test client | 0.15 day |

Total ~1.1 days.

## Next step

Get user direction: implement now (single PR follow-up to #215) or
file as #216 and ship current state first?
