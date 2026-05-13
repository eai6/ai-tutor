# Content-Generation Benchmark — Plan (2026-05-13)

## Problem

The tutor-evaluation benchmark (`memory/eval_benchmark_v2_simplified.md`) scores **tutor responses** to real students. We have no parallel signal on **generated curriculum content** — lesson scripts, exit-ticket questions, and figures.

Today, when a teacher reviews a freshly generated lesson and edits things in the dashboard, three losses happen:
1. The original generated content is overwritten in place — no audit trail (`step_edit()` at `apps/dashboard/views.py:4328` mutates `LessonStep.teacher_script`, `step.media['images'][i]['url']` directly; no django-reversion installed).
2. The *reason* for the edit is never captured. Was it a factual error? A safety concern? A typo?
3. There's no per-prompt-version trend. After we tune the content-generation prompts, we can't say "factual errors are down 40%."

**Goal**: capture every teacher-driven correction as a typed `ContentReviewEvent` with a controlled error-category legend (factual / arithmetic / safety / inappropriate / minor wording / figure mismatch / etc.). Aggregate into per-content-type pass rates and per-error-category trend lines. Exact same iteration loop as the tutor benchmark — change the generation prompt, run new lessons, score, compare.

This is content-side QA. Pairs with `memory/eval_benchmark_v2_simplified.md` (which is tutor-side QA).

## Current state (from audit)

Citations are file:line throughout.

### What gets generated

- **LessonStep** (`apps/curriculum/models.py:253-527`) — fields: `teacher_script` (line 314), `question` (317), `expected_answer` (333), `choices` (328), `hint_1/2/3` (343-345), `rubric` (337), `educational_content` JSON (391: vocab, worked examples, common mistakes, seychelles_context), `curriculum_context` JSON (420), `media` JSON (356: list of image dicts with `url`, `alt`, `caption`, `source`, `model`).
- **ExitTicketQuestion** (`apps/tutoring/models.py:525-669`) — fields: `question_text` (555), `option_a/b/c/d` (558-561), `correct_answer` (563), `answer_data` JSON (572: fill-blank/matching/short-answer payloads), `explanation` (583), `image` ImageField (621).
- **MediaAsset** (`apps/media_library/models.py:17-74`) — `file`, `alt_text`, `caption`, `figure_facts` JSON (55: scene_description, labelled_features, angle_relationships).

### Generation entry points

- `apps/dashboard/views.py:3827-3880` `lesson_generate_content` (POST `/curriculum/lesson/<id>/generate/`) — wipes prior state and spawns `generate_complete_lesson` background task.
- `apps/curriculum/management/commands/generate_lesson_content.py` — CLI fallback.
- Image regenerate: `step_edit()` at `apps/dashboard/views.py:4376-4415` calls `ImageGenerationService.get_or_generate_image(model_override=...)` and overwrites `step.media['images'][i]` in place.

### Existing approval gate (partial signal — coarse)

- `Lesson.content_status` enum (`apps/curriculum/models.py:151-159`): `EMPTY | GENERATING | READY | READY_WITH_WARNINGS | FAILED`. `READY_WITH_WARNINGS` already signals "teacher review required" (Layer 3 arithmetic verification).
- `Lesson.teacher_approved` boolean (line 203) + `teacher_approved_by` (208) + `teacher_approved_at` (207). Triggered by `lesson_approve()` at `apps/dashboard/views.py:3928-3949`.
- **Gap**: approval is binary lesson-wide. No per-artifact, no error categorization, no edit history.

### No audit trail anywhere

- No django-reversion / django-simple-history. No hand-rolled `*Version` tables.
- `step_edit()` (`apps/dashboard/views.py:4328+`) overwrites `step.teacher_script`, `step.hint_1/2/3`, `step.media['images'][i]['url']` directly.
- `exit_question_edit()` (`apps/dashboard/views.py:3415+`) overwrites `ExitTicketQuestion` in place.
- Image regeneration loses the prior URL — old MediaAsset row is overwritten.

### Multi-tenancy

- `Lesson` inherits institution via `unit.course.institution` (no direct FK).
- `MediaAsset.institution` direct FK (`apps/media_library/models.py:27-31`), null = platform-wide.
- Standard `Q(unit__course__institution=inst) | Q(unit__course__institution__isnull=True)` pattern (e.g. `apps/dashboard/views.py:3283-3290`).

### Existing FeedbackReport (general, not content-specific)

- `apps/dashboard/models.py:219-281` — general bug/idea/feedback channel from the floating help button. `kind` (BUG/FEEDBACK/IDEA), `severity`, `message`, `screenshot`. Resolved at `feedback_list` view (`apps/dashboard/views.py:6084-6143`).
- **Not suitable** for content review — it's free text, no per-artifact target FK, no controlled vocabulary.

## Target design

A **`ContentReviewEvent`** captures one teacher action against one generated artifact. Inline with the existing edit flow — when the teacher clicks Save in `step_edit` / `exit_question_edit` / image regenerate, a categorization sidebar collects the error tags + severity before the actual edit applies.

Mirrors `apps/benchmark/` patterns: dedicated sub-app `apps/content_eval/`, one source of truth for the legend (`labels.py`), `sampling.py` analog (just listing recently-generated unreviewed artifacts), `scoring.py` analog (computing per-category counts + pass rates), Django dashboard views.

### Components

```
apps/content_eval/
├── __init__.py
├── apps.py
├── models.py             # ContentReviewEvent, ContentSnapshot, ContentEvalRun
├── labels.py             # ERROR_CATEGORIES legend, SEVERITIES, CONTENT_KINDS
├── sampling.py           # list_reviewable_artifacts(...) — what hasn't been reviewed
├── scoring.py            # compute_content_metrics(events) → dict
├── snapshots.py          # capture_snapshot(target) called from generation pipeline
├── urls.py
├── views.py              # review_list, review_artifact, runs_list, score_now, run_detail
├── tests/
└── migrations/

templates/content_eval/
├── review_list.html
├── review_artifact.html  # split-pane: generated vs edited, legend picker
├── runs_list.html
└── run_detail.html
```

Plus integration points (small edits, not new files):

- `apps/curriculum/content_generator.py` → call `capture_snapshot()` after each artifact write
- `apps/dashboard/views.py:4328` (`step_edit`) → on POST, accept `error_categories[]` + `severity` + create `ContentReviewEvent`
- `apps/dashboard/views.py:3415` (`exit_question_edit`) → same
- `apps/dashboard/views.py:4376` (image regenerate) → same
- Sidebar UI added to `templates/dashboard/curriculum/lesson_detail.html` and step_edit template

### Data model

```python
# apps/content_eval/models.py

class ContentSnapshot(models.Model):
    """As-generated state of one artifact. Captured at generation time and
    immutable thereafter. Diffed against the live row to derive what
    the teacher changed."""

    content_kind = models.CharField(
        max_length=20,
        choices=[
            ('lesson_step',  'Lesson Step'),       # LessonStep.id
            ('exit_question','Exit Ticket Question'),  # ExitTicketQuestion.id
            ('image',        'Step Image'),        # (LessonStep.id, image_index)
        ],
    )
    target_id = models.CharField(max_length=80)   # composite for image
    lesson = models.ForeignKey(
        'curriculum.Lesson',
        on_delete=models.CASCADE,
        related_name='content_snapshots',
    )
    generation_run_id = models.CharField(max_length=80, blank=True)
    # The full as-generated payload (text + image URL + JSON fields).
    payload = models.JSONField(default=dict)
    # Which prompt / model version produced this. Stored as a snapshot key
    # so we can group review events by prompt iteration without joining
    # to a live ModelConfig (which mutates).
    prompt_pack_label = models.CharField(max_length=120, blank=True)
    model_label = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['content_kind', '-created_at']),
            models.Index(fields=['lesson']),
            models.Index(fields=['generation_run_id']),
        ]
        unique_together = [('content_kind', 'target_id', 'generation_run_id')]


class ContentReviewEvent(models.Model):
    """One teacher action: accept-as-is, edit-and-save, regenerate, reject."""

    snapshot = models.ForeignKey(
        ContentSnapshot,
        on_delete=models.CASCADE,
        related_name='reviews',
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
    )
    action = models.CharField(
        max_length=20,
        choices=[
            ('accept',     'Accept as-is'),
            ('edit',       'Edit and save'),
            ('regenerate', 'Regenerate'),
            ('reject',     'Reject / delete'),
        ],
    )
    severity = models.CharField(
        max_length=20,
        choices=[
            ('none',    'None (accepted clean)'),
            ('minor',   'Minor (typo, phrasing)'),
            ('major',   'Major (substantive rewrite)'),
            ('blocker', 'Blocker (regenerate or reject)'),
        ],
    )
    error_categories = models.JSONField(default=list)  # list of legend keys
    # Only populated for action='edit' — what the teacher's edit produced.
    edited_payload = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['action']),
            models.Index(fields=['severity']),
            models.Index(fields=['-created_at']),
        ]


class ContentEvalRun(models.Model):
    """Frozen scoring snapshot, mirroring BenchmarkRun.

    Each run scores all ContentReviewEvents within a date range / prompt
    version against the legend. Aggregates pass-rate per content_kind +
    per-error-category counts."""

    label = models.CharField(max_length=120)
    prompt_pack_label = models.CharField(max_length=120, blank=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    total_artifacts = models.PositiveIntegerField(default=0)
    accepted_clean = models.PositiveIntegerField(default=0)
    metrics = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
    )
    notes = models.TextField(blank=True)
```

### Error-category legend (controlled vocabulary, ~15 tags)

Single flat list (no hierarchy) — mirrors benchmark v2's flat-30-labels approach. Defined in `apps/content_eval/labels.py`:

| Key | Definition | Typical content kind |
|---|---|---|
| `factual_error` | Statement contradicted by curriculum / KB | script, question, explanation |
| `arithmetic_error` | Math is wrong (e.g., "65 + 125 = 180") | question, explanation, worked example |
| `inappropriate_content` | Cultural / age / register inappropriate | script, question |
| `safety_concern` | Harmful / unsafe content | any |
| `figure_mismatch` | Image doesn't match the question | image |
| `figure_quality` | Image is correct but low quality / unreadable | image |
| `figure_hallucinated_facts` | Image shows labels/values not in lesson | image |
| `wording_minor` | Typo, awkward phrasing — small fix | any |
| `wording_major` | Confusing question, unclear instruction — substantive rewrite | question, script |
| `wrong_difficulty` | Too hard / too easy for grade level | question |
| `bank_authoring` | Generated values not from curriculum / out of scope | question |
| `missing_seychelles_context` | Should localize but didn't | script, question |
| `format_violation` | Missing alt text, wrong number of options, malformed JSON | any |
| `pedagogy_drift` | Doesn't teach toward stated learning objective | script, question |
| `repetitive` | Duplicates other questions / redundant content | question |
| `other` | Catch-all — flag for adding a new category | any |

Severity is independent of category — a `factual_error` can be `minor` (small mistake corrected by 2-word edit) or `blocker` (whole lesson must be regenerated).

### Pass / fail rule

```
artifact PASSES if review.action == 'accept' AND review.severity == 'none' AND review.error_categories == []
otherwise FAILS
```

`pass_rate = accepted_clean / total_artifacts`. Same shape as `BenchmarkAnnotation.passes`.

### Snapshot capture point

The generation pipeline gets one new line per artifact write — call `capture_snapshot(content_kind, target, generation_run_id, prompt_pack_label, model_label)` after persisting. Specifically:

- `apps/curriculum/content_generator.py` — after each `LessonStep.objects.create(...)` and `ExitTicketQuestion.objects.create(...)`.
- `apps/curriculum/image_service.py::ImageGenerationService.get_or_generate_image` — after each image is written into `step.media['images']`.

If we miss snapshotting at generation time (e.g., legacy lessons before this ships), reviews still work — but `edited_payload` diffing won't have an "as-generated" baseline to compare against. We accept that; backfill is opt-in (Phase 5).

### Inline review UI

Modify `templates/dashboard/curriculum/lesson_detail.html` and the existing step / exit-question / image-regen edit forms to add a **review sidebar** with:

- Action picker (`accept` / `edit` / `regenerate` / `reject`) — defaults to `edit` if any field changed
- Severity radio (`none` / `minor` / `major` / `blocker`)
- Error categories multi-select (15 tags from legend, grouped visually)
- Notes textarea (optional)

Server-side: `step_edit()`, `exit_question_edit()`, image-regenerate handlers gain a `_record_review_event(snapshot, request)` call after persisting the edit. If no snapshot exists for the target (legacy content), skip the snapshot link but still record the event with `snapshot=NULL`. *(Drop snapshot FK NOT-NULL constraint to allow this — see migration note below.)*

### Scoring + dashboard

Mirrors `apps/benchmark/scoring.py::compute_metrics`. Pure function over a list of `ContentReviewEvent` rows, optionally filtered by `prompt_pack_label` or date range:

```python
def compute_content_metrics(events) -> dict:
    return {
        'overall': {
            'total': N,
            'accepted_clean': N_clean,
            'pass_rate': N_clean / N if N else 0.0,
        },
        'by_content_kind': {
            'lesson_step':   {'total': X, 'accepted_clean': Y, 'pass_rate': ...},
            'exit_question': {...},
            'image':         {...},
        },
        'by_severity': {'none': N1, 'minor': N2, 'major': N3, 'blocker': N4},
        'by_error_category': {'factual_error': N, 'arithmetic_error': N, ...},
        'by_prompt_pack': {'<label>': {'total': N, 'pass_rate': ...}},
    }
```

`ContentEvalRun` persists this at the click of a "Score now" button on the `/dashboard/content-eval/runs/` page — same pattern as the tutor benchmark's runs_list (`73b643a`).

## Out of scope (explicitly deferred)

These are NOT v1. Calling them out so they don't sneak in:

1. **Auto-detection of error categories.** No LLM-judge for content review in v1. Humans only. Rationale: we don't know yet what the human-tagged distribution looks like; building a judge before the labelled set is premature (mirrors benchmark v1 → v2 trajectory).
2. **Full version history of edits.** Snapshot is one-shot at generation. Subsequent edits overwrite the live row as today. If a teacher edits the same step three times, the third event's `edited_payload` is the final state; intermediate states are lost. Add django-reversion in v2 if needed.
3. **Cross-school benchmark federation.** v1 is single-school. Aggregation across schools is a v2 concern.
4. **Auto-rejection / blocking publish based on score.** The `Lesson.teacher_approved` gate stays as-is. The new metrics inform but don't block.
5. **Retroactive review of pre-existing content.** Phase 5 backfill is optional; v1 only captures snapshots for newly-generated artifacts going forward.
6. **Per-field categorization.** Granularity is per-artifact (one event per LessonStep, ExitTicketQuestion, image). Tagging at the field level (which sentence in `teacher_script`?) is too granular for v1.
7. **Image quality auto-rating.** A vision model could pre-score images, but that's a separate research thread. v1 humans only.
8. **Prompt A/B testing infrastructure.** We can compare `prompt_pack_label='v1'` vs `'v2'` via the metrics, but no built-in randomization / traffic splitting. Manual swap, manual compare.

## Phased delivery

Each phase ships value standalone. Stop after any phase if signal not worth next.

| Phase | Goal | Days | Files | Success metric | Risk |
|---|---|---:|---|---|---|
| **1. Models + legend + snapshot capture** | `ContentSnapshot` + `ContentReviewEvent` + `ContentEvalRun` migrations applied. Generation pipeline calls `capture_snapshot()` for new content. | 3 | `apps/content_eval/{__init__,apps,models,labels,snapshots}.py`, migration `0001_*.py`, `apps/curriculum/content_generator.py` (call sites), `apps/curriculum/image_service.py` (call site), `config/settings.py` (INSTALLED_APPS) | After generating one new lesson, `ContentSnapshot.objects.count() == steps + exit_questions + images` for that lesson | Snapshot bloat. **Mitigate:** payload only stores the artifact's own fields, not the whole lesson. Estimate ~2 KB per snapshot; 100 lessons × ~30 artifacts = ~6 MB. |
| **2. Inline review UI on step edit** | Teacher categorizes when saving a step edit. `ContentReviewEvent` persists. | 4 | `apps/dashboard/views.py` (`step_edit` handler), `templates/dashboard/curriculum/step_edit.html` (sidebar), `apps/content_eval/views.py` (helper), `apps/content_eval/tests/test_step_edit_review.py` | Editing one step in dashboard creates one `ContentReviewEvent` with categories + severity persisted | Friction on teacher. **Mitigate:** sidebar pre-fills `severity='minor'` and an empty category list. One additional click to save. |
| **3. Exit question + image-regenerate review** | Same flow for `exit_question_edit` + image regenerate. | 3 | `apps/dashboard/views.py` (those two handlers), templates, tests | Editing one exit question or regenerating one image creates a `ContentReviewEvent` | Image regenerate has multiple categorization opportunities (which one of N images). **Mitigate:** UI prompts category-per-regenerate, one event per image |
| **4. Dashboard: runs list + score-now + detail** | Mirror `apps/benchmark/` UI. Per-content-kind pass rate + per-error-category counts displayed. | 4 | `apps/content_eval/{urls,views,scoring}.py`, `templates/content_eval/{runs_list,run_detail}.html`, dashboard sidebar nav entry, tests | "Score now" button computes metrics + persists `ContentEvalRun`; detail page renders pass rate + slices | Bug: scoring includes legacy events with no snapshot. **Mitigate:** scoring tolerates `snapshot=NULL` (treats as if the event still counts toward pass/fail). |
| **5. Backfill + prompt-version comparison** *(conditional)* | Backfill snapshots for existing READY lessons (best-effort from current state). Add prompt-pack-label dropdown filter to dashboard. | 4 | `apps/content_eval/management/commands/backfill_snapshots.py`, dashboard filter UI | After backfill, every lesson with `teacher_approved=True` shows up in the dashboard as either accepted (no events) or with prior edits | Backfill quality — no "as-generated" payload available retroactively. **Mitigate:** mark backfilled snapshots with `payload={'_backfilled': true}`; exclude from diff metrics. |

**Total Phases 1–4: ~14 focused days. Phase 5 is conditional on Phases 1–4 producing useful metrics.**

## Testing

Per `memory/feedback_verify_rendered_templates_before_push.md` and `auto-memory/feedback_chrome_devtools_default_verification.md` — every UI-touching change in this plan must be browser-verified before commit.

| Phase | New tests |
|---|---|
| 1 | `test_snapshot_capture.py` — generate a fixture lesson via the existing pipeline; assert one snapshot per LessonStep + ExitTicketQuestion + image. Snapshot payload contains expected fields. |
| 2 | `test_step_edit_review.py` — POST to `step_edit` with `error_categories=['arithmetic_error', 'wording_minor']`, `severity='minor'`; assert one `ContentReviewEvent` created with correct fields. Also: edit with no changes + `action='accept'` creates a clean event. |
| 3 | `test_exit_question_review.py` and `test_image_regenerate_review.py` mirror Phase 2 shape. Image test asserts the regenerated URL is captured in `edited_payload` AND a snapshot for the new image is created. |
| 4 | `test_content_scoring.py` — fixture: 10 events (5 accepts clean, 3 edits with categories, 2 rejects). `compute_content_metrics()` returns expected `overall.pass_rate=0.5`, slice counts. Mirrors `test_scoring.py` shape. `test_runs_list_view.py` for the dashboard. |

**Anti-pattern guard:** Don't write tests asserting on the live `LessonStep.teacher_script` value — that already gets edited-in-place. Tests assert on `ContentReviewEvent` and `ContentSnapshot` rows.

## Composition with related plans

- **`memory/eval_benchmark_v2_simplified.md`** — parallel structure. Tutor benchmark scores tutor responses; this scores generated artifacts. Different inputs, same iteration loop. Both surface in `/dashboard/.../runs/` lists.
- **`memory/agentic_platform_architecture_plan.md`** — Phase 1's `TurnSpan` model and span infrastructure don't apply to content gen (no per-turn evaluation). Independent.
- **`memory/llm_student_simulator_plan.md`** — independent. Synthetic students drive tutor sessions; this captures teacher reviews of generated content.
- **`memory/agentic_platform_architecture_plan.md`** "Mirror these patterns" rule applies — `ContentSnapshot` mirrors `BenchmarkItem.snapshot`; `ContentReviewEvent` mirrors `BenchmarkAnnotation`; `ContentEvalRun` mirrors `BenchmarkRun`. Document the symmetry in CLAUDE.md after Phase 1 ships.
- **`tutoring-engine-expert` skill** — not relevant; this plan touches `apps/curriculum/` and `apps/dashboard/`, not the tutoring engine.
- **`codebase-architecture-expert` skill** — consult Phase 1 for the new sub-app layout (mirrors `apps/benchmark/` shape).

## Open questions

Resolve before Phase 1 starts:

1. **Sub-app location.** New `apps/content_eval/`, or extend `apps/benchmark/` with a content stratum? **Recommend: new `apps/content_eval/`.** Reason: different schema, different rubric, different review surface — but parallel structure. Don't shoehorn into one app at the cost of mixed concerns.

2. **Inline categorization vs separate review queue.** Should teachers categorize in the existing edit UI, or work through a dedicated `/dashboard/content-eval/queue/` page? **Recommend: inline.** Reason: teachers already edit content in `step_edit`; adding a sidebar is one extra step. A separate queue page doubles the friction and they won't use it.

3. **Default severity / categories on Save.** When a teacher hits Save with NO categorization checked, what gets recorded? **Recommend: `action='edit'`, `severity='minor'`, `error_categories=[]`.** Forces them to think about whether this is really minor; can override.

4. **Action='accept' button location.** Where does "Accept as-is, no changes" live? **Recommend: a button next to "Edit" on the step card** in `lesson_detail.html`. One click, no modal — defaults to `severity='none'`, `categories=[]`.

5. **Image regenerate categorization granularity.** When a teacher regenerates 3 images on one step, that's 3 review events or 1? **Recommend: 3 events** (one per image regenerated). Categories may differ per image. The UI prompts per-image.

6. **Snapshot retention policy.** How long do we keep `ContentSnapshot` payloads? **Recommend: indefinitely for v1.** Estimate is ~6 MB for 100 lessons; not a problem at pilot scale. Revisit at 1000+ lessons.

7. **Backfill scope.** All `READY` lessons or just recent? **Recommend: all `READY` lessons** since that's manageable at pilot scale (~100 lessons), and Phase 5 marks them `_backfilled` so metrics can exclude from diff-style queries.

8. **Should `Lesson.teacher_approved` setter also create a `ContentReviewEvent`?** **Recommend: yes — one synthetic lesson-level event with `action='accept'`, no categories.** Captures the "approved without further edits" case for lessons that fly through. Otherwise we under-count clean lessons.

## Risks

1. **Teacher friction.** Adding a categorization step on every save is the #1 risk. **Mitigate:** sidebar pre-fills sensible defaults; "Accept as-is" is one-click; severity radio is single-select. Aim for ≤2 extra clicks per edit.

2. **Snapshot payload drift.** If we add a field to `LessonStep` later, snapshots for older content won't have it. **Mitigate:** snapshot the fields we care about by name (allowlist). New fields are absent in old snapshots, present in new — diffing tools handle this.

3. **Reviewer bias.** One reviewer's "minor" is another's "blocker". **Mitigate:** legend definitions in `labels.py` include severity examples; superuser-only for v1 (Edward + designated teachers); revisit when n_reviewers > 3.

4. **Categorization fatigue.** Long sessions of "edit + categorize + edit + categorize" → teachers stop tagging. **Mitigate:** Phase 4 dashboard exposes per-reviewer event volume; if a reviewer never tags categories, the UI nudges them.

5. **Coupling to in-flight content_status changes.** `agentic_platform_architecture_plan.md` doesn't touch `content_status`, but other in-flight plans might. **Mitigate:** ContentReviewEvent doesn't depend on `content_status` — it's keyed off the artifact, not the lesson state.

6. **Multi-tenancy regression.** New views must filter by institution. **Mitigate:** mirror `apps/benchmark/views.py` pattern (`@staff_member_required` + standard `Q(...) | Q(institution__isnull=True)`); per-app review of every queryset before merge.

## Next step

Build Phase 1 in isolation: write the three models + legend + `capture_snapshot()`, generate the migration, wire the call sites in `apps/curriculum/content_generator.py`. Generate one fixture lesson and confirm `ContentSnapshot.objects.count()` matches expectation. No UI yet — Phase 2 is the first user-visible piece.
