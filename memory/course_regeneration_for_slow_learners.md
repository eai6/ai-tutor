# Course Regeneration for Slow / Weaker Learners — Plan (2026-04-28)

## ⚠️ Design revision (2026-04-28)

**Original assumption**: per-course `generation_profile` (standard /
slow_learner / advanced) drives three different content streams.

**Revised**: lesson content is generated ONCE — always rich enough to
support the slowest learner (lots of figures, varied recognition +
production formats, multiple hint levels). **Personalisation happens
at the tutoring engine, not at content generation.**

Rationale: a single content stream is simpler for teachers (no profile
decision per course), gives every student access to the same rich
material, and locates personalisation where it belongs — at runtime,
informed by the student's actual signals. Adapting at content-time
would have produced three separate cached lessons that drift apart.

What this means for the work:
- `Course.generation_profile` field has been **dropped** (commit X).
- `_profile_rules()` collapsed to a single always-rich ruleset (the
  numbers below match what the original `slow_learner` profile used).
- `_validate_against_profile()` still runs on every generation.
- Phase 2 (tutor engine adaptation) is now the load-bearing
  personalisation work — see "Revised Phase 2" below.

## Problem

The Seychelles pilot has students at a wide range of readiness levels. The
current content generator produces lessons calibrated to a single,
implicit "average learner" profile:

- Figures are LLM-discretionary. A typical lesson has 0–3 images;
  some have none. Slow learners need more visual scaffolding.
- Practice / quiz questions are almost always one or two formats
  (free-text or MCQ). The model technically supports five answer types
  but the prompt never asks for variety, so we underuse `true_false`
  and `short_numeric`.
- The tutoring engine treats every answer type as a flat string
  hint — it does not adapt presentation (e.g. "True or False:") or
  grading per format.

We want to regenerate the Mathematics S3 course (and later other
courses) with stronger visual scaffolding and a deliberately mixed
question-format diet, then make sure the tutor presents and grades
those formats well in conversation. This plan delivers that in three
phases.

## Current state (from audit)

Generation pipeline:

- `apps/dashboard/views.py:3176` — `lesson_regenerate` view (single lesson, has UI button)
- `apps/dashboard/background_tasks.py:867` — `generate_complete_lesson(lesson_id, institution_id, log_fn=None)`
- `apps/curriculum/content_generator.py:220` — `LessonContentGenerator.generate_for_lesson(lesson, save_to_db=True)`
- `apps/curriculum/content_generator.py:562` — `_generate_steps(lesson, curriculum_context)` builds the prompt
- `apps/curriculum/content_generator.py:718-793` — the prompt body. No learner-tier or figure-density rules.
- `apps/curriculum/content_generator.py:1499` — `generate_content_for_unit(unit_id, force=False)` (function, no UI)
- `apps/curriculum/content_generator.py:1565` — `generate_content_for_course(course_id, force=False)` (function, no UI)

Schema:

- `apps/curriculum/content_generator.py:63-77` — `MediaImage` / `StepMedia`
- `apps/curriculum/content_generator.py:127` — `answer_type` Field. **Drift**: docstring says `"none, short_numeric, short_text, multiple_choice, or free_response"` but the LessonStep model accepts `{none, free_text, multiple_choice, short_numeric, true_false}` — the generator never emits `true_false` and emits invalid values (`short_text`, `free_response`).
- `apps/curriculum/content_generator.py:139` — `media: Optional[StepMedia]` — fully optional, no per-lesson floor.
- `apps/curriculum/models.py:253-258` — `LessonStep.AnswerType` enum (the source of truth).

Tutoring engine:

- `apps/tutoring/conversational_tutor.py:3504-3513` — step instruction block. Passes `answer_type` as a flat label, no format-specific scaffolding.
- `apps/tutoring/conversational_tutor.py:3813-3913` — `_evaluate_step` LLM eval. Generic; no per-format grading heuristics.
- `memory/feedback_math_tutoring.md` — math tutor must NOT evaluate bare numeric answers; must teach via named subskills + tips.

## Target design

Drive content variety from a `Course.generation_profile` field
(`standard`, `slow_learner`, `advanced`). The profile is a stable
property of the course, not a per-regeneration kwarg — it persists,
single-lesson regen also uses it, and a teacher recalibrating the
course toggles it once and triggers a bulk regen.

The profile materialises as concrete prompt rules and a
post-generation validator. The tutoring engine then reads each step's
`answer_type` and chooses format-aware presentation + grading.

## Data model changes

Add to `apps/curriculum/models.py::Course`:

```python
class GenerationProfile(models.TextChoices):
    STANDARD     = 'standard',     'Standard'
    SLOW_LEARNER = 'slow_learner', 'Slower / weaker learners (more figures, more MCQ/TF)'
    ADVANCED     = 'advanced',     'Advanced (richer free-response, fewer hints)'

generation_profile = models.CharField(
    max_length=20,
    choices=GenerationProfile.choices,
    default=GenerationProfile.STANDARD,
)
```

Migration: `apps/curriculum/migrations/00XX_course_generation_profile.py` —
single field add with default. Backfill is automatic (existing courses
get `standard`).

**Fix the schema drift** at `content_generator.py:127`: change the
docstring to match `LessonStep.AnswerType` and add `true_false` to the
allowed set. Add a normaliser in `_save_steps_to_db` that maps any
legacy `short_text`/`free_response` LLM emissions to `free_text`.

## Backend changes — Phase 1 (implementation-ready)

### 1. Pass profile into the generator

`apps/curriculum/content_generator.py:_generate_steps` (line 562)
already receives the `lesson` — read `lesson.unit.course.generation_profile`
at the top of the function and derive a small dict:

```python
profile = lesson.unit.course.generation_profile or 'standard'
profile_rules = self._profile_rules(profile, target_minutes, max_steps, is_math)
```

`_profile_rules` returns a struct like:

```python
@dataclass
class ProfileRules:
    profile: str                        # 'standard' | 'slow_learner' | 'advanced'
    min_figures: int                    # floor on StepMedia images across steps
    figure_required_step_types: tuple   # e.g. ('teach','worked_example')
    answer_type_distribution: dict      # {'multiple_choice': 1, 'short_numeric': 1, 'true_false': 1, 'free_text': 1}
    teach_sentence_max: int             # 4 standard, 3 slow_learner, 6 advanced
    hint_count: int                     # 3 slow_learner, 2 standard, 1 advanced
```

Concrete numbers for `slow_learner` (math S3, 6-step lesson):

- `min_figures = 4` (≥1 image on each of: ENGAGE, TEACH, WORKED_EXAMPLE, PRACTICE)
- `figure_required_step_types = ('engage', 'teach', 'worked_example')`
- `answer_type_distribution = {'multiple_choice': ≥2, 'short_numeric': ≥1, 'true_false': ≥1, 'free_text': ≤1}`
- `teach_sentence_max = 3` (shorter chunks)
- `hint_count = 3`

### 2. Inject profile rules into the prompt

Add a new block in `_generate_steps` between the existing
`CONTENT GUIDELINES` block (currently `content_generator.py:770`) and
the closing prompt fence. Render as a labelled section:

```
LEARNER PROFILE: {profile}
{profile_description}

VISUAL SCAFFOLDING RULES:
- Generate at least {min_figures} StepMedia images across the lesson.
- Every step of type {figure_required_step_types} MUST include a media figure.
- Figures should illustrate the concept directly (diagram, schematic, labelled
  example), not decorative. Caption each figure with the key takeaway.

QUESTION FORMAT MIX (across practice + quiz steps):
- At least {N_mcq} multiple_choice questions (4 plausible distractors, single correct letter)
- At least {N_short_numeric} short_numeric questions (math: with units; specify tolerance in expected_answer)
- At least {N_true_false} true_false questions (the question must be a single declarative statement)
- At most {N_free_text} free_text questions (open-ended; only when the concept actually needs explanation)
- For multiple_choice steps: choices field is REQUIRED, expected_answer is the LETTER (A/B/C/D)
- For true_false steps: expected_answer is exactly "True" or "False"; choices is null
- For short_numeric steps: expected_answer is the number (or "number unit"); no prose

TEACHING DEPTH RULES:
- Teach steps: max {teach_sentence_max} sentences. Use one analogy + one concrete example.
- Provide exactly {hint_count} hints per practice/quiz step, scaffolding general → specific.
```

For `standard` and `advanced` the floors relax — but the same
template is used so the prompt structure stays consistent.

### 3. Post-generation validation + retry

Add `_validate_against_profile(steps, profile_rules) -> list[str]`
called from `_generate_steps` after the instructor-parsed result
returns. Returns a list of human-readable issues like:

- `"Lesson has 2 figures, slow_learner requires ≥ 4"`
- `"No true_false question found, slow_learner requires ≥ 1"`
- `"Step 3 is multiple_choice but choices is empty"`

If issues are non-empty, do **one** retry: re-call instructor with the
correction prompt:

```
Your previous response did not meet the profile requirements.
Issues:
{bulleted issues}

Regenerate the lesson, fixing every issue. Keep all other content the same.
```

Cap retries at 1 (already the existing `max_retries=3` is for instructor
parse retries; this is a content-quality retry on top). Log every
retry via `print("[ContentGen] profile retry: ...", flush=True)`.

If validation still fails after the retry, accept the result anyway
and log a warning — better degraded content than a stuck `generating`
status.

### 4. Schema fix + answer_type normaliser

- Update `content_generator.py:127` docstring + Field validator to
  match `LessonStep.AnswerType.values`.
- In `_save_steps_to_db` (find via `grep _save_steps_to_db
  content_generator.py`), map LLM emissions:
  - `'short_text'` → `'free_text'`
  - `'free_response'` → `'free_text'`
  - any other unknown value → `'free_text'` with a logged warning.

### 5. Files to edit (Phase 1)

| File | Change |
|------|--------|
| `apps/curriculum/models.py::Course` | Add `GenerationProfile` enum + `generation_profile` field |
| `apps/curriculum/migrations/00XX_*.py` | New migration |
| `apps/curriculum/content_generator.py:127` | Fix `answer_type` schema docstring |
| `apps/curriculum/content_generator.py::_generate_steps` (562) | Read profile, build rules, inject prompt block, run validator + retry |
| `apps/curriculum/content_generator.py::_save_steps_to_db` | Normalise answer_type values |
| `apps/curriculum/content_generator.py` (new method) | `_profile_rules(profile, target_minutes, max_steps, is_math)` |
| `apps/curriculum/content_generator.py` (new method) | `_validate_against_profile(steps, rules)` |
| `apps/curriculum/tests/test_content_generation_profile.py` | Unit tests for rule selection + validator |

## Backend changes — Revised Phase 2 (the load-bearing personalisation)

The single content stream means Phase 2 has to do all the work of
adapting to learner level. Two layers:

### Layer A — format-aware presentation (mechanical)

`apps/tutoring/conversational_tutor.py`:

- `_get_step_phase_instructions` (~3504): replace the flat
  `f"ANSWER TYPE: {step.answer_type}"` with format-specific blocks:
  - **multiple_choice**: render the choices as `A) ... / B) ... / C) ... / D) ...`, instruct tutor to ask "Which letter (A, B, C, or D) is your answer?"
  - **true_false**: instruct tutor to ask "Is this True or False? <statement>"
  - **short_numeric**: instruct tutor to ask for a number with the expected unit; on grading, accept ±5% tolerance unless the expected_answer specifies otherwise.
  - **free_text**: free-form, current behaviour (no change).
  - **none**: no question (engage/teach/worked_example).
- `_evaluate_step` (~3813): pass the format-specific grading hint
  into the LLM evaluator system prompt. For `short_numeric` add a
  deterministic pre-check that strips units and compares numerically
  before the LLM call (mirrors the math_tutor_fix pattern).
- Math constraint: keep the rule that math tutor must not evaluate a
  bare numeric answer in `free_text` mode (see
  `memory/feedback_math_tutoring.md`). Format-specific path only
  applies when `answer_type` is explicit.

### Layer B — student-signal-aware adaptation (personalisation)

The tutor reads the student's signals at session start and adapts
per step. Sources of signal:

- `student.skills_snapshot` (existing) — pretest sub-skill bitmap
- `pretest_diagnostic` (existing) — first-lesson exit-ticket result
- Per-session "Too Hard / Too Easy" buttons (existing in the engine
  but underutilised)
- Recent `StudentSkillMastery.mastery_level` for skills tagged on
  the current lesson

Derive a session-level `scaffolding_level ∈ {high, medium, low}`
("high" = slow learner, "low" = advanced). Per step:

- **scaffolding_level = high (slow learner signal)**:
  - For a step with `answer_type=free_text`, present the prompt as
    a T/F or MCQ if the LLM can synthesize plausible distractors
    (extra LLM call gated to once per step, cached).
  - Offer all 3 hints early, with shorter sentences.
  - Slow pace: don't advance until 2 successful attempts.
  - Use simpler language — invoke an existing
    "simplify-explanation" prompt path.
- **scaffolding_level = medium (default)**:
  - Present each step in its source format. Offer hints on first
    wrong attempt. Standard advancement criteria.
- **scaffolding_level = low (advanced signal)**:
  - For a step with a recognition-format question (MCQ, T/F),
    convert the prompt to free-text form by stripping the choices
    ("Explain why X is the answer..."). The LLM grades.
  - Withhold hints until 2 wrong attempts.
  - Skip practice steps that the student has already shown
    mastery on (per `StudentSkillMastery` ≥ 0.8 for tagged skill).

Implementation sketch:

- New helper `_compute_scaffolding_level(student, lesson, session)` →
  `'high' | 'medium' | 'low'`. Cached on `engine_state`.
- New helper `_adapt_step_presentation(step, scaffolding_level)` →
  returns a dict that drives the per-step LLM call.
- Per-step LLM call in the existing flow gets the adaptation dict
  injected; format-specific blocks from Layer A still apply.
- Per-session difficulty buttons override the computed level.

## Frontend / dashboard — Phase 3 (sketched)

- `templates/dashboard/course_detail.html`: add a "Generation profile"
  dropdown in the course header (Standard / Slow learners / Advanced)
  + a "Regenerate all lessons with this profile" button.
- New view `course_regenerate(request, course_id)` in
  `apps/dashboard/views.py`: validates the user can manage the course,
  saves the profile, queues `run_async(generate_complete_course,
  course_id, institution_id)`.
- New `generate_complete_course(course_id, institution_id, log_fn)` in
  `apps/dashboard/background_tasks.py`: iterates lessons,
  serially calls `generate_complete_lesson` (already idempotent +
  CAS-guarded). Serial, not parallel — concurrent generation has bitten
  us before (see `background_tasks.py` CAS comments).
- Progress UI: existing `course_detail` already polls `content_status`
  per lesson; the bulk button just lights up multiple lessons at once.

## Out of scope (this iteration)

- Per-student adaptive content (e.g. "regenerate this lesson tailored
  to student X's skills_snapshot"). Profile is course-wide.
- Per-lesson profile override. Course-level only for now.
- Exit ticket regeneration with new format mix. Exit tickets already
  have the richest format diversity (`memory` MEMORY.md notes
  five formats); we'll touch them only if needed for consistency.
- Image generation tuning. Existing `gpt-image-2` pipeline is
  unchanged; we're only asking the content LLM to *request* more
  figures.
- Tanzania-specific profile or context. Add later when the Tanzania
  pilot needs it.
- Migrating existing institutions' courses. Field defaults to
  `standard`; teachers opt in by toggling the profile.

## Phased delivery

| Phase | Work | Solo-dev days |
|-------|------|---------------|
| **1** | Course.generation_profile field + migration; profile rules + prompt injection; validator + retry; answer_type schema fix; tests; regenerate Math S3 with `slow_learner` and eyeball 5 sample lessons | **2** |
| **2** | Format-aware presentation + grading in conversational_tutor.py; deterministic short_numeric pre-check; tests against the regenerated lessons | **1.5** |
| **3** | Course-level "Regenerate all" UI + background task; serial pipeline; status polling integration | **1** |

Total: ~4.5 focused days. Calendar: probably one week running alongside other Seychelles pilot work.

## Open questions

1. **Should `slow_learner` enforce shorter lessons (e.g. 15-min cap) on top of the figure/format rules?**
   *Recommend: no.* Lesson duration is already calibrated by `estimated_minutes`. Shorter sessions are a separate teacher decision.

2. **Validation retry: hard-fail to `failed` status if the second attempt also fails the profile check, or accept-and-warn?**
   *Recommend: accept-and-warn.* Stuck `generating` status is a known sharp edge (see CLAUDE.md "Stuck content generation"). A "good enough" lesson with a warning beats no lesson.

3. **Should single-lesson regen (existing button) also respect `course.generation_profile`?**
   *Recommend: yes, automatic.* Profile is a course property; reading it is free. The teacher already chose the profile by setting it on the course.

4. **Should we expose `figure_density` / `format_mix` as separate teacher knobs, or hide them inside the profile?**
   *Recommend: hide inside the profile.* Three named profiles is easier to reason about than four sliders. We can split later if a teacher asks.

5. **Tutor presentation for true_false: render as "True / False" buttons in the chat UI, or just text?**
   *Recommend: text for v1.* Buttons are a frontend change; getting the LLM to ask the question correctly is the load-bearing part. Defer button UI.

## Next step

Add the `Course.generation_profile` field + migration, then thread
`profile_rules` into `_generate_steps` and the prompt — Phase 1 step 1
and step 2 from the table above. Run a single-lesson regen on one
S3 math lesson with `profile='slow_learner'` to validate the prompt
shape before touching the validator + retry path.
