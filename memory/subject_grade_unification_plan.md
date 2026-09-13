# Subject and grade — one stored field each (2026-09-13)

**Status: SCOPE FOR REVIEW. Nothing changed.**

Written after a teacher set subject and grade on the upload form and the
platform-wide materials still did not reach the course. That bug is fixed
(`63c3483`), but it was a symptom: there are **five** fields describing a
course's subject and grade, they are written by different code at different
times, and nothing keeps them in step.

Numbers are measured on the local DB (8 courses, 37 units).

---

# Part 1 — What exists

## The five fields on `Course`

| field | kind | who reads it | what it decides |
|---|---|---|---|
| `grade_level` | CharField, free text, comma-separated (`'S1,S2,S3,S4,S5'`) | **118 refs** | everything human-facing: prompts, image prompts, KB queries, display |
| `grade_levels` | JSONField list (`['S3']`) | 25 refs | material sharing, global-KB merge |
| `subject_code` | choices — `mathematics`, `geography`, `physics`… | 30 refs | material sharing (the fine-grained key) |
| `subject_type` | choices — `math`, `science`, `humanities`, `language` | 29 refs | `is_math`, benchmark sampling, CLI subject filter |
| `is_math` | **property** | 47 refs | the math tutoring rules in CLAUDE.md |

Plus, off `Course`: `Unit.grade_level` (11 of 37 units set), `CurriculumUpload`
{`subject_name`, `grade_level`, `subject_code`}, `TeachingMaterialUpload`
{`subject_name`, `grade_level`}, `CurriculumChunk` {`subject`, `grade_level`},
`StudentProfile.grade_level`, `SchoolClass.grade_level`.

## What the data actually looks like

```
course                      grade_level   grade_levels   subject_code   subject_type   is_math
Belonie Geography S3        S3            ['S3']         geography      (empty)        False
Geography S1-S5             S1..S5        ['S1'..'S5']   geography      humanities     False
Geography S3                S3            ['S3']         geography      (empty)        False
Layer S Demo — Math S3      S3            ['S3']         mathematics    (empty)        True
Mathematics S3              S3            []             (empty)        math           True
Mount Fleuri Geography S3   S3            ['S3']         geography      (empty)        False
Perseverence Geography S3   S3            ['S3']         geography      (empty)        False
Pointe Larue Geography S3   S3            ['S3']         geography      (empty)        False
```

Three things fall out of that table.

**The two grade fields agree everywhere except where it broke.** 7 of 8 match
exactly; the eighth is Mathematics S3, the course that reported the bug. So the
list is not carrying information the text lacks — it is a copy that can drift,
and drifting is the only thing it has ever done.

**No course has both subject fields.** Every row has one or the other. That is
not a coincidence — two different code paths write them, and neither writes the
other's.

**`is_math` ignores `subject_code` entirely.** Read the property:

```python
if self.subject_type:
    return self.subject_type == self.SubjectType.MATH
return any(kw in (self.title or '').lower() for kw in self.MATH_KEYWORDS)
```

`Layer S Demo — Math S3` is explicitly `subject_code='mathematics'` and is only
`is_math=True` because its **title contains "Math"**. Rename it "Angles around
a point" and the math tutoring rules switch off silently. CLAUDE.md names this
fallback as an anti-pattern to remove; it is currently load-bearing for every
maths course in the database.

## The cost, already paid twice

`apps/tutoring/cli/session.py:66` carries the workaround in its docstring:

> Matches subject_code OR subject_type because courses in this database are
> classified through different fields … Checking only one field silently
> returns nothing for half the catalogue.

And `backfill_course_subjects` exists solely to repair the divergence after the
fact — a command whose necessity is the design defect.

---

# Part 2 — The target

**Two stored fields. Everything else derived.**

| keep | derive |
|---|---|
| `Course.grade_level` (text, human, already what 118 sites read) | `grade_levels` → parse the text |
| `Course.subject_code` (fine-grained, the sharing key) | `subject_type` → map from code · `is_math` → `subject_code == mathematics` |

Nothing can drift, because there is nothing to keep in step. The bug fixed in
`63c3483` stops being a bug that can exist.

## Why this direction and not the other

Making the *list* canonical and deriving the text would touch 118 call sites and
every prompt that interpolates a grade. Making the *text* canonical touches the
25 that read the list, and none of them filter it in the database — both the KB
match (`knowledge_base.py:1488`) and the badge
(`views._inherited_materials_summary`) pull the rows and intersect in Python.
So `grade_levels` can become a `@property` with no query rewrites at all.

Same argument for subject: `subject_code` is the finer key and the one the
upload form already collects. `subject_type` is a four-way bucket that
`subject_code` determines completely.

## The one real obstacle

`subject_type` **is** filtered in the database, in two places:

- `apps/benchmark/sampling.py:282-297`
- `apps/tutoring/cli/session.py:84-89`

A property cannot be filtered, so those two must move to `subject_code` first.
Both already match on `subject_code` OR `subject_type`; they become
`subject_code`-only, which is a simplification rather than a port.

---

# Part 3 — Steps

Each step ships and is verifiable on its own.

### 1. `is_math` reads `subject_code` — smallest, highest value

```python
@property
def is_math(self):
    if self.subject_code:
        return self.subject_code == self.SubjectCode.MATHEMATICS
    if self.subject_type:
        return self.subject_type == self.SubjectType.MATH
    return any(kw in (self.title or '').lower() for kw in self.MATH_KEYWORDS)
```

Three lines, no migration, and it stops a maths course being decided by its
title. The keyword fallback stays until step 4 proves it unreachable.

**Verify:** `Layer S Demo` keeps `is_math=True` with the title changed.

### 2. The two DB filters move to `subject_code`

`sampling.py` and `cli/session.py`. Precondition for step 3.

**Verify:** each filter returns the same lesson set before and after, on a DB
where the backfill has run.

### 3. `grade_levels` becomes a property

Delete the JSONField; parse `grade_level`. A migration drops the column after a
release that has stopped writing it.

```python
@property
def grade_levels(self):
    return [g.strip() for g in (self.grade_level or '').split(',') if g.strip()]
```

**Verify:** `_inherited_materials_summary` and the KB merge return identical
matches for all 8 courses. Every writer of `grade_levels` is gone first — the
pipeline's `course_defaults`, the backfill command, `course_edit`.

### 4. `subject_type` becomes a property

Mapping from `subject_code`; column dropped a release later.

**Verify:** the benchmark sampler and CLI filter still select the same rows.

### 5. The upload form is the only place either is set

`CurriculumUpload.subject_code` + `grade_level` → `Course`. The
`Edit Course` form keeps them editable. `backfill_course_subjects` is deleted —
there is nothing left to backfill.

---

# Part 4 — Deliberately out of scope

- **`StudentProfile.grade_level`.** A person's year, not a course's target
  years. Different axis; unifying them would be wrong.
- **`Unit.grade_level`.** A real distinction — a multi-grade syllabus has S1
  units and S5 units, and today's figure/monitor scoping uses it. Keep.
- **`CurriculumChunk.{subject, grade_level}`.** Denormalised search metadata on
  an index, written once at ingest. Not a source of truth and not read for
  matching.
- **`TeachingMaterialUpload.{subject_name, grade_level}`.** Free text used for
  linking a material to a course at upload time. Worth revisiting once the
  Course fields are settled, not before.
- **`PlatformConfig.grades`.** The country's grade vocabulary (Seychelles S1–S5,
  Mozambique 8ª–12ª Classe). It defines the values, and stays the definition.

---

# Part 5 — Risks

1. **`grade_level` is free text with no validation.** `'Grade 3'`, `'High
   School'` and `'Adult'` are its documented examples. Parsing it into a list is
   only safe because every real row holds canonical codes — verified across all
   8 local courses. The parser must return `[]` rather than guess on anything
   else, which is exactly today's no-match behaviour.
2. **Dropping a column is irreversible.** Both drops go in a release AFTER the
   one that stops writing them, so a rollback has the data.
3. **Mozambique.** Grade codes are `8ª Classe`, not `S3`. The parser splits on
   commas and strips — no S-prefix assumption anywhere. Worth an explicit test.
4. **A property cannot be `select_related`/`only()`-ed.** Two call sites use
   `.only('id', 'title', 'grade_levels')`; those become `.only('id', 'title',
   'grade_level')`.

---

# Part 6 — What this does not fix

The teacher still sets a subject and a grade in two places — the upload form
and Edit Course — and a course created by other means has neither. The
warning shipped in `63c3483` makes that visible. Making it impossible would
mean requiring both at course creation, which is a product decision, not a
refactor.

Refs: memory/curriculum_material_sharing_plan.md
