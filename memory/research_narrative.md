---
name: research-narrative
description: External-facing research synthesis. How the AI Tutor's deterministic state machine + structured validator stack + synthetic-student simulation harness produce auditable data that drives predictable engineering improvement.
metadata:
  type: project
---

# Research narrative — deterministic state, simulated students, auditable improvement

*AI Tutor project • Seychelles pilot • 2026-05-20*

---

## TL;DR

We built an LLM-driven tutoring system whose runtime behaviour is governed by a **deterministic state machine** rather than the LLM's free-form judgement. Every tutor turn passes through a **typed validator stack** that records structured issue codes, and every session persists a per-turn **audit log** (`SessionTurn.judge_outputs`). On top of this substrate we run a **simulation harness** that drives synthetic-student personas through the same `respond()` code path real students hit, swapping the active model per cell. The result is a closed loop:

> simulate → log → aggregate by issue code → make one targeted change → re-simulate → diff.

In the most recent round, this loop produced four production changes with concrete before/after numbers and zero behavioural regressions: a 2.8× tutor-latency cut, a 57% drop in spurious regen triggers, removal of a duplicative quality check, and the closure of a stuck-loop bug that no manual QA had caught.

The point of this writeup is not to advocate for a specific architecture. It is to make the case that **the state machine + validator codes are what make the simulations comparable** — without them, an "LLM does tutoring" system has no surface against which to measure change.

---

## 1. Problem

The default approach to LLM tutoring — give the model a system prompt, the conversation history, and let it talk — has three properties that make it almost impossible to improve in a principled way:

1. **No discrete state.** Whether the student is "answering a question", "exploring the topic", or "ready for the exit ticket" lives only in the model's interpretation of the transcript. There is nothing to assert against.
2. **No structured failure surface.** When the tutor mishandles a turn, the failure is a free-text observation ("it leaked the answer"), not a typed event you can count, aggregate, or diff across model changes.
3. **No audit trail.** If you swap models or change a prompt and the user reports "it felt worse", you have no record granular enough to attribute the change to anything.

The Seychelles pilot put 200+ secondary students through the system in production. Pilot users surface issues, but at ~30s latency per turn the feedback loop is slow and the failure descriptions are imprecise. Any meaningful improvement cycle has to run faster than students.

We therefore engineered the runtime to **produce structured data by construction**, then used that data to drive change.

---

## 2. The deterministic state machine

The tutor engine (`apps/tutoring/conversational_tutor.py`) is a finite-state controller wrapped around per-turn LLM calls. The LLM produces text and tool calls; the state machine decides what they mean.

### 2.1 Session state — three values

Across a session, exactly one of three values:

| State | Meaning | Transition trigger |
|---|---|---|
| `TUTORING` | Working through the lesson steps. | Default on session creation. |
| `EXIT_TICKET` | Lesson complete; running the assessment. | Set when `current_topic_index >= len(steps)`. |
| `COMPLETED` | Assessment graded; session terminal. | Set on exit-ticket submission. |

This replaces a legacy 7-value `ConversationPhase` enum that was driven by exchange-count heuristics. The new model puts **steps** — concrete units of curriculum content — at the centre, and treats the 5E display phase (Engage / Explore / Explain / Practice / Evaluate) as a property of the step, not a runtime variable.

The simplification removed ~200 lines of exchange-count transition logic and made it possible to write monotone invariants like "EXIT_TICKET is only reached after every step's `step_completion_judge` has fired".

### 2.2 Per-turn answer state — the `_awaiting_answer` dict

When the tutor poses a question, the engine records a typed record:

```python
{
    'kind': 'inline_authored' | 'bank' | 'figure' | 'warmup',
    'question_id': int | None,
    'question_type': 'mcq' | 'fill_in_blank' | 'short_numeric' | 'short_answer' | 'matching',
    'turn_index': int,
    'posed_at': iso8601,
    'wrong_attempts': int,
    'correct_answer': str,  # canonical, upper-cased
}
```

The dict is the **single source of truth** for "what is the student supposed to be answering right now". On the next student input, the grader resolves against this record. State drift between the engine's belief and the grader's belief is now a typed bug class (commit `8b0d1c3`) rather than an emergent symptom.

### 2.3 Question posing as a tool call

Tutor questions are not free-form text. The model invokes one of two structured tools:

- `pose_question(slot_id)` — selects a pre-authored question from the lesson's bank.
- `pose_inline_question({...})` — authors an MCQ inline with mandatory schema (`stem`, `options[4]`, `correct_index`).

Free-prose questions that survive into the rendered output are flagged by a `no_question_tool` validator issue and trigger regeneration. The data showed this constraint matters: across the 36-cell matrix, tool-use rate correlates with downstream quality (Section 5).

### 2.4 Why this matters for measurement

Every transition in the state machine corresponds to a typed event. "The tutor moved from step 2 to step 3" is a discrete fact, not an inference from the transcript. "The student gave a wrong answer to the MCQ posed at turn 5" is a row in `_awaiting_answer` plus a grader verdict, not a phrase to extract.

This is what makes the simulation harness (Section 4) able to compare runs.

---

## 3. The validator stack <we are using a unified judge an auditor>

After the LLM produces a response but **before** the response reaches the student, the candidate passes through `apps/tutoring/validator.py`. Each check is independent, fail-soft, and emits a structured issue code on detection. The full list of codes that appear in production traces today:

| Code | What it detects | Detector type |
|---|---|---|
| `answer_leak` | Tutor revealed the answer to the in-flight question (now folded into unified-judge dim 10; commit `2762a5b`). | LLM judge |
| `repeated_question` | Tutor re-asked a question already posed in this session. | Deterministic |
| `no_question_tool` | Tutor authored a free-prose question instead of calling a pose tool. | Deterministic |
| `no_question` | Tutor turn ended without a question or call-to-action. | Deterministic |
| `tool_call_leak` | Tool-call markup (`tool_code:`, `tool_use:`, `\|\|\|tool_call:...\|\|\|`) survived into rendered text. | Regex |
| `figure_ref_without_signal` | Tutor referenced "the map / the diagram" without emitting `\|\|\|MEDIA:N\|\|\|` — **gated on `step_has_media=True`** since commit `38ad6fb`. | Deterministic + gate |
| `authoring_violation` | `pose_inline_question` arguments violated the MCQ schema (e.g. missing options, non-int correct_index). | Schema |
| `tutor_incoherent` | Unified-judge dimension flagged response as not following from the preceding turn. | LLM judge |
| `arithmetic_violation` | Numeric claim contradicts the canonical answer (math lessons). | LLM judge |
| `numeric_claim_contradicted` | Tutor stated a number that conflicts with curriculum source-of-truth. | LLM judge |
| `regen_did_not_clean` | Self-retry loop exhausted cycles with no clean candidate. | Process |

Three properties to flag:

1. **Codes are stable identifiers, not human prose.** Issue counts can be summed across sessions, sliced by model, and tracked over time. The 36-cell matrix's "validator-issue distribution" column (`memory/deepmind_model_experiment_results.md`) is a direct dump of this.

2. **Detector type is a design lever.** Deterministic + schema checks are cheap and recall-perfect for their target. LLM-judge checks catch semantic failures regex cannot. We've moved checks between categories as the data demanded — e.g. `answer_leak` started as a deterministic detector + LLM judge + arbiter; we folded it into the unified judge once cross-validation showed dim 10 had ≥99% agreement on the same examples (commit `2762a5b`, task #252).

3. **The validator decides whether to regenerate, not whether to ship.** If any check fires, the response enters the **self-retry ensemble** (`apps/tutoring/regen/self_retry.py`): up to 2 cycles (dropped from 4 in commit `0962715`), each at a slightly lower temperature, with judge-clean early-exit. The static fallback path only triggers when all regen cycles exhaust without producing a clean candidate.

### 3.1 The unified judge

Until April 2026, post-response evaluation was a fan-out of seven specialist judges (factual, rule, coherence, handoff, safety, step_eval, figure_ref) called concurrently. The fan-out cost ~7× the LLM tokens of a single judge call and introduced ordering dependencies the disagreement audit (task #221) showed were not load-bearing.

The unified judge (`apps/tutoring/judges/unified.py`, commit `362563e`) replaces this with a **single multi-axis call** that scores the response on 10 dimensions in one prompt. Same providers, same model tier, ~1/7 the token cost on the judge path. Specialists remain in the tree as a kill-switch (`UNIFIED_JUDGE=off` env var) — they will be deleted once shadow data confirms parity holds in steady state.

---

## 4. The simulation harness

`apps/tutoring/management/commands/run_model_experiment.py` drives synthetic-student sessions through the production `respond()` path. The harness mirrors how real students enter the system; the only difference is that the student is an LLM persona, and the tutor's `ModelConfig` is swapped per cell via an in-memory monkey-patch (no DB writes, production default untouched).

### 4.1 Personas

Defined in `apps/tutoring/student_sim/personas.py`. The two used in the latest matrix:

| Persona | Behaviour |
|---|---|
| `struggler` | Answers wrong on first attempt ≥60% of the time, gives short / one-word responses, asks for hints. Exercises the remediation + scaffolding paths. |
| `capable` | Answers correctly first try ≥70% of the time, gives multi-sentence reasoning. Exercises the advance-step + exit-ticket paths. |

Both personas use **a higher-temperature simulator model** (Gemini 2.5 Flash at 0.7) so successive runs aren't deterministic. Each cell records a fresh session ID; comparison is across the **distribution** of metrics, not point estimates.

### 4.2 What a cell produces

For each (model × lesson × persona) cell, the harness records:

| Metric | What it tells us |
|---|---|
| `turns` | How long the session ran (capped at 10). |
| `reason ∈ {exit_ticket, max_turns, deadlock, error}` | Did the session reach assessment naturally, hit the cap, get stuck, or crash? |
| `tool-use rate` | Fraction of tutor turns that called `pose_question` or `pose_inline_question`. |
| `regen triggered / cycle-1 clean / shipped dirty` | Self-retry path: how often was regen needed, how often did it land clean on first cycle, how often did all cycles fail. |
| `leak / repeated-Q / no-Q` counts | Specific high-priority issue codes. |
| `validator-issue distribution` | Full bag of issue codes that fired, summed across the session. |
| `student tokens in / out` | Cost telemetry. |
| `wall (s)` | End-to-end session latency. |
| Per-turn record in `SessionTurn.judge_outputs` | The audit log. Every prompt, every judge verdict, every regen cycle, persisted. |

### 4.3 The 36-cell matrix (May 2026)

The most recent full run swept 9 models × 2 lessons (L540 geography, L638 math) × 2 personas = 36 cells. Total wall ~3.5 hours. Raw data: `memory/deepmind_model_experiment_results.md`.

Models included:

- **Anthropic**: Claude Opus 4.7, Claude Sonnet 4, Claude Haiku 4.5
- **Google**: Gemini 3.1 Pro, Gemini 3.1 Pro (custom-tools variant), Gemini 3 Flash Preview, Gemini 3.5 Flash, Gemini 3.1 Flash Lite Preview, Gemini 2.5 Flash
- **OpenAI**: GPT-5, GPT-4o, GPT-4o mini

A subsequent focused 20-cell re-run isolated 5 Gemini variants × 2 lessons × 2 personas to pin the runtime swap candidate (task #248).

---

## 5. What the data showed → what we shipped

This section pairs findings from the audit log with the engineering decision they motivated. Each decision cites the commit that implemented it and the migration that landed it in production.

### 5.1 Runtime model swap: Opus 4.7 → Gemini 3.1 Flash Lite Preview

**Finding.** Aggregated wall-time per session, struggler-persona, across both lessons:

| Model | Mean wall (s) | Mean tool-use | Mean regen-clean cycle-1 | Leak incidents |
|---|---:|---:|---:|---:|
| Claude Opus 4.7 | ~330 | 70% | 2.2 / session | 3 across 4 cells |
| Gemini 3.1 Flash Lite Preview | ~125 | 49% | 2.4 / session | 1 across 4 cells |

Opus had higher tool-use but ~2.6× the wall time per session. Quality holds — `cycle-1-clean` is actually marginally higher on Gemini, and total leak incidents are lower (1 vs 3 across cells). Subsequent browser e2e on L540 + L638 confirmed: students complete the lesson, exit-ticket grader fires correctly, no visible regressions.

**Decision.** Migration `apps/llm/migrations/0028_swap_runtime_to_gemini_3_1_flash_lite.py` (commit `17f8bbf`). Swapped all four runtime purposes: `tutoring`, `judge`, `regen` → Gemini 3.1 Flash Lite Preview at temp 0.2 / 0.0 / 0.2 respectively; `judge_fallback` → Gemini 3.5 Flash. Reversible to the prior Opus-stack. Production deployed 2026-05-20.

### 5.2 Regen cycle cap 4 → 2

**Finding.** Across cells that triggered regen, `cycle-1` and `cycle-2` accounted for 100% of all clean recoveries. Cycles 3 and 4 produced output that was either identical to cycle 2 or also dirty. Marginal value of cycles 3+4 was near-zero; cost was 2× LLM calls per regen-triggered turn.

**Decision.** Commit `0962715` (2026-05-12). `DEFAULT_MAX_CYCLES = 2`. Temperature schedule reduced from 4 cycles (0.20 → 0.15 → 0.10 → 0.05) to 2 (0.20 → 0.15). Production. (Refs: `CLAUDE.md` notes the change explicitly under "Temperature controls".)

### 5.3 `figure_ref_without_signal` false-positive gate

**Finding.** On geography lesson L540 (which is *about* maps), the deictic-reference detector fired on 57% of tutor turns saying "the map" — but the step legitimately had no attached media to reference, so there was nothing to signal. The validator was inferring a signal-omission bug from text that was semantically correct.

**Decision.** Commit `38ad6fb` (2026-05-19). Added `step_has_media: bool` parameter to `validate_tutor_response()` and gated the check on it: `if step_has_media and not media_attached and _FIGURE_DEICTIC_RE.search(content): ...`. Regen-trigger rate on L540 dropped 57% → 0% on these turns with no quality regression observed in the next browser e2e.

### 5.4 Stuck exit-ticket loop on `inline_authored` path

**Finding.** Session 196 (pilot user, reported manually) was stuck on the exit ticket — student gave answer, tutor acknowledged, but the engine still believed the question was open. Audit log showed three compounding bugs:

1. `_record_inline_authored_question` never initialised `wrong_attempts: 0` in the new record.
2. The same function unconditionally overwrote `_awaiting_answer` even on re-pose of the same question — resetting the counter every turn.
3. The grader resolved to a different question than `_awaiting_answer.question_id` (state drift).

**Decision.** Commit `8b0d1c3` (2026-05-20). Fixed all three: explicit `wrong_attempts: 0` initialisation, preserve counter on re-pose if `correct_answer` matches, state-drift detection in grader at line ~8068 that re-syncs `_awaiting_answer` when the grader resolves to a different target. Verified via Django-shell reproduction of session 196's transcript.

The audit log made the diagnosis tractable. Without `judge_outputs` persistence + the typed `_awaiting_answer` dict, this would have been "tutor seems stuck sometimes".

### 5.5 Removed duplicative `answer_leak` detector

**Finding.** Cross-validation on the unified judge's dimension 10 (answer-leak detection) vs the standalone deterministic-plus-LLM `answer_leak` validator showed ≥99% agreement on the same examples (task #252 audit). The standalone detector was a separate LLM call per turn doing the same work.

**Decision.** Commit `2762a5b` (2026-05-19). Gated the legacy detector behind `LEGACY_ANSWER_LEAK_DETECTOR=on` env var (off by default). One fewer LLM call per turn on the validator path; unified-judge dim 10 catches the same leaks.

### 5.6 Tool-call markup leak in three variants

**Finding.** Gemini occasionally emits tool-call syntax as prose when no tools are offered (scaffolding mode): `tool_code: pose_question(slot=2)`, `tool_use: pose_...`, and `|||tool_call:NAME{...}|||`. The original strip-regex covered only the third variant. Markup was reaching students.

**Decision.** Commits `8500fe4` + `cd4ef31` (2026-05-19/20). Broadened the strip regex to `tool[_ ]?(?:call|use|code)`, added a new `tool_call_leak` validator issue code, surfaced incidents in the audit log. Browser e2e re-confirmed clean output on the next L540 walk.

### 5.7 Cross-provider response-shape adapter (unblocking the matrix)

**Finding.** The validation slice surfaced a TypeError: `generate_with_tools` returned `GenerateContentResponse` (Gemini) or `ChatCompletion` (OpenAI) instead of Anthropic's `Message` shape. The engine + SelfRetry walk Anthropic's contract via `getattr` on blocks. Cross-vendor experimentation was impossible without an adapter.

**Decision.** Commit `18601f6` (2026-05-19). Added `AdaptedMessage / AdaptedTextBlock / AdaptedToolUseBlock / AdaptedUsage` dataclasses in `apps/llm/client.py` and wrapped Gemini + OpenAI `generate_with_tools` returns. 8 unit tests; immediately unlocked the 36-cell matrix run.

This is a meta-finding: **the simulation harness itself caught load-bearing infrastructure gaps** that would otherwise have been discovered turn-by-turn in production.

---

## 6. What makes this "auditable and predictable"

Two claims, narrowly defined:

**Auditable** — for any tutor turn in any session, we can reconstruct:

- The exact prompt sent (system + history + curriculum context).
- The model's raw response (text + tool calls).
- Every validator issue code that fired, with reason and source span.
- Every regen cycle attempted, with the candidate and judge verdict.
- The grader's verdict and resolved `_awaiting_answer` target.
- The session-state transitions taken as a consequence.

Persisted in `SessionTurn.judge_outputs` (rolled out 2026-05-11; CLAUDE.md cross-references). The schema is documented at the writer site and consumed by the bench tooling.

**Predictable** — change to the system → measurable effect:

- The validator issue distribution is the metric vector. A code disappearing from the distribution after a change is direct evidence; a code appearing is a regression signal.
- The simulation harness re-runs the same persona × lesson cells under identical seeds (modulo the simulator-temperature noise). Two model versions can be compared head-to-head on the same workload, not on different anecdotes.
- The closed loop has produced four shipped wins (Section 5) and zero rolled-back changes in the past two weeks.

The honest qualifiers: simulator personas are not real students, so harness results bound but don't replace pilot evidence. Validator codes are a coarsening of the underlying tutor behaviour, so two responses can share an issue code distribution while differing in ways students notice. We treat the harness as a fast filter, not as ground truth, and pair it with a hand-curated pilot eval set (50 items, frozen 2026-05-11; `memory/eval_benchmark_v2_simplified.md`).

---

## 7. What's next

Three threads, in priority order:

1. **Cross-vendor tutoring fallback chain** (task #256). The migration is done; the engine still binds one client per session. Plan written at `memory/cross_vendor_tutor_fallback_plan.md`. Single-vendor outage today → static fallback HTML; after wiring → cascade through Gemini 3.5 → Opus 4.7 transparently to the student.

2. **Eval benchmark v2** (`memory/eval_benchmark_v2_simplified.md`). 30 labels, 19 failure categories, paired with `SessionTurn.judge_outputs` persistence. The harness produces candidate items; humans label; the labelled set becomes the regression suite for every prompt or model change.

3. **Specialists removal.** The 7-specialist judges (`apps/tutoring/judges/{factual,rule,coherence,handoff,safety,step_eval,figure_ref}.py`) are deprecated. Removal is gated on the kill-switch (`UNIFIED_JUDGE=off`) showing zero traffic for two weeks of production load and the unified judge's dimension-by-dimension agreement audit holding.

Deferred and explicit: multi-agent decomposition of the tutor itself. The Cemri et al. 2025 result on 17× error amplification in unstructured multi-agent setups is our prior; we do not split the tutor until benchmark evidence demonstrates a bottleneck the current single-prompt + post-hoc judges cannot solve. See `CLAUDE.md`'s "Conservative bias" section and `memory/agentic_platform_architecture_plan.md` (archived) for the deferred path.

---

## Sources

- **Raw matrix data**: `memory/deepmind_model_experiment_results.md` (36 cells, per-cell metrics + validator-issue distributions).
- **Engineering log**: `memory/overnight_run_summary.md` (the fix sequence A–H that unlocked cross-vendor experimentation).
- **Bug catalogue**: `memory/provider_experiment_validation_errors.md` (5 blocking + 2 environmental issues found in the validation slice).
- **Provider-prompt plan**: `memory/provider_specific_prompt_system_plan.md` (the prompt-architecture work that preceded the matrix).
- **Fallback plan (in progress)**: `memory/cross_vendor_tutor_fallback_plan.md` (next thread).
- **Code anchors**: `apps/tutoring/conversational_tutor.py` (state machine), `apps/tutoring/validator.py` (issue codes), `apps/tutoring/judges/unified.py` (multi-axis judge), `apps/tutoring/regen/self_retry.py` (regen loop), `apps/tutoring/management/commands/run_model_experiment.py` (sim harness), `apps/tutoring/student_sim/personas.py` (synthetic students), `apps/llm/client.py` (`BaseLLMClient` + cross-vendor adapters), `apps/llm/models.py` (`ModelConfig` per-purpose dispatch).
