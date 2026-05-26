# Conversational Tutor Refactor — Analysis & Recommendations

Companion to: `design/refactor/current-conversational-tutor.md` (the as-is).

This document records the analysis of the proposed refactor and the recommended design direction after iteration. The recommendations supersede the initial pass; rationale for what changed is preserved inline.

Service and tool definitions used throughout this document are inlined in §3.

---

## 1. Framing

- **No backward-compat constraint.** Seychelles pilot has been cancelled because of the P1 unacceptable errors. Tanzania is not active. No `v1_classic`, no rollback flag, no shadow rollout. Cut what doesn't serve P1 (deprecate now, remove once the new design clears the benchmark — see §3 deletion table).
- **The unified judge + regen stack is disconfirmed evidence**, not in-flight investment. It shipped, ran in production, and the system failed P1 anyway. Retention is the failure case, not the safe case.
- **The CLAUDE.md "don't multi-agent without measured bottleneck" rule releases.** The bottleneck has been measured the hard way — by a cancelled pilot. Architectural conservatism that protected the old design no longer applies. CLAUDE.md guidance describing the unified judge as the 2026-05-18 default will be rewritten as part of this refactor; the cancelled pilot is the evidence that overrides it.
- **Scope: runtime tutoring only.** Authoring-time pipelines (`ImageGenerationService` with its pre/post-gen judges and auto-regen, `ContentGenerator`'s JSON-repair retry loop, the figure-facts extractor) are out of scope. They run before a student touches a lesson and have not been implicated in the P1 failures.
- **Don't over-script.** The cancelled pilot also surfaced that the current implementation is too rigid — heavy phase machinery, long internally-contradictory prompts, deterministic flow gates that fight personalization. Every replacement component below is sized to be the *floor*, not the script: deterministic gates are safety rails, not the lesson plan.

---

## 2. Root cause: why the current judges DON'T catch the 3 unacceptable errors

The three unacceptable errors (referred to throughout this document as **P1**):
1. Tutor says a student's correct answer is wrong.
2. Tutor says a student's wrong answer is correct.
3. Posing incomplete questions (missing crucial info needed to answer).

**Error A (correct → wrong) and Error B (wrong → correct).** The tutor LLM writes a correctness assertion in its own prose. The unified judge's `answer_correct` axis defers to the deterministic verdict — but for anything outside the ±0.01 / MCQ-letter happy path (equivalent algebraic forms, alternate units, valid paraphrase, prose math with mixed notation), there IS no deterministic verdict, so the judge falls back to its own LLM judgment. Two LLMs with overlapping failure modes evaluating the same thing correlate — they don't catch each other. This is the classic critic-loop weakness: a critic from the same family as the generator doesn't see what the generator missed.

**Error C (incomplete questions).** The current system has answer-key coverage for several paths (curriculum-authored bank, exit-ticket bank, inline MCQ tool) and `NO_AUTHORING` blocks the tutor from inventing new concrete problems. The actual gap is narrower: **no path validates that the *visible question text* contains every piece of information a student needs to answer.** A tool call can carry a `correct_answer` while the prompt the student sees is missing context the canonical relied on. Tutor-improvised prose questions and chat-authored prose questions are the worst offenders, but even tool-posed questions can be under-specified if their canonical was derived from hidden KB chunks rather than the visible prompt.

Common pattern: **LLM-generates-then-LLM-judges has a coverage ceiling.** Defense-in-depth made of layers from the same model family is one layer.

---

## 3. Revised architecture: grader-driven correctness

The single biggest move: **correctness is produced by a dedicated verifier/grader, not by the tutor response generator.** The tutor never *independently decides* whether the student is right or wrong — it receives a verdict from `StudentGrader` and renders feedback in conformance with it (which may include affirming "yes, that's right", but only because the grader said so). Math uses **executable verification** where the LLM-emitted decomposition can be re-checked against the visible problem text and then executed in Python. Non-math uses **grounded adjudication** — LLM-mediated judgment with KB citations and/or Gemini Google-grounding, with a conservative confidence threshold that returns `unverified` rather than guess. Calling the non-math path "computation" overstates it; the goal is to move correctness *off the response generator* and onto a path with grounding and an explicit "I don't know" escape valve.

### Service and tool surface

Core services — **all stateless** (every service receives full context as input; nothing is implied from the prompt). All context, transcript, and prior state are passed in explicitly via typed Pydantic contracts owned by `ContextManager`.

- **StudentGrader** — single grader/adjudicator across all paths (conversational student answers, exit-ticket batch grading, runtime claim adjudication). Uses the KnowledgeBase, Gemini Google-grounding, and `MathVerificationTool` to ground its decisions. Three closely related responsibilities:

  1. **Student-answer grading** — produces `{ verdict (correct | partial | wrong | unverified), private_canonical, student_safe_feedback, student_value, reasoning, citation }`. Two paths:
     - **Math path** — executable verification. LLM decomposes the problem into a constrained JSON DSL; a **DSL-validation pass** independently checks that the DSL's variable bindings and operations are consistent with the **visible problem text**; on mismatch the verdict escalates to `unverified`. The Python interpreter then executes the validated DSL and returns the canonical plus step-by-step trace.
     - **Non-math path** — grounded adjudication. KB-with-citation lookup or Gemini Google-grounding produces a confidence-scored judgment; below the confidence threshold the verdict is `unverified`. This is *not* "computation" — it is LLM-mediated judgment with grounding and a conservative escape valve. For exit-ticket bank questions, the deterministic `bank_grader` runs first (string/MCQ-letter match against the bank's answer key); grounded adjudication is fallback only for free-text rubric questions where bank-deterministic grading does not apply.

  2. **Pre-pose check** for runtime-generated, transformed, *and* (for the pilot) teacher-authored bank questions. Enforces the **student-visible derivability invariant**: the canonical must be derivable from the student-visible prompt plus visible artifacts (attached figure, recent conversation) — with hidden KB chunks suppressed during the derivation. If the canonical relies on something the student can't see, the question is refused, not posed. Returns a signed/opaque **`pre_pose_token`** that backs the question's provenance at the tool layer (see "Tool answer-key provenance" below). For pilot scope, bank questions go through the same runtime pre-pose check (`BANK_PREPOSE_RECHECK=on`, see §7 item 11) — the authoring-time validation gate does not yet exist, and the pilot relaunch timeline does not permit waiting for it. Flag flips off once the authoring-time gate lands.

  3. **Tutor-claim adjudication** — when invoked by the conformance layer on tutor prose. Same grounded-adjudication machinery as path 1's non-math branch, applied to factual/arithmetic claims surfaced by the conformance classifier (see "Tutor-prose claim verification" below). Returns `{ supported | contradicted | unverified, citation }`. This is the runtime replacement for the deprecated `factual` and `arithmetic` judges — they checked tutor prose, not student answers, and that job does not vanish; it moves here.

  **Output redaction.** The grader returns two parallel feedback channels: `private_canonical` (raw answer, never shown to the student, never passed to `StudentTutor` for wrong/partial moves) and `student_safe_feedback` (rubric-shaped: `what_right`, `what_missing`, `first_misconception_redacted` — phrased so it directs the student without revealing the canonical). `StudentTutor`'s move prompts and the safe terminal templates receive `student_safe_feedback`, not `private_canonical`. Canonical suppression is necessary but not sufficient; redacted feedback fields are what actually prevents leak via paraphrase.

- **StudentTutor** — single tutor that conducts the conversation, guided by the science of learning. Given the grader verdict (when applicable), profile snapshot, transcript, current objective, KB chunks, and selected move, generates one response. Implements pedagogy via the move table in §4, not by meta-LLM principle selection. Never asserts correctness independently of the grader.

- **StudentProfiler** — single mastery tracker maintaining a per-student `profile_summary` (TEXT column on `StudentProfile`). For each session, summarizes what was learned, student strengths and challenges, questions posed (to prevent repeats), examples shown. Retains records for the last 10 sessions on `TutorSession`; older sessions archive to cold storage. The tutor reads the *last persisted* snapshot, not a live one.

Supporting services:

- **TutorEngine** — orchestrates grader, tutor, and profiler each turn. Selects moves deterministically from inputs; enforces safety valves (max turns per session/objective, force-close on verdict-less drift); handles state transitions and persistence. Primary objective: keep the student *actively doing* — answering, computing, choosing — with feedback. **No predefined lesson scripts and no rigid workflows**: the flow adapts to student responses and profile state. More turns for struggling students, fewer for advanced ones.

- **ContextManager** — assembles the input context for each stateless service call (transcript, profile snapshot, objective, KB chunks, verdict if any) and owns the typed Pydantic contracts (`TutoringContext`, `GradingRequest`, `GradingResult`, `ProfileUpdate`) that replace the 40-key untyped `engine_state` blob.

- **MediaService** — thin selector that picks lesson-scoped figures/media by KB similarity and injects a small top-N catalog per turn. Not an agent; scoped at the `Lesson` level, not per step. See R8.

- **ExitTicketService** — orchestrated by `TutorEngine` at end of session. Selects a subset of pre-authored `ExitTicketQuestion` rows from the lesson's `ExitTicket` bank (avoiding recently-attempted ones via `ExitTicketAttempt` history) and hands off the selected batch to the frontend modal for quiz rendering. Exit tickets are *not* posed through `QuestionTool` and are not part of the conversational turn flow — they render as a separate quiz UI. Each response is routed back through `StudentGrader` (the single grader for all paths), which runs `bank_grader` deterministic grading first (string/MCQ-letter match) and falls back to grounded adjudication only for free-text rubric questions. The standard verdict shape `{ verdict, private_canonical, student_safe_feedback, student_value, reasoning, citation }` is identical to conversational grading. The aggregate "did the student pass" is a derived count of `correct` verdicts compared against `ExitTicket.passing_score`, computed by `ExitTicketService` (or `TutorEngine`) — not a separate grader output. The current engine's "exit-ticket hold gate" and "force-clear after N hold cycles" are dropped per R6: when objective evidence is sufficient, `TutorEngine` transitions straight to the exit ticket.

Tools (called by the LLM via the function/tool-calling interface):

- **MathVerificationTool** — script-based math verifier. The LLM decomposes the problem into variables and a sequence of operations expressed as a constrained JSON DSL (whitelisted opcodes); a small Python interpreter executes the DSL and returns the canonical answer plus a step-by-step trace. See §7 item 5.

- **QuestionTool** — the existing `pose_question` tool, schema-tightened. Used for full assessment questions. Tool schema accepts `bank_question_id` OR `pre_pose_token` (and `choices` for MCQ) — NOT a raw `correct_answer` argument. See "Tool answer-key provenance" below.

- **InlineQuestionTool** — the existing `pose_inline_question` tool, schema-tightened the same way. Used for inline assessment checks.

**Design invariant:** assessment / learning questions (one specific verifiable answer expected) flow **only** through `QuestionTool` or `InlineQuestionTool` with backend-enforced provenance (`bank_question_id` for DB lookup, or signed `pre_pose_token` from `StudentGrader`'s pre-pose check). Reflective, hint, and Socratic prompts ("Is there another way to combine those?", "Why do you think that step works?") have no canonical answer and stay in prose. The conformance classifier draws the line.

### Tool answer-key provenance (backend-enforced)

The tool schemas do **not** accept a raw `correct_answer` argument from the LLM. Tool calls must reference one of two authoritative sources:

- `bank_question_id` — integer FK to an `ExitTicketQuestion` or curriculum bank row. The backend looks up the canonical from the DB. The LLM cannot manufacture this — invalid IDs reject the tool call.
- `pre_pose_token` — opaque signed token returned by `StudentGrader`'s pre-pose check (valid only within the current session, single-use). The backend validates the token against an in-memory cache and retrieves the canonical it stamped.

Without backend-enforced provenance, "tool-only posing" is theatre — a compliant-looking tool call with an LLM-manufactured `correct_answer` would still ship an unverified question. The schema closes that path.

### Tutor-prose claim verification

Student-answer grading and tool-only posing close the P1 surface for what the *student* does. The tutor's own explanatory prose still contains factual claims (causal explanations, definitions, world-knowledge) and arithmetic claims (worked-example computations, intermediate sums) that the deprecated `factual` and `arithmetic` judges used to check. That responsibility does not disappear; it is split:

- **Constrain by construction.** Move prompts for `explain`, `worked_example`, and `confirm_and_extend` instruct the tutor to ground claims in cited KB chunks (or to abstain). Arithmetic in tutor prose, when present, must be reproducible from the same DSL the math grader uses.
- **Adjudicate by classifier + grader.** The conformance classifier surfaces `contains_factual_claim` and `contains_arithmetic_claim` labels (part of the nine listed in the conformance check below). Surfaced claims route to `StudentGrader`'s tutor-claim adjudication path; arithmetic claims also route to `MathVerificationTool` for execution. A `contradicted` or `unverified` outcome rejects the response. A `supported` outcome passes.
- **Safe fallback.** Persistent failure on the same prose chunk falls back to a verdict-keyed safe template that contains only grader-supplied / KB-cited content, never improvised claims.

This is the runtime replacement for the deprecated `factual` and `arithmetic` judges. It is narrower than re-judging the whole response — only surfaced claims are adjudicated — and the labels are produced by the same classifier already running, so cost is bounded.

### No-verdict student claims

`StudentGrader` only produces a verdict when an assessment question is open. But P1 correctness flips can also occur in free conversation: the student volunteers a claim ("Photosynthesis happens in the mitochondria, right?"), and the tutor cheerfully agrees. There is no open question, so there is no grader verdict, so the per-verdict conformance rules above never fire.

**Invariant:** the tutor may not affirm or refute a student claim unless a grader verdict exists for that claim. Enforcement:

- The conformance classifier adds a `student_claim_present` label (whether the student's prior turn contained an assertion of fact). When this is true and no grader verdict was produced this turn, the conformance rule is: `affirms_correctness` and `refutes_correctness` must both be false; `surfaces_uncertainty` or "invite verification" must be true.
- If the tutor *wants* to confirm or correct, the path is: route the student's claim through `StudentGrader`'s tutor-claim adjudication path first (treating the student claim as the claim under adjudication), then generate the response with that verdict in hand. This produces a synthetic verdict on the fly so the verdict-keyed conformance rules can apply.
- Default behavior when neither path fires: treat as `unverified` — the tutor must reflect/probe, not adjudicate.

### Turn-by-turn flow

*Conversational turns only. The end-of-session exit-ticket path is described under `ExitTicketService` above — it does not flow through this diagram.*

```
Student message
   ↓
StudentGrader (only when an assessment question is open)
   - Math: LLM decomposes the problem into a JSON DSL → DSL-validation pass checks the
     decomposition against the visible problem text → MathVerificationTool executes the
     validated DSL in Python and returns the canonical + step-by-step trace
     (validation mismatch → verdict = unverified; see §7 item 8)
   - Non-math (curriculum content): KB-grounded adjudication with citations
   - Non-math (general world knowledge): Gemini with Google-search grounding
   - Returns: { verdict: correct | partial | wrong | unverified,
                private_canonical, student_safe_feedback,
                student_value, reasoning, citation }
     · private_canonical: raw answer, NEVER passed to StudentTutor on wrong/partial moves
     · student_safe_feedback: redacted rubric fields (what_right, what_missing,
       first_misconception_redacted) — safe to surface and to use in templates
   - `unverified` is a first-class verdict, not an error. Used when no grounding source
     can return a confident answer.
   ↓
StudentTutor generates ONE response, constrained by:
   - The verdict (full conformance rules below; summary here)
     · correct     → must affirm; must hand the floor back or transition
     · wrong       → must not affirm; must hand the floor back or transition
     · partial     → must NOT bare-affirm or bare-refute; must surface "what's right /
                     what's missing" shape; must hand the floor back or transition
     · unverified  → must surface the uncertainty; must NOT assert a verdict;
                     must hand the floor back or transition
     · no-verdict turn with student_claim_present → must not affirm/refute; must reflect,
                     probe, or route through grader adjudication; must hand the floor back
   - Profile snapshot from StudentProfiler (last persisted, not live)
   - Conversation transcript (full — no windowing in MVP; see §7 item 10)
   - Current objective + KB chunks
   - Allowed "moves" (see §4) — selected as a tool the tutor reaches for, not a script
   - Structural rule: any ASSESSMENT/LEARNING question (one specific verifiable answer
     expected) MUST be posed via QuestionTool or InlineQuestionTool. REFLECTIVE / HINT /
     SOCRATIC prompts ("Is there another way to combine those?", "Why do you think that
     step works?") remain in prose — they have no canonical answer by design.
   ↓
Structural conformance check (deterministic gates + one fast-LLM classifier + tutor-claim adjudication + safe terminal template):
   - Fast-LLM classifier returns nine binary labels per candidate response:
       { affirms_correctness, refutes_correctness, surfaces_uncertainty,
         contains_assessment_question_in_prose, hands_floor_back_or_transitions,
         contains_partial_feedback_shape, contains_factual_claim,
         contains_arithmetic_claim, student_claim_present (read from PRIOR turn) }
     Each is a narrow binary decision; `student_claim_present` is computed from the
     student's last message, the rest from the candidate tutor response.
   - Deterministic answer-leak check (scoped, not universal): runs under verdict=wrong,
     verdict=partial, and any turn where an open assessment question is unanswered.
     Normalized string + paraphrase match against `private_canonical`. Match → reject.
     `private_canonical` is also suppressed from the move prompt for wrong/partial moves;
     only `student_safe_feedback` is passed in. Under verdict=correct (the student has
     already produced the canonical), affirmative restatement of the answer is allowed.
   - Deterministic safety pre-screen: lexicon + classifier check for unsafe content
     (self-harm, adult, violence, jailbreak attempts). Inherits the existing safety judge's surface.
   - Deterministic state-coherence check: validates the response against the active
     `SessionRuntimeState` — open_question must still match, current move must be one
     the engine selected, last verdict referenced must be the verdict in hand.
     Catches state flips the transcript-as-input cannot guarantee (e.g. tutor claiming
     a question was answered when it wasn't). Cheap; runs every turn.
   - Tutor-claim adjudication: if `contains_factual_claim` or `contains_arithmetic_claim`
     is true, route the surfaced claim(s) through `StudentGrader`'s tutor-claim adjudication
     path. `contradicted` or persistent `unverified` → reject. (See "Tutor-prose claim
     verification" above for the full path.)
   - Praise filter (deterministic) under every non-`correct` verdict, stripping bare
     affirmative openers ("Correct!", "Right!", "Yes!") before the classifier sees the response.
   - verdict=correct    → reject if `refutes_correctness`;
                          reject if NOT `hands_floor_back_or_transitions`.
   - verdict=wrong      → reject if `affirms_correctness`;
                          reject if NOT `hands_floor_back_or_transitions`.
   - verdict=partial    → reject if `affirms_correctness` (bare affirmation isn't right);
                          reject if `refutes_correctness` (bare refutation isn't right either);
                          reject if NOT `contains_partial_feedback_shape`
                          (require "partly right because X / what's missing is Y" structure);
                          reject if NOT `hands_floor_back_or_transitions`.
   - verdict=unverified → reject if `affirms_correctness` or `refutes_correctness`;
                          reject if NOT `surfaces_uncertainty`;
                          reject if NOT `hands_floor_back_or_transitions`.
                          (Detail and rationale in §7 item 1.)
   - **No-verdict turns** (no assessment question was just graded):
                          if `student_claim_present` is true,
                          reject if `affirms_correctness` or `refutes_correctness`
                          (the tutor must not adjudicate without a grader verdict —
                          see "No-verdict student claims" above);
                          reject if NOT `hands_floor_back_or_transitions`.
   - All verdicts        → reject if `contains_assessment_question_in_prose`
                          (forces tool use for verifiable-answer questions);
                          reject if safety pre-screen failed;
                          reject if state-coherence check failed.
   - **`hands_floor_back_or_transitions`** is the relaxed replacement for an "ends with a
     question/directive" rule. Satisfied by: a directive to the student, a posed tool-question,
     an explicit topic close (`close_topic` move), an exit-ticket modal launch, or a UI
     transition signal. Administrative/system messages and end-of-session summaries are
     exempt — the rule guards against passive monologue, not legitimate closure.
   - On rejection: single retry with violated rules surfaced.
   - On retry still failing: fall back to a deterministic safe template keyed to verdict
     (see "Safe terminal templates" below). Never release a free-form response that failed
     conformance twice — for P1 risk, fail-safe beats fail-open.
   ↓
StudentProfiler updates profile_summary TEXT
   (async; batched every N turns or at session end — never per-turn synchronous)
   ↓
Persist
```

### Safe terminal templates

When conformance retry still fails, the response is replaced by a deterministic verdict-keyed template — never released free-form. Templates draw their content from the grader's **`student_safe_feedback`** fields (redacted to not contain the canonical) so they remain content-specific without leaking, not generic apologetics. The "next move" slot is filled from the next action `TutorEngine` would have selected — which might be a tool-posed question, a topic close, an exit-ticket transition, or a worked example.

- **correct** → "Yes — [student_safe_feedback.affirmation_phrase]. [next action from TutorEngine]"
- **partial** → "You've got part of it: [student_safe_feedback.what_right]. What's still missing: [student_safe_feedback.what_missing]. [next action from TutorEngine — usually a retry or scaffold]"
- **wrong** → "Not quite. [student_safe_feedback.first_misconception_redacted]. [next action from TutorEngine — usually a scaffold_hint]"
- **unverified** → "I want to check that with you before I'm sure either way. [next action from TutorEngine — usually an open probe]"
- **no-verdict turn with student_claim_present** → "Let's check that together rather than guess. [next action from TutorEngine — probe or route to grader adjudication]"

Templates are the **safety floor**, not the default path; most turns must pass conformance on first try. The template path is logged separately so its trigger rate is a quality signal (high rate = the move prompts or the classifier are mistuned).

### What gets deprecated / kept vs. today

| Component | Status |
|---|---|
| Unified judge (10-axis re-evaluation) | **Deprecate.** Removed from the runtime path; replaced by per-call verification at point of generation + conformance check. Module retained until benchmark confirms parity. |
| Regen ensemble (N candidates × N judges × 2 cycles) | **Deprecate.** Removed from the runtime path; replaced by single conformance retry plus safe terminal template. |
| `step_eval` and `coherence` judges | **Deprecate.** Step-completion logic moves into `TutorEngine`'s move state machine and safety valves; cross-turn coherence is handled by the deterministic state-coherence check in conformance plus transcript-as-input. |
| `factual` and `arithmetic` judges | **Replace, not delete — for tutor prose.** They check the tutor's *own explanations*, not student answers, and that responsibility does not move into `StudentGrader`'s student-answer grading. Replaced by the conformance classifier's `contains_factual_claim` and `contains_arithmetic_claim` labels, which route surfaced claims to `StudentGrader`'s tutor-claim adjudication path (and to `MathVerificationTool` for arithmetic). See "Tutor-prose claim verification" above. |
| `rule` judge | **Partially replace; not all coverage subsumed.** Tool-only posing covers NO_AUTHORING for assessment questions. DSL validation covers arithmetic rules on math turns. **Remaining surface kept as a thin deterministic rule check**: numeric mutation guard (the tutor must not silently change a problem's numbers between turns), and unauthorized authored-example detection (the tutor introduces a worked example whose numbers aren't from the bank). Cheap; runs every turn under math/worked-example moves. |
| `handoff` judge | **Replace, not delete.** The `hands_floor_back_or_transitions` classifier label takes over; rejected if missing. Keeps the current system's strongest active-engagement enforcement, with explicit exemptions for legitimate closure (close_topic, exit-ticket transition, session end). |
| `safety` judge | **Keep.** Runs as the deterministic + lightweight LLM safety pre-screen in the conformance check. P1 doesn't replace child-safety guards. |
| `answer_leak` | **Keep** as a scoped deterministic post-check inside conformance: normalized string + paraphrase match against `private_canonical` under verdict=wrong/partial and any unanswered-open-question turn; `private_canonical` is suppressed from move prompts for those moves and only `student_safe_feedback` is passed in. Under verdict=correct, affirmative restatement of the canonical is allowed. |
| `figure_ref` judge (deictic-phrase detection) | **Keep as a thin deterministic check.** "looking at the diagram" with no attached figure → reject. Cheap; runs every turn. Extended: when a figure IS attached and the tutor makes a quantitative or spatial claim about it ("the angle is 40°", "the line on the left"), the claim must be present in `figure_facts` for that asset — otherwise reject. Mitigates the figure-mismatch regression for explanatory prose, not just deictic references. |
| `figure_vision` judge (vision-model figure↔text match) | **Deprecate**, with the regression noted explicitly: the tutor will not catch figure-mismatch errors via vision at runtime. Mitigation: tutor cannot improvise figure-dependent assessment questions (see §7 item 9), and the extended `figure_ref` check covers explanatory quantitative/spatial claims. Revisit if pilot data shows residual mismatch matters. |
| `history` judge | **Replace, not delete.** Its role (cross-turn coherence) is partly subsumed by transcript-as-input plus the move state machine, but transcript-as-input does not *validate* consistency — it gives the model the opportunity to be consistent. Add a deterministic state-coherence check in conformance (open_question matches, current_move matches, last verdict matches) to catch state flips. |
| 460-line system prompt with internal contradictions | **Deprecate.** Out of the runtime path; replaced by short per-move prompts (§4). |
| 40-key untyped `engine_state` JSON | **Deprecate.** New sessions use typed Pydantic context objects; legacy column retained for read-only access to historical sessions. |
| `ConversationalTutor` (~12.6 KLOC single class) | **Deprecate.** Routing flips to the new engine; legacy class is no longer instantiated for new sessions *unless* the R10 ops kill switch is flipped, in which case new sessions route back to the frozen legacy class. |
| Praise filter (deterministic) | **Keep** as a conformance rule. |
| Repeated-question Jaccard | **Keep**, but moved to the synchronous in-session ledger (`SessionRuntimeState.posed_question_ledger`), not a profile-side check. See §7 item 7. |
| Numeric ±0.01 tolerance, MCQ letter match, bare-answer detection | **Keep** as deterministic gates inside `StudentGrader`. |
| `Question` abstraction + `bank_grader` | **Keep**, lifted into `StudentGrader` service surface. (Note: `answer_leak` is listed separately above — it lives in conformance, not in `StudentGrader`.) |
| `ast`-based student working analyzer | **Keep** in its existing student-input role. `MathVerificationTool` is a separate new component (see §7 item 5). |
| Media signal `\|\|\|MEDIA:N\|\|\|` parser | **Keep**, used by simplified `MediaService`. |

### How this kills the 3 unacceptable errors

**A & B (correctness flips):** The tutor never *independently decides* correctness. The verdict comes from `StudentGrader`, and the tutor's job is to render feedback in conformance with the verdict — a much easier and more reliable LLM task than judging correctness from scratch. For math, the path is: LLM decomposes the problem into a JSON DSL → **DSL-validation pass** checks the decomposition against the visible problem text → Python executes the validated DSL → tool returns the canonical plus step-by-step trace. The DSL-validation pass is itself partly LLM-mediated, but its decision space is narrow (does this decomposition compute the answer to *this* problem?) and a mismatch escalates to `unverified` rather than producing a confidently-wrong canonical. Equivalence cases that broke the current numeric ±0.01 check (algebraic equivalents, alternate units, mixed notation) are handled by a small comparator (SymPy / Pint / numeric ±0.01) over the canonical (from `MathVerificationTool`) and the student value (from the existing `ast`-based working analyzer) — not by string-matching surface forms. `MathVerificationTool` is a new build; the working analyzer keeps its student-side role; see §7 item 5 for why they are separate and §7 item 8 for the DSL-validation pass.

For the residual `unverified` verdict, the conformance check (above) prevents the tutor from substituting its own judgment. The tutor must surface the uncertainty in the response, not silently ad-lib a correctness call.

**C (incomplete questions):** Any **assessment / learning question** (one specific verifiable answer expected) is tool-only — `QuestionTool` for full questions, `InlineQuestionTool` for inline checks. The tool schemas refuse a raw LLM-supplied `correct_answer`; they accept only `bank_question_id` or `pre_pose_token` (see "Tool answer-key provenance" above). The canonical comes from one of: (i) the curriculum bank (teacher-authored answer key), (ii) `StudentGrader`'s pre-pose check. For pilot scope, both paths run the pre-pose check (`BANK_PREPOSE_RECHECK=on`, see §7 item 11) because the authoring-time gate that would otherwise back bank questions does not yet exist. The pre-pose check is not just "is there an answer key?" — it enforces the **student-visible derivability invariant**: the canonical must be re-derivable from the student-visible prompt plus visible artifacts alone (captured in `open_question.visible_context_at_pose`), with hidden KB chunks suppressed. A question whose canonical relied on context the student can't see fails the check and is refused, not posed. Combined with backend-enforced provenance, this closes the gap identified in §2: tool-only posing proves an answer exists; the visible-derivability invariant proves the *student* can answer it; and provenance proves the canonical didn't come from the response generator.

Reflective and Socratic prose prompts ("Why do you think that?", "Is there another way to combine those?") have no canonical answer by design and remain in prose — they cannot be "incomplete" the same way an unanswerable assessment question can.

Principle: **stop trying to catch errors after generation; make the errors structurally impossible at generation time.**

---

## 4. How StudentTutor implements science of learning — simply

Instead of meta-LLM "pick top 2 principles per turn", the tutor selects a **move** per turn from a small fixed set, and each move embeds the principles relevant to it.

| Move | Triggered when | Principles baked in |
|---|---|---|
| `pose_question` | Need active engagement; objective not yet probed | Active processing, retrieval practice |
| `confirm_and_advance` | Verdict=correct, mastery sufficient | Immediate feedback, cognitive load (don't over-teach) |
| `confirm_and_extend` | Verdict=correct, new angle worth exploring | Immediate feedback, desirable difficulty |
| `scaffold_hint` | Verdict=wrong, first or second attempt | Faded scaffolding, cognitive load |
| `name_misconception` | Verdict=wrong, third attempt on the same item | Specific feedback, prerequisite activation |
| `worked_example` | Topic new or student confused | Worked examples, cognitive load |
| `explain` | Concept needs framing before question | Cognitive load, prior-knowledge activation |
| `pivot` | `name_misconception` fired on a prior turn and the very next attempt is still wrong; or 4+ wrong attempts on the same item | Productive-struggle limit (avoid learned helplessness); within-session deferral of the failed item. *Not* algorithmic spacing — real cross-session spacing is out of scope for MVP (see §4 note below). |
| `close_topic` | Objective evidence sufficient | Mastery gating |

Move selection is **deterministic from inputs** — verdict + attempt count + profile state + objective coverage — not an LLM call. Each move is a *tool the tutor reaches for*, not a script: within a move, the focused prompt (200–400 tokens, not 10 KB) embeds the principles relevant to that move but leaves the tutor latitude in how it expresses itself. This is the deliberate counter to the cancelled pilot's "too scripted" failure mode — moves bound the *shape* of a turn, not its wording.

Why this beats meta-LLM principle selection:
- **Simpler.** No extra hop, no new failure surface.
- **Debuggable.** Every turn has a named move you can grep for in logs.
- **Testable.** Fixture tests per move.
- **Right layering.** Per-turn principles (active processing, immediate feedback, scaffolding, cognitive load) bake into move prompts. Cross-session principles (spacing, interleaving, mastery progression) belong in `TutorEngine`'s item-selection logic but are *not designed in this refactor* — see note below. Universal preamble principles (growth mindset, effort praise) stay as preamble.

**Note on spacing/interleaving — explicitly out of scope for MVP.** Real spacing requires a scheduling pass at session-start using `last_practiced` timestamps, retention estimates, and due-for-review flags — none of which exist as a runtime algorithm today. `pivot`'s "within-session deferral" is not that algorithm. Designing cross-session spacing/interleaving is a follow-up; the data fields it needs (`last_practiced`, attempt outcomes) are already in `StudentProfiler`'s scope, so the future scheduling pass has somewhere to read from. Don't claim spacing is delivered when it isn't.

---

## 5. Recommendations

| # | Recommendation | Status |
|---|---|---|
| R1 | Keep `LessonStep` as a *hint*, not a script. Preserve teacher authoring, content generation, exit tickets, offline pack, skill graph, benchmark. `TutorEngine` may skip, reorder, expand, or substitute steps based on the profile. | **Adopted.** |
| R2 | Replace per-student markdown files with a `profile_summary` TEXT field on `StudentProfile`. Keep structured tables for the dashboard; add markdown summary as a column. 10-session retention becomes a row-archival job on `TutorSession`. | **Adopted.** |
| R3 | Replace judges + regen with **grader-driven correctness + structural conformance**. `StudentGrader` is the single grader/adjudicator: math uses executable verification (DSL-validated then Python-executed); non-math uses grounded adjudication (KB / Google grounding with `unverified` escape). The grader serves student-answer grading, pre-pose checking, *and* tutor-claim adjudication on prose claims surfaced by the conformance classifier. Assessment-question posing is **tool-only** with backend-enforced provenance (bank ID or signed pre-pose token only). Regen ensemble replaced by a single conformance retry plus safe terminal templates. | **Adopted (revised from earlier "judges as shadow monitors" position — pilot evidence shows judge layer doesn't catch the errors).** |
| R4 | Phased rollout with v1/v2 flag. | **Withdrawn — pilot cancelled, no compat constraint.** |
| R5 | Type the inter-service contracts with Pydantic. `TutoringContext`, `GradingRequest`, `GradingResult`, `ProfileUpdate`. Delivers the stateless-services principle (every service receives full context as input; nothing implied from the prompt) and fixes CLAUDE.md "untyped JSON state" anti-pattern. | **Adopted — more important now with multiple services.** |
| R6 | Preserve specific deterministic gates as **safety floors**, not flow controllers. **Keep verbatim:** numeric ±0.01 tolerance and MCQ letter match (inside `StudentGrader`), Jaccard repeated-question detector (synchronous, in `SessionRuntimeState.posed_question_ledger` — not in `StudentProfiler`; `StudentProfiler` handles only cross-session repeat avoidance via the persisted summary), praise filter (inside conformance), bare-answer detection (math path). **Reshape:** hard-cap exchanges becomes a per-session and per-objective safety valve in `TutorEngine` rather than a step-level advance trigger. **Drop:** deterministic-correct fast-path advancement, minimum-floor exchange counts, exit-ticket hold gate, force-clear cycles — these were flow controllers that fought personalization. The pilot was too scripted; the deterministic layer must bound the conversation, not drive it. | **Adopted.** |
| R7 | `StudentTutor` implements science of learning via **moves**, not via meta-LLM principle selection. See §4. | **Adopted.** |
| R8 | `MediaService` is a thin selector, not an agent. Media scoped at `Lesson` level (not step), small KB-similarity-ranked top-N injected per turn. Media is a secondary concern; go simple. | **Adopted.** |
| R9 | Reuse the existing `Question` abstraction. Keep the `ast`-based working analyzer in its student-input parsing role. Build `MathVerificationTool` as a *separate* new component (LLM → JSON DSL → Python interpreter); do not conflate it with the analyzer. See §7 item 5. | **Adopted (revised — analyzer and MathVerificationTool are different responsibilities).** |
| R10 | Env-var kill switch (e.g. `NEW_TUTOR=off`) that routes new sessions back to a frozen legacy path. Rollback for *operations*, not for product backward-compat. Even with no legacy users, student-facing P1 risk justifies a one-flip emergency stop. | **Adopted (revised — kept as ops-reliability mechanism, not legacy-compat).** |

---

## 6. Blockers from the original analysis — updated status

**B1. LessonStep elimination has ~40+ file blast radius.** Resolved by R1 — keep the model, change runtime behavior.

**B2. Markdown-file student profiles are the wrong storage choice.** Resolved by R2 — `profile_summary` TEXT column instead.

**B3. Unified judge mid-rollout.** No longer a blocker — it failed in production. Deprecate alongside the rest of the judge stack; retain read-only until the new design is benchmark-validated.

**B4. Pilot regression risk.** Reduced but not eliminated — Seychelles cancelled and Tanzania inactive, so there is no *legacy-compat* requirement. However, operational risk to future students remains a concern: see R10 (kill switch retained as ops-reliability mechanism).

**B5. CLAUDE.md guidance encodes the unified-judge default (2026-05-18) and the "no multi-agent without measured bottleneck" rule.** Both are invalidated by the pilot evidence — CLAUDE.md is updated as part of the refactor, alongside the code. The pilot cancellation is the missing measurement.

---

## 7. Resolved design decisions

All decisions below are committed for MVP. Sub-decisions listed under each are follow-ups to be made during implementation, not gates on starting code.

1. **Non-math verification + how `unverified` is enforced — resolved.** `unverified` is a *first-class* verdict and the default fallback whenever the non-math grounded-adjudication path (KB grounding, Gemini Google-grounding) cannot answer with confidence. KB-grounded grading does NOT need to match the math path's accuracy bar; it needs to know *when to escalate to `unverified`* (confidence threshold tunable from pilot data; start conservative — when in doubt, return `unverified`). The pre-pose / verification path by question source:
   - **Teacher-authored curriculum bank questions** → for pilot scope, run `StudentGrader`'s pre-pose check (`BANK_PREPOSE_RECHECK=on`, see §7 item 11). Accept the latency hit until the authoring-time gate lands. Long-term, with authoring-time validation in place, the flag flips off and bank questions pose without runtime re-verification.
   - **Teacher-authored exit-ticket bank questions** → same as above. Pre-pose recheck on by default for pilot.
   - **Runtime-generated or transformed assessment questions** (e.g. tutor proposes a new check question on the fly) → require `StudentGrader`'s pre-pose check unconditionally — this stays on regardless of the bank flag, because runtime-generated questions never had any prior validation.
   - **Free-chat student-initiated questions** → verdict = `unverified` whenever grounding can't decide.
   - **Enforcement of "correctness comes from the grader, not the response generator" under `unverified`** — two layers cooperate:
     1. **Prompt-level instruction in StudentTutor.** The `unverified` branch of the move prompt instructs the tutor to surface the uncertainty explicitly ("I want to check this with you — let's work through it together", "I'm not certain about that one; let's verify"), to invite collaborative verification, and never to assert "correct" or "wrong".
     2. **Conformance classifier (fast LLM call).** The single conformance classifier from §3 returns nine binary intent labels: `{ affirms_correctness, refutes_correctness, surfaces_uncertainty, contains_assessment_question_in_prose, hands_floor_back_or_transitions, contains_partial_feedback_shape, contains_factual_claim, contains_arithmetic_claim, student_claim_present }`. Under verdict=`unverified`, the relevant labels are: reject if `affirms_correctness` or `refutes_correctness` is true; reject if `surfaces_uncertainty` is false; reject if `hands_floor_back_or_transitions` is false (active engagement, with exemptions for close_topic and exit-ticket transitions). Regex/keyword matching is rejected for the affirm/refute layer because paraphrases ("nailed it", "spot on", "not wrong", "you're not far off") slip past lexicons and double-negatives produce false positives. The classifier's decision space is narrow (binary intent labels on a short response) and its failure surface is much smaller than full correctness judgment. This is *deliberate* LLM-judges-LLM surface, bounded to a narrow, auditable decision.
   - The conformance retry surfaces the violated rule; the tutor rewrites once. No ensemble.

   *Sub-decisions:* exact KB-confidence threshold for escalating to `unverified`; whether the threshold differs between curriculum-content grounding and Google grounding.

2. **`StudentProfiler` write cadence — resolved.** Async post-response, batched every N turns or at session end — never per-turn synchronous. The tutor reads the *last persisted* snapshot, not a live one. *Sub-decision:* exact N (start with end-of-session only; add mid-session batching if profile drift hurts within-session adaptation).

3. **`TutorEngine` safety valves — resolved (recommendation).** Concrete caps so unbounded flow can't spin:
   - **Max turns per session: 40.** Typical full lesson length; current per-step caps × typical step count land here.
   - **Max turns per objective: 12.** Gives personalization room (the current per-step cap of 10 was tight); pivot or close_topic should fire well before this.
   - **Force-close objective after 6 consecutive turns without a grader verdict.** Catches free-chat drift where neither side returns to an answerable question.
   - **Move-level limits stay in moves**, not as engine-level safety valves: `pivot` already triggers at 4+ wrong on the same item; `close_topic` triggers when objective evidence is sufficient. The engine caps above are the *outer* fence — moves should fire first under normal conditions.

   *Sub-decisions:* tune all four numbers from pilot data; the recommendation is a starting point, not a contract.

4. **What gets imported from the deprecated engine — resolved.**
   - **Pull forward as utilities (unchanged in role):** deterministic gates, `Question` abstraction, `bank_grader`, `ast`-based working analyzer (in its student-input role), praise filter, Jaccard repeat detector, media-signal parser.
   - **Replace, not delete (responsibility moves to a new home):** `factual` and `arithmetic` judges → tutor-claim adjudication path inside `StudentGrader` + classifier labels; `handoff` judge → `hands_floor_back_or_transitions` classifier label; `history` judge → deterministic state-coherence check; `figure_ref` judge → extended deterministic check covering both deictic phrases and quantitative/spatial claims vs `figure_facts`; `rule` judge → thin deterministic numeric-mutation + authored-example-provenance check. Old judge modules can be deleted once the new homes are benchmark-validated.
   - **Deprecate outright:** unified judge, regen ensemble, `step_eval`, `coherence`, `figure_vision`, the 460-line system prompt, the 40-key `engine_state`, the `ConversationalTutor` class itself — retained read-only until the new design is benchmark-validated, then removed.
   - **Keep verbatim:** `safety` judge (as the conformance pre-screen), `answer_leak` (as the scoped conformance post-check).

5. **MathVerificationTool implementation — resolved.** Build `MathVerificationTool` as a new component; do NOT extend the existing `ast`-based working analyzer to do the verification job. They are different responsibilities at different points in the flow:
   - **Working analyzer** parses *student input* (natural-language working / answers) into structured numeric values for grading. Student-side. Kept in its existing role.
   - **`MathVerificationTool`** decomposes the *problem* into an executable form that yields the canonical answer. Problem-side. New build.
   The grading pipeline composes three small pieces: (i) `MathVerificationTool` → canonical from problem, (ii) working analyzer → student value from student prose, (iii) comparator → equivalence (SymPy for symbolic, Pint for units, ±0.01 for plain numeric).

   **For the decomposition itself, the LLM emits a constrained JSON DSL**, not raw Python. The DSL has a small whitelisted opcode set (add, sub, mul, div, pow, sqrt, log, trig, eq, solve, …) plus a variables block. A small interpreter walks the DSL and produces the canonical plus a step-by-step trace (each opcode = one trace step, for free). This is materially safer than `exec()` on LLM-emitted code, easier to unit-test, and the LLM-decomposes-then-Python-executes approach already implies a DSL — there is no benefit to letting the LLM emit arbitrary Python.

   *Sub-decisions:* exact opcode set; comparator library choices (SymPy vs lighter symbolic check; Pint vs hand-rolled unit map); how the trace is rendered for student-facing explanations vs internal logs.

6. **Runtime state persistence — resolved.** "Stateless services" describes the *service-call contract* (every call gets full context in), not the absence of session state. The 40-key untyped `engine_state` JSON is replaced by a typed `SessionRuntimeState` Pydantic model persisted as a JSONB column on `TutorSession`. Explicit fields (additive — new fields land as schema migrations, not free-form keys):
   - `open_question` — the question currently awaiting a student answer, if any. Fields: `id`, `source` (bank | pre_pose_token), `private_canonical` reference (not exposed to the response generator), and **`visible_context_at_pose`** — a snapshot of what the student could see when the question was posed (exact question text, attached media IDs, allowed prior-turn window). The student-visible derivability invariant is checked against this snapshot, not against live conversation that may have changed after subsequent hints. `null` when no question is open.
   - `attempts_on_open_question` — counter, drives `scaffold_hint` → `name_misconception` → `pivot` move thresholds.
   - `posed_question_ledger` — synchronous in-session ledger (see item 7); distinct from the async profile.
   - `objective_progress` — per-objective evidence: attempts, correct count, last-verdict timestamp.
   - `media_shown` — set of `MediaAsset` IDs already injected, to avoid repeats.
   - `remediation_state` — active misconception label if `name_misconception` fired, cleared on `pivot` or `close_topic`.
   - `current_move` and `move_history` — last move executed and a short stack for the move state machine.
   - `unverified_run_length` — consecutive `unverified` verdicts; feeds the safety-valve "force-close objective after 6 verdict-less turns" (§7 item 3).
   - `safety_valve_counters` — turns-this-session, turns-this-objective, verdict-less-streak.
   - `resume_marker` — last-stable point for session resumption.

   `ContextManager` reads and writes `SessionRuntimeState` between turns; service calls receive a frozen snapshot via the typed `TutoringContext` / `GradingRequest` / `ProfileUpdate` contracts.

7. **Synchronous posed-question ledger vs. async StudentProfiler — resolved.** Repeat-question prevention is *not* delegated to the async profile. Within a session, every question posed (bank, inline, runtime-generated) is appended to `SessionRuntimeState.posed_question_ledger` (canonicalized stem + Jaccard signature) **synchronously**, before the question is shown. The pre-pose check rejects a question that matches the ledger. The async `StudentProfiler` provides *cross-session* repeat prevention via the persisted profile summary; the synchronous ledger handles *within-session* repeats. Two different layers, two different cadences.

8. **Math DSL validation against problem text — resolved.** Before Python executes the LLM-emitted decomposition, a separate validation pass checks that the DSL's variable bindings and operations are consistent with the **student-visible problem statement**. Implementation: a small structured check that maps DSL variables back to numbers/quantities named in the problem text, plus a focused LLM call ("does this decomposition compute the answer to *this* problem?") for the cases the structured check can't decide. On mismatch the verdict escalates to `unverified` rather than execute a confidently-wrong decomposition. The validation pass is part of `StudentGrader`'s math path, not optional. *Sub-decisions:* exact division of labor between structured check and LLM check; how strict the structured check is on free-form word problems.

9. **Figure / vision handling — resolved (recommendation: keep it simple).** Figures and media are a secondary concern, consistent with R8. The tutor cannot improvise figure-dependent assessment questions: figure-dependent questions only flow from the curriculum bank, validated at authoring time. `MediaService` selects figures purely as illustrations to accompany the tutor's prose — never as the subject of a question the tutor poses on its own. The runtime keeps the **extended `figure_ref` check** (see §3 deletion table): the deictic-phrase guard *plus* a quantitative/spatial claim check against `figure_facts` for any attached figure. So claims like "the angle is 40°" or "the line on the left" must match the figure's recorded facts; otherwise the response is rejected. The vision-model `figure_vision` judge is dropped. Residual regression: any figure-claim correctness that depends on facts NOT captured in `figure_facts` (e.g. subtle visual properties never extracted at authoring time). Mitigation depends on `figure_facts` coverage at authoring time. Revisit if pilot data shows residual mismatch matters.

10. **Conversation transcript — resolved.** Send the full transcript to the tutor every turn. No windowing, no summarization at generation time in MVP — same as the existing engine. Cost/latency is acceptable at pilot scale and the alternative (phase-gated summarization) risks reintroducing the over-scripted failure mode.

11. **Bank question re-verification — resolved (Option B fallback).** The runtime design ideally skips re-verification for teacher-authored bank and exit-ticket questions on the assumption they were validated at authoring/publish time against the student-visible derivability invariant. **That authoring-time gate does not currently exist** in `apps/curriculum` or the exit-ticket authoring path, and **the pilot relaunch timeline does not allow waiting for it.**

    **Decision:** ship the runtime with **bank-question re-verification enabled** — `StudentGrader`'s pre-pose check runs on bank/inline/exit-ticket questions too, accepting the per-pose latency hit. Gated by a config flag (`BANK_PREPOSE_RECHECK=on` default) so the recheck can be turned off later once the authoring-time gate lands. The recheck is the same derivability check used for runtime-generated questions; no new code path.

    *Follow-up (not blocking pilot):* the curriculum team implements the authoring-time derivability check; once it lands and back-validates the existing bank, the runtime flag flips to off and the latency is reclaimed. Tracked separately from the tutoring refactor.

---

## 8. TL;DR

The P1 errors (defined in §2) come from (i) LLM-judges-LLM correlation where critic and generator share failure modes, (ii) no path that validates the *visible question text* is complete enough for the student to answer (the gap is narrower than "no guard at all" — see §2 Error C), and — surfaced in review — (iii) unchecked correctness in the tutor's *own explanatory prose* and the *provenance* of tool-call answer keys. Fix by:

1. **Move correctness off the response generator and onto a dedicated grader/adjudicator.** Math = executable verification (LLM-emitted DSL validated against the visible problem text *then* executed in Python). Non-math = grounded adjudication (KB / Google grounding with a conservative `unverified` escape valve — not "computation"). The tutor never *independently decides* correctness; it renders feedback in conformance with the grader's verdict.
2. **Forbid prose-posed assessment questions** — tool-only with backend-enforced provenance. Tool schemas refuse raw LLM-supplied `correct_answer`; they accept only `bank_question_id` (DB lookup) or signed `pre_pose_token`. Enforce the student-visible derivability invariant on every runtime-generated question. For pilot scope, the same check runs on bank questions too (`BANK_PREPOSE_RECHECK=on`) because the authoring-time validation gate does not yet exist; flag flips off when it lands.
3. **Verify the tutor's own prose claims**, not just student answers. Conformance classifier surfaces `contains_factual_claim` and `contains_arithmetic_claim`; surfaced claims route to `StudentGrader`'s tutor-claim adjudication path (and arithmetic through `MathVerificationTool`). This is the runtime replacement for the deprecated `factual` / `arithmetic` judges — that responsibility does not vanish.
4. **No-verdict no-assertion invariant.** When the student volunteers a claim outside an open assessment question, the tutor may not affirm or refute it without routing the claim through grader adjudication. Default behavior is `unverified` — reflect/probe, never adjudicate from the generator.
5. **Replace the regen ensemble with a fast-LLM conformance classifier (nine binary labels) + deterministic checks (state-coherence, answer-leak scoped to wrong/partial, safety, praise filter, figure-claim rule) + verdict-keyed safe terminal templates.** All four verdicts plus the no-verdict-with-claim case have explicit rules. Single retry; on second failure, fall back to a safe template that draws from redacted `student_safe_feedback` fields (canonical never leaks).
6. **Don't drop unrelated guards.** Safety, handoff (active engagement), answer-leak, figure-deictic, and a thin rule check for numeric mutation / authored-example provenance all survive as conformance rules. Only correctness *judging* of student answers is replaced.
7. **`StudentTutor` implements science of learning by selecting a *move* per turn** (deterministic from inputs) with a focused prompt. Moves are tools the tutor reaches for, not scripts.
8. **State is typed, not absent.** `SessionRuntimeState` (typed Pydantic on `TutorSession`) holds `open_question` (with `visible_context_at_pose` snapshot), attempts, synchronous posed-question ledger, objective progress, media shown, safety-valve counters, resume markers.
9. **Keep an ops kill switch.** Reliability for student-facing P1 risk, not legacy compat.
10. **Deprecate the old engine.** Routing flips to the new design; legacy modules retained read-only until benchmark confirms parity, then removed.
