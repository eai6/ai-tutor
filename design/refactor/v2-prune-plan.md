# v2 Prune Plan — collapse to the irreducible loop

**Author**: 2026-05-27, after the run-8 evaluation (`test-reports/MATHS-S1-evaluation-2026-05-27-run8.md`, `test-reports/GEO-S5-evaluation-2026-05-27-run8.md`) showed 5 P1s / 7 turns on math, 2 P1s / 12 turns on geo, fallback_used on 14 of 19 student turns.

**Thesis**: After 8 evaluation runs and ~6 P1-driven gate additions, the **dominant cause of every P1 today is `fallback_used: true`**. Every gate that was added to catch a past P1 is now the trigger for a new one — the conformance audit rejects a fine response → the safe template fires → the student sees a stuck loop. The fix is not another gate. It is to **remove the gates and the fallback** so the primary path is the only path, and force the upstream signals (grader, router, tutor LLM, pose tool) to carry the load they were always supposed to carry.

The diagnosis documents already converge on this: `DIAGNOSIS-regression-2026-05-27.md` §2 makes the regression timeline explicit — run 2 had **0 P1s** with the simpler grader; each subsequent gate added a P1 mode without fixing the upstream cause. This plan deletes the gates.

---

## 1. Scope (per project owner directives, 2026-05-27)

**Kept (touched only to fix the root cause, not redesigned):**

1. **Grader** stays. Fix: **delete the `unverified` verdict** entirely. Secondary-school maths and geography do not produce ungradeable student responses — the grader must return `correct`, `partial`, or `wrong` for every student input that is an answer attempt. `partial` is load-bearing for multi-slot questions (one slot right, one slot wrong) and for `scaffold_hint`'s partial-credit rule; collapsing it to binary would itself become a P1 source. Help-requests are routed by the LLM router (which runs first, unconditionally) and the grader is simply not called on a turn the router classifies as no-verdict-needed.
2. **LLM Move Router and Tutor LLM** stay. Fix: tighten the router prompt; rely on the per-move tutor prompts (`move_prompts.py`) for the response shape. No replacement with a deterministic table.
3. **`pose_question` as a tool** stays — but the tool changes shape. The **tool itself owns session-scoped delivery and dedup**: it tracks which lesson_step_ids have been delivered in the current `TutorSession`, and the LLM **asks** for a question with context (topic / subskill / difficulty hint) rather than **selecting** a specific slot. The tool picks and returns. Two-phase commit and pose-attempt feedback loops in the LLM go away.
4. **Legacy implementation (`apps/tutoring/conversational_tutor.py`, `judges/`, `regen/`, `prompts/`, legacy `engine_state`)** stays. Untouched by this plan. The kill switch (`NEW_TUTOR=off`) remains the safety net.
5. **Pipeline outside the tutoring runtime** stays. Content generation, authoring, teacher dashboards, exit-ticket creation flow, curriculum import, KB indexing — none of this is in scope.
6. **Lesson steps** stay. The authored content model (`LessonStep`, `teacher_script`, `worked_example`, canonical answers) is the source of truth for what `pose_question` returns.
7. **Deterministic gates: `safety`, `figure_ref`, `answer_leak`** stay. These are cheap, fast, and catch real harms. Everything else under `conformance/` goes.

**Everything else in `apps/tutoring/v2/` goes.**

---

## 2. The pipeline that survives

```
Student message
   │
   ▼
[1] LLM Move Router      → runs first, unconditionally. Output: {move,
   │                       verdict_needed: bool}. Help-requests are
   │                       classified here (router prompt names the case
   │                       explicitly) — no engine short-circuit.
   │
   ├── verdict_needed = false (help-request, opening turn, etc.) ──┐
   │                                                                │
   ▼                                                                │
[2] Grader               → only called when router asked for it.    │
   │                       Returns: correct | partial | wrong.      │
   │                       No third "unverified" option.            │
   │                                                                │
   └──────────────────────────────┬─────────────────────────────────┘
                                  │
                                  ▼
[3] Tutor LLM            → per-move prompt from move_prompts.py + grader
   │                       verdict (or "no verdict — router skipped grader")
   │                       + context + (optional) pose_question tool use
   │
   │      ├──── if question needed ────► pose_question(topic, subskill,
   │                                       difficulty_hint)
   │                                       returns: {stem, canonical_answer,
   │                                                 lesson_step_id}
   │                                       (tool owns dedup: lesson_step_id
   │                                        not in session_delivered_ledger)
   │
   ▼
[4] 3 deterministic gates: safety, figure_ref, answer_leak
   │       ├── pass → ship to student
   │       └── fail → server-side recovery (one retry with gate-specific
   │                  reminder); if 2nd attempt also fails → redact / strip
   │                  the offending span and ship. Response ALWAYS ships;
   │                  the frontend never sees a gate_failed error.
```

Four hops, one optional tool call, three cheap gates. The pre-rewrite count was ~15 hops. Each removed hop removes its own failure mode AND removes the failure modes of every gate downstream of it.

---

## 3. File-by-file disposition (`apps/tutoring/v2/`)

### KEEP / FIX

| Path | Disposition | What changes |
|---|---|---|
| `services/student_grader.py` (2,133 LOC) | **Keep + fix** | Remove the `unverified` verdict branch entirely. Every grading path returns `correct`, `partial`, or `wrong`. Math DSL path: if the DSL extractor cannot extract from the stem, fall through to LLM grading rather than returning `unverified`. Non-math path: prompt the grader to force a ternary verdict. Re-grade on internal disagreement before returning. The grader is only invoked when the router asked for a verdict (`verdict_needed: true`) — on help-request / opening turns the grader is not called at all. |
| `services/grader_prompts.py` (703 LOC) | **Keep + fix** | Strip the "you may return UNVERIFIED if …" instructions. Replace with: "you MUST return CORRECT, PARTIAL, or WRONG. PARTIAL is for multi-slot questions where the student named some slots correctly and missed others. If you are uncertain between PARTIAL and WRONG, prefer PARTIAL so the tutor credits whatever the student did get right." |
| `services/move_router.py` (369 LOC) | **Keep + retune** | Stays as the LLM router. **Runs first on every turn, unconditionally — and is the single source of truth for move selection.** No engine-side override layer. Output schema depends on the case the router classified: non-answer-attempt turns return `{case, move, verdict_needed: false, reason}`; answer-attempt turns return `{case: "answer_attempt", verdict_needed: true, moves_by_verdict: {correct: <move>, partial: <move>, wrong: <move>}, reason}`. The conditional output lets the router commit to the final move for each possible grader outcome with the full counter context in hand; the engine just looks up the matching row after the grader returns — no decision, no override. Remove the `principle_emphasis` and `focus_note` outputs (they leak the router's reasoning into the tutor prompt and produce drift). |
| `services/router_prompts.py` (399 LOC) | **Keep + retune** | Trim. The router needs: the last student turn, the named counter fields from `runtime_state` (so it never re-counts from the transcript), the closed move list with a one-line description of each, and explicit case naming for help-requests / opening turns / answer attempts. The prompt instructs the router to enumerate `moves_by_verdict` on answer-attempt turns so the routing decision is fully captured in one LLM call without a follow-up call after the grader. No principle table, no focus_note schema, no engine-side verdict→move table. |
| `services/student_tutor.py` (1,306 LOC) | **Keep + trim** | Stays as the tutor LLM caller. Remove: conformance retry orchestration, pose-attempt feedback loop (Phase A rejection retries), safe-template fallback dispatch. Tutor calls LLM, gets response, returns. If the LLM uses the pose_question tool, hand the tool result back as a normal tool_result block; no Phase A / Phase B ceremony. |
| `services/move_prompts.py` (998 LOC) | **Keep + trim** | The per-move prompt bodies are the strongest part of the codebase. Three deletions in `SHARED_PREAMBLE_TEMPLATE`: (a) the `GRADER:` / `EVIDENCE:` header-line ritual (unnecessary once the conformance classifier is gone — the classifier was their only reader); (b) the `doing_rate_window` cadence paragraph (move selection carries this signal); (c) every UNVERIFIED branch in every move prompt (no such verdict exists post-#1). |
| `tools/pose_question.py` (494 LOC) | **Keep + restructure** | Tool changes contract. **New input**: `{topic_or_subskill: str, difficulty_hint: "easier" \| "same" \| "harder", reason: str}`. **New behavior**: the tool reads the current session's `delivered_lesson_step_ids` ledger from `runtime_state`, queries the lesson for unposed `LessonStep`s matching the topic/difficulty, picks one, appends it to the ledger atomically, and returns `{stem, canonical_answer, lesson_step_id}`. **Removed**: signed `pre_pose_token`, Phase A / Phase B split, derivability check (LLM never picks the slot now, so it cannot pick an invalid one), repeat guards as a separate layer (built in). |
| `tools/math_verification.py` (442 LOC) | **Keep** | Used by the math grader. No changes. |
| `services/conformance/gates.py` (605 LOC) | **Keep — trimmed to 3 gates** | Keep only `run_safety_check`, `run_figure_ref_check`, `run_answer_leak_check`. Delete `run_state_coherence_check`, `run_rule_check`, `run_praise_filter`, `run_open_question_stickiness_check`. Move the three survivors to `apps/tutoring/v2/services/safety_gates.py` (rename — `conformance/` becomes empty) and delete the `conformance/` directory. |
| `services/exit_ticket.py` (261 LOC) | **Keep** | Outside the tutoring-runtime decision loop; this is the transition handoff. |
| `services/media.py` (277 LOC) | **Keep** | Frontend signaling; orthogonal to the response pipeline. |
| `services/bare_answer.py` (66 LOC) | **Keep** | Used by the grader for bare-answer detection. Small, focused, no failure modes. |
| `contracts/runtime_state.py` (263 LOC) | **Keep + trim + add counter fields** | Remove `unverified` from the `Verdict` enum. Add `delivered_lesson_step_ids: list[int]` (the new pose-tool ledger). Add the router-context counters the engine maintains and hands to the router prompt every turn: `wrong_attempts_on_open_question: int`, `partial_attempts_on_open_question: int`, `consecutive_wrong_on_open_question: int`, `objective_turn_count: int`, `prior_answer_attempts_on_objective: int`, `correct_on_objective: int`, `unscaffolded_correct_on_objective: int`, `recent_verdicts: list[Verdict]` (capped at last ~10). The engine increments these in a single writer per turn — the router LLM never re-derives them. Remove `posed_question_ledger` (replaced by the new ledger), `pose_attempts_this_turn`, `pre_pose_token` cache fields, and any verdict-history fields that only existed to feed conformance. |
| `contracts/tutoring.py` (280 LOC) | **Keep + trim** | Drop `unverified` from `GradingResult`. Drop the conformance result types entirely. Tighten the tutor-response contract: prose + optional structured pose result. |
| `services/tutor_engine.py` (1,501 LOC) | **Keep + heavy rewrite** | Becomes the orchestrator of the 4-hop pipeline above. Estimated post-rewrite size: ~300 LOC. Drop all conformance-retry, fallback-dispatch, sticky-retry-classification, safety-floor-application, move-escalation orchestration. The engine: detects help-requests → calls grader → calls router → calls tutor → runs the 3 gates → returns. |
| `routing.py` (369 LOC) | **Keep + trim** | Entry-point routing. Simpler now — no v2 sub-modes to dispatch between. Sticky engine_version per session stays (preserves legacy kill switch). |
| `config/flags.py` (79 LOC) | **Keep** | Kill switch (`NEW_TUTOR=off`). |

### DELETE

| Path | LOC | Why |
|---|---|---|
| `services/conformance/check.py` | 420 | Conformance orchestrator. The classifier + verdict matrix it orchestrates are deleted; the orchestrator goes with them. The 3 surviving gates run directly from the engine. |
| `services/conformance/classifier.py` | 231 | 9-binary-label fast-LLM classifier. The audit-over-generation pattern is the dominant fallback trigger. Trust the per-move tutor prompt as the source of response-shape correctness. |
| `services/conformance/verdict_matrix.py` | 233 | Verdict-keyed rule application over the classifier labels. Dies with the classifier. |
| `services/conformance/__init__.py` | 57 | Package shell — deleted with the rest. The 3 surviving gates move to `services/safety_gates.py`. |
| `services/safety_floors.py` | 379 | Five deterministic floors that **override** the router. If the router is the source of truth (directive #2), the floors are an admission that the router can't be trusted — which we're not accepting any more. The router must be tuned (router prompt fix) so the floors are unnecessary. Concrete cases the floors caught (turn caps, evidence saturation, help-request misroute) move into the router prompt as explicit instructions, OR into the engine as deterministic pre-router checks (help-request short-circuit). |
| `services/templates.py` | 500 | **The fallback templates themselves.** Every P1 in run 8 was a fallback. With this layer gone, the engine cannot emit a deterministic safe template — on a gate failure it returns a structured error to the frontend. The student sees an honest "try again" instead of a fake teacher line. |
| `services/move_escalation.py` | 97 | Existed only for the conformance-retry path (escalate to a different move on second rejection). No conformance retry → no escalation. |
| `services/question_extractor.py` | 196 | Post-generation question detection (was there a question? was it a tool-pose?). The new pose tool makes "the LLM posed a question" a structural fact (it called the tool) rather than something to detect from prose. |
| `services/profiler.py` | 387 | Per-turn student profiling. Not load-bearing for the response pipeline; surfaces metrics the dashboard reads. Move any genuinely useful telemetry into the trace span emitted by the engine; delete the service. |
| `services/context_manager.py` | 325 | Built up turn-prompt context with the now-deleted GRADER/EVIDENCE headers + doing-rate window + conformance-feedback channel. Replace with a ~50-LOC helper inside `student_tutor.py` that assembles `{verdict, recent_transcript, lesson_step_context, open_question_state}`. |
| `tools/token_cache.py` | 196 | Signed `pre_pose_token` cache for the two-phase commit. Phase A/B is gone. |
| `tools/repeat_guards.py` | 154 | Cross-session question repeat guard. Folded into `pose_question` tool (which now owns the delivery ledger). |
| `utilities/question_validity.py` | 136 | Validity check for LLM-picked pose slots. Tool picks slots now; LLM can't pick an invalid one. |
| `utilities/tool_call_strip.py` | 89 | Defensive stripper for `<function_calls>` XML leaks. Once the tool path uses the tool API natively (not in-message XML), the leak vector is gone. |

**Total LOC removed**: ~3,400 of 14,155 (~24%) from `v2/`, plus heavy trimming in the keepers (`tutor_engine.py` ~1,200 lines lighter, `student_tutor.py` ~600 lines lighter, `move_prompts.py` ~200 lines lighter, `student_grader.py` ~300 lines lighter once unverified branches go). Realistic end state: **~7,000 LOC**, down from ~14,000.

---

## 4. Concrete changes to the keepers

### 4.1 Grader: delete `unverified`

`services/student_grader.py`:

- Remove `Verdict.UNVERIFIED` from the enum. Keep `CORRECT`, `PARTIAL`, `WRONG`.
- `_grade_math`: when DSL extraction from the stem fails, fall through to `_grade_llm_grounded` instead of returning unverified.
- `_grade_llm_grounded`: prompt must say "You MUST return CORRECT, PARTIAL, or WRONG. There is no fourth option. PARTIAL is for multi-slot questions where the student named some slots and missed others. If you cannot decide between PARTIAL and WRONG, prefer PARTIAL so the tutor credits whatever the student did get right; if you cannot decide between CORRECT and PARTIAL, prefer PARTIAL so the next turn extends rather than closes."
- Disagreement loop: if the math DSL says CORRECT and the LLM cross-check says WRONG (or vice versa), re-call the LLM once with the disagreement surfaced. After one re-call, the LLM's verdict wins. No fourth option, no escalation chain.
- The grader is only invoked when the router returned `verdict_needed: true`. On help-request / opening turns the grader is not called.

`services/grader_prompts.py`: same edits in the prompt text. Strip every "if you cannot verify…" branch and any examples of an unverified response. Keep the partial-credit instructions.

Move prompts (`services/move_prompts.py`): delete the UNVERIFIED branches in `SCAFFOLD_HINT`, `NAME_MISCONCEPTION`, `EXPLAIN`. The partial-credit branches in `SCAFFOLD_HINT` stay — they are load-bearing for multi-slot scaffold.

### 4.2 LLM Router: better prompt, narrower output, runs first on every turn

`services/move_router.py` + `services/router_prompts.py`:

- Router runs **first on every turn, unconditionally** — including help-request turns. No deterministic pre-router short-circuit anywhere in the engine. The router is the single source of truth for move selection; there is no engine-side override layer.
- Router output schema depends on the case the router classified:
  - **Non-answer-attempt turns** (help-request, opening turn): `{case, move, verdict_needed: false, reason}`.
  - **Answer-attempt turns**: `{case: "answer_attempt", verdict_needed: true, moves_by_verdict: {correct: <move>, partial: <move>, wrong: <move>}, reason}`.

  The conditional `moves_by_verdict` form on answer-attempt turns lets the router commit to the final move for each possible grader outcome — *with the full counter context in hand* — in one LLM call. The engine just picks the matching row after the grader returns. No second router pass, no engine-side decision logic, no override.
- `reason` is kept on every output. One sentence justifying the routing choice (e.g. *"answer attempt on profit-vs-percentage; saturation reached on correct → close_topic; partial-credit fallback on partial; wrong-with-named-reasoning is the 2nd attempt → name_misconception on wrong"*). Threaded into the tutor LLM's user prompt as a steering hint (replacing the deleted `focus_note`, but lighter — one sentence, not a structured note). Also lands on the trace span (`router.reason`) for debug. Drop `principle_emphasis` and the structured `focus_note` schema; keep the human-readable `reason` string.
- Router prompt names the closed move set with a one-line description each, plus three named case classes:
  - **Help-request** (student says "I don't understand", "explain", "show me", "what does X mean", "I'm stuck", "tell me how", etc.): return `worked_example` if there is an open question, `explain` if not. `verdict_needed: false`.
  - **Opening turn** (no open question, no prior student answer attempt this objective): return `explain`. `verdict_needed: false`.
  - **Answer attempt** (the student's turn is an attempt at the open question): return `verdict_needed: true` and a `moves_by_verdict` object with one row per possible grader outcome. The router considers the verdict→move logic for itself, using the named counter fields from `runtime_state` to apply saturation / attempt-threshold / named-misconception rules.
- The cases that previously lived in `safety_floors.py` fold into the router prompt's named cases and the `moves_by_verdict` enumeration. The router writes the verdict→move logic into its own output every turn, with the full attempt-history + saturation + named-reasoning signals visible. `safety_floors.py` deletes; no replacement table lives in the engine.

### 4.3 PoseQuestion: tool owns slot selection + dedup

New tool contract:

```python
# Input (LLM provides)
{
  "topic_or_subskill": "compass bearings — conversion from compass point to degrees",
  "difficulty_hint": "easier" | "same" | "harder",
  "reason": "student just got NE→045 right; pushing a parameter twist on the same subskill"
}

# Output (tool returns)
{
  "stem": "Convert the compass direction South-East (SE) to a three-figure bearing.",
  "canonical_answer": "135°",
  "lesson_step_id": 10026,
  "exhausted": false  # true when no eligible undelivered slot exists
}
```

Tool logic (in `tools/pose_question.py`):

1. Read `runtime_state.delivered_lesson_step_ids` for this session.
2. Query `LessonStep.objects.filter(lesson_id=..., is_assessment=True).exclude(id__in=delivered_ids)` filtered by topic match (Jaccard over the existing `topic_or_subskill` field, or a simple substring/keyword index).
3. Rank by difficulty match against the hint.
4. Pick the top match. Append its `id` to `delivered_lesson_step_ids` atomically (single DB write, optimistic concurrency).
5. Return the slot. If no match: return `{exhausted: true}` and the tutor LLM closes the topic.

What dies with this design:

- `pre_pose_token` (the LLM no longer picks; nothing to sign).
- `derivability_check` (the LLM no longer proposes a stem; nothing to validate).
- Phase A rejection feedback loop (no rejection — the tool always returns either a slot or `exhausted`).
- Cross-session repeat guard (the per-session ledger is enough; cross-session is a `StudentProfile.asked_questions` problem handled at lesson-start, not per-turn).
- `MAX_POSE_ATTEMPTS_PER_TURN` (one attempt, deterministic outcome).

### 4.4 Gates: 3 only, per-gate server-side recovery, response always ships

`services/safety_gates.py` (new file, ~250 LOC; replaces the whole `conformance/` package):

```python
def run_safety_gates(response_text, ctx) -> GateResult:
    for gate in (run_safety_check, run_figure_ref_check, run_answer_leak_check):
        result = gate(response_text, ctx)
        if not result.passed:
            return result
    return GateResult(passed=True)
```

Three gates only — `safety`, `figure_ref`, `answer_leak`. No classifier, no verdict matrix, no tutor-claim adjudicator, no praise filter, no rule check, no state coherence check.

**Recovery is server-side and per-gate. The response always ships; the frontend never sees a gate-failed error.** The engine wraps the tutor call + gates in a one-retry-then-degrade loop:

| Gate | What it catches | Retry strategy (one attempt) | Degrade-and-ship if retry fails |
|---|---|---|---|
| `safety` | unsafe content (jailbreak, off-topic, harmful) | re-call tutor LLM with explicit `"Do not include <flagged span>"` appended to the user prompt | redact the offending sentence(s); ship the rest of the response |
| `answer_leak` | tutor revealed the canonical answer to the current open question | re-call tutor LLM with explicit `"Do not reveal the answer; the student needs to derive it"` appended | replace the leaked canonical span with `___` (or strip the leaking sentence); ship the rest |
| `figure_ref` | tutor cited a figure (`[Figure 3]`, `as shown in the map below`) that doesn't exist | re-call tutor LLM with the available-figure list explicitly named | strip the offending sentence; ship the rest |

The retry budget is **one per gate per turn**. After one retry, the response is degraded and shipped — never replaced by a fake teacher template, never returned as an error to the frontend. The frontend's contract does not change at all from today: it always receives a tutor message.

Trade-off this commits to: in the rare case both attempts fail the same gate, the student sees a slightly redacted/stripped response. That is strictly better than (a) a fallback template lying about the student's correctness, or (b) a "system error" UI that breaks the teacher-student illusion. And it's bounded — each gate triggers on a narrow content shape, not on broad audit rules.

Observability: every gate failure (1st attempt and 2nd attempt) emits a `gate.failure` span with the gate name + reason + the degraded-or-redacted action taken. The dashboard surfaces both the 1st-attempt failure rate (how often gates trigger) and the 2nd-attempt failure rate (how often degrade-and-ship fires). The latter is the real quality signal.

### 4.5 Engine: thin orchestrator

`services/tutor_engine.py` (post-rewrite ~350 LOC, was 1,501):

```python
def respond(session, student_message) -> TutorResponse:
    # 1. Router runs first, unconditionally — single source of truth
    #    for move selection. On answer-attempt turns the router emits
    #    a moves_by_verdict object enumerating the final move for
    #    each possible grader outcome.
    routing = router.decide(session, student_message)
    # → non-answer-attempt: {case, move, verdict_needed: false, reason}
    # → answer-attempt:     {case, verdict_needed: true,
    #                        moves_by_verdict: {correct, partial, wrong},
    #                        reason}

    # 2. Grader runs only if the router asked for a verdict
    verdict = None
    if routing.verdict_needed:
        verdict = grader.grade(session, student_message)
        # → correct | partial | wrong (never unverified)
        move = routing.moves_by_verdict[verdict.value]
        # Trivial dict lookup — no decision logic, no override.
    else:
        move = routing.move

    # 3. Tutor LLM (may call pose_question tool)
    response = tutor.respond(session, move, verdict, student_message)

    # 4. Safety gates with per-gate one-retry-then-degrade recovery
    response = run_gates_with_recovery(
        response,
        retry_fn=lambda reminder: tutor.respond(
            session, move, verdict, student_message, extra_reminder=reminder
        ),
    )
    # → response always non-null; degraded fields recorded on the trace

    # 5. Persist + ship
    persist_turn(session, verdict, move, response)
    return response
```

The whole thing fits on one screen. No help-request branch in the engine (router handles it). No verdict→move decision logic in the engine (router enumerates it). No fallback template path. No "return error to frontend" path. The engine is orchestration only — call router, call grader if asked, look up the matching row, call tutor, run gates with recovery, persist, ship.

---

## 5. Removal sequence (safe order)

Each step is independently shippable. Land them in order; do not start step N+1 until step N has soaked for ~3 days of test traffic.

1. **Implement per-gate server-side recovery (`run_gates_with_recovery`) and delete the fallback templates** (`templates.py`). The recovery loop is what replaces the templates. No frontend change required — the response always ships. **This alone should drop the P1 count visibly** — fake-success-becoming-stuck-loop is the largest current P1 category.
2. **Delete the conformance classifier + verdict matrix** (`classifier.py`, `verdict_matrix.py`). Wire the engine to call the 3 deterministic gates directly via `run_gates_with_recovery`. `move_escalation.py` and `question_extractor.py` go in the same change (their only callers were the deleted modules).
3. **Delete `safety_floors.py`**. Fold the help-request case + opening-turn case into the router prompt as named cases (router runs first, classifies the case, returns `verdict_needed: false` for both). Fold the saturation / pivot-threshold / name_misconception-window cases into the router's `moves_by_verdict` enumeration on answer-attempt turns — the router writes the verdict→move logic into its own output every turn, using the named counter fields. No engine-side mapping table.
4. **Restructure `pose_question` tool**. New contract, server-side selection, in-tool dedup. Delete `token_cache.py`, `repeat_guards.py`, `utilities/question_validity.py`, `utilities/tool_call_strip.py`. Update `student_tutor.py` to use the new tool contract.
5. **Fix the grader: delete `unverified`**. Updated enum, grader-prompt edits, disagreement-loop logic. Remove UNVERIFIED branches from move prompts.
6. **Trim the router**. Remove `principle_emphasis` and `focus_note`. Tighten the router prompt with the safety-floor cases folded in.
7. **Trim the tutor / shared preamble**. Drop GRADER/EVIDENCE header ritual, drop doing-rate paragraph. Strip `student_tutor.py` of conformance retry orchestration.
8. **Delete `profiler.py`, `context_manager.py`**. Move any retained telemetry into the trace span emitted by the engine.
9. **Trim `runtime_state.py`** and `tutoring.py` contracts to the fields the new pipeline actually uses.
10. **Rewrite `tutor_engine.py`** to the ~300-LOC thin orchestrator above.

Each step has a clear rollback: revert the commit, the previous version still works.

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| **The frontend contract does not change.** No new error states, no new response shapes. The engine's `run_gates_with_recovery` loop guarantees a tutor message ships on every turn (possibly redacted, never replaced by a template, never an error). | None needed — this is the design, not a risk. Listed for completeness because the earlier draft of this plan proposed a frontend-visible error path that has now been dropped. |
| Forcing a ternary verdict on ambiguous student input may surface "partial" or "wrong" verdicts where `unverified` would have hedged. | Acceptable. `partial` is the soft option (credits whatever the student named); `wrong` routes to `scaffold_hint` which asks the student to show their work — exactly what unverified-hedging produced, without the cross-move complexity. Grader prompt biases toward `partial` over `wrong` when the LLM is uncertain. Watch the wrong-verdict rate for 1 week post-cutover; if grader precision drops, tighten the grader prompt rather than re-introducing the fourth option. |
| Server-side slot picking in `pose_question` may pick a less-good question than the LLM would have. | The LLM still provides the steering context (topic, subskill, difficulty hint). The tool ranks against authored lesson steps — which are the only valid slots anyway. The LLM never had latitude to *invent* a question; it could only pick among the same authored steps. Server-side picking is strictly safer with no quality loss. |
| Router runs on every turn, including help-request turns, so a router misclassification on help-requests would re-introduce the failure mode that motivated `safety_floors.py`. | Mitigated by router-prompt design: the router prompt names the help-request case explicitly with examples, and the help-request → `worked_example`/`explain` mapping is the simplest call the router has to make (no verdict reasoning, just a surface-text classification). If the router still misroutes help-requests measurably, the fix is router-prompt tuning, not a deterministic short-circuit. The router stays the source of truth per directive #2. |
| Per-gate retry-then-degrade may visibly redact a sentence the student would have benefited from. | Acceptable trade-off. A redacted sentence is strictly better than a fallback template that lies about correctness or promises a missing exit ticket. The 2nd-attempt failure rate is the observability signal — if it climbs, the fix is gate-recovery-prompt tuning (the `extra_reminder` text) or moving the offending pattern upstream into the tutor prompt itself, not re-adding a template layer. |
| The kept `move_router.py` is still an LLM call that could pick wrong. | Acceptable — directive #2. The router prompt is the lever. Mis-routing one turn is recoverable (the next turn re-routes); silently rejecting a correct response (which is what conformance did) is not. |
| Telemetry for the v2 observability dashboard depends on conformance-result types we are deleting. | The dashboard at `/dashboard/v2-observability/` (Phase 3 §3.3) needs its data source updated. Plan: in step 2's commit, change the dashboard to read the trace spans the engine still emits (router.move, router.verdict_needed, grader.verdict, gate.failure first-attempt rate, gate.failure second-attempt rate). Drop the conformance-specific panels. |

---

## 7. What this plan does NOT do (per directives)

- Does not touch legacy (`apps/tutoring/conversational_tutor.py`, `judges/`, `regen/`, `prompts/`, `combined_judge.py`, legacy `engine_state`). Kill switch remains the rollback.
- Does not touch content generation, authoring, teacher dashboards, exit-ticket creation, KB indexing, curriculum import.
- Does not touch `LessonStep` shape, canonical answers, or any authored content. The new `pose_question` tool reads the same `LessonStep` rows the old path read.
- Does not replace the LLM router with a deterministic table. Directive #2 is explicit: the router stays as an LLM call.
- Does not replace `pose_question` tool with a structured-output field. Directive #3 is explicit: it stays a tool, with the tool's contract restructured.

---

## 8. Success criteria

After all 10 steps have landed:

- `fallback_used: true` rate is **0** (the fallback no longer exists). The replacement signal is `gate.failure` spans — first-attempt failure rate is acceptable (gates doing their job), second-attempt failure rate is the quality alarm (target ≤2%).
- P1 errors on the standard eval set (MATHS-S1, GEO-S5) drop to **0** per session.
- `v2/` LOC count drops from ~14,000 to ~7,000.
- The decision pipeline per turn fits on one screen of `tutor_engine.py`.
- The frontend's tutor-message contract is unchanged from today (no new error states, no new response shapes).
- The legacy kill switch still works (a session created with `NEW_TUTOR=off` still routes to `ConversationalTutor`).

Failing any of these → the plan misfired and needs revisiting. Hitting all of them → the v2 architecture has paid back the bottleneck measurement that justified the refactor in the first place (`refactor-analysis.md` §1).

---

## References

- `test-reports/MATHS-S1-evaluation-2026-05-27-run8.md`, `test-reports/GEO-S5-evaluation-2026-05-27-run8.md` — observed P1s and fallback rates.
- `test-reports/DIAGNOSIS-grader-2026-05-27.md` — the engine→grader contract failure that prompted directive #1.
- `test-reports/DIAGNOSIS-regression-2026-05-27.md` — the regression timeline showing each gate added a P1.
- `design/refactor/refactor-analysis.md` §1 — the original cancelled-pilot bottleneck that justified the v2 split (still load-bearing — the pruned v2 still answers the bottleneck).
- `design/refactor/refactor-implementation-plan.md` — the Phase 1–3 build plan this prune partially reverses.
- `design/science-principles.md` — the per-move prompt grounding. Untouched by this plan.
- CLAUDE.md "Grader-driven correctness + LLM-routed moves + structural conformance is the default" — the section to **update** when this plan lands: structural conformance is no longer in the default per-turn flow.

---

## Appendix A — Sample LLM Move Router prompt

Reference template the rewritten `services/router_prompts.py` should follow. Structure: direct role statement, explicit task statement, closed set with one-line definition + 2–3 example student turns per move, turn-classification cases (the three classes that replace the deleted `safety_floors.py` regex), explicit if-then routing rules, context block, query at the end, strict JSON output schema.

Not the final wording — this is the shape. Real prompt-engineering pass goes through `claude-prompting-expert` per the CLAUDE.md "consult the prompting skills" rule.

````
You are a move classifier for a one-to-one tutoring session.

Pick the next tutor move based on the student's last turn, the open question
state, and the recent attempt history. Return one move from the closed set
below, plus whether a verdict is needed before the tutor responds.

MOVES (closed set — pick exactly one):

1. confirm_and_advance — the student gave a correct answer; affirm what they
   got right and pose the next question.
   Examples: student wrote "9 SCR" (correct); student wrote "x = 8" (correct).

2. confirm_and_extend — the student gave a rich correct answer that named
   the mechanism; affirm and pose a single twist on the same concept.
   Examples: student wrote "5² + 12² = 169 and 13² = 169, so it's a right
   triangle" (full reasoning shown).

3. scaffold_hint — the student was wrong or partially right; credit any
   partial they named, name the slip without revealing the answer, ask
   ONE smaller step on the SAME open question.
   Examples: student wrote "is it 90?" on a bearing question expecting 045°;
   student wrote "profit is 9" on a multi-slot question (one of two slots).

4. name_misconception — the student has been wrong several times on the
   same item AND named their faulty reasoning ("because it's halfway");
   name the specific misconception, give one more attempt.
   Examples: student wrote "is north east 180 because its halfway"; student
   wrote "x = 21 because i added 3 to both sides" on 3x = 18.

5. worked_example — the student explicitly asked for help, an example, or
   said they don't understand; walk through ONE example with labelled
   subgoals anchored to the open question, end with a single practice
   prompt.
   Examples: student wrote "show me how to do this"; "I don't get it,
   can you walk me through it?"; "give me an example".

6. explain — the student asked for a definition or concept, or this is an
   opening turn before any question has been posed; deliver the rule or
   definition in 2–4 short sentences, end with one action the student takes.
   Examples: student wrote "what's a bearing?"; opening turn of a lesson on
   one-step equations.

7. pivot — the student has been wrong ≥4 times on the same item;
   acknowledge the difficulty, pose a different question on the SAME
   concept at the same rigor (do not lower the bar).
   Examples: 4 wrong attempts in a row on the same Pythagoras prove-or-
   disprove problem.

8. close_topic — objective evidence has saturated (≥2 unscaffolded correct
   retrievals), OR a session-level safety cap has been reached; name what
   was done in one sentence and signal the transition.
   Examples: student just gave the 2nd consecutive unscaffolded correct
   answer on this objective; turn count on this objective has reached 12
   with insufficient evidence (forced close).

TURN CLASSIFICATION — three cases the router must distinguish:

1. ANSWER ATTEMPT — the student is attempting to answer the open question.
   verdict_needed: true. You emit a `moves_by_verdict` object enumerating
   the final move for each possible grader outcome (correct / partial /
   wrong) using the named counter fields. After the grader returns, the
   engine looks up the matching row from your output — there is no
   engine-side mapping table. The routing decision is yours.

2. HELP REQUEST — the student is asking the tutor to teach, explain, give
   an example, or otherwise hand the work back to the tutor.
   verdict_needed: false. Return worked_example (if there is an open
   question) or explain (if not).
   Signal phrases: "I don't understand", "I don't know", "I'm stuck",
   "explain", "show me", "what does X mean", "what is X", "how do I",
   "tell me how", "can you walk me through", "I'm lost", "I forgot how".

3. OPENING TURN — no open question yet, no prior student answer attempt
   on this objective.
   verdict_needed: false. Return explain.

ROUTING RULES — non-answer-attempt cases (each rule references named
counters from the CONTEXT block — do not count from the transcript,
the engine has already done the counting):

- If the student's turn matches a HELP REQUEST signal phrase →
  case: "help_request", move: worked_example (if `open_question_present`
  is true) or explain (if not). verdict_needed: false.
- If `open_question_present` is false AND `prior_answer_attempts_on_objective`
  is 0 → case: "opening_turn", move: explain. verdict_needed: false.
- If `objective_turn_count` ≥ 12 AND `correct_on_objective` is 0
  → case: "forced_close", move: close_topic. verdict_needed: false.
- Otherwise → case: "answer_attempt"; see below.

ROUTING RULES — answer-attempt case (you enumerate the final move for
EACH possible grader outcome — correct, partial, wrong — using the
named counter fields. The engine looks up the matching row after the
grader returns. The verdict→move logic lives in your output, not in
the engine):

For each verdict, pick from the closed move set above:

- correct:
    * if `unscaffolded_correct_on_objective` ≥ 1 already (so this would
      be the 2nd unscaffolded correct, saturation reached)
                                              → close_topic
    * if the current student turn is rich (names the mechanism, formula,
      or full chain of reasoning explicitly)  → confirm_and_extend
    * otherwise                               → confirm_and_advance
- partial:
    * always                                  → scaffold_hint
      (partial-credit branch — credit what the student named, ask one
      step on the SAME open question)
- wrong:
    * if `wrong_attempts_on_open_question` ≥ 4 (counting THIS attempt)
                                              → pivot
    * if `wrong_attempts_on_open_question` ∈ [2, 3] (counting THIS
      attempt) AND the current student turn named their reasoning
      ("because…", "I think…", explicit faulty rule)
                                              → name_misconception
    * otherwise                               → scaffold_hint

The "named their reasoning" judgment is a content read of the current
turn — THAT is your job. The attempt counts are fields — do not
re-derive them.

CONTEXT (every field is engine-supplied; do not re-derive any count
from the transcript):

State of the open question:
- open_question_present: ${true|false}
- open_question_stem: ${stem_or_NONE}
- wrong_attempts_on_open_question: ${int}     # 0 if none yet
- partial_attempts_on_open_question: ${int}
- consecutive_wrong_on_open_question: ${int}

State of the current objective:
- objective_turn_count: ${int}                # total tutor+student turns
- prior_answer_attempts_on_objective: ${int}
- correct_on_objective: ${int}
- unscaffolded_correct_on_objective: ${int}   # used for saturation
- recent_verdicts (oldest → newest): ${["wrong","wrong","partial","correct", ...]}

Recent transcript (last 5 turns, oldest first; for your context only —
do not count from it):
${recent_transcript}

CURRENT STUDENT TURN:
"${student_turn_text}"

Respond with ONLY a JSON object — no prose before or after.

For NON-answer-attempt cases (help_request, opening_turn, forced_close):
{
  "case": "help_request | opening_turn | forced_close",
  "move": "confirm_and_advance | confirm_and_extend | scaffold_hint | name_misconception | worked_example | explain | pivot | close_topic",
  "verdict_needed": false,
  "reason": "one sentence — names the case and the rule that fired"
}

For the ANSWER_ATTEMPT case (you enumerate one move per possible verdict):
{
  "case": "answer_attempt",
  "verdict_needed": true,
  "moves_by_verdict": {
    "correct": "<move from closed set>",
    "partial": "<move from closed set>",
    "wrong":   "<move from closed set>"
  },
  "reason": "one sentence — names the counter values that drove each branch (e.g. 'unscaffolded_correct=1 so next correct closes; partial → scaffold; wrong_attempts=1 so wrong → scaffold')"
}
````

### Notes on the shape

- **Direct task statement, no persona priming.** "You are a move classifier" / "Pick the next tutor move" — no "you are a friendly expert teacher who…" framing. Persona priming on a classifier is overhead that hurts on Gemini 3 (per `gemini-prompting-expert`) and adds noise on Claude.
- **Closed set with examples.** Each move has a one-line definition and 2–3 concrete example student turns. Examples are the load-bearing piece — they let the LLM ground "what does scaffold_hint mean" in real chat shapes rather than abstract description.
- **Turn classification before routing rules.** The router's first job is to classify the turn into one of three cases (answer attempt / help request / opening turn). Cases drive `verdict_needed`. Rules then constrain the move within the case.
- **Routing rules are explicit if-then.** No "be thoughtful about" or "consider whether"; each rule is a deterministic condition with a named outcome. The set is small (~6 rules) so the LLM can hold them all at once.
- **Verdict→move logic lives in the router's own output, not in the engine.** On answer-attempt turns the router emits `moves_by_verdict` enumerating the final move for each possible grader outcome. The engine just looks up the matching row after the grader returns — no decision, no override. This keeps directive #2 ("trust the router") clean: there is no engine-side mapping table that could drift from the router prompt, and the LLM gets to consider all three branches together with the full counter context in hand.
- **Context block is structured fields, not prose. Counters are named fields, not derived.** Every count the routing rules reference (`wrong_attempts_on_open_question`, `objective_turn_count`, `correct_on_objective`, `unscaffolded_correct_on_objective`, `prior_answer_attempts_on_objective`) is supplied by the engine as an explicit field. The LLM never counts from the transcript — counting is deterministic state work, not a language task, and asking the LLM to do it is both slow and unreliable (long-context counting accuracy degrades fast). The transcript is included for *qualitative* context only ("does the current turn name a faulty rule?") — the prompt says so explicitly so the LLM doesn't fall back to re-deriving.
- **Query at the end.** The student turn comes after all the rules and context, matching the query-last structure that maximizes attention to the actual decision (`prompting-fundamentals-expert`).
- **Strict JSON output schema.** Three fields, each enumerated. The `reason` field is constrained to name the case and the rule that fired — that's both an interpretability win (the trace tells you why) and a self-discipline cue for the LLM (forcing it to justify in a structured way reduces drift).
- **No "do not" or negative-only instructions.** Every rule is a positive "if X then Y" — `gemini-prompting-expert` and `prompting-fundamentals-expert` both call out negative-only instructions as drift-prone.
