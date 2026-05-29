# Curriculum-fidelity principle — assessment contract for the v2 tutor

*AI Tutor • drafted 2026-05-28 • Roy Manzi + Claude*

Resolves the design conversation triggered by the Map Scale (L1425) preview
regression on commit `dda0e3b` (screenshot 2026-05-28 20:05 EDT): the tutor's
EXPLAIN opener ended with a prose-posed verifiable question
("Which type of map — large-scale or small-scale — would be more useful if
you needed to find a particular street in your town?"), the student's
correct answer ("large scale") landed without a verdict, and the next turn
posed an unrelated bank True/False with no acknowledgment.

The root cause is NOT just an EXPLAIN-checklist violation. The engine's
state contract assumed the tutor would never author assessable questions
in prose; there is no structural defense when it does. The fix is to
make that assumption load-bearing — codify it as a contract and enforce
it.

---

## The principle

**Bank-authored questions are the only assessments.** Prose-posed
questions exist for conversational warmth, not for learning evidence.
The tutor is forbidden from generating its own assessable questions.

Three corollaries:

1. **Assessment provenance.** Every `OpenQuestion` in
   `SessionRuntimeState` has `source ∈ {LESSON_STEP, EXIT_TICKET_QUESTION}`.
   `INLINE_GENERATED` is never written — it remains in the enum for now
   (deletion is a follow-up cleanup) but no code path produces it.
2. **Grader scope.** The grader is called only when the router fires
   Rule 7 against a bank-posed `open_question`. There is no
   "grounded-grade-against-prose-Q" path. Adding one would legitimize
   the curriculum violation.
3. **Mastery accounting.** Counters that drive Mastery Ch.13 thresholds
   (`unscaffolded_correct_on_open_question_objective`,
   `wrong_attempts_on_open_question`, `attempts_on_open_question`)
   increment only on bank-grounded verdicts. Prose-Q engagement is
   conversational, not pedagogical evidence.

What this principle does NOT forbid:

- **Genuinely reflective prose prompts.** "What do you think happens
  when rain hits clay soil?", "Which of these matches your intuition?",
  "Where have you seen this happen near you?" — open-ended prompts with
  no canonical answer are legitimate conversational scaffolding. The
  EXPLAIN move prompt already authorizes these as opener path (b)
  (`move_prompts.py:868-872`).
- **Warm acknowledgment of substantive engagement.** When a student
  shares a thought on a reflective prompt, the tutor should reference
  what they shared. Acknowledgment is conversational, not pedagogical
  evaluation.

The line: a question is *assessable* (and therefore forbidden in prose)
if it has a single canonical answer derivable from the lesson content
— a number, a letter, a named term, an ordered sequence, a binary, a
choice from a closed set. A question is *reflective* (and therefore
permitted in prose) if it has no single canonical answer.

---

## What this invalidates (alternatives considered + rejected)

Documented so future readers know we considered and chose otherwise.

**Option A — dual-source `OpenQuestion` (synthesize from prose).**
Rejected. Synthesizing an `INLINE_GENERATED` `OpenQuestion` from
prose-posed Qs and grading it via the grounded path would elevate
curriculum violations to graded events. The grounded grader's
accuracy on canonical-less Qs is also empirically weaker.

**Option B — router decides verdict_needed from transcript (full
rewrite).** Rejected as scope. Most of Option B's complexity was the
synthesis path (extracted stem → synthetic open_question → grounded
grade). Under the curriculum-fidelity principle that path doesn't
exist, so Option B collapses to "the router routes substantive prose
engagement to a warm-acknowledge-and-pose move." That's a move-prompt
change, not a router rewrite.

**Option C — derive `open_question` from transcript walk-back.**
Rejected. Same root critique as A — derives state that doesn't
correspond to a legitimate assessment.

**Option D — constrained-output redesign.** Out of scope. Worth
revisiting if the move-prompt-level enforcement plus the structural
gate prove insufficient over the next 2-4 weeks of preview traffic.

**Forcing `tool_choice="any"` on EXPLAIN.** Rejected previously
(conversation 2026-05-28). Forces a pose every opener, which breaks
legitimate reflective openers and degrades help-request EXPLAINs.

---

## Enforcement points

Two layers, defense in depth:

### Layer 1 — Move-prompt level (preventive)

The EXPLAIN move prompt already says "never end with a verifiable-
answer question typed in prose" (`move_prompts.py:872-883`). The
problem is that this rule lives at line ~873 of a long prompt, after
~50 lines of help-request defensives. Long prompts get ignored late.

**Action**: lift the curriculum-fidelity rule into `SHARED_PREAMBLE`
so it applies to all 7 non-terminal moves and appears early in every
prompt. Phrasing draft:

> *Curriculum-fidelity rule (applies to every move).* All assessable
> questions go through the `pose_question` tool. Never type a question
> with a single canonical answer in prose — not a "what is the value
> of", not a "which is X — A or B", not a "is X true", not a "rank
> these from largest". The tool emits the assessment; your prose is
> for explanation, acknowledgment, and transition. Genuinely reflective
> prompts (no single canonical answer) are still allowed in prose.

The move-prompt body for `confirm_and_advance` also needs its
no-verdict branch updated — see "Layer 3" below.

### Layer 2 — Structural gate (corrective)

New gate in `safety_gates.py`: `curriculum_fidelity`. Detects when a
non-terminal move's response ends with a verifiable-shape question
and no `pose_question` tool call fired in the turn. On detection:
retry the move with a reminder; on second failure, truncate the
trailing prose-Q sentence and let the next turn pose via tool.

Detection: heuristic regex matching the assessable-shape patterns
listed in the principle section. Detection is **precision-favoring**
(false positives that block legitimate reflective Qs are acceptable —
the retry mechanism allows recovery; false negatives that let through
assessable prose Qs corrupt the assessment chain and must be avoided).

Scope: all 7 non-terminal moves (`confirm_and_advance`,
`confirm_and_extend`, `scaffold_hint`, `name_misconception`,
`worked_example`, `explain`, `pivot`). Catches the EXPLAIN opener
failure and also the mid-move prose-diagnostic stacking observed in
MATHS run-11 §3 R1 (T1853, T1861, T1863, T1870).

### Layer 3 — `confirm_and_advance` no-verdict branch rewrite

The current no-verdict branch (`move_prompts.py:319-325, 349-360`)
assumes the only no-verdict case is "no prior student answer." Under
the principle, there are two sub-cases:

- **(a)** Forward signal ("next", "ready", "ok next") → pure
  transition, no affirmation. Current behavior is correct.
- **(b)** Substantive engagement on a reflective prompt (e.g., student
  shared a thought on "what do you already know about maps?") → warm
  one-sentence acknowledgment that references what they shared
  (concept name, example they offered, framing they used), then
  transition + tool-pose. NOT graded — the prompt had no canonical.

Draft addition to the no-verdict branch:

> *Sub-case (b) — student gave substantive content on a reflective
> prompt (no canonical existed to grade against):*
> - *Open with one short clause acknowledging what they shared. Name
>   the specific concept, example, or framing they offered — not
>   generic praise.*
> - *Examples:*
>   - *Bad: "Great thought! Let's continue." (no content reference)*
>   - *Good: "That's a useful starting intuition about pore size —
>     let's look at a specific case."*
>   - *Good: "Right idea naming the inverse step — let's apply it."*
> - *Do not grade the response or claim correctness. The prompt had
>   no single right answer; you're recognizing engagement, not
>   evaluating retrieval.*
> - *Follow the acknowledgment with the tool-posed next assessment.*

Same rewrite probably needed for `EXPLAIN` and `WORKED_EXAMPLE`
no-verdict paths — review during implementation.

---

## What's NOT changing

- **The router.** Rules 1-9 are correct under the principle. Rule 7
  fires on bank-posed `open_question`. Rule 6 fires on no question
  pending. No re-write needed.
- **The grader.** Source-dispatch in the grader stays
  `LESSON_STEP`-only. No grounded-path for prose Qs.
- **The Mastery counters.** Continue to increment only on Rule 7
  graded verdicts.
- **The `OpenQuestion` schema.** `INLINE_GENERATED` stays in the
  enum (no migration) but no code writes it. Flag for cleanup pass
  in 2-4 weeks if still unused.
- **The state contract.** `runtime_state.open_question` semantics
  unchanged.

---

## Implementation plan

Three phases. Phase 1 ships independently; Phase 2 and 3 can ship as
separate commits or bundled.

### Phase 1 — Structural gate (curriculum_fidelity)

Files touched:

- `apps/tutoring/v2/services/conformance_check.py` (new) —
  `is_verifiable_prose_question(response_text: str) -> bool`.
- `apps/tutoring/v2/services/safety_gates.py` — add
  `run_curriculum_fidelity_check`, extend `_GATE_ORDER`,
  `_reminder_for`, `_degrade_for`.
- `apps/tutoring/v2/services/tutor_engine.py` — thread `selected_move`
  + `posed_via_tool` into `GateContext` (already partially present).

Tests:

- `apps/tutoring/v2/tests/test_curriculum_fidelity_detector.py` —
  20 cases (10 verifiable, 10 reflective) drawn from run-11
  transcripts + the Map Scale screenshot.
- `apps/tutoring/v2/tests/test_curriculum_fidelity_gate.py` —
  5 cases covering retry success, retry-then-degrade truncation,
  pose-tool-fired bypass, reflective-prompt pass-through, non-
  EXPLAIN moves.

Regression: `pytest apps/tutoring/v2/tests/` — full suite must pass.

### Phase 2 — `confirm_and_advance` no-verdict branch rewrite

Files touched:

- `apps/tutoring/v2/services/move_prompts.py` — extend
  `CONFIRM_AND_ADVANCE.body` with sub-cases (a) and (b). Audit
  `EXPLAIN.body`, `WORKED_EXAMPLE.body` for the same shape; update
  if present.

Tests: prompt-content regression tests only (verify the new strings
exist in the prompt body) — the behavior is LLM-driven and tested
in Phase 4.

### Phase 3 — `SHARED_PREAMBLE` curriculum-fidelity rule

Files touched:

- `apps/tutoring/v2/services/move_prompts.py` —
  `SHARED_PREAMBLE` gains a `## Curriculum-fidelity rule` section.

Tests: same as Phase 2 (content regression).

### Phase 4 — Live verification

- Local manual reproduction via `mcp__chrome-devtools__*` on Map
  Scale (L1425): confirm the new opener does not author a verifiable
  prose Q (or the gate catches it), and that a reflective opener's
  substantive student response is warmly acknowledged.
- Optional eval replay against L1148 (math) + L1454 (geo): confirm
  run-11 P1 counts (0 hits) hold.
- Push to `refactor/conversational-tutor-redesign` → preview Container
  App deploy → repeat reproduction on the deployed URL.

Rollback: each phase is one commit; `git revert` restores prior
behavior. The Container App's single-revision mode means a revert
push redeploys the prior image cleanly.

---

## Open decisions for the implementation pass

- **Detector phrasing**: heuristic regex is the default per the
  earlier conversation. If preview traffic surfaces false positives
  (legitimate reflective Qs being blocked), swap to the Haiku
  `conformance_classifier` ModelConfig (purpose seeded, currently
  unused). One file change.
- **Scope of Phase 1 gate**: starts on all 7 non-terminal moves.
  If regression risk is too high, narrow to EXPLAIN-only for the
  first ship and extend after one week of preview traffic.

---

## Code anchors (verified 2026-05-28)

- Router rules — `apps/tutoring/v2/services/router_prompts.py:151-260`.
- `_resolve_move` — `apps/tutoring/v2/services/tutor_engine.py:601-635`.
- Grader call site (Rule 7 gate) — `tutor_engine.py:221`.
- `commit_pending_pose` — `context_manager.py:66-94`.
- EXPLAIN move prompt — `move_prompts.py:818-948`.
- `CONFIRM_AND_ADVANCE` no-verdict branch — `move_prompts.py:319-325, 349-360`.
- `_GATE_ORDER` — `safety_gates.py:431`.
- `POSE_CAPABLE_MOVES` / `POSE_DOMINANT_MOVES` — `student_tutor.py:65-95`.
- `OpenQuestion` / `QuestionSource` — `runtime_state.py:22-86`.
- `_apply_mastery_close_floor` — `tutor_engine.py:677-758`.

---

## Refs

- Conversation 2026-05-28 (this design pass).
- `test-reports/GEO-S5-evaluation-2026-05-28-run11.md` (run-11 GEO,
  L1454 — the lesson whose reflective opener worked).
- `test-reports/MATHS-S1-evaluation-2026-05-28-run11.md` (run-11
  MATHS, §3 R1 — the mid-move prose-diagnostic stacking pattern).
- Map Scale screenshot 2026-05-28 20:05 EDT — Container App revision
  `--0000002` (commit `dda0e3b`).
- `design/refactor/refactor-implementation-plan.md` Phase 3 (post-
  prune engine layout that this principle lives inside).
- `CLAUDE.md` "Critical rules" — multi-tenancy, session state, v2
  engine cutover, math tutoring rules — all consistent with this
  principle; no conflict.
