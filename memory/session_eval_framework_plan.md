# Session-level pedagogical evaluation (SessionEval) — Plan (2026-08-11)

## Context

We need to evaluate whole tutoring sessions against the eight pedagogical
dimensions of Maurya et al., *Unifying AI Tutor Evaluation* (NAACL 2025,
arXiv:2412.09416), using real production sessions from the Seychelles pilot.
This becomes Objective 2 of the study.

Today we have two harnesses and neither does this:

- **`apps/benchmark/`** (dashboard → Developer → "Tutoring Evaluation") annotates
  **one tutor turn** at a time against a 30-label internal rubric. Real
  production data, human + LLM annotators, a working review UI — but the wrong
  unit of analysis.
- **`evals/`** judges **whole sessions** on an 8-dimension rubric — but only
  sessions it simulated itself from synthetic personas. `evals/fixtures/extract.py`
  extracts *curriculum*, not sessions; nothing anywhere loads a real
  `TutorSession` and judges it end to end.

`llm_rubric.score_trajectory()` already accepts `list[{'role','content'}]`, so
the missing piece is an adapter plus a place to record human judgements.

### Three findings that shape the design

**1. The paper's scale is 3-way, ours is binary.** `PEDAGOGICAL_DIMENSIONS`
(`evals/scorers/llm_rubric.py:121-187`) asks for yes/no. The paper uses
**Yes / To some extent / No**, with *Revealing* as Yes-correct / Yes-incorrect /
No and *Tone* as Encouraging / Neutral / Offensive. Our code also treats
`neutral` tone as passing, where the paper's desideratum is **Encouraging**
only. Decision: build 3-way, paper-faithful.

**2. Names are genuinely in the transcripts.** The legacy v1 engine injects the
student's first name into the system prompt
(`conversational_tutor.py:4226`), and `TutorSession.Engine` still defaults to
`V1` — so historical tutor replies address students by name. The existing
`anonymize()` (`apps/benchmark/sampling.py:46-64`) matches only
`Hi|Hello|Hey|Welcome + Name` and **keeps the name**, appending `[STUDENT]`.
`ContentSafetyFilter.PII_PATTERNS` covers SSN/card/email/phone/address and has
**no name pattern at all**. Current sampling applies **no safety filter**
whatsoever. This is the part of the plan that must not be rushed.

**3. The paper found LLM judges unreliable on this taxonomy** — predominantly
*negative* correlations with human annotators, except human-likeness (~0.08–0.11).
Decision: **human-only to start.** The LLM judge is Phase 4, and ships only if
it clears agreement against a human gold set.

## Design

A new **`SessionEvalItem`** (a redacted, safety-screened session) and
**`SessionEvalAnnotation`** (one annotator's 8-dimension verdict), living in
`apps/benchmark/` beside the existing turn-level models and reusing its
sampling → review → score → export shape.

### Data model — `apps/benchmark/models.py`

```
SessionEvalItem
  item_id            'SESS_MATH_1014'   unique, human-readable
  source_session     FK TutorSession, SET_NULL
  subject, lesson_id, turn_count
  transcript         JSON  [{turn, role, content}]  REDACTED
  redaction_report   JSON  {replacements, residual_flags[]}
  status             pending_review | approved | rejected
  reject_reason      text
  reviewed_by / reviewed_at
  session_key        salted hash of session id (never the raw id in exports)

SessionEvalAnnotation
  item               FK SessionEvalItem
  annotator_role     human | llm_judge          (llm_judge unused in Phase 1-3)
  annotator_user / annotator_model
  mistake_identification, mistake_location, revealing_answer,
  providing_guidance, actionability, coherence, tutor_tone, human_likeness
                     CharField with per-dimension 3-way choices
  notes              text
  unique (item, annotator_role, annotator_user, annotator_model)
  passed  →  property: True iff every dimension sits at its desideratum
```

Dimension definitions, allowed values and desiderata go in **one module**,
`apps/benchmark/pedagogy.py`, quoting the paper verbatim — the single source of
truth for the model choices, the annotation form, the scoring and any future
judge prompt. Mirrors how `labels.py` anchors the turn-level rubric.

### Phase 1 — Sampling with child protection

`apps/benchmark/management/commands/sample_sessions.py`, plus a
`session_sampling.py` module. Four gates in order; **a session must clear all
four**, and each rejection is recorded rather than silently dropped:

1. **Safety exclusion.** Reject if any of: `TutorSession.is_flagged`; any
   `SessionTurn.is_flagged` with `flag_type in SAFETY_FLAG_TYPES`
   (`dashboard/views.py:128`); any `SafetyAuditLog(session_id=…,
   event_type='content_flagged')`; or the student is currently
   `is_tutor_suspended`. All four already exist — nothing new to instrument.
2. **Redaction.** An LLM pass over the transcript replacing personal names with
   `[STUDENT]` / `[NAME]`, on top of `ContentSafetyFilter`'s regex sweep for
   email/phone/address. Both applied; the regex is fast and precise where it
   matches, the LLM catches free-text names it cannot.
3. **Residual-name detector.** After redaction, scan for name-like tokens
   (capitalised non-sentence-initial words not in a subject vocabulary, plus the
   student's own `first_name`/`last_name`/`username` looked up directly). Any
   hit → `status='rejected'`, reason recorded. **Cheap and decisive: we know the
   student's real name, so we can always check whether it survived.**
4. **Human sign-off.** Nothing reaches an annotator at `status='pending_review'`.
   A reviewer sees the redacted transcript and the residual-flag report, then
   approves or rejects.

Stratify like the turn-level sampler (`sampling.py:326-370`) but per session:
by subject, by outcome (passed exit ticket / failed / never reached it), and by
engine (`v1` vs `simple`), so the sample is not all one shape.

### Phase 2 — Review + annotation UI

Two views in `apps/benchmark/views.py`, two templates, following
`templates/benchmark/list.html` and `annotate.html`:

- **`session_eval_review`** — the sign-off gate. Redacted transcript, residual
  flags surfaced prominently, Approve / Reject with reason.
- **`session_eval_annotate`** — the full redacted transcript, then eight radio
  groups (one per dimension) with the paper's definition shown inline as the
  annotator's guidance, a notes box, and Save & next. Reuse the existing
  `?annotator_role=` override pattern (`views.py:565-585`) so a scripted
  annotator can write rows later without being mislabelled as human.
- **`session_eval_list`** — the filter-bar pattern from `list.html` (subject,
  status, annotated/unannotated), plus CSV/JSONL export carrying the query
  string. No pagination exists in that view today; add it here — session
  transcripts are far heavier than turns.

Add both under the existing **Developer** nav section
(`templates/dashboard/base.html:666-696`).

### Phase 3 — Scoring and export

`apps/benchmark/session_scoring.py`:

- **Per-session:** pass iff all eight dimensions sit at their desiderata.
- **Per-dimension pass rate** across the annotated set — the more informative
  number, and what the paper reports.
- **Slices** by subject, engine, outcome and persona-equivalent stratum.
- **Inter-annotator agreement** — Cohen's κ per dimension once two annotators
  overlap. Build the hook now even with one annotator, since the paper reports
  κ = 0.71 and a reviewer will ask.
- **Export**: JSONL, one object per session, carrying `session_key` (salted
  hash) and **never** the raw session or student id — reuse the hashing from
  `aggregate_export_csv` (`dashboard/views.py:9769-9772`).

### Phase 4 — LLM judge, gated on agreement (do not start until 1-3 are done)

Add `annotator_role='llm_judge'` rows via a new
`score_session_dimensions()` in `evals/scorers/llm_rubric.py`, modelled on
`score_pedagogical_dimensions()` (`:241-330`) but emitting the 3-way scale and
taking a whole transcript. Judge defaults follow the multi-turn precedent —
**Sonnet-class at temperature 0**, not Haiku, which the 2026-07-06 A/B found too
lenient on long sessions (κ = 0.09, chance-level).

**Ship it only if** per-dimension Cohen's κ against the human gold set is
acceptable. If agreement is poor — which the paper predicts — that is a
publishable finding, not a failure. Report it either way.

## Out of scope

- Retrofitting the turn-level `apps/benchmark/` tooling; it stays as it is.
- Judging simulated sessions — `evals/` already does that.
- Any change to the tutoring runtime.
- Comparing against MRBench directly (different domain and dialogue source).

## Verification

- **Safety gate, adversarially**: seed a session containing a known first name,
  one with a `harmful` flag, and one with a suspended student. Assert all three
  are rejected, with the reason recorded. This test is the one that matters.
- **Redaction**: assert the student's actual `first_name` from the database does
  not appear in the stored transcript for any approved item — a property test
  over the whole approved pool, not a sample.
- **No raw identifiers in export**: assert the JSONL contains no session id,
  student id, username or email, mirroring the aggregate-export tests
  (`apps/dashboard/tests/test_aggregate_export.py`).
- **Scoring**: a hand-built annotation with seven dimensions at desiderata and
  one at "To some extent" must fail the session; all eight at desiderata must
  pass. Tone at `Neutral` must **fail** (the paper's desideratum is
  Encouraging) — pins the divergence from our current binary code.
- **End to end**: sample from production, walk one session through
  review → annotate → score → export in the browser, with a screenshot at each
  step per `CLAUDE.md`.

## Risks

- **Redaction is never provably complete.** The residual detector catches the
  student's own name because we can look it up; a *classmate's* name typed in
  free text is harder. The human sign-off gate exists precisely for that, and
  the plan should not claim more than it delivers.
- **Sample size vs annotation cost.** Judging a 20-40 turn session takes far
  longer than a turn. Expect a gold set in the tens, not hundreds — size the
  research claims accordingly.
- **Dimension conditionality at session scope.** "Mistake identification"
  presumes a mistake occurred. At session level most sessions contain one, but
  the annotation form needs an explicit **N/A** that is excluded from scoring
  rather than counted as failure — the same correction already made in
  `llm_rubric` (`:57-62`).
