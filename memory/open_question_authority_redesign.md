# Design memo: relocate `open_question` authority off the optional tool path

**Status:** Proposal — investigation complete, trace analysis pending (see §10).
**Date:** 2026-05-29.
**Area:** `apps/tutoring/v2/` — `move_router`, `tutor_engine`, `student_tutor`, `context_manager`, `tools/pose_question`, `services/safety_gates`, `services/conformance_check`.
**Prompted by:** the GEO-S5 "Compass Directions and Bearings" (Lesson 1426) prod session — broken resume + stacked-question turn — and the observation that `open_question`/`verdict` desync drives a recurring class of P1 bugs that five successive conformance gates have only contained, not cured.

---

## 1. The flaw, in one sentence

A question can be **shown to the student without `open_question` being committed**, because the actor that authors the student-facing text (the `StudentTutor` LLM) and the actor that records "a question is pending" (the server's Phase-B commit) are different, fire at different times, and the commit is on an **optional** path.

`open_question` is simultaneously (a) the runtime record of *what was posed* and (b) the **control variable that drives routing**. When (a) and the rendered text diverge, routing breaks.

---

## 2. How `open_question` couples to routing today

- **Written** by the server in Phase B: `ContextManager.commit_pending_pose` (`context_manager.py:69-105`) — only after the `pose_question` tool fired *and* the conformance gates passed. It hydrates `OpenQuestion{source, id, canonical, rendered_stem, answer_type, visible_context_at_pose}` from the selected bank `LessonStep`.
- **Read** by the router: `build_router_request` (`move_router.py:604,662-679`) projects `runtime_state.open_question` into the boolean `open_question_has_pending`, which selects the **case**:
  - pending → `case="answer_attempt"`, `verdict_needed=True` → grade the input; move keyed by verdict (`moves_by_verdict`).
  - not pending → `case="opening_turn"`/`help_request`, `verdict_needed=False` → **don't grade**; `move="explain"`.
- The fail-soft default encodes the same branch (`_fallback_decision`, `move_router.py:828-844`).

So "is the student's message an answer to grade?" is decided **entirely** by whether the server committed `open_question` last turn — independent of whether the student actually saw a question.

The pose path itself is optional: every non-terminal move is pose-capable with `tool_choice="auto"` (`student_tutor.py`), so the LLM *may or may not* call the tool, and may *additionally* author questions in prose.

---

## 3. Evidence this is the root, not a lesson quirk

- **Broken resume (prod GEO-S5).** The EXPLAIN opener authored a conceptual question in prose ("What compass direction lies exactly between north and east?") — not a bank step, so no `PendingPose`, so no `open_question`. On resume `v2_resume_dispatch` (`routing.py:290-327`) found `open_question is None` and emitted the dead-end "Welcome back — let's keep going from where we paused." The student then answered "north west" (wrong for that opener), but with no `open_question` the router took the non-answer branch and never graded it → silent pivot to the first bank question.
- **Five gates, same wound.** `_GATE_ORDER = (curriculum_fidelity, stem_duplication, safety, figure_ref, answer_leak)` (`safety_gates.py:844`). Three of these — `curriculum_fidelity`, `stem_duplication`, `answer_leak` — plus the `contains_assessment_question_in_prose` conformance label and the Haiku `question_extractor` exist **solely to force the LLM's prose to agree with the server's `open_question`**. `stem_duplication` (commit eb29e1d, 2026-05-28) is the *fifth* such patch; its own commit message notes the prior gate "did not catch this." This is a containment wall around a divergence the architecture permits, not a fix.
- **Stacked questions still leak (Issue 3).** Session 100 T1560: the LLM re-posed a full bank MCQ inside explanatory prose; the `question_extractor` (an LLM classifier with an explicit "options listed during explanation don't count" carve-out, `grader_prompts.py:733-734`) returned `action_count:1` and missed it. `is_verifiable_prose_question` also can't catch a mid-prose MCQ because the rendered text ends with the options, not `?` (`conformance_check.py:226`).

---

## 4. The invariant we actually want

> **The routing signal is derived from the transcript** (what the student actually saw), so it cannot disagree with the screen — regardless of how a question got there.

Today the signal is a *stored flag written at pose time* that the gates try to keep in sync with the rendered text, after the fact. The fix is to stop maintaining a drift-prone denormalized flag and instead **derive `open_question` from the single source of truth (the transcript) each turn** (§5). The orchestration skill frames the principle: correctness/trust are **system properties**, not behaviours you hope a generator/critic loop catches — and a critic sharing the writer's blind spot (LLM writer + LLM extractor) misses the same cases (Cemri et al. error amplification). Deriving from ground truth removes the disagreement entirely; the rejected §5b alternative removes it the other way (control the write path).

---

## 5. Proposal — derive `open_question` from the transcript (primary)

**Clarified 2026-05-29.** The chosen approach is *not* "control the write path." It is: **`open_question` becomes derived state the router recomputes each turn by reading the conversation transcript** — presence + identity of a pending question, no canonical. The transcript is the single source of truth for what the student saw, so there is no separate flag to drift.

Per turn:

1. **Router** (LLM) — now given the **transcript** (today it is deliberately counter-driven and does not see it; this is the main wiring change). It answers one thing: *is there a question on the table the student is expected to answer right now, and what is its text?* It sets `open_question` = **presence + verbatim question text** (`{has_pending, question_text}`) — **no `lesson_step_id`, no canonical, no bank lookup.** `open_question_has_pending` continues to drive `verdict_needed` exactly as today (`move_router.py:662-679`) — but its value is now *perceived*, not *committed*.
2. **Grader owns bank-matching end-to-end** (decided 2026-05-29). Given the `question_text`, the grader matches it back to a bank `LessonStep`, resolves the canonical, and grades — the deterministic fast-path (793958f) applies on a bank hit. No bank match → grounded adjudication with `unverified` as the honest fallback. **All identity-resolution and canonical provenance live in one place (the grader); the router never matches the bank or authors a canonical.** Keeps the matching logic un-split (Rule of Three) and the router's perception sub-task tightly scoped.
3. **StudentTutor** renders the turn. It may pose (bank or, where allowed, tutor-authored) — the router will *perceive* whatever it poses next turn, so authoring-path no longer needs policing.

**Why this beats the stored-flag approaches.** It is robust to *every* way a question reaches the screen — bank tool, prose, EXPLAIN opener, future inline — uniformly, because it reads ground truth instead of trusting a write-time flag. The silent-pivot ("north west") and the resume dead-end both vanish: on resume the router reads the transcript, sees the unanswered question, and routes to grade. Fits the project bias — LLM perception over brittle flags, gates as safety floors not flow-controllers.

**The gap it must close — presence ≠ canonical.** Detecting "a question is open" says *whether to grade and which*, not *the answer*. For the ~90% MCQ/bank case, P1-safety requires the canonical, so canonical resolution moves to the grader (step 2) against the bank. The only residual P1 exposure is grading a non-bank tutor-authored question ungrounded — a pre-existing risk, now *explicit* (an `unverified` verdict) rather than hidden behind a flag that silently dropped the question.

**Cost.** Perception is non-deterministic (the router may misjudge "still open?" on a partial answer or rhetorical question), but it is recomputed every turn so errors self-correct, and it is almost certainly more accurate than today's commit-flag. Marginal cost = feeding the router the transcript.

### 5b. Alternative considered — commit-before-render (rejected as primary)

The inverse: keep `open_question` a *stored* fact but make the write path provably consistent — engine selects a bank step post-grade, `commit_pending_pose` writes `OpenQuestion{canonical,stem,...}` *before* render, `StudentTutor` reduced to a pure renderer that cannot author assessment, pose tool + two-phase commit retired. This *prevents* prose questions rather than *perceiving* them. Sound, but more machinery and brittle to any new path that puts a question on screen. Its one advantage — canonical is committed atomically with presence — is recovered in the primary design by resolving canonical in the grader. Kept here as a fallback if transcript-derivation proves too noisy in practice.

---

## 6. Change surface (primary design)

| File | Change |
|---|---|
| `services/move_router.py` + `router_prompts.py` | Feed the router the **transcript** (it is counter-driven today). Router output gains an `open_question` perception: `{has_pending: bool, question_text}` — verbatim text only, no id, no bank lookup. `open_question_has_pending` still drives `verdict_needed` (`move_router.py:662-679`) — value now perceived, not read from a committed flag. |
| `services/student_grader.py` | **Owns bank-matching end-to-end.** Given `question_text`: match to a `LessonStep`, resolve canonical, grade (793958f fast-path on a bank hit). No bank match → grounded adjudication / `unverified`. All identity + canonical logic lives here. |
| `services/context_manager.py` | `open_question` becomes *derived/cached per turn* from the router perception rather than written only by `commit_pending_pose`. Keep the typed `OpenQuestion` dataclass shape; `commit_pending_pose` may stay for the bank-tool path but is no longer the sole authority for the routing signal. |
| `apps/tutoring/v2/routing.py` | `v2_resume_dispatch` runs the router over the transcript instead of reading a stored flag → detects the last unanswered question → routes to grade. The `open_question is None` dead-end disappears. |
| `services/safety_gates.py` | `curriculum_fidelity`, `stem_duplication`, `all__no_assessment_in_prose` stop being flow-controllers (prose questions are now perceived, not prevented) → retire/demote. `answer_leak` re-scoped, `safety` + `figure_ref` stay as safety floors (§7). |
| `services/student_tutor.py` / `tools/pose_question.py` | Pose tool + two-phase commit + Phase-A loop + `MAX_POSE_ATTEMPTS_PER_TURN` no longer load-bearing for correctness (the router perceives whatever is posed). `select_pose_slot`/`_render_bank_stem_with_options` stay as the *bank question source* the tutor draws from. |

Mirrors codebase patterns: `OpenQuestion` stays a typed `contracts/` dataclass (the "typed contract" rule; avoids the untyped-`engine_state` anti-pattern). No new agent, no new service — this **removes** the desync-reconciliation machinery (workflow-first; agent-count-is-a-cost). The router gains a responsibility (perceive open-question state) but stays a single LLM call.

---

## 7. What retires, what stays

**Retires / demotes** (a perceived prose question is no longer a bug to prevent):
- `curriculum_fidelity` (both paths) — the EXPLAIN-opener-authors-a-question case is now *perceived and graded*, not a violation. The whole "verifiable prose question = corruption" premise dissolves.
- `stem_duplication` (eb29e1d) and `all__no_assessment_in_prose` — same: provenance-policing flow-controllers, no longer load-bearing.
- The pose tool's two-phase commit / Phase-A loop / `MAX_POSE_ATTEMPTS_PER_TURN` — no longer needed for correctness.

**Stays:**
- **`safety`, `figure_ref`** — genuine safety floors, unrelated to assessment provenance.
- **`answer_leak`** — re-scoped: the tutor still must not reveal the canonical the grader resolved. Keep.
- **One-question-per-turn check (Issue 3)** — *orthogonal* to this redesign; it's a cognitive-load constraint (Principle #5), not a provenance one, so it survives whatever we do with `open_question`. **Decided 2026-05-29 (belt-and-suspenders, not replace):** a deterministic floor ("≥2 non-reflective `?`-sentences / MCQ option-block ⇒ flag", catches the session-100 T1560 shape the Haiku extractor missed) PLUS the re-wired Haiku `question_extractor` as the ceiling (generalises to imperative/fill-in/"now you try" prompts the regex can't see + the active-end rule). Pure-deterministic was rejected — it optimises for one bug class and contradicts the "prefer fast Haiku over regex" guideline; LLM-only let the T1560 carve-out through. Lives in a dedicated `one_question_per_turn` gate (`safety_gates.run_one_question_check`), separate from provenance, so curriculum_fidelity can demote (step 4) without touching it.

Net: 5 provenance gates + 1 conformance classifier + 1 question-extractor → ~2 safety floors + `answer_leak` + 1 deterministic one-question check. The simplification the orchestration skill prescribes — perceive state with the LLM that already runs every turn, rather than police it with a critic loop sharing the writer's blind spot.

---

## 8. Why this is sound orchestration (skill-grounded)

- **Workflow over agent.** The tutor pipeline is a workflow (predefined path), not a dynamic agent. The proposal makes pose-commit a deterministic code path instead of model-directed tool use — strictly more workflow-shaped, which is the recommended default. No agent added.
- **Error containment.** Cemri et al.: unstructured handoffs amplify errors. Today's handoff ("LLM may pose via tool, may pose in prose, gates reconcile") is exactly the unstructured kind. A deterministic commit removes the handoff ambiguity.
- **Don't trust a critic with the generator's blind spot.** The current `question_extractor` (Haiku) and `contains_assessment_question_in_prose` (fast LLM) miss what the writer LLM produces. Structural truth (engine-committed stem) beats a same-family critic.
- **Cap/structure retained.** Keep the trace spans (`tool.commit`, `gate.*`, `v2_trace`) — failure analysis needs the trace, not the answer.

---

## 9. Risks & open questions

1. **Perception noise.** The router judges "is this question still open?" each turn — it can misread a partial answer, a multi-part question, or a rhetorical question. Recoverable (recomputed next turn), but a single misperception can mis-route one turn (e.g. grade a non-answer, or skip grading a real answer). Need eval coverage on partial-answer and rhetorical-question transcripts.
2. **Non-bank canonical grounding (the real P1 exposure).** When the open question is tutor-authored with no bank match, the grader adjudicates ungrounded. Mitigation: prefer bank questions; require the grader to return `unverified` (not a confident verdict) when it can't ground a canonical, and have the tutor treat `unverified` as "ask for working / re-pose," never as correct/incorrect. This is the one place the old commit-before-render design was stronger (canonical committed atomically) — watch it.
3. **Bank-matching (resolved 2026-05-29: grader owns it end-to-end).** Router emits only `{has_pending, question_text}`; the grader does all `LessonStep` matching + canonical resolution. Single home for the logic (Rule of Three). Open sub-risk: the grader's text→bank match must be robust to the tutor paraphrasing a bank stem (line-wrap, light rewording) — reuse the normalized-substring machinery (`find_prose_stem_duplicates`) and fall to `unverified` rather than mis-match a different step.
4. **Re-grading / state flips.** If perception flips "open → not open → open" across turns on the same question, ensure attempt counters (`attempts_on_open_question`, `wrong_attempts_on_open_question`) don't double-count or reset wrongly.
5. **Router prompt growth.** Feeding the transcript every turn enlarges the router prompt (latency/cost) and risks diluting move selection. Project stance is latency/cost-tolerant, but cap transcript window (last N turns) and keep the perception sub-task tightly scoped in the prompt.
6. **Migration / engine_version stickiness.** In-flight sessions carry `engine_version`; gate the change so existing v2 sessions don't observe a mid-session contract change.
7. **Issue 2 (data, not engine).** L1426 idx3 "Convert NE…" is authored `short_numeric`, not MCQ — independent content fix, not part of this redesign.

---

## 10. Trace analysis (to justify scope) — DONE 2026-05-29

**Dataset:** 302 traced tutor turns across 40 local v2 sessions (all non-synthetic; mixed lessons). Prod sessions (incl. the GEO-S5 screenshots) are *not* in the local DB, so prod-only effects like the resume dead-end show 0 locally — local sessions don't resume with `open_question is None`. Some sessions predate the move-router prune (the legacy `pose_question` move appears 32×).

**Findings:**

| Signal | Count | % of 302 |
|---|---|---|
| **Retry used** | 79 | **26.2%** |
| **Fallback used** | 70 | **23.2%** |
| `contains_assessment_question_in_prose` = true | 94 | 31.1% |
| ...of which **zero conformance violations fired** ("leak surface") | 53 | **17.5%** |
| Assessment-provenance/flow gate fires (`all__no_assessment_in_prose` 15, `rule_check` 9, `active_end_required` 4, `one_question_per_turn` 4, `open_question_stickiness` 3) | ~35 fires | — |
| True safety-floor fires (`figure_ref` 2, `state_coherence` 6; `safety` 0) | 8 fires | — |
| Stacked-question turns caught (`action_count≥2` or `one_question_per_turn`) | 4 | 1.3% |
| Resume dead-end turns (local) | 0 | 0% |
| Student answers after a question-bearing turn that recorded **no verdict** (silent-pivot *proxy*) | 65 / 186 | **34.9%** |

**Interpretation:**

1. **Churn is the headline.** ~1 in 4 turns retries (26%) and ~1 in 4 hits a fallback (23%). This is the cost of reconciling LLM-authored text against the server's `open_question` after the fact. A deterministic commit-before-render removes the reconciliation, so most of this churn should disappear — the single biggest argument for the redesign.

2. **Large unguarded leak surface.** Assessment text appears in prose on 31% of turns, and on **17.5% of all turns it does so with no gate firing at all**. Some is legitimate (engine-appended bank stem), but it confirms the gates are not a tight boundary — exactly the "critic shares the generator's blind spot" failure. A structural invariant (only the committed stem renders) collapses this surface.

3. **Provenance/flow gates do ~4× the work of true safety floors** (~35 vs 8 fires). That asymmetry is the whack-a-mole: most gate activity is reconciling assessment provenance, which the redesign makes structural. `safety`/`figure_ref`/`state_coherence` stay; the rest mostly retire.

4. **Silent-pivot proxy (34.9%) is an upper bound — caveat.** It counts any ungraded student turn after a question-bearing turn, which *correctly* includes legitimate non-answers ("ready for a question", "i don't understand, walk me through" → correctly no verdict, routed to help/opening). The *concerning* subset is **substantive answer attempts that went ungraded** — visible in the examples (e.g. session 79: student typed full "Evaporation — the process where solar energy…" and "Condensation occurs when water vapour…", both got `next_move=pose_question` with `verdict=None`). These are the P1-adjacent cases: a real answer, no grade, tutor just poses the next question. The exact count needs an answer-attempt classifier; qualitatively the pattern is present and matches the prod GEO-S5 "north west" silent-pivot.

5. **Stacked questions (b) are under-detected, not rare.** Only 4 caught, but T1560 (session 100) proves the Haiku `question_extractor` misses mid-prose MCQs (carve-out at `grader_prompts.py:733-734`). The 1.3% is a floor; true rate is higher. This validates keeping a *deterministic* renderer-fidelity check (§7) rather than trusting the classifier.

**Decision (per §10 rule):** the churn (26%/23%) + leak surface (17.5%) + silent-pivot pattern dominate; resume dead-end is real but prod-only. → **Commit-relocation is the high-leverage primary change**; the deterministic renderer-fidelity check is a required secondary (stacked-question class is orthogonal and under-detected). Most provenance gates retire; two safety floors + `answer_leak` + one renderer check remain.

---

## 11. Recommendation

Proceed with **transcript-derivation (§5)** as the primary design: the router reads the transcript and sets `open_question` = presence + identity (no canonical); the grader resolves the canonical from the bank by id/match; the provenance gates (`curriculum_fidelity`, `stem_duplication`, `all__no_assessment_in_prose`) demote from flow-controllers; `safety`/`figure_ref`/`answer_leak` + a deterministic one-question check remain. Fix resume by running the router over the transcript. The trace analysis (§10) confirms the desync class (26% retries, 23% fallbacks, 17.5% unguarded leak surface, the silent-pivot pattern) dominates over the orthogonal stacked-question class — so this is the high-leverage change.

Watch the two soft spots before committing: **perception noise** (§9.1) and **non-bank canonical grounding** (§9.2). If transcript-derivation proves too noisy in eval, fall back to the commit-before-render alternative (§5b).

**Suggested build order:** (1) router gets transcript + emits `{has_pending, question_text}` perception, behind `engine_version` gate; (2) grader matches `question_text`→`LessonStep` + resolves canonical, `unverified` fallback on no match; (3) resume runs the router; (4) demote provenance gates once eval shows the perception holds; (5) one-question gate = deterministic floor + re-wired Haiku extractor. Validate each on the eval benchmark before the next.

**Implementation status (2026-05-29):**
- ✅ **Steps 1 + 2** — committed `ea7fe42` (router perception + grader bank-matching + engine grade-gate + trace). 752 tests; live-validated on Lesson 1426.
- ✅ **Step 3 — resume** — `v2_resume_dispatch` no-open-question branch delegates to `v2_start_dispatch` (state-driven: poses the next bank question; idempotent after commit), and prepends `"Welcome back — let's keep going. "` (resume-only, double-greet-guarded) so the student gets the re-entry acknowledgment AND the question (Issue 1). Live-validated: `"Welcome back — let's keep going. Convert the compass direction North-East (NE) to a three-figure bearing."`; reload re-rendered, no re-pose.
- ✅ **Step 5 — one-question gate** — new `run_one_question_check` (deterministic floor + Haiku `question_extractor` re-wired as a live service), added to `_GATE_ORDER` after `stem_duplication`. Catches image-#2 / T1560 stacking + active-end. 12 new tests.
- 🧹 Removed dead `PRE_POSE_SYSTEM` prompt (pre-pose check gone in the prune; not returning under the redesign).
- ⏳ **Step 4 (demote provenance gates)** — pending, gated on eval.
- ⏳ **Eval benchmark** — the GEO/MATHS harness has NOT been run; validation so far is unit tests (764) + live session walkthroughs. Run before ship.
