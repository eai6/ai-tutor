# Curriculum Material Sharing Across Schools — Plan (2026-05-14)

## Problem

Four schools in the Seychelles pilot use different term-specific syllabi but share the same textbooks, worksheets, and exam papers. Those shared materials are uploaded once at the platform-wide level (`institution=None` → `GLOBAL_INSTITUTION_ID = 0`). Per-school courses created from each school's syllabus do NOT automatically benefit from the platform-wide materials — students querying their school's "Geography S3" KB get no textbook chunks unless the school re-uploaded them.

There IS a partial fallback (`apps/curriculum/knowledge_base.py:1228` `query_with_global_fallback`) but it only triggers when the school's own collection returns < 3 results (`FALLBACK_THRESHOLD`). If the school has even ONE indexed material — even an irrelevant one — the global merge never fires.

User instinct surfaced a real correctness risk: matching free-typed course titles ("Geography S3" vs "Geography S3 Term 1" vs "S3 Geography") to platform-wide courses is fragile. A robust match needs a normalised subject + grade pair.

## Current state (from audit)

- ChromaDB uses **one collection per institution** (`apps/curriculum/knowledge_base.py:124`): `curriculum_<institution_id>`. Strict isolation by collection name, NOT metadata filter.
- Platform-wide materials live in `curriculum_0` (the global collection).
- `Course.subject_type` exists but is only 5 buckets (`math|science|humanities|language|other`) — too coarse to match "Geography" to "Geography" (both = humanities).
- `Course.grade_level` is a free-text CharField, multi-grade allowed (e.g. `"S1,S2,S3,S4,S5"`).
- `Course.title` is descriptive free text — not safe to string-match.
- `query_with_global_fallback` (`knowledge_base.py:1282`) merges global results only when `len(merged) < FALLBACK_THRESHOLD` (=3).
- The global query at line 1304 uses ONLY the subject filter from the original query — drops grade_level + institution filters.
- No course-to-course linking. No M2M `parent_course` / `linked_courses`.
- Curriculum upload (`apps/dashboard/views.py:1030+`) does NOT auto-attach materials to the new course.

## Decisions confirmed (user, 2026-05-14)

- **Layer A only** for v1 — auto-merge global materials into every school course's KB queries when subject + grade match. Layer B (explicit per-course inheritance UI) deferred.
- **Subject + grade need to become enums (dropdowns)**, not free-typed strings. User's correctness concern: "if subject name was a dropdown then this will be more robust." Concur — string matching across "Geography" / "Geo" / "geography term 1" is unsalvageable.
- Existing rows must be backfilled cleanly, not silently mis-categorised.

## Target design

### A. Add `Course.subject_code` enum

`SubjectType` (3-5 buckets) stays — drives `is_math` rules. New `subject_code` is a finer dropdown for matching.

```python
class SubjectCode(models.TextChoices):
    MATHEMATICS = 'mathematics', 'Mathematics'
    GEOGRAPHY   = 'geography',   'Geography'
    PHYSICS     = 'physics',     'Physics'
    CHEMISTRY   = 'chemistry',   'Chemistry'
    BIOLOGY     = 'biology',     'Biology'
    ENGLISH     = 'english',     'English'
    FRENCH      = 'french',      'French'
    HISTORY     = 'history',     'History'
    COMPUTER_SCIENCE = 'computer_science', 'Computer Science'
    OTHER       = 'other',       'Other'

subject_code = models.CharField(
    max_length=32, choices=SubjectCode.choices, blank=True, default='',
    help_text="Canonical subject for material-sharing match (e.g. 'geography'). "
              "Drives the always-merge global-KB query path.",
)
```

Open: should we surface it as `Subject` model instead of enum? Recommend enum for v1 — adding subjects is rare; if pilot adds 3+ per year, migrate to model in v2.

### B. Make `grade_level` a multi-select of canonical grades

Existing field stays a CharField (preserves "S3", "S1,S2,S3,S4,S5" backward-compat). Add a normalised `grade_levels` JSONField (list of `SecondaryYear` enum values) that the course-creation form populates from a multi-select dropdown.

```python
class SecondaryYear(models.TextChoices):
    S1 = 's1', 'S1'
    S2 = 's2', 'S2'
    S3 = 's3', 'S3'
    S4 = 's4', 'S4'
    S5 = 's5', 'S5'
    S6 = 's6', 'S6'

grade_levels = models.JSONField(
    default=list, blank=True,
    help_text="Normalised grade list (multi-grade allowed). Matches platform-wide "
              "courses by intersection. Populated from the grade-level dropdown.",
)
```

Existing `grade_level` (CharField) stays for display + backward-compat; backfill parses it into `grade_levels` once.

(There's already a `grade_levels` JSONField on `Lesson` at `apps/curriculum/models.py:555` — naming consistency check; if it's the same intent, reuse the schema pattern.)

### C. Always-merge global materials in KB queries

Drop the `FALLBACK_THRESHOLD` gate. Rename `query_with_global_fallback` → `query_with_global_merge`. New behaviour: ALWAYS query both the school KB and the global KB in parallel, merge results, dedupe by chunk content hash.

Match condition for the global query:
- subject_code = institution_course.subject_code (exact match)
- grade_level: any overlap between course.grade_levels and global course's grade_levels

If the school course doesn't have `subject_code` set (legacy), fall back to current behaviour (subject string heuristic). Don't break unmigrated rows.

### D. Backfill

One-time management command `python manage.py backfill_course_subjects`:
- For every existing Course, attempt to map title → subject_code using a curated keyword map (e.g., "geography" in title → GEOGRAPHY)
- Parse grade_level → grade_levels list (split on `,`, normalise to lowercase canonical)
- Print every assignment for human review (`--dry-run` first, then `--apply`)
- Unmapped rows printed to stderr — admin manually fixes those via the dashboard

Don't run in a migration (data backfills in migrations are surprising and slow). Standalone command, run once, document in deployment notes.

### E. UI — course create/edit form

Two new fields on the course-edit form:
- `subject_code`: required dropdown
- `grade_levels`: multi-select checkboxes for S1–S6

`title` and `grade_level` (CharField) become **derived/secondary** — title is still free text for display; CharField grade_level is auto-populated from grade_levels join (`",".join(["S1", "S3"])`).

### F. UI — visibility of inherited materials

On the course detail page (`templates/dashboard/curriculum/course_detail.html`), add a small "Inherited materials" section showing:
- "X chunks from platform-wide *Geography S3* (read-only)"
- Link to the platform-wide course materials list

Helps the teacher SEE that they're not orphaned just because they didn't upload textbooks themselves.

## Data model changes

| Model | Change |
|---|---|
| `apps/curriculum/models.py::Course` | Add `subject_code` (CharField+choices), `grade_levels` (JSONField default=list); both nullable/empty for legacy rows |

Migration `0030_course_subject_code_grade_levels.py` — additive, no data backfill (separate command).

## Backend changes

| File | Change |
|---|---|
| `apps/curriculum/models.py:55` | Add `SubjectCode` enum + `subject_code` field |
| `apps/curriculum/models.py:37` | Add `SecondaryYear` enum + `grade_levels` JSONField |
| `apps/curriculum/migrations/0030_*` | Additive migration |
| `apps/curriculum/knowledge_base.py:1228` | Rename `query_with_global_fallback` → `query_with_global_merge`; drop FALLBACK_THRESHOLD gate; gate the merge on `subject_code + grade_levels` overlap when both available |
| `apps/curriculum/knowledge_base.py` (new helper) | `_global_courses_matching(subject_code, grade_levels) -> Iterable[int]` — return list of platform-wide course IDs whose subject_code matches and grade_levels overlap. Used by the merge to scope the global query to relevant chunks |
| `apps/curriculum/management/commands/backfill_course_subjects.py` | New command, `--dry-run` / `--apply` flags |
| `apps/dashboard/views.py` (course create/edit) | Add subject_code + grade_levels to form, validate, save |
| `apps/dashboard/views.py` (course detail) | Compute "inherited materials count" — query global KB collection for chunks matching this course's subject + grade |

## Frontend changes

- Course create / edit form: subject dropdown + grade multi-select
- Course detail page: "Inherited from All Schools" badge + count

## Out of scope (deferred)

- **Layer B** — explicit "Inherit materials from course X" dropdown in curriculum upload. Useful but not needed for the immediate use case (4 Seychelles schools all want the same All-Schools materials)
- Per-material exclude (e.g. "include all global materials EXCEPT this one")
- Cross-school borrowing (school A using school B's materials, neither platform-wide)
- Auto-detection of subject_code from uploaded curriculum document (defer; the dropdown is fast enough during course creation)
- A `Subject` model with FK (defer until v2 if subjects diversify)
- Removing the legacy `grade_level` CharField (keep both for now; collapse later when we're confident `grade_levels` is fully populated)

## Phased delivery

| Phase | Work | Days |
|---|---|---|
| **R2.1 — Schema + dropdown UI** | (1) Add SubjectCode + SecondaryYear enums; (2) Migration; (3) Course form fields; (4) Backfill command; (5) Run backfill on prod (manual, supervised) | 1 |
| **R2.2 — Always-merge global KB** | (1) Drop FALLBACK_THRESHOLD gate; (2) Add `_global_courses_matching` helper; (3) Update `query_with_global_merge` to use subject_code + grade_levels overlap; (4) Test with a real school course querying for KB chunks; verify global Geography textbook chunks appear | 0.5 |
| **R2.3 — Inherited materials UI** | (1) Course detail page: count + badge; (2) Document in CLAUDE.md the multi-tenant materials sharing model | 0.5 |

Total: ~2 solo-dev days.

## Risks

- **Fallback when subject_code is empty** must not break: keep the current heuristic path active for legacy rows. Pilot Seychelles courses can be backfilled in one sitting.
- **Grade-level overlap semantics**: courses with `grade_levels=['S1','S2','S3','S4','S5']` (multi-year platform-wide) overlap with any school course's `grade_levels=['S3']`. That's the intended behaviour. But: a platform-wide "S3" course should NOT match a school's "S5" course. Strict intersection check (any-of), no transitive sharing.
- **ChromaDB query cost**: doubling queries (school + global) per request. The existing `query_with_global_fallback` already has the global codepath; this just runs it always. Negligible latency cost — both collections are local SQLite.
- **Backfill quality**: free-text "S3 Geography" → GEOGRAPHY/S3 mapping might miss edge cases ("Geographie", typos). Dry-run + manual review before apply.

## Open questions

1. **Subject dropdown content**: current proposal covers 9 named subjects + OTHER. Seychelles syllabus subjects beyond this list? (Recommend: ship with 9, add as needed.) << we should also make the subject names configurable like the grade by the super admin, we can add it to the setting page like the others>>
2. **Should `subject_code` be required for new courses?** Recommend yes — drop the legacy "leave it blank" path for new rows. Existing rows tolerate empty during migration window.
3. **Cross-institution sharing within the pilot**: should two school courses with the same subject_code/grade_levels ALSO see each other's materials, or only platform-wide? Recommend: only platform-wide for v1 (school-to-school sharing is a privacy concern; teachers should opt in via Layer B later).
4. **What about teaching-material `subject_name`** (`apps/dashboard/models.py::TeachingMaterialUpload.subject_name` — free CharField) — does it need the same enum treatment? Recommend: defer; the matching that matters is on Course not Material. Materials inherit course's subject_code via FK.

## Next step

User confirms the 9-subject list (Q1) and the "school-to-school sharing deferred" call (Q3). Then start R2.1 (schema + form + backfill), supervised on the prod data.

Refs: memory/large_textbook_parsing_plan.md (related materials work)
