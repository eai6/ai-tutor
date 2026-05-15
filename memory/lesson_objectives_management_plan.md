# Lesson Objectives Management — Plan (2026-05-15)

## Problem

Two related gaps in how teaching objectives are managed.

**Problem 1 — No way to add objectives to an existing lesson.** The `+ Add Lesson` button on the curriculum-review screen lets teachers create whole new lessons, but there is no affordance to add a Terminal Objective (TO) or Enabling Objective (EO) to a *lesson that already exists* — neither during the review flow nor on the published lesson-detail page. If the parser misses an objective the teacher has to either re-upload the source document or accept that lesson is incomplete.

**Problem 2 — Extracted objectives have redundancies and mis-attributions.** The parser appends every TO/EO it finds in the source document into a flat list per unit/lesson, with **no deduplication** at extraction time. Two visible failure modes from the user's screenshot:

- **Within-lesson duplication**: lesson 5 has its title duplicated as the first TO ("Describe the different processes involved in each type of weathering" appears as both lesson title and TO #1).
- **Cross-lesson misplacement**: the same lesson 5 has "Describe processes of water flow in hydrological cycle" as a TO, but that's a totally different unit (water cycle, not weathering). Some EOs that the parser puts on one lesson would fit better as TOs of a different lesson.

User wants a dedup + cross-lesson reassignment pass before the teacher commits the parsed structure.

## Current state (from audit)

**Schema reality** (`apps/curriculum/models.py`):
- `Unit.terminal_objectives` (JSONField list, line 187) — TOs live at **unit** level
- `Unit.enabling_objectives` (JSONField list, line 192)
- `Lesson.objective` (TextField, line 241) — singular "what the student will learn"
- `Lesson.enabling_objectives` (JSONField list, line 258) — list of teaching steps per lesson
- `LessonStep.enabling_objective` (CharField, line 521) — per-step free-text reference

**Naming bug**: `templates/dashboard/curriculum/process.html:457` renders `lesson.enabling_objectives` under the label "Lesson Terminal Objectives". The user's screenshot is showing this. So when the user says "add a TO to a lesson," they mean `lesson.enabling_objectives` (despite what the label says). Worth renaming the label as part of this work.

**UI surfaces** (`apps/dashboard/views.py` + templates):
- **Review-time** (`process.html`, used during curriculum upload before approve): `+ Add Lesson` exists; `+ Add TO` exists at the **unit** level only (line 412), not at the lesson level
- **Post-publish** (`lesson_detail.html`): displays `unit_terminal_objectives` read-only (lines 331-344), no edit affordance
- **Backend writes**: `lesson_create` view (`urls.py:46`) creates a lesson + objective in one shot, no per-objective mutation; no view to edit `lesson.enabling_objectives` post-creation

**Extraction pipeline** (`apps/curriculum/curriculum_parser.py:1633, 1637, 1690-1692`):
- Appends every parsed objective to the relevant unit/lesson list
- **Zero dedup** at extraction time
- Dedup only happens at **consumption** time inside `combined_objectives_for_lesson()` (`content_generator.py:266-275`), case-insensitive whitespace-normalized

**Cross-lesson hierarchy**: doesn't exist in the schema. No "this EO is enabling-of that TO" relationship is tracked. All objectives are stored as sibling list items.

## Decisions to confirm with user (open questions section)

The plan commits to a default for each but the user can override.

## Target design

### Feature A — Add / edit / remove objectives per lesson

Two surfaces:

**A1 — Review-time** (`process.html`, before teacher clicks "Approve & Generate Content"):
- Each lesson card already shows its `enabling_objectives` list with editable textareas + ✕ delete (`process.html:457` area). **Add a `+ Add TO` button per lesson** that JS-clones a new empty input row, mirroring the existing `+ Add Lesson` pattern. No backend route needed — the form already serializes the full lesson list and the existing `curriculum_approve` view writes it back.

**A2 — Post-publish** (`lesson_detail.html`, after the lesson is created and published):
- Add an **inline editable** Enabling Objectives panel: each EO in `lesson.enabling_objectives` rendered with an inline edit field + ✕ delete + a `+ Add` button at the bottom.
- New view `lesson_edit_objectives` (POST `/dashboard/curriculum/lesson/<id>/edit-objectives/`):
  - Reads `enabling_objectives` from POST (one input per item)
  - Validates: trim, dedup case-insensitively, drop empties
  - Writes to `lesson.enabling_objectives`, saves
  - Redirects back to lesson_detail with a flash
- The TOs at the **unit** level (`unit.terminal_objectives`) — out of scope for v1 (those usually come from the syllabus and are stable). Add an "Edit unit objectives" link separately if needed later.

**Naming fix**: rename the label "Lesson Terminal Objectives" → "Lesson Enabling Objectives" in `process.html:457` and any other place. The TO/EO distinction is what the user actually wants exposed; the screenshot's mislabeling is what made them think TOs were lesson-level.

### Feature B — Post-extraction dedup + cross-lesson reassignment

Slot point: **after** the parser produces `parsed_data` but **before** it lands on `process.html` for teacher review. Pure normalization — the teacher still sees the result and can approve/edit.

**B1 — Within-lesson dedup**: trivial. Walk each lesson's `enabling_objectives`, normalize (strip + lowercase + collapse whitespace) for comparison, keep first occurrence of each canonical key, drop the rest.

**B2 — Lesson-title duplication**: if `lesson.objective` (or `lesson.title`) appears verbatim as the first EO, drop the EO. The screenshot shows "Describe the different processes involved in each type of weathering" as both lesson title AND first TO — this is the most common false positive.

**B3 — Cross-lesson dedup (hardest)**: an EO appearing on lesson X may also appear on lesson Y. Two policies:
- **Conservative**: only flag exact-text duplicates across lessons; the **dedup decision goes to the teacher** in the review UI (highlight the dupes, let them ✕ from the wrong lesson).
- **Aggressive**: pick a "best home" automatically using subject-keyword overlap with the lesson title; move the EO and drop from the other.

Recommend Conservative for v1. Aggressive guesses can move objectives to the wrong place silently. The user said "after extraction we should de-duplicate" — review-time is the right moment.

**B4 — Cross-lesson misplacement (e.g. hydrological cycle EO on a weathering lesson)**: this is **semantic**, not just a string match. Two ways:
- **Manual**: surface a "✕" button next to every EO (already exists per A1) and a "Move to..." dropdown to reassign to another lesson in the same unit.
- **LLM-assisted**: a one-shot LLM call after parsing that takes (unit's lesson titles, all extracted EOs) and proposes (lesson_idx, EO_text) assignments. The teacher sees the result and can adjust.

Recommend **Manual move** for v1 (cheap, no LLM cost, teacher has authority). LLM-assisted is a follow-up if the manual move turns out to be too tedious in pilot use.

**B5 — Where the dedup runs**: insert a `_dedupe_parsed_curriculum(parsed_data)` step in `apps/curriculum/pipeline.py` between parse and review-stash. It mutates `parsed_data` in place. Logs each dedup it did so the teacher sees a count ("Removed 4 duplicate objectives across 12 lessons") at the top of the review screen.

## Data model changes

**None for v1.** Both features mutate existing JSONField lists (`Unit.terminal_objectives`, `Lesson.enabling_objectives`) via existing or new view code. The "Move EO between lessons" feature also just shuffles JSONField entries — no schema change.

If we later want to track EO → TO hierarchy explicitly (cross-lesson dependencies), that's a separate `EnablingObjective` model + FK refactor. Out of scope.

## Backend changes

| File | Change |
|---|---|
| `apps/curriculum/pipeline.py` | New `_dedupe_parsed_curriculum(parsed_data) -> dict` step. Within-lesson + lesson-title-vs-EO + cross-lesson exact-match dedup. Returns mutated dict + count of changes; pipeline logs the count. |
| `apps/dashboard/views.py` | New view `lesson_edit_objectives(lesson_id)` — POST-only, validates and writes `lesson.enabling_objectives` |
| `apps/dashboard/views.py` | New view `lesson_move_objective(lesson_id)` — POST-only, accepts `eo_text` + `target_lesson_id`, removes from source lesson's list, appends to target's list |
| `apps/dashboard/urls.py` | Two new routes: `lesson/<int:lesson_id>/edit-objectives/`, `lesson/<int:lesson_id>/move-objective/` |

## Frontend changes

| File | Change |
|---|---|
| `templates/dashboard/curriculum/process.html` | (1) Per-lesson `+ Add TO` button with JS clone; (2) Highlight cross-lesson duplicates with subtle warning badge; (3) Show dedup count at top of review screen. Rename "Lesson Terminal Objectives" → "Lesson Enabling Objectives" |
| `templates/dashboard/curriculum/lesson_detail.html` | New "Lesson Enabling Objectives" editable panel with per-EO inline edit + ✕ + `+ Add EO` button + per-EO `Move to → [select lesson]` dropdown. Posts to the new views above. |

## Out of scope (deferred)

- **LLM-assisted reassignment** of misplaced EOs (B4 aggressive variant) — defer; manual move is sufficient and cheaper for v1
- **Hierarchy model** (EnablingObjective FK, cross-lesson dependency tracking) — defer; the JSONField shape works for the immediate UX
- **Editing unit-level Terminal Objectives** post-publish — defer; usually those are stable from the syllabus
- **Bulk-move multiple EOs at once** — defer; one at a time is fine for the sizes in the pilot
- **Undo / version history** for objective edits — defer; teachers rarely need to revert
- **Extraction-time normalization in the parser itself** — defer; running dedup as a pipeline step is cleaner because the parser stays a "raw extractor"
- **Renaming `Lesson.enabling_objectives` field** to `terminal_objectives` to match teacher intuition — too disruptive (field name appears in many places); fix the template label only

## Phased delivery

| Phase | Work | Hours |
|---|---|---|
| **L1 — Add-EO at review time** | (1) `+ Add EO` button per lesson on `process.html`; (2) JS clone helper. The existing `curriculum_approve` view already serializes the list — no backend change needed. | 1 |
| **L2 — Add-EO post-publish** | (1) `lesson_edit_objectives` view + URL; (2) editable panel on `lesson_detail.html`; (3) flash + redirect | 2 |
| **L3 — Within-lesson + lesson-title dedup** | (1) `_dedupe_parsed_curriculum()` helper in `pipeline.py`; (2) call before review-stash; (3) log the count + surface on the review screen | 2 |
| **L4 — Cross-lesson dedup highlight + manual move** | (1) Detect cross-lesson exact dupes in the dedup pass, mark them; (2) Subtle warning badge in review UI + on `lesson_detail`; (3) `lesson_move_objective` view + URL; (4) Move-to dropdown next to each EO | 3 |
| **L5 — Naming fix** | Rename "Lesson Terminal Objectives" → "Lesson Enabling Objectives" in templates + the audit-noted other spots | 0.5 |

Total: ~8.5 hours. Can ship L1 + L5 immediately (an hour and a half). L2-L4 are the meatier work.

## Risks

- **Dedup false positives**: case-insensitive whitespace-normalize is conservative but could merge "Identify rocks" and "Identify rock-types" if punctuation varies. Mitigation: only exact-text match after normalization; flag "near-duplicates" in the UI but don't auto-merge.
- **Move-to dropdown UX**: if a unit has 30 lessons, the dropdown gets unwieldy. Mitigation: scope to lessons in the same Unit (typically <10); add search if it grows.
- **Concurrent edit race**: teacher A adds an EO while teacher B is reviewing — the new EO might get lost on B's save. Mitigation: pilot is small enough that this is unlikely; add `updated_at` optimistic-lock check if it bites.
- **Parsing pipeline disruption**: inserting a new step in `pipeline.py` between parse and stash. Mitigation: the dedup helper is pure (no side effects); easy to disable if it causes regressions.
- **Naming refactor blast radius**: `enabling_objectives` is referenced in many places. We're only changing the template label, not the field name — safe.

## Open questions

1. **Conservative vs aggressive cross-lesson dedup?** Recommend **conservative** — flag exact-text duplicates in the UI, let teacher decide. Aggressive auto-move with subject-keyword overlap can hide bad moves.
2. **Cross-lesson dedup scope: same unit only, or whole course?** Recommend **same unit only** — duplicate EOs across units are usually intentional (similar concepts in different contexts). Within-unit dupes are the high-value catch.
3. **Does dropping "lesson title duplicated as first EO" need teacher confirmation, or just do it silently?** Recommend **silent**, log the count. The dedup screen will show the total at the top so the teacher sees something happened.
4. **Should L2 ("post-publish" edit panel) go on `lesson_detail.html` or be a separate "edit lesson" page?** Recommend `lesson_detail.html` — fewer clicks, matches the inline-edit pattern already used elsewhere.
5. **Move-to UI**: dropdown vs drag-and-drop? Recommend **dropdown** — drag-and-drop adds JS complexity for marginal UX gain at pilot scale.
6. **Should L5 (rename label) ship in L1 or be its own commit?** Recommend bundling with L1 — both are template-only changes, one commit.
7. **What happens if a step's `enabling_objective` text references an EO that the teacher then deletes from the lesson?** Recommend leave the step's CharField alone (it's frozen at generation time); flag it as orphan in the lesson detail UI when there's a mismatch. Out of scope for v1.

## Next step

Confirm the open-question recommendations (especially #1 conservative dedup, #2 same-unit scope) and I'll start L1 (the smallest leverage win — `+ Add EO` button at review time). L1+L5 ship together as one commit since they're both template-only.
