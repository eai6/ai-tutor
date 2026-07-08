# Fix: 500 on dashboard student detail — ExitTicketAttempt.answers format mismatch

**Status: fixed + deployed (2026-07-08)**

## Context

Production (seselai.sc) throws `Server Error (500)` on `/dashboard/students/337/` (and 240 — will grow to affect any recently active student). Azure logs show:

```
File "/app/apps/dashboard/views.py", line 718, in student_detail
    rows = per_concept_breakdown(attempt)
File "/app/apps/tutoring/competency.py", line 79, in per_concept_breakdown
    tag = (a.get("concept_tag") or "").strip() or "(untagged)"
AttributeError: 'str' object has no attribute 'get'
```

**Root cause:** `per_concept_breakdown()` iterates `ExitTicketAttempt.answers` assuming the legacy **list** format `[{concept_tag, correct, ...}]` written by the retired conversational_tutor engine. Since commit `2d38758` (2026-05-26), the simple_tutor exit-ticket writer (`apps/tutoring/simple_tutor/exit_ticket.py:220`) stores a **dict**: `{'per_question': [...], 'eo_competency': {...}}`. The diagnostic pretest (`apps/tutoring/views.py:2628`) also writes a dict with a `per_question` list. Iterating a dict yields its string keys → `.get()` on a str → crash. Every student whose *latest* attempt on any lesson is post-May-26 crashes their whole detail page.

**Same root cause, silent variant:** three dashboard read paths guard with `isinstance(answers, list)` and silently show zeros/blanks for new-format attempts:
- Lesson competency heatmap: `apps/dashboard/views.py:2337`
- Per-student objective rows: `apps/dashboard/views.py:2376-2380`
- Session exit review page: `apps/dashboard/views.py:8004`

**Scope (user-confirmed):** fix the crash + the read-only consumers. The teacher override write-path is a follow-up (below).

Both formats' per-question rows share the needed keys (`concept_tag`, `correct`, `selected`, `question_type`), so a single normalizer fixes all consumers.

## Changes

1. **New shared normalizer** `answer_rows(attempt)` in `apps/tutoring/competency.py`: list → pass through; dict → `answers.get('per_question')`; anything else → `[]`; drops non-dict entries. (Summative attempts store rows under `answers['result']['per_question']` with `is_correct` keys, but summatives have `lesson=None` so no lesson-scoped consumer reads them — deliberately not handled.)
2. **Crashing paths** (`apps/tutoring/competency.py`): `per_concept_breakdown()` uses `answer_rows(attempt)`; `competency_snapshot()` total uses `len(answer_rows(attempt))` (dict format previously counted its 2 keys).
3. **Silent-zero dashboard paths** (`apps/dashboard/views.py`): heatmap loop (~2337), per-student rows (~2376), `session_exit_review` stored answers (~8004) all switch to `answer_rows(...)`.
4. **Tests**: `apps/tutoring/tests/test_competency_answer_rows.py` — both formats, garbage inputs, per_concept_breakdown over dict format (the exact prod crash), snapshot total.

## Verification

1. pytest new file + `apps/tutoring/ apps/dashboard/` suites.
2. Django shell repro: dict-format attempt → `per_concept_breakdown` crashes before fix, returns rows after.
3. chrome-devtools: `/dashboard/students/<id>/` renders with weak-concepts data (screenshot).
4. Push to main → Azure deploy → verify `seselai.sc/dashboard/students/337/` and `/240/`.

## Follow-up (out of scope)

- `session_exit_review_override` (`apps/dashboard/views.py:8172`): for dict-format attempts it 400s ("Index out of range") and, if naively patched, would flatten `answers` back to a list destroying `eo_competency`. Needs a format-aware write path.

Commit: ec24e9d
