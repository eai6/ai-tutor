# Reorder Units & Lessons — Plan (2026-06-07)

## Problem
Teachers can't change the display order of units within a course or lessons within a
unit. The order is fixed at parse/creation time. We want drag-and-drop reordering on
the course-detail page that persists.

## Current state (from audit)
- `Unit.order_index` (`apps/curriculum/models.py:226`) + `Meta.ordering=['order_index']`
  (`:244`); `Lesson.order_index` (`:293`) + `Meta.ordering=['order_index']` (`:356`).
  Querysets already order by it. **No migration needed.**
- Student-facing catalog reads order_index (`apps/tutoring/views.py:302-309`) — reorder
  takes effect for students immediately.
- Course detail view: `apps/dashboard/views.py:867-884` (units `.order_by('grade_level',
  'order_index')`). Template loop `templates/dashboard/curriculum/course_detail.html`:
  units `{% for unit in units %}` (858-1024), each a `.card` (867); lessons
  `{% for lesson in unit.lessons.all %}` (899-1003), each a `.lesson-row` (900); a
  non-lesson header `.lesson-row` sits at 890-897.
- No existing reorder endpoints/UI; no drag library loaded. Each Course is single-grade
  (per-grade fanout), so a course's units are one flat list.
- CSRF AJAX pattern: `X-CSRFToken: getCookie('csrftoken')` (see `_includes/feedback_button.html`).
- `get_scoped_object_or_404(model, institution, **kwargs)` at `views.py:195`.

## Decisions (confirmed)
- **Drag-and-drop via SortableJS** (vendored to `static/js/Sortable.min.js`, not CDN —
  Azure/Mozambique reliability).
- **Units + lessons** both reorderable.
- Persist immediately on drop via AJAX; small toast on success, revert + message on error.
- Gate behind `can_edit_content` (same as the existing "Add Lesson" control).

## Backend
Two `@teacher_required @require_POST` JSON endpoints in `apps/dashboard/views.py`,
institution-scoped via `get_scoped_object_or_404`:
- `course_reorder_units(course_id)` — body `{order: [unitId,...]}`. Validate every id
  belongs to this course; in a transaction assign `order_index = position` for each.
- `unit_reorder_lessons(unit_id)` — body `{order: [lessonId,...]}`. Same, scoped to the
  unit (and unit.course.institution).
Both reject if the id set doesn't match the entity's current children (no partial/foreign
ids). Return `{success, count}`.

URLs (`apps/dashboard/urls.py`):
- `curriculum/course/<int:course_id>/units/reorder/`
- `curriculum/unit/<int:unit_id>/lessons/reorder/`

## Frontend (`course_detail.html`)
- Wrap the units loop in `<div id="units-sortable" data-course-id=...>`; add
  `class="unit-card" data-unit-id` + a drag handle (⠿) in each unit `.card` header.
- Wrap each unit's lesson rows in `<div class="lessons-sortable" data-unit-id=...>`; add
  `data-lesson-id` + a handle to each `.lesson-row` (in the `#` cell). The header row
  stays outside the sortable container.
- `{% block extra_js %}`: `{% load static %}`, load Sortable, init one Sortable on the
  units container (`handle:'.unit-handle', draggable:'.unit-card'`) and one per
  `.lessons-sortable` (`handle:'.lesson-handle', draggable:'.lesson-row'`); on `end`,
  POST the new id order, toast on success. Only rendered when `can_edit_content`.

## Out of scope
- Reordering LessonSteps (separate). Cross-unit lesson moves (drag a lesson into another
  unit) — v2. Reordering across grades (each course is single-grade).

## Verification
- Shell: post a shuffled order → assert order_index reassigned; foreign-id rejected.
- chrome-devtools: drag a unit + a lesson, confirm persistence after reload (screenshot).

## Status: COMPLETE (2026-06-07)
- Vendored `static/js/Sortable.min.js` (1.15.6).
- Views `course_reorder_units` + `unit_reorder_lessons` (+ `_apply_order` helper that
  rejects non-permutation id sets) + URLs.
- `course_detail.html`: `#units-sortable` + `.lessons-sortable` containers, ⠿ handles,
  `extra_js` SortableJS init (gated on `can_edit_content`), immediate AJAX persist +
  toast + client renumber.
- Verified: shell test (reorder reassigns order_index; foreign/partial ids → 400);
  template renders with no tag leaks; browser screenshot shows handles; live in-browser
  AJAX reorder persisted across reload (units 168,169,170 → 169,170,168) then restored.
  Note: chrome-devtools `drag` uses HTML5 DnD which SortableJS (pointer-based) ignores,
  so the drag itself was verified via the real onEnd fetch path, not the synthetic drag.
