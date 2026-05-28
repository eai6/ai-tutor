# Deep-Dive Diagnosis — MATHS-S1 run 9 root causes
**Author**: evaluator analysis, 2026-05-27.
**Scope**: Three questions raised after `MATHS-S1-evaluation-2026-05-27-run9.md`:
1. Why does `runtime_state.open_question` stay `None` through every turn?
2. What does the live grader return when re-run on the exact run-9 inputs?
3. Is the closed verdict→move table in the router LLM prompt — and should it be removed?

---

## Headline findings

| # | Question | Root cause |
|---|----------|------------|
| 1 | Why `open_question` ends up `None` | **It doesn't** stay `None` mid-turn — it is committed on each pose, then cleared on `CORRECT` verdict. The visible-at-end-of-session `None` is because the *only* verdict the grader ever returned was `correct` for one turn (1585), and the close-topic flow that followed left a stale-and-cleared state. The *real* problem behind the false-wrong cascade is one layer up: the grader received correct `OpenQuestion` records on every grading turn (verified via `metadata.v2_trace.verdict_needed: true` returning a non-null verdict), but **silently ignores `open_question.canonical`** and re-derives a canonical from the *problem text* via an LLM — which hallucinates wrong canonicals for Y/N and MCQ items. |
| 2 | What does the live grader say now | **Reproduced 4/4 P1s** by replaying the exact (stem, student answer) pairs through `StudentGrader.grade_student_response`. Verdicts came back as `wrong`, `partial`, `wrong`, `wrong` against the true canonicals "Yes", 41, "A", "B". The `private_canonical` field surfaces *why*: the grader's LLM-A invented multi-slot intermediate-computation canonicals (`'a squared plus b squared=169; c squared=169'` for a Y/N question) or collapsed to a boolean (`'True'` for an MCQ-letter question). |
| 3 | Verdict→move table location | Yes — `apps/tutoring/v2/services/router_prompts.py:144-178` (rules) + `:191-201` (`moves_by_verdict` schema). The LLM enumerates the move for all three possible verdicts *before* the grader runs. Removing the table is **safe and cleaner** but **would not have fixed run 9**: the wrong verdict was upstream of the table, and the table did exactly what it was supposed to given that input. Recommendation in §3 below. |

There is also a fourth latent issue surfaced in the dig — `student_tutor.py:62-64` and CLAUDE.md both reference an `all__no_assessment_in_prose` conformance gate that **does not exist** in `safety_gates.py`. The 9-gate classifier conformance package was removed in the v2-prune; only `safety` / `figure_ref` / `answer_leak` survive. The intent — "structurally force tool-only posing" — has no enforcement layer behind it. Worth knowing even though it isn't the run-9 root cause.

---

## 1. Why `open_question` stays `None` — corrected from the run-9 report

### What I claimed in run 9 report

> "`runtime_state.open_question` is `None` throughout the session… The grader has nothing to grade against."

### What is actually true

Reading `apps/tutoring/v2/services/tutor_engine.py:199-228` + the persisted `metadata.v2_trace` on turns 1571 / 1573 / 1575 / 1577 / 1579 / 1581 / 1583:

- The grader **did run** on every answer-attempt turn (`verdict_needed: True` and a non-null `verdict` is recorded for all 7 wrong-graded turns).
- For the grader to run, `runtime_state.open_question is not None` at the moment of grading (line 201 guard).
- So `open_question` *is* being populated by `commit_pending_pose` (`apps/tutoring/v2/services/context_manager.py:66-101`) at the previous turn's Phase B commit. The pose tool path IS firing.
- After a `wrong` verdict, `_apply_open_question_counter_updates` **keeps** the open_question and increments counters. After a `correct` verdict it clears it (line 557-558). The only `correct` in this run was turn 1585 (7-24-25 True). The post-run `open_question: None` is the post-clear state, plus the close-topic loop that followed.

**Net**: my run-9 report was wrong on this specific point — the grader was being fed a valid `OpenQuestion`. The cascade is *not* a state-management failure inside the engine. It is a grader semantics failure that happens *despite* a correctly-populated open question.

### What the open-question record actually contains at grading time

From `apps/tutoring/v2/services/context_manager.py:85-92`, Phase B commit writes:

```python
state.open_question = OpenQuestion(
    source=pending.question_ref.source,
    id=pending.question_ref.id,
    canonical=pending.canonical,       # ← e.g. "Yes" or "B"
    rendered_stem=pending.rendered_stem,
    ...
)
```

So `open_question.canonical` holds the authoritative answer that Phase A validated at pose time. For run 9 turn 1569 (5-12-13 Yes/No), `pending.canonical = "Yes"`. For turn 1574-ish (8-15-17 MCQ), `pending.canonical = "B"`. These are passed through into `OpenQuestion.canonical` and onward into `GradingRequest.open_question.canonical`.

### The actual bug: the grader ignores `open_question.canonical`

`apps/tutoring/v2/services/student_grader.py:282`:

```python
problem_text = request.open_question.rendered_stem or ""
# 1. Extract the DSL from the LLM.
extraction = self._extract_math_dsl(problem_text)
```

A grep across the whole file confirms: **`open_question.canonical` is never read by the math grader**. The math path takes the *question stem*, hands it to `MATH_DSL_SYSTEM` (`grader_prompts.py:35-75`), and asks the LLM to extract a canonical from scratch — every single turn, ignoring the answer that was validated at pose time.

For computable problems (e.g. "Solve 2x+3 = 11", "find the third angle"), this is fine — re-deriving via DSL gives the same answer. For Y/N, T/F, and MCQ-letter problems — the bulk of run 9's questions — it is the source of every P1.

---

## 2. Live grader replay — reproducing run 9 P1s

Setup: `/tmp/grader_replay.py` constructs `GradingRequest`s with the exact (rendered_stem, student_input, canonical) tuples from run 9 and calls `StudentGrader.grade_student_response` against the live `MATH_DSL` + `STUDENT_CLAIMS` LLM clients.

```
=== P1-1: 5-12-13 Y/N + full working ===
canonical: Yes
S: Identify longest side c=13. Compute a^2 + b^2 = 5^2 + 12^2 = 25 + 144 = 169.
   Compute c^2 = 13^2 = 169. Since 169 = 169, a^2 + b^2 = c^2, so by Pythagoras
   the triangle IS right-angled.
VERDICT: wrong
reason_code: conclusion_inconsistent_with_canonical
private_canonical: 'a squared plus b squared=169; c squared=169'   ← INVENTED
reasoning: math: two-llm comparator (multi-slot)

=== P1-2: 9-40-41 hypotenuse ===
canonical: 41
S: c^2 = 9^2 + 40^2 = 81 + 1600 = 1681. c = sqrt(1681) = 41 m. Check: 9^2 + 40^2
   = 1681 = 41^2 OK. So the hypotenuse is 41 m and the theorem is satisfied.
VERDICT: partial
reason_code: None
private_canonical: 'hypotenuse=41; confirmation=True'              ← INVENTED 2-slot
reasoning: math: two-llm comparator (multi-slot)

=== P1-4: 6-8-10 MCQ (A) ===
canonical: A
S: A. a^2+b^2 = 36+64 = 100 and c^2 = 10^2 = 100, so yes the theorem is satisfied.
VERDICT: wrong
reason_code: conclusion_inconsistent_with_canonical
private_canonical: 'a2 plus b2=100; c2=100'                        ← INVENTED
reasoning: math: two-llm comparator (multi-slot)

=== P1-6: 8-15-17 MCQ (B) ===
canonical: B
S: B. 8^2 + 15^2 = 64 + 225 = 289 = 17^2, so by Pythagoras theorem the triangle is right-angled.
VERDICT: wrong
reason_code: conclusion_inconsistent_with_canonical
private_canonical: 'True'                                          ← COLLAPSED to bool
reasoning: math: two-llm comparator — conclusion mismatch
```

4/4 run-9 P1s reproduced deterministically. The failure mode is consistent:

| Question type | What `pending.canonical` says | What LLM-A invents | Student answer match? |
|---|---|---|---|
| Y/N "is it right-angled? show working" | `"Yes"` | `[a²+b²=169, c²=169]` (multi-slot intermediates) | No — student says "yes/right-angled", not "169" |
| Find the hypotenuse + verify | `"41"` | `[hypotenuse=41, confirmation=True]` | Partial — student says 41 but doesn't write "True" |
| MCQ "Does it satisfy the theorem? A/B/C/D" | `"A"` | `[a²+b²=100, c²=100]` (intermediates again) | No — student says "A" with working, not bare numbers |
| MCQ "Which statement is true? A/B/C/D" | `"B"` | `True` (collapsed to bool) | No — student says "B" with working, not "True" |

### Root mechanism inside the math grader

The `MATH_DSL_SYSTEM` prompt at `apps/tutoring/v2/services/grader_prompts.py:35-75` is purely arithmetic. It has:
- No instructions for handling Y/N or T/F problems.
- No instructions for handling MCQ-letter answers.
- An `expressions` (multi-slot) mode that the LLM eagerly uses whenever the problem text *mentions* multiple computations, even if the actual answer is a single label.

So for "A triangle has sides 5, 12, 13 — show that it is right-angled. Show all your working", the prompt's natural reading is: "I see two numeric quantities (a²+b² and c²) in the problem; emit multi-slot DSL." The LLM is *not wrong* given its instructions — it's missing the case where the *answer slot* is a label, not a number.

The student then writes the full Pythagoras proof + says "the triangle IS right-angled". `STUDENT_CLAIMS_SYSTEM` extracts `claims = [a²+b²=169 ✓, c²=169 ✓]` and `conclusion = {stated_answer: "the triangle is right-angled", answer_label: ""}`. The comparator compares `[a²+b²=169, c²=169]` (invented canonical) against `conclusion.answer_extracted_value` (no scalar; the student stated a conclusion, not numbers) → no match → `wrong`.

The student got everything right. The grader threw away the canonical that *would have* matched ("right-angled" ≡ "Yes") and asked itself a question the student never tried to answer.

### Why P1-2 is `partial` not `wrong`

For the hypotenuse-and-verify question, the invented canonical was `[hypotenuse=41, confirmation=True]`. The student wrote "c = 41". `STUDENT_CLAIMS_SYSTEM` extracted the 41 as a scalar. The multi-slot comparator (`student_grader.py:644-685`) matched the `hypotenuse=41` slot but not the `confirmation=True` slot (the student wrote "the theorem is satisfied" but no extracted boolean). 1 of 2 slots matched → `partial`.

This is *the same bug* showing in a different surface — the canonical was still invented (the original answer was just "41", not two slots).

---

## 3. The verdict→move table — is it in the router prompt, and should it go?

### Location

`apps/tutoring/v2/services/router_prompts.py`:

- **Lines 144-178** — natural-language rules ("if X then move = Y") for the three verdict branches.
- **Lines 191-201** — the strict-JSON output schema for the `answer_attempt` case, requiring `moves_by_verdict: {correct, partial, wrong}`.

The engine then runs the grader and looks up `moves_by_verdict[grader.verdict]` to pick the final move (`tutor_engine.py:_resolve_move` — line 231).

### What the LLM is actually deciding in the table

Two qualitative judgments only:

1. **"is this turn rich?"** — distinguishes `confirm_and_advance` (bare correct, e.g. "x = 8") from `confirm_and_extend` (full working + named mechanism).
2. **"did the student name their reasoning?"** — distinguishes `scaffold_hint` from `name_misconception` on the wrong branch when 2-3 prior attempts exist.

Every other branch is mechanical: counter thresholds (`unscaffolded_correct_on_objective ≥ 1` → `close_topic`; `wrong_attempts_on_open_question ≥ 4` → `pivot`; partial always → `scaffold_hint`) that don't need an LLM.

### Cost/benefit of removing it

| Aspect | Keep | Remove (deterministic + 1 LLM judgment) |
|---|---|---|
| Prompt complexity | High — 3-branch enumeration + counter rules | Low — LLM picks turn class + emits 1 string per turn |
| Latency / tokens | One router call per turn (current) | Same call, smaller output (≤50 tokens) |
| Failure modes | LLM can put the wrong move on a branch (e.g. `pivot` where `scaffold_hint` was right). 1 P1 from run 6 was exactly this. | Engine bug instead of LLM bug — testable, deterministic. |
| Coupling | Engine + router prompt move in lockstep on table edits | Router emits classifier output; engine owns the lookup. Edits stay one side or the other. |
| Effort to remove | Rewrite output schema, rewrite engine resolver, rewrite ~15 router tests | Same |
| **Would it have fixed run 9?** | n/a | **No.** Verdict was `wrong`; deterministic table also routes `wrong` → `scaffold_hint`. Same student outcome. |

### Recommendation

**Yes, remove the table — but do it as a separate, low-risk cleanup, not as a "run-9 fix".** The deterministic shape:

```python
# apps/tutoring/v2/services/move_router.py — new resolver
def resolve_move(decision: RouterDecision, verdict: Verdict | None, counters: Counters) -> str:
    if decision.case != "answer_attempt":
        return decision.move                           # router decided directly
    if verdict == Verdict.CORRECT:
        if counters.unscaffolded_correct_on_objective >= 1:
            return "close_topic"
        return "confirm_and_extend" if decision.turn_richness == "named_mechanism" else "confirm_and_advance"
    if verdict == Verdict.PARTIAL:
        return "scaffold_hint"
    # WRONG
    if counters.wrong_attempts_on_open_question >= 4:
        return "pivot"
    if counters.wrong_attempts_on_open_question in (2, 3) and decision.named_reasoning:
        return "name_misconception"
    return "scaffold_hint"
```

The router LLM now emits *one* compact JSON:

```json
{
  "case": "answer_attempt",
  "verdict_needed": true,
  "turn_richness": "bare | named_mechanism",
  "named_reasoning": true,
  "reason": "…"
}
```

This is provably equivalent to the current table on every routing rule the prompt currently encodes, and removes one class of LLM-mistake (putting the wrong move name on a branch).

The actual fixes that *would* have prevented run 9 are §1 + §2 here — let those land first and treat the router cleanup as a follow-up.

---

## 4. Recommended fixes — ordered by impact on run-9 class P1s

### Fix A (P0, blocks production-grade math) — make the grader use `open_question.canonical`

Change `student_grader.py:_grade_math` so that when `request.open_question.canonical` is non-empty AND the open question's `answer_format` is one of {`multiple_choice`, `true_false`, `yes_no`, `boolean`}, the grader takes the canonical *directly* and compares against `STUDENT_CLAIMS_SYSTEM`'s `conclusion.answer_label` / `conclusion.stated_answer`. No LLM-A call. The DSL path is preserved for genuinely numeric problems.

Pseudocode:

```python
def _grade_math(self, context, request):
    canonical = (request.open_question.canonical or "").strip()
    answer_format = request.open_question.answer_format  # added to OpenQuestion
    if canonical and answer_format in LABEL_FORMATS:
        return self._grade_label_canonical(context, request, canonical)
    # … existing DSL path below …
```

Required: add `answer_format` to `OpenQuestion` (it's already on `PendingPose` from the pose tool — just needs to be threaded through Phase B commit at `context_manager.py:85`).

Run-9 P1-1, P1-3, P1-4, P1-5, P1-6, P1-7 all collapse to one cheap label compare and return `correct`.

### Fix B (P0) — math DSL canonical also keyed by `open_question.canonical` when present

For the residual case where `answer_format` is `free_text` *and* `canonical` is non-empty (e.g. "41" hypotenuse problems), bypass LLM-A and pass `canonical` directly into the comparator as the canonical_value. LLM-A invents multi-slot canonicals far too often (P1-2 here) when the question text *describes* multiple computations.

### Fix C (P1) — restore or replace the `all__no_assessment_in_prose` enforcement

The student_tutor comment at lines 62-64 says this gate is "the only enforcement needed", and the gate doesn't exist. Two options:

- Reinstate a regex/classifier check on tutor output: if move is in `POSE_CAPABLE_MOVES` AND `pending_pose` is `None` AND the tutor's text contains an "interrogative + answer-key shape" sentence (regex over `? + (a/b/c/d|yes/no|true/false)`), reject and retry with `tool_choice="any"`.
- Cheaper: when `move in POSE_CAPABLE_MOVES`, call the LLM with `tool_choice="any"` on the *first* attempt (not `"auto"`). One retry with `"auto"` if the slot bank is exhausted.

Without this, the architecture relies on the LLM to follow a prose rule it currently honours about 70% of the time (sampled from run-9 transcript), and the other 30% leaks prose-only questions that bypass the grader contract entirely.

### Fix D (P2, cleanup) — collapse the verdict→move table

§3 above. After A + B + C land and re-evals come clean, factor the table into engine Python and shrink the router LLM output schema.

---

## Appendix — repro

```bash
./venv/bin/python /tmp/grader_replay.py        # 4/4 P1s reproduced deterministically
```

Run-9 turn metadata for reference:

```bash
./venv/bin/python manage.py shell -c "
from apps.tutoring.models import SessionTurn
for t in SessionTurn.objects.filter(session_id=101).order_by('id'):
    tr = (t.metadata or {}).get('v2_trace', {})
    print(t.id, tr.get('selected_move'), tr.get('verdict'))
"
```
