# Curriculum Re-upload (edit) + Per-Grade Page-Range Upload — Plan (2026-06-06)

## Problem

Two related gaps in the curriculum upload pipeline:

1. **Edit-by-re-upload.** Today a teacher/admin can only "re-parse" a course from
   the *original stored PDF* (`course_edit` `action='reparse'`). There is no way to
   correct/extend an existing course's units & lessons by uploading a **new** PDF.
   We want: pick an existing course → upload a fresh curriculum PDF → re-parse →
   upsert units/lessons in place (preserving student data).

2. **Per-grade page scoping.** Curriculum PDFs are often multi-grade (e.g. a single
   national syllabus covering 10ª–12ª Classe). Today the whole PDF is read as one
   blob, the LLM detects *all* grades, and `complete_curriculum_upload` fans out one
   Course per detected grade. We want the workflow to be **one grade per upload**,
   with optional **first-page / last-page** inputs that slice the PDF to just that
   grade's pages. When the page range is omitted, behaviour is unchanged (detect
   grade(s) from context, fan out as today).

## Current state (from audit)

- **Upload form/view**: `apps/dashboard/views.py:1113` `curriculum_upload()`. Template
  `templates/dashboard/curriculum/upload.html`. POST saves the PDF to
  `MEDIA_ROOT/curriculum_uploads/`, creates a `CurriculumUpload` (status `pending`),
  redirects to `curriculum_process`. Grade is a multi-select →
  comma-joined `grade_level` string.
- **Model**: `CurriculumUpload` (`apps/dashboard/models.py:10`) — `file_path`,
  `grade_level` (CSV string), `locale`, `subject_code`, `status`, `parsed_data`,
  `lesson_duration_minutes`, `created_course`. **No page-range fields.**
- **Parse orchestrator**: `process_curriculum_upload(upload_id)`
  (`apps/curriculum/curriculum_parser.py:1191`) → `parse_curriculum(file_path, *,
  subject_hint, grade_hint, locale, institution_id, progress_cb)`
  (`curriculum_parser.py:1016`).
- **Text extraction**: `extract_text_from_file(file_path, progress_cb)`
  (`apps/curriculum/curriculum_parser_archive.py:100`) → `extract_from_pdf(file_path,
  progress_cb)` (`:188`) using **PyMuPDF (`fitz`)**. The core loop is
  `for page in doc: text += page.get_text()` (`:207`) — **reads the entire PDF**.
  Vision-OCR fallback `_extract_pdf_with_vision(doc, …)` (`:533+`) also iterates the
  whole doc. **This is the single plug-in point for page-range slicing.**
- **Grade detection**: `detect_subject_and_locale(text, subject_hint, grade_hint,
  locale_hint)` (`curriculum_parser.py:338`) → returns `grade_levels: list[str]`
  (exact-as-source). `grade_hint` is a *soft prior* the LLM can override.
- **Per-grade fanout + upsert**: `complete_curriculum_upload(upload_id)`
  (`apps/curriculum/pipeline.py:1365`). Groups parsed units by `grade_level`
  (`:1394`), creates **one Course per grade** via `Course.objects.update_or_create`
  (`:1439`), then `Unit.objects.update_or_create` keyed on (course, unit_title)
  (`:1462`) and `Lesson.objects.update_or_create` keyed on (unit, lesson_title)
  (`:1482`).
- **Existing re-parse (reuses stored file)**: `course_edit` `action='reparse'`
  (`apps/dashboard/views.py:6932`). Finds the originating `CurriculumUpload`, sets
  `status='processing'`, spawns `process_curriculum_upload`. **Deliberately does NOT
  delete units/lessons** — the upsert keeps Lesson PKs so student progress, sessions,
  mastery, exit-ticket history all survive; lessons absent from the new parse are
  left as orphans (`views.py:6958-6973`). **This is the machinery the re-upload-edit
  feature rides on — no new data-preservation logic needed.**

## Target design

### Part A — Page-range scoping (the new mechanism)

Thread an optional `(first_page, last_page)` 1-based inclusive page range from the
upload form all the way to `extract_from_pdf`, where we slice the PDF before
extraction. Everything downstream (detection, outline, fanout) is unchanged — it
simply operates on less text.

Flow: form inputs → `CurriculumUpload.first_page/last_page` → `process_curriculum_upload`
reads them off the upload → `parse_curriculum(..., first_page=, last_page=)` →
`extract_text_from_file(..., first_page=, last_page=)` → `extract_from_pdf(...,
first_page=, last_page=)` slices.

**Slicing rule** (PyMuPDF is 0-based; teacher enters 1-based human page numbers):

```python
# in extract_from_pdf, after doc = fitz.open(file_path)
n = doc.page_count
if first_page or last_page:
    lo = max(1, first_page or 1)
    hi = min(n, last_page or n)
    page_iter = (doc[i] for i in range(lo - 1, hi))   # inclusive, 1-based -> 0-based
else:
    page_iter = doc                                    # unchanged whole-doc path
```

Apply the same `lo/hi` bounds to the `_extract_pdf_with_vision` fallback (pass the
range through, or pass a pre-sliced page-index list) so scanned PDFs scope too.

**Grade authority when a range is given.** DECIDED: grade detection still runs
normally even with a page range — the LLM detects grade(s) from the *sliced* text
and may override the teacher's selected grade, exactly as in the whole-document
path. The page range only narrows *which pages* are read; it does not change the
detection contract. No `force_grade` flag needed; `pipeline.py` grouping is
unchanged. (Slicing to one grade's pages naturally yields a single detected grade
in practice.)

**"One grade at a time" UX.** Keep the existing multi-grade fanout intact (don't
break bulk uploads), but the page-range workflow is single-grade by construction:
slice to one grade's pages + one selected grade → one Course. The form copy and a
soft single-select hint nudge toward one grade per upload when a range is used.

### Part C — Additive merge on re-parse (DECIDED 2026-06-06: applies to ALL re-parses)

**Problem with today's behaviour.** `complete_curriculum_upload` (`pipeline.py:1462,1482`)
uses `update_or_create` keyed on exact title:
- existing units/lessons matched by title get `objective`/`description`/`order_index`/
  `metadata` **overwritten** with the new parse (row PK + generated steps survive, but
  the teaching objective and ordering are silently rewritten);
- a **reworded** title for the same lesson does NOT match → a **duplicate** lesson is
  created.

**Target: strictly additive merge.** A re-parse must never overwrite or delete an
existing unit/lesson or its generated content (LessonStep, ExitTicket). It only
**appends** genuinely new units, and new lessons inside existing units. Applies to
BOTH re-parse entry points (existing stored-file reparse AND new re-upload).

Two parts:

1. **Write path → create-only (`pipeline.py`).** In additive mode:
   - `Course`: `get_or_create` (don't overwrite course meta on an existing course).
   - `Unit`: `get_or_create` keyed on (course, title); if it exists, leave all fields
     untouched and only append new lessons.
   - `Lesson`: `get_or_create` keyed on (unit, title); only create new lessons. New
     lessons get `order_index = (max existing order in that unit) + 1 + offset` so
     they append after current content. New units likewise append after the max
     existing unit order.
   - Existing rows: **no `defaults` write at all** → objective, metadata, ordering,
     steps, exit tickets, and all student data are preserved verbatim.
   - Counters report only `*_created` (already the case).

2. **Parser gets existing structure as context (LLM context-dedupe, DECIDED).** When
   re-parsing onto a course that already has content, pass the existing unit titles +
   objectives and per-unit lesson titles into the outline + lessons passes, instructing
   the LLM to return **only units/lessons not already covered** (semantic dedupe by the
   model). Exact-title `get_or_create` is the backstop. This touches parser prompts
   (`outline_pass`, `_lessons_fanout` in `curriculum_parser.py`) — **consult the
   prompting skills before editing those prompts** (CLAUDE.md non-negotiable):
   `prompting-fundamentals-expert` then the provider-specific skill for the parser's
   model.

**Mode signalling.** Add an `additive: bool` parameter to `complete_curriculum_upload`
(and thread an `existing_structure` context into `parse_curriculum`). First-time
uploads (no existing course/units) run with `additive=False` (current behaviour;
get_or_create == create anyway). Re-parse and re-upload entry points pass
`additive=True` and supply the existing-structure context.

### Part B — Edit an existing course by re-uploading a PDF

Extend the existing reparse path to optionally accept a **new** file (and an
optional page range). This is small because the additive-merge write path (Part C)
guarantees no data loss.

- On the course detail page, add an "Update from new PDF" form (next to the existing
  reparse button) with: file input, optional first/last page inputs.
- In `course_edit` (or a dedicated `course_reupload` view — see Open Questions), when
  a new `curriculum_file` is posted with `action='reparse'`:
  1. Save the new file to `MEDIA_ROOT/curriculum_uploads/`.
  2. Locate the course's `CurriculumUpload` (existing logic: `curriculum_upload_id`
     or `created_course=course`); update `file_path` to the new file, set
     `first_page/last_page`, `status='processing'`.
  3. Spawn `process_curriculum_upload` → re-parses new PDF → `complete_curriculum_upload`
     upserts onto the **same** course (keyed by unit/lesson title), preserving
     student data exactly as the current reparse does.
- Orphan handling is identical to today (lessons absent from the new parse are left
  in place, not deleted). No change.

## Data model changes

`apps/dashboard/models.py` — add to `CurriculumUpload`:

```python
first_page = models.PositiveIntegerField(
    null=True, blank=True,
    help_text="1-based first page of the target grade within a multi-grade PDF. "
              "Null = parse whole document (detect grade from context).")
last_page = models.PositiveIntegerField(
    null=True, blank=True,
    help_text="1-based last page (inclusive) of the target grade. Null = end of document.")
```

Migration: one additive migration in `apps/dashboard/migrations/` (nullable, no
backfill needed — null preserves current whole-document behaviour). Name e.g.
`00XX_curriculumupload_page_range.py`.

## Backend changes

1. **`apps/curriculum/curriculum_parser_archive.py`**
   - `extract_from_pdf(file_path, progress_cb=None, *, first_page=None, last_page=None)`
     — slice as above; thread range into `_extract_pdf_with_vision`.
   - `extract_text_from_file(file_path, progress_cb=None, *, first_page=None,
     last_page=None)` — forward range to `extract_from_pdf` (ignored for non-PDF;
     log a warning if a range is given for a non-PDF file, per CLAUDE.md "no
     silent-skip").

2. **`apps/curriculum/curriculum_parser.py`**
   - `parse_curriculum(..., first_page=None, last_page=None)` — forward to
     `extract_text_from_file` at `:1062`.
   - `process_curriculum_upload` (`:1251`) — pass `first_page=upload.first_page,
     last_page=upload.last_page`. Detection/grouping unchanged (grade still LLM-detected).
   - Add a `processing_log` line noting the page range when present (mirrors the
     existing `_bump`/`add_log` style).

3. **`apps/curriculum/pipeline.py`** — additive-merge write path (Part C).
   `complete_curriculum_upload(upload_id, feedback='', *, additive=False)`. Grade
   grouping unchanged (LLM-detected `grade_levels`). When `additive`:
   - `Course`/`Unit`/`Lesson` → `get_or_create` (no `defaults` write on existing rows).
   - New units/lessons appended after the max existing `order_index`.
   - Existing rows + their LessonStep/ExitTicket/student data left untouched.

3b. **`apps/curriculum/curriculum_parser.py`** — context-dedupe (Part C).
   `parse_curriculum(..., existing_structure=None)` threads existing unit/lesson
   titles+objectives into `outline_pass` and `_lessons_fanout` so the LLM returns only
   new items. **Prompt edits here require the prompting skills first** (CLAUDE.md).
   `process_curriculum_upload` builds `existing_structure` from the upload's
   `created_course` when re-parsing and passes `additive=True` to completion.

4. **`apps/dashboard/views.py`**
   - `curriculum_upload` (`:1132` POST) — read `first_page`/`last_page` from POST,
     validate (positive ints, `last >= first`), store on the new `CurriculumUpload`.
   - NEW `course_reupload(request, course_id)` view + URL (Part B): accepts a new
     `curriculum_file` + optional page range, overwrites the course's
     `CurriculumUpload.file_path`, sets the range, `status='processing'`, spawns
     `process_curriculum_upload`. Leaves `course_edit`'s stored-file reparse path
     untouched (DECIDED).

5. **Validation helper** — small shared validator: both ints positive, `last >=
   first`; surface a `messages.error` and redirect on bad input. Bounds against
   actual `page_count` are enforced defensively in `extract_from_pdf` (clamp), but
   reject obviously bad form input early.

## Frontend changes

- **`templates/dashboard/curriculum/upload.html`** — add an optional "Page range
  (for multi-grade PDFs)" fieldset: two number inputs (First page / Last page) with
  help text "Leave blank to auto-detect grade(s) from the whole document." Nudge
  single-grade selection when a range is entered (JS soft hint, not a hard block).
- **Course detail page** (`templates/dashboard/` course detail) — add "Update from
  new PDF" form: file input + optional first/last page + submit. Place beside the
  existing reparse control.
- Reuse existing `curriculum_process` progress UI unchanged (it already streams
  `processing_log`).

## Out of scope

- Auto-detecting grade→page-range boundaries for the teacher (we ask them to enter
  the range; no TOC/heading auto-split).
- Deleting orphan lessons that disappear from a re-parse (current leave-in-place
  behaviour is retained; pruning UI is separate work).
- Changing the multi-grade fanout for whole-document uploads.
- Per-unit or per-lesson re-upload (course-level only this iteration).
- OCR / vision-extraction quality improvements.
- Reconciling against `memory/soft_delete_architecture_plan.md` (orphan handling
  stays as-is).

## Phased delivery

| Phase | Work | Est. (solo days) |
|-------|------|------------------|
| 1 | Data model fields + migration; thread `first_page/last_page` through `extract_from_pdf` → `extract_text_from_file` → `parse_curriculum` → `process_curriculum_upload`; slicing + vision-fallback range | 1.5 |
| 2 | Upload-form page-range inputs + validation; local end-to-end test on a real multi-grade PDF (Mozambique syllabus) | 1.0 |
| 3 | Part C additive-merge write path in `pipeline.py` (get_or_create, append ordering); make all re-parses additive; verify existing units/lessons + generated content untouched | 1.0 |
| 4 | Part C context-dedupe: thread `existing_structure` into parser, tune outline/lessons prompts (consult prompting skills first) | 1.0 |
| 5 | Part B: re-upload-to-edit existing course (`course_reupload` view + course-detail form), riding additive merge | 0.75 |
| 6 | Visual checks (chrome-devtools screenshots of both forms + progress + result), edge cases (out-of-bounds range, range on non-PDF, single vs multi grade, re-parse adds-only) | 0.5 |

**Total ≈ 5.75 focused days.**

## Decisions (2026-06-06, confirmed by user)

1. **Grade authority with a page range** → grade is **still LLM-detected** from the
   sliced text (may override the teacher's selection). Page range only narrows pages
   read. No `force_grade`; `pipeline.py` unchanged.
2. **Re-upload wiring** → **dedicated `course_reupload` view + URL**. Leaves the
   existing `course_edit` reparse-from-stored-file path untouched.
3. **Re-upload row reuse** → reuse the course's existing `CurriculumUpload` row
   (overwrite `file_path`, set page range), preserving the `created_course` linkage
   so the upsert lands on the same Course.
4. **Grade select with a range** → **soft nudge** (JS hint; backend uses the first
   selected grade if a range is present). Multi-grade uploads still work.
5. **Re-parse semantics** → **strictly additive for ALL re-parses** (existing
   stored-file reparse + new re-upload). Existing units/lessons and their generated
   content are never overwritten or deleted; only new units/lessons are appended.
6. **Dedupe on re-parse** → **LLM context-dedupe**: existing structure fed to the
   parser so it returns only new items; exact-title `get_or_create` backstop.

## Progress

- **Phase 1 DONE** — `CurriculumUpload.first_page/last_page` + migration `0022`;
  page-range slicing in `extract_from_pdf` (+ vision fallback `end_page`); threaded
  through `extract_text_from_file` → `parse_curriculum` → `process_curriculum_upload`.
  Verified with a generated multi-page PDF.
- **Phase 2 DONE** — upload-form page-range inputs + `_parse_page_range` validation +
  soft single-grade nudge JS. Verified via chrome-devtools screenshots.
- **Phase 3 DONE** — additive merge in BOTH live writers:
  `create_curriculum_from_structure` (archive — used by new-upload approve + the new
  re-upload path) and `pipeline.complete_curriculum_upload` (used by the existing
  `course_edit` reparse button). Both now `get_or_create` and append new
  units/lessons after the max existing `order_index` (explicit None check to dodge
  the order_index==0 falsy trap). Existing rows + LessonStep/edits preserved.
  Verified with rollback-based shell tests on both writers.
  NOTE: discovered there are TWO live `complete_curriculum_upload` defs (parser-v2
  vs pipeline) on two paths — both made additive rather than rerouted.

- **Phase 4 DONE** — context-dedupe. Consulted prompting-fundamentals +
  claude-prompting skills first (parser = Claude Sonnet 4). Threaded
  `existing_structure` ([{title, grade_level, lessons[]}]) through
  `parse_curriculum` → `outline_pass` (`existing_units`) + `_lessons_fanout` →
  `lessons_pass` (`existing_lessons`). On a re-parse the prompts gain an
  `<existing_units>`/`<existing_lessons>` data block in `<context>` and a dedupe
  guideline appended inside the trailing `<task>` (query-last preserved); both
  render to "" on first upload so the original prompt is byte-identical → no
  first-upload regression. `_build_existing_structure(upload)` snapshots the
  course's current units/lessons (None when no course yet). Verified: prompt blocks
  present/absent correctly; structure snapshot correct.

- **Phase 5 DONE** — `course_reupload` view + URL (`/curriculum/course/<id>/reupload/`)
  + "Update from new PDF" form on course_detail. Reuses the course's existing
  CurriculumUpload row (or creates+links one), sets file + page range, spawns the v2
  `process_curriculum_upload(skip_review=True)` → additive complete. Added a
  `target_course` param to `create_curriculum_from_structure` + v2 complete so a
  re-upload lands deterministically on the chosen course (no duplicate-course risk if
  detection shifts the computed title). Verified: form renders (screenshot); no
  duplicate course created with target_course; view creates/links upload + carries
  page range/grade.

- **Phase 6 DONE** — live end-to-end with real PDF (`mozambique/Grade8_Biology_1Ciclo.pdf`,
  60pp) + real Claude Sonnet 4. Initial parse (pages 20-30) → 2 per-grade courses;
  reupload (pages 20-40, overlapping) → target course reused (no dup), overlapping
  units de-duped (zero duplicate rows), tagged lesson's PK + edited objective +
  generated step all preserved, new grade spawned its own course. Found & fixed a
  real gap during this run: the v2/archive writer never set `course.curriculum_upload`
  (why courses showed "no source PDF linked" + why `_build_existing_structure`
  missed them) — now linked on creation.

## Status: COMPLETE

All 6 phases shipped + verified. Feature done:
- Per-grade page-range upload (optional first/last page; falls back to whole-doc
  grade detection when blank).
- Edit an existing course by re-uploading a PDF (`course_reupload`), additive merge.
- All re-parses additive (never overwrite/delete existing units/lessons/content).
- Parser context-dedupe to suppress reworded-title duplicates.

Remaining (not blocking; optional follow-ups): orphan-lesson pruning UI; a dedicated
pytest covering additive merge (shell-tested only); consider backfilling
`course.curriculum_upload` for pre-existing v2 courses.
