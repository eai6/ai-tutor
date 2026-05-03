# AI Tutor — Platform Context for the Help Assistant

This document is the help assistant's primary reference for explaining
how the platform works. It is indexed into the `help_docs` ChromaDB
collection at deploy time. Sections labelled `[STAFF]` are
teacher/admin only — students don't see them.

## Mission

AI Tutor is a Django LLM-tutoring platform for secondary school
students. Conversational sessions follow the 5E pedagogical model
(Engage / Explore / Explain / Practice / Evaluate). Teachers manage
curriculum on a dashboard; students take lessons one at a time and
finish each with an exit ticket.

Pilot context: Seychelles secondary schools, S1–S5. Subjects in pilot
are Mathematics and Geography. The platform is curriculum-agnostic —
schools upload their own syllabi.

## Audience

- **Students** — sign in, pick a course, work through lessons,
  retake exit tickets when they fail.
- **Teachers** — generate curriculum, watch class progress, assign
  lessons by week, review flagged chats. `[STAFF]`
- **Super admins** — operate the platform across multiple schools.
  Same teacher tools but unfiltered. `[STAFF]`

## Core flow — student side

### Sign in
1. Student opens the URL their school gave them.
2. Picks "I'm a Student", signs in (username + password) or registers.
3. Lands on the catalog (`/tutor/`) showing the courses for their grade.

### Take a lesson
1. Tap a course, then tap a lesson — the tutor opens at `/tutor/lesson/<id>/`.
2. The tutor walks through 4–10 steps following the 5E phases. Each
   step has a teaching script + sometimes a media image.
3. Practice steps ask a question. Type the answer the way you'd write
   it on paper. The math toolbar above the chat gives quick-insert
   buttons for `°  π  √  ²  ³  ±  ≤  ≥  ≠  ×  ÷  ½  ¼  ¾  θ`.
4. After all steps, the tutor switches to **exit ticket mode**:
   10 questions drawn from a 35-question bank.

### Exit ticket formats (5)
- **Multiple choice** — 4 options, pick a letter.
- **Fill in the blank** — type the missing word/number.
- **Matching** — pair items left-side ↔ right-side.
- **Short answer** — one or two sentences.
- **Short numeric** — type a single number (math lessons).

Pass threshold is shown at the top. Pass = mastery; fail = remediation.

### Remediation
When a student fails an exit ticket the tutor:
1. Names which **enabling objectives** (sub-skills) the student got
   right vs missed.
2. Walks through every failed question in lesson-EO order — student
   re-attempts each one, deterministic grading.
3. After all failed questions reviewed, runs a **re-quiz**: 2 fresh
   questions per failed EO drawn from the published bank with
   weighted sampling (failed=5x, unattempted=3x, mastered=1x).
4. If a student scores 100% on an EO's re-quiz, that EO promotes to
   mastered.

### Group sessions
Some schools enable group mode — one device, multiple students
joining the same lesson. The tutor tracks who answered which question
so individual mastery still counts.

### Difficulty signals
- Tap **Too hard?** — tutor drops to easier rungs of the complexity
  ladder.
- Tap **Too easy?** — tutor escalates.

### Audio mode
Tap the speaker icon on a tutor message to hear it read aloud.

### Help / FAQ
Public at `/dashboard/help/`. Linked from the bottom of every page
and from the landing-page footer. Anonymous users can read it.

### Install as an app
On Chrome / Edge / Android, the platform is a PWA — the browser will
prompt to install AI Tutor as a standalone app. Same login, faster
repeat loads on flaky connections.

## Core flow — teacher side `[STAFF]`

### Dashboard home `/dashboard/`
- KPI cards: total students, active students, sessions, mastery rate.
- Sessions chart (last 14 days, with a real Y-axis).
- Recent activity feed.

### Curriculum
- Upload a curriculum PDF / DOCX → parser extracts units, lessons,
  terminal objectives, enabling objectives.
- Each lesson can be **regenerated** with per-scope checkboxes:
  - **Steps** — wipe + rebuild teaching script, ~2 min.
  - **Exit ticket** — force-replace question bank, ~30s, attempt
    history preserved.
  - Both — full regen.
- Course-level "Regenerate content" form mirrors the same checkboxes
  for every lesson in the course in parallel (10 workers, ~2 min/lesson).

### Class detail page `/dashboard/classes/<grade>/`
- Lists every course generated for that grade (e.g. all S3 courses).
- Per course: lesson count, students-attempted, class mastery average
  (mastered cells / S3 students × lessons).
- Roster scoped to that grade only.
- Selective promote/graduate: tick individual students, click promote
  → only those move up. Default promote-all is still available.

### Class competency map `/dashboard/curriculum/course/<id>/competencies/`
- One row per teaching objective.
- Columns: baseline, latest, final, Δ, mastered ≥ 70%.
- Source: per-lesson exit-ticket attempts.
- Roster denominator scoped to the course's grade level.

### Student detail `/dashboard/students/<id>/`
- Per-course mastery breakdown.
- Per-lesson cards showing achieved/total + competency band:
  - **EE** — Exceeding Expectation (≥ 90%).
  - **ME** — Meeting Expectation (≥ 70%).
  - **AE** — Approaching Expectation (≥ 50%).
  - **BE** — Below Expectation (< 50%).
  - **UN** — Unassessed (no exit-ticket attempt yet).

### Weekly assignments
- On the course detail page, teachers assign which lessons students
  should focus on this week. Students see a "this week" panel on
  their catalog.

### Flagged chats
Sessions a student or the safety filter flags as concerning land in
`/dashboard/flagged/`. Teachers review and resolve.

### Help assistant chat
Bottom-right of every page. Documentation lookup + navigation only —
it cannot read student data. For data questions ("how is X doing")
use the dashboard pages directly.

## Architecture overview `[STAFF]`

### Stack
- **Backend**: Django 5, Python 3.11.
- **Database**: PostgreSQL in production, SQLite in dev.
- **LLMs**: Anthropic, OpenAI, Google, or local Ollama, picked per
  purpose via `llm.ModelConfig`. Abstraction in
  `apps/llm/client.py::BaseLLMClient`.
- **Vector DB**: ChromaDB + sentence-transformers (all-MiniLM-L6-v2,
  offline). Two collections: curriculum + help_docs.
- **Deploy**: Azure Container Apps (Pixel Design Labs subscription),
  Pulumi IaC in `infra/`. GitHub Actions deploy on push to `main`.

### Apps under `apps/`
- `accounts` — auth, Institution, Membership, StudentProfile,
  PlatformConfig.
- `curriculum` — Course → Unit → Lesson → LessonStep, KnowledgeBase,
  ContentGenerator, parametric question templates.
- `tutoring` — TutorSession + `conversational_tutor.py` engine,
  ExitTicket, ExitTicketQuestion, ExitTicketAttempt, skills tracking.
- `dashboard` — teacher UI views + templates.
- `llm` — provider abstraction.
- `media_library` — uploaded teaching materials.
- `safety` — content flagging, audit log.
- `support` — help assistant chat + ChromaDB help_docs.

### Multi-tenancy
Every query touching user data filters by institution.
`Q(institution=inst) | Q(institution__isnull=True)` includes
platform-wide content. `institution=None` means "all schools";
normalised to `GLOBAL_INSTITUTION_ID = 0` when indexing ChromaDB.

### Session state machine
The tutor uses a simple `SessionState` enum:
- `TUTORING` — walking through steps.
- `EXIT_TICKET` — student is in the assessment.
- `COMPLETED` — done.

5E phase comes from each step's `phase` field, not session state.

### Math tutoring rules
- The LLM never calculates correct answers. It only compares student
  answers to verified, approved schema answers stored on the
  question record.
- Math practice grading uses `LessonStep.expected_answer` via the
  deterministic math check.
- Bank-pulled questions during tutoring use a deterministic grader
  (P3 of curriculum_tutor_v2 plan).
- Math exit-ticket questions are templated (parameterised + computable),
  so the answer is whatever the formula evaluates to.

### Conceptual integrity (math)
Question premises must respect the lesson's core rule. Templates
declare `constraints` (e.g. `a + b < 360` for angles around a point)
and the renderer only samples values that satisfy them. This blocks
"three angles around a point are 120°, 130°, 140°" pseudo-questions
where the sum violates the rule.

## Recent platform updates (2026-05)

- **PWA support**: service worker + web manifest. Chrome/Edge/Android
  prompt to install.
- **Class detail pages**: dedicated `/dashboard/classes/<grade>/` with
  per-course mastery averages and a grade-scoped roster.
- **Lesson regen scope flags**: checkboxes for Steps + Exit ticket on
  lesson and course detail pages. Exit-ticket-only is ~30s.
- **EO-driven remediation**: walkthrough of failed questions + re-quiz
  with EO-weighted sampling, then promotion of mastered EOs.
- **Help page public**: `/dashboard/help/` no longer requires login.
- **Help assistant locked down**: catalog reduced to documentation +
  navigation only — no DB-reading or write tools.
- **Sessions chart Y-axis**: real ticks, gridlines, day-of-month
  labels. Replaced the old course-progress widget on the home page.
- **Student detail UN band**: untaken lessons show UN (gray) instead
  of BE (red).
- **Math conceptual-integrity prompt**: forbids premises that violate
  the lesson's core rule. Templates must declare guard constraints.

## Key files for the assistant `[STAFF]`

- `apps/tutoring/conversational_tutor.py` — engine. `respond()`,
  `_handle_exit_ticket`, `_start_remediation`,
  `_maybe_advance_walkthrough`.
- `apps/curriculum/content_generator.py` — `generate_complete_lesson`,
  `generate_exit_ticket_for_lesson`,
  `_expand_to_granular_subskills`, `_normalize_enabling_objective`.
- `apps/curriculum/parametric_renderer.py` — math templates.
- `apps/tutoring/question_bank.py` — bank sampling, weighted draw,
  re-quiz queue.
- `apps/tutoring/bank_grader.py` — deterministic grader for bank
  questions.
- `apps/dashboard/views.py` — teacher dashboard.
- `apps/support/tools.py` — help assistant tool catalog.

## Glossary

- **Terminal Objective (TO)** — broader unit-level outcome. Stored on
  `Unit.terminal_objectives`.
- **Enabling Objective (EO)** — granular lesson-level sub-skill.
  Stored on `Lesson.enabling_objectives`.
- **Mastery** — passed the lesson's exit ticket above the pass
  threshold (default 70%).
- **Bank** — the lesson's pool of 35 published exit-ticket questions.
- **Walkthrough** — the post-fail review of every wrong answer.
- **Re-quiz** — fresh questions per failed EO, drawn after the
  walkthrough.
- **Promote** — move a student up a grade. Done from the class detail
  page, supports per-student selection.
- **Soft delete** — courses get a 30-day grace period before purge.
  Student data is never hard-deleted.

## What the help assistant should NOT do

- **Don't read student data.** Direct the user to the relevant
  dashboard page.
- **Don't make changes.** No assignments, no edits, no deletions.
- **Don't guess.** If the docs don't cover a question, escalate to
  human support.
- **Don't repeat the user's question back at them.** Be terse: 2–4
  sentences plus a clear next step.
