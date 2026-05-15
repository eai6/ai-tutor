# Content-Quality Review + Regeneration Plan (2026-05-15)

## Problem

The runtime tutoring pipeline has a strong AI quality gate — 8 concurrent
post-hoc judges + bounded regen ensemble — but the **content generation**
pipeline that produces lesson steps, exit-ticket questions, and lesson
images has only **deterministic arithmetic checks**. Everything else lands
in the database unreviewed. Teachers report visible quality issues
(misleading images, factually-shaky questions, awkward step scripts) and
have no AI safety net.

User asks for four things:

1. **AI judge stack for generated content** — mirror the tutoring judge
   pattern, run after generation, recommend regen if issues are found.
   Use Gemini (with Google Search grounding) since OpenAI generates the
   images — different provider does the review.
2. **Bounded regen cycles** — like the tutoring regen (configurable, ~2
   cycles by default).
3. **Manual regen UI per content piece** — on the lesson-step detail
   page and the exit-ticket question editor, a "Regenerate with prompt"
   input that takes the current text as context and applies a teacher's
   guidance to produce a revised version. Same shape as the existing
   media regen flow.
4. **Content-quality benchmark** — track every human edit to images /
   exit-ticket questions / step scripts, tag with error categories,
   surface metrics. Mirrors the existing tutoring benchmark structure.
   Goals: improve quality, baseline current quality, A/B prompt changes.

## Current state (from audit)

**Generation entry points:**
- `apps/curriculum/pipeline.py::generate_lesson_content()` (~line 883) —
  one LLM call per lesson via structured-output instructor wrapper,
  produces `LessonContentResult` (steps array + exit ticket + vocab).
- `apps/tutoring/image_service.py::get_or_generate_image()` (line 122) —
  dual-provider routing (OpenAI gpt-image-2 / Gemini 3.1-flash-image),
  supports in-place edits when `current_image_url` is passed.
- Math arithmetic check is the only AI gate today (deterministic regex
  on numeric content), confined to runtime tutoring not content gen.

**Reusable scaffolding identified:**
- `apps/tutoring/judges/` — 8 fail-soft concurrent judges with structured
  `CombinedJudgeResult`. Clean interface to mirror.
- `apps/tutoring/regen/` — cycle cap + temp decay + score-and-pick.
  Pure on a validation result; can target generated content directly.
- `apps/tutoring/image_service.py::_generate_with_gemini()` lines 385-391
  — Gemini Google Search grounding already wired, image-only today.
- `apps/benchmark/` — frozen-snapshot + auto-label-derivation pattern
  works for any content type, not just tutor turns.
- `apps/dashboard/views.py::regenerate_media` (line 4682-4731) —
  per-image manual regen flow; replicable shape for step text + exit
  question.

**Existing surface affordances:**
- Lesson detail page shows steps; no "Regenerate step" button today.
- Exit ticket editor (`exit_question_edit`, line 3721) lets teacher
  edit fields individually; no AI regen.
- `lesson_detail.html` already has per-image regen UI to copy.

## Target design — four sub-systems

### A. Content-quality judges (mirror `apps/tutoring/judges/`)

New package: `apps/curriculum/content_judges/`. Each judge is fail-soft,
runs concurrently via `ThreadPoolExecutor`, returns a structured result
with `passed: bool`, `issues: list[str]`, `recommended_fix: str|None`,
`skipped: bool`, `skip_reason: str`.

**Judges shipped in v1:**

> **Pre-generation vs post-generation** — per user direction, the
> `image_prompt` judge runs **BEFORE** image generation (not after), so a
> bad prompt gets revised before a bad image is even produced. Same may
> apply to step prompts in a future iteration. Post-gen judges still run
> on the produced output as a defensive layer.

1. **`image_prompt.py`** — **PRE-GEN.** Runs *before* image generation.
   Is the prompt clear, single-concept, free of hallucination triggers
   ("Mount X in Seychelles" should be flagged before we ever ask the
   model to draw it)? When the judge flags issues, the prompt is rewritten
   (using the judge's recommended_fix) and re-validated. Cap = 2 cycles.
   Only after a clean prompt does image generation fire.
2. **`figure_alignment.py`** — **POST-GEN.** Does the generated image
   actually match the (now-validated) prompt + the lesson context?
   Uses Gemini vision + Search grounding. Verifies: subject matter,
   key concepts visible, no misleading labels, no hallucinated geography.
3. **`factual_step.py`** — does the lesson step's teaching content
   contain factually questionable claims? Uses Gemini Search to verify
   any named place, date, statistic, scientific claim.
4. **`pedagogy_step.py`** — is the step pedagogically sound? Has clear
   teaching objective alignment, appropriate DOK level, age-appropriate
   language, ends with a learning-promoting prompt (not just exposition).
5. **`exit_question.py`** — does the exit ticket question have a single
   defensible correct answer, unambiguous distractors, no trick wording,
   no errors in stated answer?
6. **`safety_content.py`** — child-safety + cultural-appropriateness
   for the Seychelles pilot context. Smaller-stakes version of the
   tutoring `safety.py` judge.

Each judge defines a **map_to_violations()** helper that converts its
output into a small, stable set of violation codes — e.g.
`figure_alignment` → `{FIGURE_SUBJECT_MISMATCH, FIGURE_HALLUCINATED_LABEL,
FIGURE_LOW_RESOLUTION}`. The violation set is the contract that the
regen + benchmark layers consume.

**Provider strategy — multi-provider chain per judge.** Each content
judge gets a provider chain (mirroring `apps/curriculum/vision_ocr.py`):
Gemini primary (for Google Search grounding), Anthropic / OpenAI
fallbacks. If Gemini is rate-limited or down, the fallback provider
runs the same judge (without grounding for non-Gemini providers — call
that out in the result). Per-judge override via
`ModelConfig.get_for('content_judge_<judge_name>')` so each judge can
prefer a different model independently. **Cross-provider review by
design** — OpenAI generates the image, Gemini reviews it; same model
should never be both generator AND judge for the same artefact.

### B. Bounded content regen (mirror `apps/tutoring/regen/`)

New module: `apps/curriculum/content_regen/__init__.py`.

**Triggers:**
- **Automatic at generation time** — after each LessonStep + ExitTicket
  + image is created, judges run; if any judge fails, regen fires (up
  to `CONTENT_REGEN_MAX_CYCLES` default 2). **If after all cycles the
  content STILL has judge violations**, the content is saved BUT flagged:
  `content_quality_status = 'needs_human_review'` and the unresolved
  judge issues are persisted on the row. The lesson detail UI surfaces
  a "⚠ Needs human review" badge on the step; a dashboard tab lists
  all such items so teachers can address them in batch. **No silent
  failure** — every piece that the AI couldn't fix bubbles up.
- **Manual at teacher request** — `+ Regenerate with AI review` button
  on lesson step / exit ticket / image (separate from manual-prompt
  regen below, which is "teacher knows what's wrong, here's a prompt").

**Cycle behavior:** mirror tutoring regen with temperature decay
(0.20 → 0.15 → 0.10 over 2 cycles). Per-cycle: regenerate using prior
content + judge feedback as context, re-run judges, score, pick best.

**Audit trail:** every regen cycle writes to `step.metadata['regen_audit']`
with `{cycle, judge_issues, candidate_text_preview, scoring_breakdown,
model_used}`. Same shape as `SessionTurn.metadata['regen_audit']`.

### C. Manual regen UI per content piece

Three surfaces — same UX pattern across all three (mirroring existing
media regen at `lesson_detail.html` + `views.py::regenerate_media`):

| Surface | Edit field | Regen control |
|---|---|---|
| Lesson step detail page | `step.content` textarea | "🔁 Regenerate with prompt" — teacher writes "make this shorter / less abstract / add Seychelles example" + clicks Apply; current step content goes in as context, regen happens, result replaces the textarea (review-before-save) |
| Exit ticket question edit page | question_text + options | Same pattern, current question fields as context |
| Per-image (already exists) | image URL | Existing affordance; just confirm it stays in place |

**Two-mode toggle on each surface:**
- **Auto-review mode** — runs the AI judges + regen ensemble, no teacher
  prompt needed. "Make this better."
- **Prompt mode** — teacher supplies specific guidance text. Bypasses
  the judges, just regenerates with the prompt + current content.

Both modes write to the regen audit. Teacher reviews the result before
saving — no auto-commit.

### D. Content-quality benchmark (mirror `apps/benchmark/`)

New models in `apps/curriculum/quality_benchmark/` (separate Django app):

- **`ContentEditEvent`** — fires every time a teacher edits a step's
  content, an exit-ticket question's text/answer, or replaces an image.
  Captures `{content_type, content_id, lesson_id, before_text,
  after_text, edit_diff, error_tags: list[str], teacher_notes, edited_by,
  edited_at}`. Auto-populated `suggested_error_tags` from the diff +
  any pre-existing judge_outputs on the content.
- **`ContentEditTag`** — controlled vocabulary, stable labels:
  `FACTUAL_INCORRECT`, `MISLEADING_IMAGE`, `WRONG_ANSWER_KEY`,
  `AMBIGUOUS_QUESTION`, `OFF_TOPIC`, `WRONG_GRADE_LEVEL`,
  `POOR_PEDAGOGY`, `CULTURAL_MISFIT`, `FORMAT_ISSUE`, `OTHER`. Same
  shape as `apps/benchmark/labels.py` ISSUE_LABELS.
- **`ContentQualityRun`** — aggregate metrics over a slice of edits
  (per-judge precision, per-tag frequency, regen-success rate).

**Auto-label suggestions** — derived from the actual edit diff (e.g. if
teacher changed "Mount Trois Frères is 3000m" → "Mount Trois Frères is
700m", suggest `FACTUAL_INCORRECT`). Same approach as
`apps/benchmark/autopopulate.py::derive_suggested_labels`.

**Goals served:**
1. **Quality measurement** — quantify "how much do teachers actually
   correct?" per generation prompt version. Surface in admin dashboard.
2. **Baseline benchmark** — frozen edit set becomes the test fixture
   for prompt-tuning A/B (run new prompt → check if it produces content
   that would survive edits).
3. **Prompt iteration** — high-frequency tag → that's where the prompt
   needs to be tightened. Surface "FACTUAL_INCORRECT triggers most
   often on geography lessons in S3" type insights.

CSV + JSONL export endpoints mirror `apps/benchmark/views.py`.

## Data model changes

**New tables:**

```python
# apps/curriculum/content_judges/models.py — minimal, for audit
class ContentJudgeRun(models.Model):
    """One run of the judge stack against one piece of content."""
    content_type = models.CharField(choices=('step', 'exit_question', 'image'))
    content_id = models.IntegerField()  # PK of LessonStep / ExitQuestion / MediaAsset
    lesson = models.ForeignKey(Lesson, on_delete=models.SET_NULL, null=True)
    judge_outputs = models.JSONField(default=dict)  # {judge_name: result_dict}
    triggered_by = models.CharField(choices=('auto_generation', 'manual_regen', 'reprocess'))
    cycle = models.IntegerField(default=0)  # 0 = first attempt, 1+ = regen cycle
    created_at = models.DateTimeField(auto_now_add=True)


# apps/curriculum/quality_benchmark/models.py
class ContentEditEvent(models.Model):
    content_type = models.CharField(...)
    content_id = models.IntegerField()
    lesson = models.ForeignKey(Lesson, on_delete=models.SET_NULL, null=True)
    before_payload = models.JSONField()   # frozen content state pre-edit
    after_payload = models.JSONField()    # post-edit state
    error_tags = models.JSONField(default=list)  # list of ContentEditTag values
    teacher_notes = models.TextField(blank=True)
    edited_by = models.ForeignKey(User, ...)
    created_at = models.DateTimeField(auto_now_add=True)
```

**Existing-model additions:**

```python
# apps/curriculum/models.py::LessonStep
content_quality_status = models.CharField(
    choices=('unreviewed', 'auto_ok', 'auto_flagged', 'human_approved', 'human_edited'),
    default='unreviewed',
)
last_judge_run = models.ForeignKey(ContentJudgeRun, ..., null=True)
# Similar fields on ExitTicketQuestion and MediaAsset (or step.media JSON)
```

## Backend changes

| Module | Change |
|---|---|
| `apps/curriculum/content_judges/__init__.py` | `run_all_content_judges(content, content_type, context) -> CombinedContentJudgeResult` mirroring `apps/tutoring/judges/__init__.py::run_all_judges` |
| `apps/curriculum/content_judges/figure_alignment.py` | Vision + grounding judge |
| `apps/curriculum/content_judges/factual_step.py` | Grounding judge for teaching content |
| `apps/curriculum/content_judges/pedagogy_step.py` | LLM judge with rubric |
| `apps/curriculum/content_judges/exit_question.py` | Question-answer-key alignment + answerability |
| `apps/curriculum/content_judges/image_prompt.py` | Prompt clarity heuristics |
| `apps/curriculum/content_judges/safety_content.py` | Child-safety filter |
| `apps/curriculum/content_regen/__init__.py` | Bounded regen ensemble for content pieces |
| `apps/curriculum/pipeline.py` | Wire judges + regen into `generate_lesson_content()` and `generate_exit_ticket_questions()` |
| `apps/tutoring/image_service.py` | Add post-generation judge call in `get_or_generate_image` |
| `apps/dashboard/views.py` | `step_regenerate_content()` view (auto + prompt modes); `exit_question_regenerate()` view |
| `apps/dashboard/urls.py` | New routes |
| `apps/curriculum/quality_benchmark/{models.py,views.py,labels.py,sampling.py,autopopulate.py}` | Mirrors apps/benchmark layout for content edits |

## Frontend changes

- **Lesson step detail page** — new "🤖 AI Review" section showing latest
  judge result (pass/fail per judge); "🔁 Regenerate" button with
  auto/prompt-mode toggle + textarea.
- **Exit ticket editor** — same controls per question.
- **Lesson detail** — quality-status badges per step (✓ auto_ok, ⚠
  auto_flagged, 👤 human_approved, ✏ human_edited).
- **Content edit benchmark admin** — list view + per-event detail with
  tag picker + diff viewer; CSV/JSONL export buttons.

## Out of scope (deferred)

- Replacing content generation entirely with an agentic pipeline. We
  keep single-call generation + post-hoc review.
- Auto-resolving every flagged issue without teacher review. Manual
  approval stays in the loop.
- Cross-lesson consistency judges ("does this step's terminology match
  the rest of the unit?"). Defer until per-step judges land.
- Real-time judge calls during interactive teacher editing. Judges run
  on save / explicit regen request, not keystroke-by-keystroke.
- Per-institution / per-grade-level judge configuration. v1 uses one
  global judge config; tune via ModelConfig overrides if pilot needs.
- Automated A/B testing of generation prompts. We collect the
  benchmark data; the A/B harness is a follow-up plan.

## Phased delivery

| Phase | Work | Days |
|---|---|---|
| **Q1 — Judge scaffolding + 2 judges** | Build `content_judges/` package skeleton mirroring `tutoring/judges/`. Ship `factual_step.py` + `figure_alignment.py` (highest-leverage 2). Wire into `generate_lesson_content()` post-hoc (no regen yet). Surface results on lesson detail page as read-only badges. | 2 |
| **Q2 — Bounded regen ensemble** | Build `content_regen/__init__.py` mirroring `tutoring/regen/`. Auto-fire on judge-flagged content during generation. Cap = 2 cycles. Write audit trail. | 1.5 |
| **Q3 — Manual regen UI** | "🔁 Regenerate" button + auto/prompt-mode toggle on lesson step + exit ticket detail pages. Backend views handle both modes. Mirror existing image regen UX. | 2 |
| **Q4 — Remaining 4 judges** | `pedagogy_step.py`, `exit_question.py`, `image_prompt.py`, `safety_content.py`. Each ~half a day including tests. | 2 |
| **Q5 — Content edit benchmark** | New `apps/curriculum/quality_benchmark/` app. Models + label vocab + auto-populate from diff. Admin listing + export endpoints (CSV + JSONL). | 2.5 |
| **Q6 — Dashboards + iteration** | Aggregate metrics: per-judge precision (auto-flagged vs human-edited correlation), per-tag frequency, regen-success rate. Surface in admin to drive prompt tuning. | 1 |

Total: ~11 solo-dev days. **First useful slice (Q1+Q2) ships in 3.5
days** — content gets factual + figure-alignment judging plus auto-regen.

## Risks

- **Cost blow-up.** Every lesson generation now triggers 6 judge calls
  + up to 2 regen cycles. Per lesson: 6 judges × 1 call + 12 regen calls
  worst case = up to 18 LLM calls. For a 100-lesson course that's
  1800 calls during initial generation. Budget concern. Mitigation:
  configurable judge subset (e.g. skip `pedagogy_step` for math courses
  where DOK alignment is more deterministic); per-institution cost cap;
  measure actual rates in Q1 before locking the full chain.
- **False-positive churn.** A judge that flags too many true negatives
  causes pointless regen + teacher fatigue. Mitigation: ship each judge
  to a small pilot first; track auto_flagged vs human_edited correlation
  (Q6 metric); deprecate or retune judges with poor signal.
- **Provider lock-in.** All judges default to Gemini for grounding.
  If Gemini rate-limits or contracts change, the whole content
  pipeline stalls. Mitigation: `apps/curriculum/vision_ocr.py`'s
  fallback chain pattern is the template — content_judges should
  accept a provider chain too, not a single client.
- **Edit-tracking privacy.** The benchmark captures every edit's diff.
  If teachers paste anything sensitive, it persists. Mitigation:
  scope edit-tracking to text fields with no PII expected; document in
  the admin UI that edits are recorded.

## Confirmed decisions (user, 2026-05-15)

1. **Judge cadence** — auto-run every generation + manual regen + every
   teacher-triggered review. Cap at 2 cycles. **If still flagged after
   cycle 2 → save anyway, set `content_quality_status='needs_human_review'`,
   surface in dashboard.** No silent failure.
2. **Cross-provider review** — REQUIRED. Same model never both
   generator AND judge for the same artefact. Per-judge model
   configurable (see #6).
3. **Manual-regen scope** — granular per-question for exit tickets
   (mirrors existing `exit_question_edit`). Per-step for lesson steps.
4. **Benchmark scope at launch** — ALL THREE content types (step text +
   exit question + image edits) shipped together. User: "we need to
   evaluate everything now before the pilot."
5. **Quality-status field** — informational only (no publish gate).
   Surfaces a "needs human review" badge + dashboard listing for items
   the judges couldn't auto-fix.
6. **`content_judge` ModelConfig purposes** — per-judge configurable.
   New purposes: `content_judge_image`, `content_judge_factual`,
   `content_judge_pedagogy`, `content_judge_safety`,
   `content_judge_question`, `content_judge_image_prompt`. Each can
   pick its own provider/model. Defaults to Gemini with grounding
   where useful.
7. **Diff storage** — store BOTH full before/after payloads AND
   structured diffs. Payloads for fidelity, diffs for fast querying.

## Next step

All 7 open questions resolved. Start **Q1+Q2 first push**:
- `content_judges/` scaffolding mirroring `tutoring/judges/` shape
- Ship `image_prompt.py` (PRE-gen) + `factual_step.py` + `figure_alignment.py`
  (the 3 highest-leverage judges)
- Multi-provider chain per judge (mirroring `vision_ocr.py`)
- Bounded regen wrapper with `needs_human_review` flag-out behavior
- Wire into `generate_lesson_content()` and `image_service.py`
- Read-only quality badges on lesson detail page

Ships in ~3.5 days. Gives first measurable signal on what the judges
catch and how often regen actually rescues content.
