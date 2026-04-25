# Exit-Ticket-Driven Lesson Competency — Plan (2026-04-23)

## Problem

Today, "competency" is an ambiguous concept in the platform. The audit surfaced three overlapping tracking layers:
1. **Lesson mastery** — binary `not_started / in_progress / mastered` on `StudentLessonProgress`
2. **Enabling-objective (EO) coverage** — per-EO yes/no tracked in `engine_state.covered_enabling_objectives` during tutoring
3. **Skill mastery** — separate `StudentSkillMastery` decimal spaced-repetition system

The surface story is "mastery is driven by enabling objectives," but the actual code:
- **Already transitions `mastery_level='mastered'` only on exit ticket pass** (`conversational_tutor.py:4098`), or when lesson has no exit ticket and all steps done (`conversational_tutor.py:3774`)
- **Ignores `Lesson.mastery_rule`** — stored but never consulted (dead field)
- **Hardcodes the passing threshold to 8** (`conversational_tutor.py:4020`) rather than using `ExitTicket.passing_score`
- **Never updates `StudentLessonProgress.best_score`, `correct_streak`, `total_attempts`, `total_correct`** — dead fields
- **Teacher dashboard EO reporting** (`dashboard/views.py:1656-1689`) already consults exit ticket `concept_tag` first, then falls back to engine-state coverage

**The user's ask** — "calculate competency based on exit tickets, not enabling objectives" — is really three things bundled:

1. **Formalize exit tickets as the single source of truth for lesson competency.**
2. **Fix the bugs** (ignored `passing_score`, unpopulated `best_score`, dead `mastery_rule`).
3. **Clean up the data model** to match the actual behavior, remove dead fields, and make teacher/student visibility coherent.

Enabling objectives keep their role as *content structure* (they guide lesson design and remediation targeting) but stop being the *measurement primitive* for whether a student mastered the lesson.

---

## Target model

### Three primitives, clearly separated

| Concept | Source of truth | Type | Where it lives |
|---|---|---|---|
| **Lesson mastery** (binary) | Best exit ticket attempt ≥ passing threshold | bool | `StudentLessonProgress.mastery_level` |
| **Lesson competency** (graded) | Best exit ticket attempt score (%) | float 0.0–1.0 | `StudentLessonProgress.best_score` (populated) |
| **Per-concept competency** | Best exit ticket attempt, aggregated by question `concept_tag` | dict | Computed view on top of `ExitTicketAttempt.answers` |

Enabling objectives are **derived** from exit ticket `concept_tag` — when teachers define an exit ticket, its questions' `concept_tag` values ARE the lesson's measurable enabling objectives. If a concept isn't tested in the exit ticket, it isn't measured.

### What stays, what goes

**Keep and use properly**:
- `ExitTicket.passing_score` — start using it (stop hardcoding 8)
- `ExitTicketAttempt.{score, passed, answers}` — already the source; formalize
- `ExitTicketQuestion.concept_tag` — becomes the canonical identifier for a measurable objective
- `StudentLessonProgress.{mastery_level, best_score, last_session_at}` — keep, properly populate

**Deprecate / remove**:
- `Lesson.mastery_rule` — dead field, remove
- `StudentLessonProgress.{correct_streak, total_attempts, total_correct}` — unused, remove
- `TutorSession.mastery_achieved` — derivable from `StudentLessonProgress`; consider removing (later migration)

**Keep but demote**:
- `Lesson.enabling_objectives` JSONField — stays for pedagogy/lesson design, but no longer drives mastery
- `engine_state.covered_enabling_objectives` — stays for remediation targeting during a session, but doesn't feed mastery
- `StudentSkillMastery` — independent skill-graph system, orthogonal to lesson competency; leave alone for now

---

## Calculation logic

### Lesson competency (graded, 0.0–1.0)

```
competency(student, lesson) = best_attempt_score / total_questions
  where best_attempt = max(ExitTicketAttempt by score for this student+lesson)
```

Stored in `StudentLessonProgress.best_score` as a float in `[0.0, 1.0]`. Updated on every exit ticket submission.

### Lesson mastery (binary)

```
mastered = best_score >= (exit_ticket.passing_score / total_questions)
```

Stored in `StudentLessonProgress.mastery_level`:
- `not_started` — no attempts, no tutoring started
- `in_progress` — tutoring started OR at least one attempt but none passing
- `mastered` — at least one attempt ≥ passing threshold

**Monotonic**: once `mastered`, never regresses. Retakes after mastery can raise `best_score` but won't demote.

### Per-concept competency

For each distinct `concept_tag` in the lesson's exit ticket:

```
concept_competency(student, lesson, concept) =
  correct_count_for_concept / total_count_for_concept
  in the best attempt
```

Computed on demand from `ExitTicketAttempt.answers` — not stored. Cheap enough: an exit ticket has ~10 questions.

Exposed in:
- Teacher dashboard (per-concept heatmap)
- Student progress view (weak areas)
- Remediation targeting (failed concepts drive re-teach focus)

### No exit ticket? (Edge case)

Some lessons today have no `ExitTicket` (mostly older/placeholder lessons). Options:
- **Block mastery** for those lessons — student sees "Exit ticket pending" until teacher adds one
- **Grandfather**: if all steps completed, mark `in_progress` but never `mastered` until an exit ticket is added
- **Current behavior** (`conversational_tutor.py:3774`): auto-grants `mastered` when all steps done and no exit ticket

**Recommend**: deprecate the grandfather path. New lessons require an exit ticket for mastery. Add `Lesson.has_exit_ticket` (derived property), and in dashboards, flag lessons without exit tickets as "needs exit ticket" so teachers can fill the gap.

### Multiple attempts

Best-score wins. Rationale: a student who scored 70% then 90% has clearly improved; we credit the higher score. Attempt history stays in `ExitTicketAttempt` rows for full audit.

---

## Data model changes

### Migration 1 — Add populated fields, remove dead fields

**`apps/curriculum/models.py::Lesson`**
```python
# REMOVE:
# mastery_rule = models.CharField(...)  # dead field

# No additions; enabling_objectives stays as-is (demoted, not removed)
```

**`apps/tutoring/models.py::StudentLessonProgress`**
```python
# REMOVE:
# correct_streak, total_attempts, total_correct  # never updated

# KEEP AND POPULATE:
# best_score: FloatField(null=True)  # now becomes 0.0-1.0, populated on every attempt
# mastery_level: CharField(...)       # now properly driven by best_score vs passing threshold
# last_session_at: DateTimeField     # keep

# NEW:
last_attempt_at = models.DateTimeField(null=True, blank=True)
attempts_count = models.PositiveIntegerField(default=0)
```

**`apps/tutoring/models.py::ExitTicket`**
```python
# No schema change. But now the field is ACTUALLY USED:
# passing_score: PositiveIntegerField(default=8)

# Optional addition (later, if teachers want per-lesson pass thresholds expressed as %):
# passing_score_pct = models.FloatField(default=0.8)
# Prefer this over count-based, since question count varies. Migration: passing_score_pct = passing_score / 10
```

**`apps/tutoring/models.py::ExitTicketAttempt`**
```python
# No schema change. Already has what we need: score, passed, answers (with concept_tag per answer)

# Add convenience property:
@property
def score_pct(self) -> float:
    total = len(self.answers) if self.answers else 1
    return self.score / total if total else 0.0

@property
def per_concept_results(self) -> dict[str, dict]:
    """Returns {concept_tag: {correct: int, total: int, pct: float}}"""
    ...
```

### Migration 2 — Backfill

One-off data migration:
```python
# For every StudentLessonProgress:
#   attempts = ExitTicketAttempt.objects.filter(student=sp.student, exit_ticket__lesson=sp.lesson)
#   if attempts.exists():
#       best = max(attempts, key=lambda a: a.score)
#       sp.best_score = best.score / len(best.answers)
#       sp.attempts_count = attempts.count()
#       sp.last_attempt_at = attempts.latest('completed_at').completed_at
#       # Re-evaluate mastery against actual passing threshold:
#       threshold_pct = best.exit_ticket.passing_score / len(best.answers)
#       if sp.best_score >= threshold_pct:
#           sp.mastery_level = 'mastered'
#       elif sp.attempts_count > 0:
#           sp.mastery_level = 'in_progress'
#       sp.save()
```

Run as part of deploy; idempotent.

### Migration 3 — Remove dead fields (deferred — next release)

After Migration 1 is live and observed working for a week, drop the dead columns. Don't do it in the same release — keeps rollback safe.

---

## Tutor engine changes

### `apps/tutoring/conversational_tutor.py`

**Fix the hardcoded threshold** (line 4020):
```python
# BEFORE:
passed = correct >= 8

# AFTER:
total = len(results)
threshold = self.exit_ticket.passing_score  # now actually used
passed = correct >= threshold
```

**Update `_complete_session_with_results()`** (line 4031-4099) to write `best_score`:
```python
# After computing per-session score:
score_pct = correct / total if total else 0.0

# Update StudentLessonProgress:
progress = StudentLessonProgress.objects.get(student=self.session.student, lesson=self.lesson)
progress.attempts_count = F('attempts_count') + 1
progress.last_attempt_at = timezone.now()
if progress.best_score is None or score_pct > progress.best_score:
    progress.best_score = score_pct
# Only upgrade mastery, never downgrade:
if score_pct >= (self.exit_ticket.passing_score / total) and progress.mastery_level != 'mastered':
    progress.mastery_level = 'mastered'
progress.save()
```

**`_start_remediation()`** (line 4176-4272) — no change needed. Failed `concept_tag`s still drive remediation targeting; that logic is orthogonal to competency measurement.

**`_load_enabling_objectives()`** (line 605-625) — still loads EOs for system-prompt injection and remediation targeting. Keep. Note in the docstring that EOs no longer drive mastery decisions.

**`engine_state.covered_enabling_objectives`** — still tracked during session for remediation. Keep. Note in docstring: "In-session coverage signal, not a mastery gate."

### Dead code removal

- Delete any references to `Lesson.mastery_rule` (grep shows none in logic, only in seeds — remove from seeds too)
- Delete any attempted updates to `correct_streak`, `total_attempts`, `total_correct` — none in current code, but audit seeds/tests

### Group session interaction

(Cross-reference to `group_lessons_plan.md`)

Group session exit ticket submission → iterate participants, apply same update logic per student. Each gets:
- Same `best_score` update (if this attempt exceeds their prior best)
- Same `attempts_count` increment
- Same mastery promotion check

Group sessions don't complicate competency — every student independently gets their own `StudentLessonProgress` updated against the collective score.

---

## Backend API changes

### Existing endpoints — behavior

**POST `/tutor/api/chat/<session_id>/exit-ticket/`** and **POST `/api/v1/sessions/<id>/exit-ticket/`**

Response shape extension:
```json
{
  "passed": true,
  "score": 8,
  "total": 10,
  "score_pct": 0.8,                          // NEW
  "passing_threshold": 8,
  "passing_threshold_pct": 0.8,              // NEW
  "best_score_pct": 0.85,                    // NEW — reflects StudentLessonProgress after this update
  "attempts_count": 3,                       // NEW
  "mastery_level": "mastered",               // NEW
  "per_concept": [                           // NEW — per-concept breakdown of this attempt
    {"concept": "Newton's 2nd Law", "correct": 2, "total": 3, "pct": 0.67},
    ...
  ],
  "results": [...]  // unchanged
}
```

### New endpoint

**GET `/api/v1/lessons/<lesson_id>/competency/`** — per-student competency view:

```json
{
  "lesson_id": 42,
  "lesson_title": "Newton's Second Law",
  "mastery_level": "in_progress",
  "best_score_pct": 0.7,
  "passing_threshold_pct": 0.8,
  "attempts_count": 2,
  "best_attempt": {
    "attempt_id": 1234,
    "completed_at": "2026-04-23T14:30:00Z",
    "score": 7,
    "total": 10,
    "per_concept": [
      {"concept": "Newton's 2nd Law", "correct": 2, "total": 3, "pct": 0.67},
      {"concept": "Free-body diagrams", "correct": 3, "total": 3, "pct": 1.0},
      {"concept": "Unit conversion", "correct": 2, "total": 4, "pct": 0.5}
    ]
  },
  "weak_concepts": ["Unit conversion", "Newton's 2nd Law"],  // pct < threshold
  "attempts": [...] // abbreviated attempt history
}
```

This feeds student progress views AND teacher reports AND mobile — single source for all UI surfaces.

### Deprecated endpoint shape

If any existing API response exposes `mastery_rule`, `correct_streak`, `total_attempts`, `total_correct` — remove those fields (after a deprecation window if external consumers exist; internal only = remove immediately).

---

## Dashboard / teacher UI changes

### `dashboard/lesson_session_report()` (`views.py:1600-1799`)

This view already does the heavy lifting. Simplify:

**Before** (three sources for per-EO competency, in fallback order):
1. Exit ticket `concept_tag` match
2. Engine state `covered_enabling_objectives`
3. `StudentSkillMastery` by skill text

**After** (exit ticket is the only source):
1. Use `ExitTicketAttempt.per_concept_results` directly
2. If no attempt exists, show "no attempt yet"
3. Remove the engine-state and StudentSkillMastery fallbacks from this view (they measure different things)

### `dashboard/home.html::avg_mastery`

Change calculation from "mean of mastery_level as 0/0.5/1" to:

```python
avg_competency = StudentLessonProgress.objects.filter(
    lesson__in=relevant_lessons
).aggregate(avg=Avg('best_score'))['avg'] or 0.0
```

Display label: "Avg lesson competency" (clearer than "avg mastery").

### `dashboard/class_readiness.html`

Already shows per-lesson mastery bars. Change the percentage from "students mastered" to "average competency score" — more granular, teachers see partial progress.

Show per-concept heatmap per lesson (new widget):
```
Lesson: Newton's 2nd Law
├─ Newton's 2nd Law     ██████░░░░  65%  (weak)
├─ Free-body diagrams   █████████░  95%
├─ Unit conversion      ███░░░░░░░  30%  (weakest)
```

### `dashboard/student_detail.html`

Show per-lesson competency with concept breakdown:

```
Lesson: Newton's 2nd Law   [in progress]   Best: 70%   3 attempts
  Weak concepts: Unit conversion (30%), Newton's 2nd Law (65%)
```

---

## Student UI changes

### `tutoring/catalog.html`

Add a small competency indicator next to the lesson card:

```
┌─ Newton's 2nd Law ─────────────────┐
│ Physics • Grade 8                   │
│ ████████░░  80% mastered            │
│ 2 attempts • Last: 2 days ago       │
└─────────────────────────────────────┘
```

Colors: green ≥ passing threshold, yellow 50-80%, red < 50%, gray no attempts.

### After exit ticket submission

Enhance the completion screen to show:
- Your score: 7/10 (70%)
- Passing threshold: 8/10 (80%)
- Your best: 85% ← from 2 attempts
- Weak areas: Unit conversion, Newton's 2nd Law
- "Keep working!" or "Great job!" based on mastery

---

## Mobile implications

(Cross-reference to `mobile_rn_plan.md`)

- `StudentLessonProgress` table in local SQLite already mirrors server — add `best_score`, `attempts_count`, `last_attempt_at` fields
- Competency displayed in the mobile home screen and lesson card, same semantics as web
- Offline exit ticket submission: same update logic runs locally, syncs on reconnect; conflict resolution = server takes max of client-reported best_score and its own

---

## Testing strategy

### Unit tests

- `test_competency_calculation.py`:
  - Given ExitTicketAttempt with score=8/10 and passing_score=8 → mastery_level transitions to mastered
  - Given score=7/10 → mastery_level stays in_progress
  - Given two attempts 7/10, then 9/10 → best_score = 0.9, mastery_level = mastered
  - Given two attempts 9/10, then 6/10 → best_score stays 0.9, mastery_level stays mastered (monotonic)
  - Given passing_score=7 → 7/10 passes
  - Given no attempts → mastery_level = not_started or in_progress based on tutoring progress

### Integration tests

- Rebuild and update `apps/tutoring/tests/test_r10_mastery_transitions.py`:
  - Full flow: start session → complete steps → take exit ticket with score 7 → verify mastery_level = in_progress, best_score = 0.7
  - Retake with score 9 → verify mastery_level = mastered, best_score = 0.9
  - Test `ExitTicket.passing_score` is respected (not hardcoded)

### Migration test

- Write the data-migration backfill; run against a copy of production DB; verify all rows look right before committing the migration.

---

## Phased delivery

| Phase | Work | Est. |
|---|---|---|
| **C1 — Fix bugs, no model change** | Replace hardcoded `8` with `passing_score`; populate `best_score` on submission; add `attempts_count` + `last_attempt_at` to the update path. Tests. | 1 day |
| **C2 — Data migration + backfill** | Add new fields; write data migration that backfills `best_score`, `attempts_count`, `last_attempt_at` from existing `ExitTicketAttempt` rows. Dry-run locally against prod-dump. | 1 day |
| **C3 — API response shape** | Update exit-ticket submission response + add new competency endpoint; OpenAPI schema update. Tests. | 1 day |
| **C4 — Teacher dashboard** | Switch `lesson_session_report` to exit-ticket-only per-concept; add per-concept heatmap widget; change `avg_mastery` semantics. | 2 days |
| **C5 — Student UI** | Catalog competency bars; post-submission screen with best-score + weak concepts. | 1 day |
| **C6 — Deprecate dead fields** | Drop `Lesson.mastery_rule`, `StudentLessonProgress.{correct_streak, total_attempts, total_correct}`. Grep for any last references. Separate deploy. | 0.5 days |
| **C7 — Mobile parity** | Extend SQLite + app UI to match new competency shape. | 1 day |

**Total: ~6.5 days** of focused work. Can be done incrementally across releases — each phase is independently useful and doesn't block the next.

---

## Open questions

1. **Passing threshold: count or percentage?**
   - Today: `ExitTicket.passing_score` is a count (default 8 out of 10). Breaks if question count varies.
   - Recommend: add `passing_score_pct` float field (default 0.8); compute absolute count from question count at grading time. Deprecate count-based. **Confirm before C2.**

2. **Lessons without exit tickets**:
   - Today: auto-grant `mastered` on all-steps-complete. Should this change?
   - Recommend: keep grandfather behavior for existing lessons, but new lessons must have an exit ticket. Add `Lesson.has_exit_ticket` property for dashboard flagging. **Confirm before C2.**

3. **Multi-attempt policy**:
   - Today: unlimited retakes after remediation. Any limit?
   - Recommend: stay unlimited; record attempts for audit. Teacher can see attempt count in dashboard and intervene if excessive.

4. **Monotonic mastery demotion**:
   - Once `mastered`, does it ever regress? e.g., student retakes and bombs?
   - Recommend: no, mastery is monotonic. `best_score` can change (shouldn't regress either), `mastery_level` strictly monotonic.

5. **Per-concept mastery threshold**:
   - Do we flag a concept "weak" at any fixed pct, or per-lesson-configurable?
   - Recommend: hardcoded `< 0.7` = weak for now. Teacher-configurable if demand arises.

6. **What about `StudentSkillMastery`?**
   - It's an orthogonal skill-graph tracking system. Does it change?
   - Recommend: leave alone. Different purpose (cross-lesson skill tracking with spaced repetition). Flag as separate concern for a future cleanup.

---

## What NOT to do

- **Don't** remove enabling objectives — they're pedagogically useful for content design and remediation targeting. Only demote them from being the mastery primitive.
- **Don't** collapse `StudentSkillMastery` into `StudentLessonProgress` — different purposes.
- **Don't** do C6 (drop dead columns) in the same release as C1–C5 — keep rollback safe.
- **Don't** change monotonicity of `mastery_level` without a strong teacher request — demotion creates confusion.

---

## Next step

Confirm open-question defaults (especially Q1: passing_score count vs pct, and Q2: grandfather lessons without exit tickets). Then start C1 — the bug-fix phase — which is self-contained and delivers immediate value (teachers stop seeing misleading mastery numbers).
