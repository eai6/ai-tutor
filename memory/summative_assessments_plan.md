# Summative Assessments — V1 plan

**Goal**: course-level summative exam, teacher-triggered, in-app, auto + LLM
graded. Generated to mirror the format and difficulty of teacher-uploaded
exam papers / question banks. Built on the existing `ExitTicket` /
`ExitTicketQuestion` infrastructure.

**Pilot constraint**: Seychelles launches 2026-05-11. V1 must ship in time
for at least one summative dry-run before launch.

## Confirmed scope (2026-04-27)

- **One summative per Course** (not per lesson, not per grade-level).
- **Mixed format**: MCQ, short-answer, data-interpretation, worked-questions
  — whatever the uploaded exam papers use. Not just MCQ.
- **Trigger**: teacher-clicked from the course detail page. No scheduling.
- **Source**: KB. Format/difficulty reference comes from
  `TeachingMaterialUpload.material_type='question_bank'` (already exists).
  Content reference from textbook + worksheet materials in the same KB.
- **Bank size**: 90 questions generated per course. **30** are randomly
  picked per student attempt, stratified so every TO + every EO is hit.
- **Build on**: extend `ExitTicket` model (do NOT create a sibling). Reuses
  figure rendering, the grading endpoint, the teacher review page, the
  student review page, and the `figure_spec` → server SVG pipeline.

## Storage shape

Extend `ExitTicket` rather than fork:

```
ExitTicket:
  + assessment_type   = 'exit_ticket' | 'summative'    (new)
  + lesson            = ForeignKey, **nullable**       (was non-null)
  + course            = ForeignKey(Course), nullable   (new)
  + question_bank_size = PositiveInteger, default 30   (new — 90 for summative)
  + questions_per_attempt = PositiveInteger, default 10 (new — 30 for summative)

  Constraint: exactly one of (lesson, course) is set.
```

`ExitTicketQuestion` is unchanged — already supports the 5 question types
and `figure_spec` via `answer_data`.

`ExitTicketAttempt` likely needs no changes; we'll re-confirm when wiring.

## Generator pipeline (revised 2026-04-27)

`apps/tutoring/summative_generator.py`:

The summative is built by **aggregating** each lesson's existing exit
ticket bank, not via a single big LLM call. Single-call generation for
90 questions has too many failure modes (truncation at the token cap,
one bad question kills the parse, no retry granularity). The
per-lesson exit-ticket pipeline is already small, robust, and proven —
we reuse it.

1. Walk every Lesson in the course (ordered by unit + lesson index).
2. For each lesson: ensure it has an exit ticket. If absent, kick the
   existing `apps.curriculum.content_generator.generate_exit_ticket_for_lesson`.
   That generator is the same one that backs the per-lesson Generate
   Content button — small ~35-question call, parallel-safe.
3. Sample K questions from each lesson's exit ticket (K = target_count /
   num_lessons, distributed evenly with a remainder pass). Bias the
   sample toward a 30/45/20 easy/medium/hard mix.
4. Tag each sampled question with `concept_tag` = the lesson's
   teaching objective (so the stratified per-attempt selector can
   guarantee coverage).
5. Aggregate into a single `ExitTicket(course=..., assessment_type='summative')`.
   Trim to `target_count` if we over-sample; pad to whatever's available
   if we under-sample (rare).
6. Run lesson workers in a `ThreadPoolExecutor(max_workers=3)`. One
   bad lesson doesn't poison the bank — failures are isolated and
   logged.

Net new LLM cost: zero when exit tickets already exist (the common
case after content generation has run). Otherwise: one normal exit-
ticket call per missing lesson, in parallel.

## Stratified selection (per student attempt)

`apps/tutoring/summative_selection.py` (new):

Given 90 banked questions and the course's TOs+EOs:

1. Group questions by `concept_tag` (each tag = one TO or EO).
2. For each objective, ensure ≥1 question is selected.
3. Fill the remaining slots with a difficulty-weighted random draw
   (~10 easy, ~15 medium, ~10 hard — same mix as exit tickets).
4. Shuffle final 30 to randomize order.

Edge cases: if the bank doesn't cover some objective, log + still ship.

## Teacher UX

On `course_detail.html`:
- New "Summative Exam" card (alongside Teaching Materials).
- States:
  - **None**: "Generate Summative Exam" button → kicks off background task.
  - **Generating**: spinner + log (uses existing partial-swap polling).
  - **Ready**: shows 90-Q bank summary (objective coverage matrix). Buttons:
    "Preview", "Regenerate", "Publish".
  - **Published**: shows attempt stats; "Unpublish" button.

Reuses existing exit-ticket review/edit page templates. We'll switch
`{% url 'dashboard:exit_ticket_edit' lesson.id %}` to a new
`dashboard:summative_edit` route that takes `course_id`.

## Student UX (V1)

Single page: `/tutor/summative/<course_id>/`. 30 questions, mixed types,
auto-graded for MCQ/fill-in-blank/matching, LLM-graded for short-answer
and data-interpretation. Reuses the existing exit-ticket take-page patterns.

Pass threshold: configurable, default 70% (so 21/30). Stored on `ExitTicket`.

## Step-by-step delivery

1. **Model + migration** — nullable lesson, add course/assessment_type/bank
   fields. Backfill: every existing `ExitTicket` becomes
   `assessment_type='exit_ticket'`, `question_bank_size=30`,
   `questions_per_attempt=10`.
2. **Generator** — new module, vendor-style prompt with KB context.
3. **Selection** — stratified random pick.
4. **Teacher trigger + view** — button on course detail, background task.
5. **Teacher review page** — list bank, preview, publish.
6. **Student take page** — reuse exit-ticket UX patterns.
7. **Grading + reporting** — extend exit-ticket grading; new course-level
   summative report on the dashboard.

Steps 1–4 are non-user-facing for the student and can ship together.
Step 5 unblocks teacher workflow. Steps 6–7 are the visible end-state.
Will check in with user after step 4 and again after step 5.

## What we deliberately defer (post-pilot)

- PDF export of the summative paper.
- Per-grade summatives spanning multiple courses.
- Scheduled / end-of-term auto-trigger.
- Splitting into a separate `SummativeExam` model — only do this if the
  shared `ExitTicket` table becomes painful in practice.
