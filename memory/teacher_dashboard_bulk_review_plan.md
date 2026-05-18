# Teacher dashboard — bulk pending-image gen + bulk AI review + mark-reviewed (#215)

## Problem

Today the teacher dashboard has per-lesson regen and per-step manual
regen buttons, but no course-level "do all the pending work in one
click" affordances. After a course gets generated, an admin has to:

- Hunt through every lesson to find images that didn't render
  (`LessonStep.media[].url` missing) and regenerate them one by one.
- Manually trigger AI review on lessons / exit-ticket questions that
  came in before the content judges ran (or were imported without
  judge_outputs populated).
- Has no way to mark a flagged item as human-reviewed — the
  `auto_flagged` state sticks even after a teacher eyeballs it and
  decides it's fine.

Three buttons:

1. **"Generate pending images"** — sweeps course, generates every image
   whose `LessonStep.media[].url` is empty.
2. **"Run AI review on unreviewed content"** — runs content judges on
   every `LessonStep` and `ExitTicketQuestion` whose `judge_outputs`
   is empty.
3. **"Mark as reviewed"** — per-item button on flagged content;
   clears the flag, records reviewer + timestamp.

## Current state (audit summary)

- **Course detail page**: `apps/dashboard/views.py:866` (`course_detail`),
  template `templates/dashboard/curriculum/course_detail.html`.
  Existing bulk-action buttons (Generate All Content, Regenerate, etc.)
  live in `<div class="bulk-actions">` around line 589.
- **Per-image gen entry**: `ImageGenerationService.get_or_generate_image`
  (`apps/tutoring/image_service.py`); bulk loop already exists at
  `apps/dashboard/background_tasks.py:258-338` (`generate_media_for_lessons`).
  Pending = `image.get('url')` is empty.
- **Per-step judge entry**: `_run_content_judges_for_steps`
  (`apps/curriculum/content_generator.py:24-236`). Runs factual,
  pedagogy, safety concurrently.
- **Per-Q judge entry**: `_run_exit_question_judge_for_mcqs`
  (`apps/curriculum/content_generator.py:285-430`).
- **Flag state on LessonStep**: `content_quality_status` enum
  (`UNREVIEWED` / `AUTO_OK` / `AUTO_FLAGGED` / `HUMAN_APPROVED` /
  `HUMAN_EDITED`). No `reviewed_by` / `reviewed_at`.
- **Flag state on ExitTicketQuestion**: only implicit via
  `judge_outputs['exit_question'].passed`. No quality_status field,
  no reviewer fields.
- **Background pattern**: `apps/dashboard/background_tasks.py:54`
  (`run_async`) — daemon threading + `CurriculumUpload` row for
  progress log. NO Celery.
- **Permission**: `@teacher_required` + `request.staff_ctx['can_edit_content']`.

## Target design

### Phase 1 — model changes (1 migration)

`ExitTicketQuestion` (`apps/tutoring/models.py`):
- Add `content_quality_status` enum matching the LessonStep one
  (UNREVIEWED / AUTO_OK / AUTO_FLAGGED / HUMAN_APPROVED / HUMAN_EDITED).
  Default UNREVIEWED.

Both `LessonStep` and `ExitTicketQuestion`:
- Add `reviewed_by` FK to `accounts.User`, nullable, `on_delete=SET_NULL`.
- Add `reviewed_at` `DateTimeField(null=True)`.
- Add migration to populate `content_quality_status` for
  ExitTicketQuestion from existing `judge_outputs`:
  - `judge_outputs.exit_question.passed == True` → `AUTO_OK`
  - `judge_outputs.exit_question.passed == False` → `AUTO_FLAGGED`
  - empty → `UNREVIEWED`

### Phase 2 — background helpers

In `apps/dashboard/background_tasks.py`:

```python
def bulk_generate_pending_images_async(course_id, upload_id, user_id):
    """Iterate every LessonStep in course, generate any image whose
    media[].url is empty. Logs to CurriculumUpload."""
    # reuse generate_media_for_lessons logic; just scope the query
    # to the whole course's lessons.

def bulk_run_unreviewed_judges_async(course_id, upload_id, user_id):
    """Iterate every LessonStep + ExitTicketQuestion in course whose
    judge_outputs is empty OR content_quality_status='unreviewed',
    run the appropriate judges, update fields."""
    # call _run_content_judges_for_steps per lesson's steps slice
    # call _run_exit_question_judge_for_mcqs per lesson's exit ticket
```

Both wrap existing per-lesson / per-question entrypoints — no new
judge code, just orchestration.

### Phase 3 — views (apps/dashboard/views.py)

```python
@teacher_required
def course_generate_pending_images(request, course_id):
    # POST only; create CurriculumUpload(status='media_processing'),
    # run_async(bulk_generate_pending_images_async, ...); redirect
    # to media_progress.

@teacher_required
def course_run_unreviewed_judges(request, course_id):
    # POST only; same shape; status='processing'; redirect to
    # content_progress.

@teacher_required
def lesson_step_mark_reviewed(request, step_id):
    # POST; flip content_quality_status to HUMAN_APPROVED,
    # set reviewed_by=request.user, reviewed_at=now. Redirect back
    # via HTTP_REFERER.

@teacher_required
def exit_ticket_question_mark_reviewed(request, question_id):
    # Same.
```

### Phase 4 — URLs (apps/dashboard/urls.py)

```python
path('course/<int:course_id>/generate-pending-images/',
     views.course_generate_pending_images,
     name='course_generate_pending_images'),
path('course/<int:course_id>/review-unreviewed/',
     views.course_run_unreviewed_judges,
     name='course_run_unreviewed_judges'),
path('lesson-step/<int:step_id>/mark-reviewed/',
     views.lesson_step_mark_reviewed,
     name='lesson_step_mark_reviewed'),
path('exit-question/<int:question_id>/mark-reviewed/',
     views.exit_ticket_question_mark_reviewed,
     name='exit_ticket_question_mark_reviewed'),
```

### Phase 5 — template changes

`templates/dashboard/curriculum/course_detail.html`:
- Add two buttons inside the existing `<div class="bulk-actions">`
  block (~line 589). Use the same `<form method="post">` + confirm()
  pattern as Generate All / Regenerate.

Wherever flagged content is rendered (lesson_detail, exit-Q list,
flagged dashboard at `/dashboard/flagged/`):
- Show a "Mark as Reviewed" button next to items where
  `content_quality_status == 'auto_flagged'`. Submit to the
  per-item mark-reviewed view.

## Out of scope

- No batching / pagination on the bulk runs — process all items in
  one async pass. Progress page polls upload log.
- No undo of "mark as reviewed" — once approved, stays approved
  until next regen. (Add later if needed.)
- No notification when bulk job finishes — teacher refreshes the
  progress page (matches existing UX).
- No retry-on-failure for individual judge calls — rely on the
  existing per-judge fail-soft.

## Open questions (need direction before coding)

1. **Should "Run AI review on unreviewed content" run ALL judges on
   ALL content (re-running anything that previously had judge_outputs
   from a prior pass), or strictly only items where `judge_outputs`
   is empty?** Default proposal: strictly unreviewed (cheap, idempotent,
   matches the button label). A separate "Re-run all" button could
   come later if needed.

2. **Should "Generate pending images" also count images that exist
   but failed a `figure_alignment` judge (e.g., vision judge said
   the image doesn't match the description)?** Default proposal: NO
   — that's a different button ("Regenerate flagged figures"). This
   button is just for images that never got generated in the first
   place.

3. **"Mark as reviewed" — should it also un-do the `auto_flagged`
   state if a teacher EDITS the content?** Edits today set
   `HUMAN_EDITED`. Proposal: leave that path as-is; this button is
   purely "approve without editing".

## Files to touch

| File | Change |
|---|---|
| `apps/tutoring/models.py` | Add `content_quality_status`, `reviewed_by`, `reviewed_at` to `ExitTicketQuestion`. Add `reviewed_by`, `reviewed_at` to `LessonStep`. |
| `apps/tutoring/migrations/0032_*.py` | Schema + data migration populating `content_quality_status` from `judge_outputs`. |
| `apps/dashboard/background_tasks.py` | `bulk_generate_pending_images_async`, `bulk_run_unreviewed_judges_async`. |
| `apps/dashboard/views.py` | 4 new views (2 bulk, 2 mark-reviewed). |
| `apps/dashboard/urls.py` | 4 routes. |
| `templates/dashboard/curriculum/course_detail.html` | 2 buttons. |
| `templates/dashboard/curriculum/lesson_detail.html` + `templates/dashboard/quality_dashboard.html` (or wherever flagged items render) | Per-item "Mark as Reviewed" button. |

## Phased delivery

| Phase | Work | Day-equivalent |
|---|---|---|
| 1 | Migration + model fields | 0.25 |
| 2 | Two background helpers (mostly reuse) | 0.25 |
| 3 | Four views + URLs | 0.25 |
| 4 | Course-detail template buttons | 0.10 |
| 5 | Mark-reviewed template integration (find every flagged-render surface) | 0.50 |
| 6 | Test locally on a course with pending images + unreviewed steps | 0.25 |

Total ~1.6 days solo.

## Next step

Get user direction on the three open questions, then start Phase 1
(migration).
