# Max-Depth Lesson Steps — Plan (2026-04-29)

## Problem

Previously, lesson duration drove the LLM step count at GENERATION
time: 15 min → 3 steps, 20 min → 4 steps, ..., 50 min → 10 steps.
Changing lesson duration required regenerating every lesson, paying
the LLM cost again.

For pilot recalibration ("our 20-min lessons are actually running
30 min — let's drop to 15") this is wasteful: 56 lessons × ~$0.10
generation cost × N adjustments.

## Design

**Always generate at max depth (10 steps), tag each step with a
priority, let the tutor engine pick a subset at session start based
on the requested duration.**

- Generation cost paid once per lesson — full 10-step bundle
- Duration changes are FREE (zero LLM calls)
- Student can request a shorter session ("I have 15 min today"); the
  engine adapts immediately
- Teacher can change `lesson.estimated_minutes` per lesson or
  per-course default; takes effect on the next session

## Step priority

`LessonStep.priority` (`PositiveSmallIntegerField`, default 1):

| Value | Name | Selection role |
|-------|------|-----------------|
| 1 | REQUIRED | Always included regardless of duration. Reserve for: first ENGAGE, primary TEACH, primary PRACTICE, final QUIZ. ~4 per lesson. |
| 2 | CORE | Included by default; first to drop on tight time. Worked example, second practice, synthesise. ~3 per lesson. |
| 3 | ENRICHMENT | Dropped on short sessions. Third+ practice variants, common-mistakes drill, extends. ~3 per lesson. |

The LLM is instructed in the generation prompt to assign these
sensibly. `_save_steps_to_db._clamp_priority` defends against bad
output by clamping to {1, 2, 3} with a default of 1 (REQUIRED).

## Selection algorithm

`ConversationalTutor._select_steps_for_duration(all_steps, target_minutes)`:

```
target_count = max(3, min(10, target_minutes // 5))
must_include = {first_idx, last_idx}      # always engage + quiz
middle = sort by (priority asc, order_index asc)
chosen = must_include ∪ middle[: target_count - 2]
return [step for i in sorted(chosen) for step in all_steps[i]]
```

- 5 min/step in conversation → target_count maps cleanly to budget
- First and last step always preserved (5E flow's anchors)
- Middle steps picked by priority, ties broken by lesson order
- Final list re-sorted by `order_index` so lesson progression is
  preserved

Verified empirically: for a 10-step lesson with priorities 1/3/2/2/1/2/3/3/3/1, selection yields:
- 15 min → engage + teach[1] + quiz (3 steps)
- 20 min → + practice[4] (4 steps)
- 25 min → + worked_example[3] (5 steps)
- 30 min → + practice[5] (6 steps)
- 40 min → + extras up to 8 steps
- 50 min → all 10

Legacy lessons (4-6 steps, all priority=1 default) work
gracefully: short sessions trim middle steps in lesson order; full
duration shows all of them.

## Target duration resolution

`ConversationalTutor._target_minutes_for_session()`:

1. `session.engine_state['target_minutes_override']` — explicit
   per-session pick (student "I have 15 min today" or teacher
   override). Set this to bypass the lesson default.
2. `lesson.estimated_minutes` — course-level default duration set
   by the teacher. Default 20.
3. `20` — fallback.

Step selection is computed once at session init from this and
cached in `self.steps`. Changes to `estimated_minutes` after a
session has started don't affect the in-flight session — they
take effect on the next session.

## Out of scope (this iteration)

- Student-facing "I have X minutes" picker on the catalog. The
  `target_minutes_override` slot is wired up; the picker UI is a
  separate ticket.
- Teacher-facing per-course default duration setting that doesn't
  trigger regen. The Regenerate-all duration dropdown still
  triggers regen; that's fine because regen is now max-depth and
  the LLM call is the same regardless of duration.
- Re-tagging existing lesson steps with priority. Legacy lessons
  default to priority=1; selection handles them gracefully.
- Adaptive step selection based on student skill level. Currently
  selection is duration-only, not skill-aware. A future iteration
  could weight enrichment higher for advanced students.

## Files modified

- `apps/curriculum/models.py` — `LessonStep.Priority` enum + `priority` field.
- `apps/curriculum/migrations/0019_lesson_step_priority.py`
- `apps/curriculum/content_generator.py`:
  - `LessonStepSchema.priority` field with prompt-binding description
  - `_generate_steps`: always produce 10 steps regardless of `target_minutes`
  - Prompt block teaching priority assignment
  - `_save_steps_to_db`: persist priority via `_clamp_priority`
- `apps/tutoring/conversational_tutor.py`:
  - `__init__`: load `all_steps`, then filter via `_select_steps_for_duration`
  - `_target_minutes_for_session()` helper
  - `_select_steps_for_duration(all_steps, target_minutes)` selector

## Why this lands cleanly

`current_topic_index` and `_step_media_ids` already index into
`self.steps` (the filtered list) via `enumerate`, not via
`order_index`. No call site assumed contiguous order_index, so the
filtered-list semantics work without changes elsewhere in the engine.

## Backward compatibility

Existing 4-6-step lessons (Math S3 has just been regenerated at
6 steps under the previous design) continue to work — every step
defaults to priority=1, the selector returns them all when
duration ≥ their length, or trims gracefully when duration is
shorter. After the next regen they'll come back at 10 steps with
priorities tagged properly.
