# Student Competency Persistence — Plan (2026-04-28)

## Problem

Student competency data is currently coupled to course/unit/lesson rows
via CASCADE FKs. A teacher re-parsing or deleting a course wipes:

- `StudentLessonProgress` (mastery, best_score, attempts)
- `StudentSkillMastery` (SM-2 per-skill state)
- `ExitTicketAttempt` history
- `TutorSession` transcripts

We want competencies to **persist with the student** — earned at the
moment of mastery, snapshotting enough context to remain meaningful
even if the source course/unit/lesson is later deleted or rewritten.

## Design

Append-only model `StudentCompetencyRecord` on the student. Two
granularities written by two triggers; live tracking models stay as-is.

### Model

`apps/tutoring/skills_models.py` (next to `StudentSkillMastery`):

```python
class StudentCompetencyRecord(models.Model):
    """Permanent transcript entry. Earned at mastery, immutable.
    Survives course/unit/lesson deletion via SET_NULL + snapshot fields.
    """

    class Granularity(TextChoices):
        LESSON = 'lesson', 'Lesson'
        SKILL  = 'skill',  'Skill'

    student = FK(User, CASCADE)
    granularity = CharField(choices=Granularity.choices)

    # Snapshot — survives source deletion
    objective_text = TextField()           # the lesson objective or skill name
    course_title_snapshot = CharField(200)
    unit_title_snapshot = CharField(200, blank=True)
    lesson_title_snapshot = CharField(200, blank=True)
    subject = CharField(20, blank=True)    # 'math', 'science', etc
    grade_level = CharField(10, blank=True) # 'S3'

    # Achievement
    score = FloatField(null=True)          # 0.0–1.0 at moment of earn
    earned_at = DateTimeField(default=timezone.now)
    source_session = FK(TutorSession, SET_NULL, null=True)

    # Optional live pointers — SET_NULL so source can be deleted
    source_lesson = FK(Lesson, SET_NULL, null=True)
    source_skill = FK(Skill, SET_NULL, null=True)
    source_course = FK(Course, SET_NULL, null=True)

    institution = FK(Institution, CASCADE)
    created_at = DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-earned_at']
        indexes = [
            Index(fields=['student', 'earned_at']),
            Index(fields=['student', 'granularity']),
        ]
        unique_together = [
            # one lesson record per student per source_lesson_id; one skill record per skill_id
            ('student', 'granularity', 'source_lesson', 'source_skill'),
        ]
```

### Write triggers

1. **Per-lesson** — `conversational_tutor.py:4979` where exit-ticket
   passing flips `progress.mastery_level → 'mastered'`. After the save,
   call `StudentCompetencyRecord.record_lesson(progress, session)` —
   idempotent via `unique_together`.
2. **Per-skill** — `skills_models.py:record_attempt` line 432 where
   `state` flips to `MasteryState.MASTERED`. After the save, call
   `StudentCompetencyRecord.record_skill(self)` — idempotent.

Write helpers live on the model as classmethods. Failures are
swallowed with `logger.warning` — the transcript is best-effort, never
blocks the live tutoring/grading path.

### Backfill

Management command:
`apps/tutoring/management/commands/backfill_competency_records.py`

- Iterate `StudentLessonProgress.objects.filter(mastery_level='mastered')` → create lesson records
- Iterate `StudentSkillMastery.objects.filter(state='mastered')` → create skill records
- Idempotent (re-runnable; `unique_together` guards duplicates)
- Print summary counts

### Out of scope

- Per-objective records (finer than per-lesson). Add later if the
  Tanzania pilot needs tighter mapping.
- Transcript UI page. Separate ticket.
- Decay / unawarding. Records are append-only.
- Migration off CASCADE on existing models. Keeping
  `StudentSkillMastery.skill = CASCADE` is fine because the **transcript
  record** is what survives deletion. Live tracking can still reset on
  course deletion — it's the historical record that needs to persist.

## Files to add / edit

| File | Change |
|------|--------|
| `apps/tutoring/skills_models.py` | Add `StudentCompetencyRecord` model + classmethod write helpers |
| `apps/tutoring/migrations/00XX_*` | Migration |
| `apps/tutoring/conversational_tutor.py:4979` | Write lesson record after mastery transition |
| `apps/tutoring/skills_models.py::record_attempt` (~432) | Write skill record after MASTERED transition |
| `apps/tutoring/management/commands/backfill_competency_records.py` | New backfill command |

## Solo-dev estimate

~0.5 day. Then continue with slow_learner Phase 2.
