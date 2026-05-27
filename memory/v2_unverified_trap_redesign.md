# v2 Unverified-Trap & Open-Question-Pivot Redesign

**Status**: Approved 2026-05-26 by Roy Manzi.
**Owner**: implementation in `apps/tutoring/v2/`.
**Driving evidence**: `test-reports/MATHS-S1-evaluation-2026-05-26-run6.md` and `test-reports/GEO-S5-evaluation-2026-05-26-run6.md`, plus runs 1–5 surfacing the same shapes on different lessons.

---

## Root-cause clusters (4, not 10)

| Cluster | Manifests as | Root cause |
|---|---|---|
| **A. Verifiability gap** | unverified-trap; correct natural-language proof graded `unverified` | `_GROUNDED_CONFIDENCE_THRESHOLD=0.5` in `student_grader.py:447–448` downgrades any `correct, confidence<0.5` to UNVERIFIED. Sparse KB → low confidence is the norm, not the exception. |
| **B. State-pivot desync** | correct MCQ graded against prior canonical; stacked questions | `open_question` not cleared on CORRECT. `/difficulty-signal/` bypasses `tutor_engine.respond()`. Stacked-question worked_example commits only one to state. |
| **C. Fallback degeneracy** | "Let me check that" 6× repeated; LO leak; worked_example with no subgoals | Conformance retry → verdict-keyed prose blob, not principled move-escalation. |
| **D. Tool-contract gaps** | "which of the following" with no options; help-request → explain not worked_example | `pose_question` tool has no `options` field. Help-request classifier isn't load-bearing — move prompts must be defensive too. |

---

## Design principles

1. **Grader verifies, doesn't cite.** Confident "yes, same answer" suffices. Sparse KB is not a reason to refuse to verify.
2. **`open_question` has exactly one writer**: `tutor_engine.respond()`. No side endpoint may pose.
3. **Move prompts express intent; rendering enforces invariants.** "One question per turn" is enforced by a cheap-LLM extractor, not by per-move rules.
4. **Fallback is move-escalation, not prose substitution.** Escalation table moves down the teaching ladder while preserving Active Learning at every step.
5. **Deterministic gates are safety floors** (conformance gates kept), but flow control is move-based.
6. **Active Learning is a first-class invariant.** Every turn ends with an action the student takes. Doing-rate ≥60% over rolling 5-turn window.

---

## Fix 1 — Answer-consistency verifier (Cluster A)

**Where**: `apps/tutoring/v2/services/student_grader.py` + new prompt in `grader_prompts.py`.

**What**: After `_call_grounded_adjudicator()` returns, when `verdict ∈ {correct, partial}` AND `confidence < 0.5`, run a second cheap-LLM call (Haiku 4.5, strict JSON):

```
Inputs: question stem, canonical (or rubric), student response, tentative verdict
Output: {confirmed: "yes"|"no"|"cant_tell", why: "<one phrase>"}
Question: "Does the student's response, interpreted charitably, 
           assert the same answer/conclusion as the canonical?"
Explicitly NOT asking: "is it well-explained?" or "did they show working?"
```

Final mapping:
- `confirmed=yes`        → keep CORRECT/PARTIAL, set `verifier_confirmed=True` in trace
- `confirmed=no`         → flip to WRONG, set `verifier_overrode=True`
- `confirmed=cant_tell`  → keep UNVERIFIED (genuinely rare terminal state)

**Generalizes across subjects** — works on math proofs, geography reasoning, definitions, language.

**Science of Learning citation**: Principle #11 *Testing Effect / Retrieval Practice* (Ch.20) — "frequent low-stakes quizzes with immediate feedback." Feedback must close the loop.

---

## Fix 2 — Single-writer for `open_question` (Cluster B)

**2a. Clear on CORRECT** — `tutor_engine.respond()`, immediately after grader returns CORRECT: set `runtime_state.open_question = None` before move selection runs.

**2b. Route difficulty-signal through `respond()`** — `apps/tutoring/views.py::chat_difficulty_signal` mutates `runtime_state.difficulty_level` then calls `tutor_engine.respond()` with a synthetic intent. **Decision**: add `Intent.SYSTEM_DIFFICULTY_CHANGE` to the enum (visible at the contract surface, not a flag).

**2c. Post-render question extractor** — new service `apps/tutoring/v2/services/question_extractor.py`. After `StudentTutor.respond()` returns, before conformance: Haiku call lists every distinct question/action-prompt in the rendered tutor text. If count > 1 → request regeneration with "exactly one action prompt". If count == 1 → that's the committed `open_question` regardless of whether it came from the tool path or prose.

**Science of Learning citation**: Principle #5 *Minimising Cognitive Load* (Ch.14) — "one idea per turn". Principle #4 *Mastery Learning* (Ch.13) — re-estimate the frontier.

---

## Fix 3 — Move-escalation table (Cluster C)

**Where**: `tutor_engine.py` escalation logic + `templates.py` (becomes thinner).

**Escalation table** — every step preserves an active end:

| Failed move | 1st escalation | 2nd / floor |
|---|---|---|
| `pose_question` | `pose_question` (different slot) | `explain → re-pose` |
| `scaffold_hint` | `worked_example` (with practice prompt) | `explain → re-pose` |
| `name_misconception` | `worked_example` | `explain → re-pose` |
| `worked_example` | `pivot` (new subskill) | `explain → re-pose` |
| `confirm_and_advance` | `pose_question` (next slot) | `close_topic` (with exit retrieval) |
| `confirm_and_extend` | `pose_question` (extension slot) | `confirm_and_advance` |
| `explain` | `explain → re-pose` (one retry, more concrete) | `pose_question` at a lower rung |
| `pivot` | `explain → re-pose` | `close_topic` |
| `close_topic` | (terminal — hands off to exit-ticket) | — |

**No escalation node is a pure prose turn**. Every move's escalation target ends with the student doing something.

**LO leak fix**: `templates.py::_question_or_objective` (L409–414) removed. The objective string is never substituted into student-facing prose. Move prompts paraphrase using visible context if re-anchoring is needed.

**Science of Learning citations**:
- Principle #2 *Direct Instruction* (Ch.11) — escalation steps down the Direct side of the ladder
- Principle #1 *Active Learning* (Ch.10) — every step keeps the doing intact

---

## Fix 4 — Tool-contract gaps (Cluster D)

**4a. MCQ options threaded** — `PoseQuestionToolArgs` gets `options: list[str] | None`. Renderer (`student_tutor.py:389+`) concatenates stem + `A) ... B) ... C) ... D) ...` when the LessonStep has `choices`.

**4b. Deterministic safety floor** — Phase A validator rejects a pose where rendered stem contains "which of the following" / "the following options" but `options is None or empty`. Belt-and-braces alongside Fix 2c's Haiku extractor (per the user's guideline: deterministic gates stay as safety floors, not flow controllers).

**4c. Help-request defensive rule in `SHARED_PREAMBLE`** — adds:

> "When the student asks to be shown, explained, or walked through ('what do I do first', 'show me', 'I'm stuck', 'I don't get it'), your turn must teach the **method**, not the principle. (Principle #2 *Direct Instruction* Ch.11; Principle #5 *Minimising Cognitive Load* Ch.14 — labelled subgoals are the load-reducer.)"

Defensive because the intent classifier *should* route to worked_example, but if it misses, the move prompt itself is the catch.

---

## Active Learning as first-class invariant

**The active-end rule** added to every move's SHAPE section:

> "End the turn with an action the student takes — answer, choose, fill in, compute, restate, identify, name. Even on `explain` and `worked_example`, the turn closes on retrieval or production. Principle #1 *Active Learning* (Ch.10) — student must be *doing* on ≥60% of turns; 'following along' is not learning."

Enforcement: the question-extractor (Fix 2c) rejects any tutor turn that doesn't contain at least one action prompt at its end.

**Doing-rate tracked as runtime invariant**:
- New field `SessionRuntimeState.student_doing_rate_window: list[bool]` — last 5 student turns, `True` if attempted (answered/chose/computed) vs `False` if hedged ("idk", "I'm stuck").
- Populated from intent classifier output (`attempting` vs `asking_help_*` vs `meta`).
- **`move_selection.pick_move()` consumes it**: if doing-rate ≤ 2/5, bias toward lighter cognitive lift (`confirm_and_extend`, `pose_question` at a lower rung) over `worked_example`.
- Surfaces in trace as `doing_rate_5turn`.

**SHARED_PREAMBLE gets the doing-rate context line**:

> "Recent doing-rate: {N}/5 turns where the student attempted an answer. If ≤ 2/5, size the next ask **smaller and easier** than the open question — break it down to a step likely to succeed. Principle #1 *Active Learning* — momentum builds on successful retrievals, not understanding-claims."

---

## Move-prompt restructure

Every move's body gets:

```
PRINCIPLE: <name (Ch.N)> — <one-line imperative from science-principles.md>

INTENT: <what the tutor is trying to accomplish>

SHAPE:
- <2–4 short bullets constraining output without scripting>
- End with an action the student takes this turn. (Active Learning)

ANTI-PATTERNS:
- <2–3 bullets of what NOT to do>
```

The `principles=(...)` tuple in the `MovePrompt` dataclass stays (Phase 2 audit invariant). The new PRINCIPLE header makes the citation visible at the top of the prompt body itself.

Specific updates per move:
- **POSE_QUESTION**: anti-patterns kept; stem-retelling and fact-assertion rules kept; "no stacked second question" becomes redundant (Fix 2c enforces) but stays as documentation.
- **SCAFFOLD_HINT**: "credit-the-partial" promoted to SHAPE imperative — *"When verdict=wrong AND student named a sub-step the canonical decomposes into, lead must affirm that sub-step before asking the next."* Principle #12 *Targeted Remediation* (Ch.21).
- **WORKED_EXAMPLE**: "exactly ONE practice prompt at end, in prose, on open question — never two, never a tool call". Principle #5 *Minimising Cognitive Load* (Ch.14).
- **EXPLAIN**: defensive rule — *"if prior student turn was a help-request, deliver the method in 2–3 numbered steps, not the principle"*. Principle #2 *Direct Instruction* (Ch.11).
- **NAME_MISCONCEPTION**: *"if you cannot name a specific misconception in one short sentence, escalate to worked_example"* (automatic via Fix 3; one-line guard).
- **SHARED_PREAMBLE**: gets help-request rule + LO-quotation prohibition + doing-rate context line.

---

## Implementation order

1. **Fix 1** — answer-consistency verifier (1 new function in `student_grader.py` + 1 prompt in `grader_prompts.py`)
2. **Fix 2a** — clear `open_question` on CORRECT (one-line invariant in `tutor_engine.respond`)
3. **Fix 2b** — route `/difficulty-signal/` through `respond()` (refactor `views.py::chat_difficulty_signal`; add `Intent.SYSTEM_DIFFICULTY_CHANGE`)
4. **Fix 2c** — question extractor (new service file, called from `tutor_engine` after `StudentTutor` returns)
5. **Fix 3** — move-escalation table (`tutor_engine.py` escalation logic; strip safe templates in `templates.py`)
6. **Fix 4a/4b** — MCQ options threaded (`pose_question.py`, `student_tutor.py`, conformance gate)
7. **Active Learning invariant** — `SessionRuntimeState.student_doing_rate_window`, `move_selection.pick_move` bias, preamble line
8. **Fix 4c + move_prompts restructure** — preamble rule + all 9 moves get PRINCIPLE/INTENT/SHAPE/ANTI-PATTERNS structure
9. **Tests** — stub scenario suite replaying run-6 P1 cases; re-run both eval personas end-to-end

---

## What stays untouched

- Two-phase commit (Phase A validate → conformance → Phase B commit) — correct architecture, kept.
- Conformance gates (incl. `open_question_stickiness`) — kept as safety floors. Less load once Fix 2c is in place.
- Closed 9-move set — kept; bounded counter-pattern.
- `BaseLLMClient` + `ModelConfig` dispatch — kept.
- `StudentGrader`'s math-DSL path — kept; only the grounded non-math path gets the verifier addition.

---

## Affected files (estimate)

- `apps/tutoring/v2/services/student_grader.py` — verifier function
- `apps/tutoring/v2/services/grader_prompts.py` — verifier prompt
- `apps/tutoring/v2/services/tutor_engine.py` — clear-on-correct, escalation, doing-rate
- `apps/tutoring/v2/services/move_selection.py` — doing-rate bias, system_difficulty_change intent
- `apps/tutoring/v2/services/question_extractor.py` — **NEW**
- `apps/tutoring/v2/services/student_tutor.py` — MCQ options rendering
- `apps/tutoring/v2/services/templates.py` — strip LO substitution, thinner safe templates
- `apps/tutoring/v2/services/move_prompts.py` — restructure all 9 moves
- `apps/tutoring/v2/runtime_state.py` (or wherever the dataclass lives) — `student_doing_rate_window` field
- `apps/tutoring/v2/tools/pose_question.py` — `options` field in tool schema, validator rejection
- `apps/tutoring/v2/conformance/gates.py` — MCQ-options safety floor
- `apps/tutoring/views.py` — difficulty-signal refactor

Commit: separate commit per fix (1, 2a, 2b, 2c, 3, 4, AL, prompts, tests).
