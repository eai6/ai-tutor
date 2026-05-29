# MATHS-S1 Tutor Evaluation — 2026-05-26

**Persona**: S1 (Cycle 4) student in Seychelles, struggling with fractions / one-step equations.
**Lesson evaluated**: L1148 — *Solve one step equations (unknown on one side), positive integers* (S3 Mathematics; substituted for the requested Multiplying/Dividing Fractions lesson, which is not present in the curriculum dataset).
**Engine**: v2 (`apps/tutoring/v2/`, `engine_version='v2'`, `NEW_TUTOR=on` default).
**Session ID**: 75.

> **Note**: this evaluation surfaced and landed a P0 architectural fix to the v2 engine
> mid-session (the `pose_question` tool was never wired into the LLM call). The
> transcript below is from the *post-fix* re-run. Without the fix the tutor literally
> cannot pose a single question. See §5 ("Architectural fix landed during this
> evaluation") and the linked commit.

---

## 1. Transcript

> Meta block on each tutor turn: `[move=… verdict=… fallback=…]`. `fallback=True` means
> structural conformance rejected the LLM's response twice and the engine emitted a
> verdict-keyed safe template instead. `fallback=False` means real generated content.

| Turn | Role | Content | Meta |
|------|------|---------|------|
| 0 | tutor | Welcome! Let's get started. 😊 In S3 maths, we cover a range of topics — algebra, geometry, trigonometry, statistics, and more. What topic would you like to work on today? | `move=explain  verdict=None  fallback=False` |
| 1 | student | i want to learn one step equations | — |
| 1 | tutor | *(first attempt — rejected by conformance)* Let's pick this back up together. Let's try a question on this together. | `move=pose_question  verdict=None  fallback=True` |
| 2 | student | *(same)* i want to learn one step equations | — |
| 2 | tutor | Great choice — let's dive straight in with a multiplication equation. **Solve the equation 3x = 18.** Show your working by identifying the operation, applying the inverse operation to both sides, and stating the solution. | `move=pose_question  verdict=None  fallback=False` ✅ tool-posed bank Q |
| 3 | student | is it 21? | — |
| 3 | tutor | I want to check that with you before I'm sure either way. Let's try a question on this together. | `move=scaffold_hint  verdict=unverified  fallback=True` |
| 4 | student | i think you add 3 to both sides so x = 21 | — |
| 4 | tutor | *(same safe template)* | `move=scaffold_hint  verdict=unverified  fallback=True` |
| 5 | student | okay x = 15 | — |
| 5 | tutor | *(same safe template)* | `move=scaffold_hint  verdict=unverified  fallback=True` |
| 6 | student | i dont know how to do this | — |
| 6 | tutor | *(same safe template)* | `move=explain  verdict=unverified  fallback=True` |
| 7 | student | is the inverse subtract 3? | — |
| 7 | tutor | *(same safe template)* | `move=explain  verdict=unverified  fallback=True` |
| 8 | student | ohhh x = 6 | — |
| 8 | tutor | *(same safe template)* | `move=explain  verdict=unverified  fallback=True` ⚠ correct answer marked unverified |
| 9 | student | wait did i get it right | — |
| 9 | tutor | *(same safe template)* | `move=explain  verdict=unverified  fallback=True` |

**Runtime state at end**: `open_question = LessonStep 9835 (3x = 18)`, `canonical = "x = 6"`,
`attempts_on_open_question = 5`, `posed_question_ledger = [9835]`, `move_history =
['explain', 'pose_question', 'pose_question', 'scaffold_hint', 'scaffold_hint',
'scaffold_hint', 'explain', 'explain', 'explain']`.

---

## 2. Science-of-learning evaluation

| # | Principle | Score | Evidence |
|---|-----------|-------|----------|
| 1 | **Active Learning** (Ch.10) | ⚠ partial | Turn 2 succeeded: bank question posed; student was *doing*, not listening. But turns 3–9 collapse into a safe-template loop — the student is not given a new attempt, only a generic re-prompt. Effective ratio of "doing" turns is 1/9 ≈ 11% (target ≥60%). |
| 2 | **Direct Instruction** (Ch.11) | ❌ fail | The opening turn never frames "what is a one-step equation" or "what does *inverse operation* mean" before jumping to a tool-posed problem. The student says "i dont know how to do this" (turn 6) — at that point an `explain` was selected but conformance rejected and fired the safe template. No instruction is ever delivered. |
| 3 | **Deliberate Practice** (Ch.12) | ❌ fail | Engine has no signal of *this student's* weakness; the lesson opens with a multiplication-inverse problem (`3x = 18`) even though the persona is struggling with fractions. No calibration. |
| 4 | **Mastery Learning** (Ch.13) | ⚠ partial | The bar is held (the canonical `x = 6` never wavers; the engine does not lower it). But the path is also unchanged: same question is re-attempted repeatedly because the grader returned `unverified` even on the correct answer. The student would gate-fail through no fault of their own. |
| 5 | **Minimising Cognitive Load** (Ch.14) | ❌ fail | The very first question is a *transfer task* ("show your working by identifying the operation, applying the inverse operation, stating the solution"). No worked example precedes it. No subgoal labels. Dual coding (figure) is absent for this lesson. |
| 6 | **Automaticity** (Ch.15) | n/a | Single-item session; not measurable here. |
| 7 | **Layering** (Ch.16) | ❌ fail | The prerequisite ("multiplication inverse = division") is never named or pre-checked. |
| 8 | **Non-Interference** (Ch.17) | n/a | Single item. |
| 9 | **Spaced Repetition** (Ch.18) | n/a | Single session. |
| 10 | **Interleaving** (Ch.19) | n/a | Single item. |
| 11 | **Testing Effect / Retrieval** (Ch.20) | ⚠ partial | The *intent* shows on turn 2 — retrieval before instruction. But because the grader can't actually grade, the retrieval-feedback loop is broken. Retrieval without diagnostic feedback is just frustration. |
| 12 | **Targeted Remediation** (Ch.21) | ❌ fail | Move-table selected `scaffold_hint` (right move) on turn 3, but conformance rejected the LLM's response twice, so no scaffold was delivered. The student saw a generic safe-template, not a diagnosis. Misconception-naming never fires. |
| 13 | **Gamification** (Ch.22) | n/a | Out of scope for the conversational engine layer. |

**Net**: in this evaluation, the v2 engine adheres to the science of learning *by
design* (move table + per-move prompts that cite the principles) but the
**runtime never delivers** because two upstream services break: the math grader
returns `unverified` even on the canonical answer, and the structural conformance
gate rejects every reasonable scaffold/explain response when verdict is
`unverified`. The student experiences a frustration loop.

---

## 3. What's working

- **Bank-question posing (after the architectural fix landed during this eval).** On
  turn 2 the tutor *correctly* used the `pose_question` tool, rendered the bank stem
  verbatim, and committed the open question to `runtime_state.open_question` +
  `posed_question_ledger`. The grader then has a target to grade against next turn.
- **Move-selection table is sound.** The selected moves track the verdict: `explain`
  → `pose_question` → `scaffold_hint` × 3 → `explain` × 3. That's the right
  pedagogical shape; the issue is at the layer below (the LLM's response is
  rejected by conformance every time).
- **Safe template is doing its job.** The engine never releases a free-form response
  that failed conformance twice — the safe template fires deterministically. That's
  the right safety floor, even when it produces a poor UX.

## 4. What's not working

### 4.1 Grader returns `unverified` on *every* student answer (incl. the canonical)

Every graded turn in this session — including `"ohhh x = 6"` on turn 8, which is
the literal canonical — returned `verdict=unverified`. The math DSL extraction
path (`StudentGrader._grade_math` →
`_extract_math_dsl` → `MathVerificationTool.evaluate` → `_parse_student_math_value`)
is failing at one of those stages and the grader exits via the
`UNVERIFIED + reasoning="…"` branch every time.

Likely failures (need server-log instrumentation to pin down):
- DSL extraction LLM returns an empty / unparseable program for "Solve 3x = 18".
- `_parse_student_math_value` can't pull "21" / "15" / "6" out of conversational
  prose ("is it 21?" / "okay x = 15" / "ohhh x = 6").

### 4.2 Conformance unverified ruleset is incompatible with the scaffold_hint / explain moves

The verdict-keyed matrix's `_ruleset_unverified()` requires:

- `surfaces_uncertainty=true`
- `affirms_correctness=false`
- `refutes_correctness=false`
- `hands_floor_back_or_transitions=true`

The scaffold_hint move's *job* is to name what's wrong with the student's attempt
(implicit refutation) and credit what they got right (implicit affirmation of
partial work). These directly contradict the unverified ruleset. Similarly,
`explain` produces factual claims about the concept which the classifier marks as
`contains_factual_claim=true`, routing to the tutor-claim adjudicator which
returns `unverified` and triggers the `tutor_claim_unverified` rule.

**Net effect**: when the grader returns `unverified`, no LLM response can satisfy
conformance, so every turn falls back to the safe template. This is the
fundamental failure mode of this session.

### 4.3 Opening message hallucinates a math curriculum

The opening "In S3 maths, we cover algebra, geometry, trigonometry, statistics…"
is plausible but generic. The lesson objective ("solve one step equations") is
never named. This is because **`TutoringContext.current_objective` is hard-coded
to empty string** in `ContextManager.assemble_context()` (the parameter has
default `""` and no caller fills it in). The engine has no lesson-level signal in
the LLM prompt.

### 4.4 First-attempt conformance rejection on turn 1

Even the *intent-to-pose* turn 1 ("i want to learn one step equations") was rejected
on first attempt and the retry produced a meaningful response only on the second
try. So even when conformance eventually passes, every turn pays a 2× latency cost.

---

## 5. Architectural fix landed during this evaluation

**Symptom** (pre-fix): every turn returned `selected_move=pose_question` with a
generic safe-template message, and `posed_question_ledger` stayed empty across 7+
turns. The session auto-closed at turn 6 with `close_topic` (safety-valve
fired on `verdictless_turns ≥ 6`).

**Root cause**: `StudentTutor.respond` called `client.generate()` text-only — no
tools — even when the selected move was `pose_question`. The per-move prompt
instructed the LLM to call the `pose_question` tool, but no tool was passed, so the
LLM emitted the question in prose. The conformance gate
`all__no_assessment_in_prose` then rejected the response, retry hit the same
condition, the verdict-keyed safe template fired. Both `PoseQuestionTool`
wrappers and `ContextManager.commit_pending_pose(...)` existed as scaffolding from
Phase 1 but were **never invoked**.

**Fix landed in this session** (in `apps/tutoring/v2/`):

1. **`tools/pose_question.py`** — added `build_anthropic_pose_question_tool(lesson_id,
   posed_step_ids) → (tool_dict, slot_to_step_map)` returning a slot-indexed
   Anthropic / Gemini / OpenAI tool surface, and
   `make_resolve_canonical_for_lesson(slot_map)` returning the callback
   `validate_pose` needs.
2. **`services/student_tutor.py`** — added `TutorResponse` dataclass
   (`text + Optional[PendingPose]`) and `POSE_CAPABLE_MOVES` set. When the
   selected move is pose-capable and the lesson has un-posed bank slots, the LLM is
   invoked via `client.generate_with_tools(...)` with `tool_choice={"type":"any"}`
   for `pose_question` (force tool) and `{"type":"auto"}` for other pose-capable
   moves. The response's `tool_use` block is parsed; the bank stem is rendered
   verbatim; `validate_pose(...)` returns a `PendingPose`.
3. **`services/tutor_engine.py`** — `_invoke_tutor_or_fallback` now returns
   `TutorResponse`. After conformance accepts the response (first attempt OR retry),
   the engine calls `context_manager.commit_pending_pose(...)` to commit Phase B —
   `open_question` is written, `posed_question_ledger` appended, the token is
   consumed.
4. **`services/conformance/verdict_matrix.py`** — added `posed_via_tool: bool`
   parameter to `apply_verdict_matrix(...)`; the `all__no_assessment_in_prose` rule
   is skipped when the candidate text came through the tool (the classifier can't
   distinguish a tool-rendered stem from a prose-authored one).
5. **`services/conformance/check.py`** — plumbed `posed_via_tool` through `run(...)`.
6. **`services/move_selection.py`** — on `UNVERIFIED` with an open question still in
   flight, the selector now picks `scaffold_hint` rather than re-posing (was
   producing the conformance fight described in §4.2).
7. **`services/tutor_engine.py::_bank_stems_for_context`** — included
   `LessonStep.question` (not just `teacher_script`); the rule-check gate was
   flagging numbers from tool-posed bank stems as "authored numbers not in any
   visible source" because the bank field it was checking was wrong.
8. **`services/move_prompts.py`** — updated the `pose_question` move prompt to (a)
   use the new slot-based tool surface, (b) emit a verdict-aware lead_in directive
   (must surface uncertainty when verdict is unverified).

**Verification**: 207 existing v2 tests pass. End-to-end test on this session
shows the tool path firing (`fallback=False`, real bank question on screen,
`open_question` + ledger committed in `runtime_state`).

---

## 6. Recommended next changes to `apps/tutoring/v2/services/move_prompts.py`

The move prompts are well-grounded in `design/science-principles.md` — they cite
the right chapters and the imperatives match the table. The issue is they assume
the *engine* is working. Specific prompt-level changes that would improve the
observed behavior independent of the upstream grader/conformance issues:

1. **`POSE_QUESTION`** (already updated by this PR) — keep the verdict-aware
   lead_in directive. Consider also: when there is *no* current objective signal,
   default to slot 0 (the canonical first practice item).

2. **`SCAFFOLD_HINT`** — currently says "Use ``what_right`` to credit what they got
   (when partial). Use ``first_misconception_redacted`` to name the slip WITHOUT
   giving the answer." On verdict=`unverified` (the case in this session), neither
   `what_right` nor `first_misconception_redacted` is populated, so the LLM has
   nothing to lean on and improvises a factual claim that conformance rejects.
   Add an `unverified` branch:

   ```
   - If the grader's verdict is ``unverified`` (couldn't be checked), DO NOT name a
     misconception or refute the student's attempt. Instead: (a) surface the
     uncertainty explicitly ("I couldn't verify that — let me check with a smaller
     question"), (b) call ``pose_question`` with a SIMPLER slot that diagnoses one
     sub-step. NEVER make a factual claim about why the answer is wrong on an
     unverified verdict — the tutor-claim adjudicator will reject it.
   ```

3. **`EXPLAIN`** — on verdict=`unverified`, the current prompt produces factual
   claims about the concept that the grounded adjudicator can't verify in dev
   environments without a populated KB. Add a guardrail:

   ```
   - If the grader's verdict is ``unverified``, frame the explanation as a
     *clarification of the question* ("Let's make sure we're solving the same
     thing"), not a *concept restatement* ("Here's how to solve linear
     equations…"). The latter triggers tutor-claim adjudication which is
     conservative under sparse KB coverage and will reject.
   ```

4. **`render_shared_preamble`** (referenced by every move prompt) — currently has no
   slot for the lesson objective or subject. Wire `context.current_objective` through
   `assemble_context()` and surface it in the preamble:

   ```
   === Lesson context ===
   Subject: {subject}
   Objective: {current_objective}
   Step in flight: "{open_question.rendered_stem}" (canonical: WITHHELD)
   ```

   Without this the LLM hallucinates the topic (turn 0 saw the geography lesson
   open with "In S3 maths…").

5. **`POSE_QUESTION` + every pose-capable move** — add a line forbidding the LLM
   from typing the bank stem in the text block when using the tool. The current
   prompt says "you must NOT type the stem in your text response" which is
   correct; on Gemini I've seen variants paraphrase the stem then call the tool,
   producing duplication. Tighten with: "Your `text` block must be ONE short
   sentence at most. The bank stem goes ONLY via the tool."

6. **Bigger pedagogical fix outside `move_prompts.py`** — the grader is the
   bottleneck. The `_extract_math_dsl` LLM call should retry with a stricter
   prompt when it returns an empty program; `_parse_student_math_value` should be
   liberalised to pull numerics out of "is it 21?" / "ohhh x = 6". These are
   `student_grader.py` changes, not `move_prompts.py` changes.

---

## 7. Open issues (not blockers for the PR, but should be tracked)

- **Grader UNVERIFIED on canonical math answers** (§4.1) — needs instrumentation
  to see *which* stage of the DSL path is failing. Suspect the DSL-extraction LLM
  call's prompt or its parsing.
- **`current_objective` never populated** (§4.3) — `views.py` and `routing.py`
  call `cm.assemble_context()` with no `current_objective` argument. Need to
  derive it from the current `LessonStep.enabling_objective` or
  `Lesson.objective`.
- **Conformance × unverified incompatibility with non-pose moves** (§4.2) — the
  matrix needs the same kind of `posed_via_tool`-style exemption for the
  scaffold/explain branches, OR the move-selection needs to choose moves that the
  unverified ruleset *can* accept.
- **Tutor-claim adjudication is over-conservative on sparse KBs** — every prose
  factual claim becomes `unverified` because the grounded grader can't ground it.
  Add a "skip adjudication when KB coverage for this lesson is below threshold"
  gate, or load richer KB content for the dev dataset.
- **First-attempt conformance rejection rate is high** even on healthy turns —
  the retry rate from `v2_trace.retry_used=true` was nearly 100% in this session.
  Worth a dashboard alert.
