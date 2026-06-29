# Deep Bottleneck Analysis and Remediation Plan for Five Lead Tutor Models

**Improved Evaluation 1 — focused study of `qwen3-next-80b-thinking`, `qwen3-next-80b-instruct`, `kimi-k2-thinking`, `deepseek-v3.1`, and `qwen2.5:7b`**

**Authors:** AI Tutor Research Team, Nyansapo / Pixel Design Labs LLC · **Date:** 2026-06-28
**Inputs:** per-scenario result JSONs in `offline_eval/results2/` and the per-model run logs. **Companion:** `offline_eval/IMPROVED_EVAL_1_PREPRINT.md`.

---

## 1. Scope and method

The Improved Evaluation 1 preprint reported one headline bottleneck per model family. This document scrutinises the **full failure set** of the five lead candidates — every failed scenario decomposed to (a) the individual rubric items that dragged the mean below the 0.70 pass threshold, (b) the eight advisory pedagogical dimensions, (c) the verbatim tutor reply, and (d) the run-log error trace. We classify each failure as one of:

- **Engine/infrastructure** — the model behaved acceptably but the harness/engine mangled or dropped its output.
- **Rubric artifact** — the model behaved acceptably but the judge mis-scored a non-applicable conditional item.
- **Genuine pedagogy** — the model's actual reply fell short of the teaching standard.

This separation is essential: **the majority of failures in this cohort are engine or rubric defects, not capability gaps.** Conflating them (as a raw pass rate does) understates every model and points remediation at the wrong layer.

### 1.1 Headline result

| Model | Eval 1 pass | Failed | Tool-call-text leak | Call-2 crash (log) | N/A-artifact-tainted fails | Genuine pedagogy fails (residual) |
|---|--:|--:|--:|--:|--:|--:|
| qwen3-next-80b-thinking | 80% | 12 | 0 | 0 | 4 | ~8 |
| **qwen3-next-80b-instruct** | 75% | 15 | **10** | 2 | 5 | ~3 |
| kimi-k2-thinking | 73% | 16 | 0 | **28** | 3 | ~13 |
| **deepseek-v3.1** | 70% | 18 | 0 | **52** | **11** | ~5 |
| qwen2.5:7b | 62% | 23 | 1 | 0 | 10 | ~15 |

"Genuine pedagogy fails (residual)" = failures remaining after excluding tool-call leaks and clear N/A artifacts. For `qwen3-next-80b-instruct` and `deepseek-v3.1`, **most failures are not pedagogical at all.**

---

## 2. Cross-cutting bottlenecks (highest leverage)

Three defects recur across models and, if fixed, lift several models simultaneously. They are ordered by total impact.

### B1 — Tool-call-as-text leak (engine + prompt)

**What happens.** On a grade-and-advance turn, the model emits the *tool invocation itself as the visible reply text* instead of (i) emitting a structured tool call through the function-calling channel and (ii) writing a student-facing sentence. The engine surfaces the raw string to the student. Examples (verbatim `tutor_response`):

```
qwen3-next-80b-instruct · math_capable_correct_bare_001   → record_answer(extracted_answer="110")
qwen3-next-80b-instruct · math_struggler_correct_advance_001 → record_answer("145")
qwen3-next-80b-instruct · geo_probe_resistant_correct_001  → record_answer(extracted_answer="180")
qwen2.5:7b             · no_unfounded_praise_001          → -record_answer{"extracted_answer": "C"}
```

**Why it is catastrophic for the score.** The student "sees" `record_answer(extracted_answer="110")`. Every rubric item — *Confirms '110' is correct*, *Advances*, tone (S7), naturalness (S8) — scores 0.0. A scenario the model effectively *graded correctly* registers as a total failure.

**Incidence.** `qwen3-next-80b-instruct`: **10 of 15 failures** (two-thirds). `qwen2.5:7b`: 1. Zero for the thinking/reasoning models, which reliably separate the tool call from the visible text. This is the single largest reason `qwen3-next-80b-instruct` (75%) ranks *below* its thinking sibling (80%) despite being the more deployment-friendly variant.

**Root cause.** Two contributing factors: (1) the model, in instruct mode on a trivial grade turn, returns only the tool call as plain text (no separate student sentence); (2) the engine's text-tool-call leak parser (which already recovers some open-model leak formats) does not catch the `record_answer(extracted_answer="…")` form on the Vertex MaaS adapted path, so the leak is neither dispatched nor stripped.

**Fix (engine + prompt).**
1. **Engine — extend the leak parser.** In the adapted-message path (`apps/llm/client.py` `_adapt_openai_dict` → engine `_dispatch_tools`), detect a reply whose visible text *is* a tool-call signature (`^(record_answer|pose_question|advance_step|request_figure|redirect_off_topic)\s*\(`), parse and dispatch it as a structured tool call, strip it from the student-facing text, and **force the second call** to compose the real reply.
2. **Engine — never surface a tool-call-only turn.** If after dispatch the student-facing text is empty or is itself a tool-call signature, run the Call-2 reply composition (don't fall through to raw text).
3. **Prompt (Block 0 rule).** Add: *"Every turn produces a student-facing message in plain language. Tool calls (record_answer / pose_question / advance_step) are separate machine actions — never write a tool call, function name, or `key=value` argument as your visible reply."* Pair with one exemplar of a correct grade turn (structured `record_answer` **plus** "Exactly — 110°. Next: …").
4. **Optional — `tool_choice` shaping.** On grade turns, the engine already knows a question is in flight; it can bias toward the structured channel.

**Projected impact.** Recovering 8 of the 10 leak failures takes `qwen3-next-80b-instruct` from 45/60 to ~53/60 (**≈ 88%**) with no change to the model's pedagogy.

### B2 — Call-2 serialization crash (engine)

**What happens.** The tutor uses a two-call loop: Call 1 decides/executes tools; Call 2 composes the student reply *with the grading verdict in hand*. For Vertex Model Garden (OpenAI-compatible) clients, Call 1 returns an `AdaptedMessage` whose content is a list of `AdaptedToolUseBlock` objects. The engine appends `{'role':'assistant','content':response.content}` to the message history for Call 2; the OpenAI-compatible client then tries to JSON-serialise those blocks for the request body and raises:

```
generate_with_tools(vertex_model_garden) failed: TypeError: Object of type AdaptedToolUseBlock is not JSON serializable
```

When Call 2 crashes, the engine falls back to **Call-1 text, which is composed *before* the verdict is known** — typically a pre-grade holding phrase. Signature replies:

```
deepseek-v3.1 · no_banned_opener_001          → "I see you've chosen option A. Let me check your answer."
deepseek-v3.1 · average_off_method_correct_001 → "I see you're working through the angles… You said "120" — let me record that."
deepseek-v3.1 · incoherent_guard_001          → "I see you're trying to answer… Let me record your attempt."
```

The verdict ("correct"/"incorrect", the specific error, the next step) **never reaches the student**, so S1 (affirm/recognise), S2 (specific location), S4 (guidance) and S5 (actionability) all fail.

**Incidence (run logs).** `deepseek-v3.1`: **52** occurrences; `kimi-k2-thinking`: **28**; `qwen3-next-80b-instruct`: 2; `qwen3-next-80b-thinking` and `qwen2.5:7b` (local Ollama path): 0. This is the dominant bottleneck for **`deepseek-v3.1`** and a major drag on **`kimi`** — both strong models whose *verdict-aware* turn is being silently discarded.

**Root cause.** The engine re-sends Call-1's adapted content objects into Call-2's message array without normalising them to a JSON-serialisable shape. The adapted block classes are internal representations, not wire types.

**Fix (engine).** Before appending the Call-1 assistant turn to the Call-2 message history, **normalise adapted content to a serialisable form** — convert `AdaptedTextBlock`/`AdaptedToolUseBlock` to the OpenAI assistant-message shape (`{role:"assistant", content:<text>, tool_calls:[{id,function:{name,arguments}}]}`) and the matching `{role:"tool", tool_call_id, content}` results. Add this normalisation at the two-call boundary in `apps/tutoring/simple_tutor/engine.py::respond` (the `messages.append({'role':'assistant','content':response.content})` site) or, preferably, in a shared adapter so every provider's Call-2 history is wire-safe. Add a regression test that runs a grade turn through the Vertex MaaS adapter and asserts Call 2 succeeds.

**Projected impact.** Restoring the verdict-aware second call is expected to recover a large share of `deepseek-v3.1`'s S1/S2/S4 failures (potentially **+8–12 scenarios**, lifting it from 70% toward the mid-80s) and several of `kimi`'s.

### B3 — Conditional-rubric N/A artifact (rubric)

**What happens.** The 8-item standard block includes *conditional* items — e.g. S1 *"If the student made a mistake, the response identifies it; if correct, affirm it"* and S2 *"If a mistake exists, point at its specific location."* On scenarios with **no mistake and no answer attempt** (off-topic, distress, clarification, non-engagement), the judge frequently scores these **0.0** while explicitly noting they do not apply. Verbatim judge reasoning:

```
qwen3-next-80b-thinking · safety_distress_signal_001 → "the student did not attempt the question … so this rubric item does not apply" (scored 0.0)
deepseek-v3.1 · (11 of 18 failed scenarios contain at least one such 0.0 on S2)
```

**Incidence (failed scenarios with ≥1 N/A-tainted 0.0).** `deepseek-v3.1`: **11/18**; `qwen2.5:7b`: 10/23; `qwen3-next-80b-instruct`: 5/15; `qwen3-next-80b-thinking`: 4/12; `kimi`: 3/16. Because the standard block is 8 of ~11 items, **one or two spurious 0.0s can pull an otherwise-passing scenario below 0.70.** This systematically understates the *strongest* models, whose residual failures cluster in exactly these no-mistake edge scenarios.

**Fix (rubric/judge).** Add an explicit `n/a` verdict to the conditional items and **exclude `n/a` items from the mean**. Concretely, in `evals/scorers/llm_rubric.py` extend `_SYSTEM_PROMPT` to permit `"score": "n/a"` for conditional items when the precondition is absent, and have the aggregation skip `n/a` items. Re-grade the existing `results2` transcripts (no model re-run needed — the tutor replies are stored) to obtain de-biased pass rates.

**Projected impact.** De-biasing recovers an estimated 2–4 scenarios each for `deepseek-v3.1` and `qwen2.5:7b`, and 1–3 for the others — *without any model or prompt change.* This is the cheapest single lift available.

---

## 3. Per-model deep dives

Each model below lists its bottleneck inventory (engine/rubric/pedagogy), the residual *genuine* pedagogy gaps with verbatim evidence, and a prioritised fix list with projected lift.

### 3.1 `qwen3-next-80b-thinking` — 80% (cleanest of the five)

**Bottleneck inventory (12 failures).** No tool-call leak (0); no serialization crash (0); N/A artifact 4. Residual genuine pedagogy ≈ 8.

**Genuine pedagogy bottlenecks.**
1. **Generic mistake diagnosis (S2).** Touches 10/12 failures (4 are N/A; ~6 genuine). On wrong-answer turns it scaffolds without *naming the specific error*.
2. **Insufficient down-shift for give-up / distress (S4 + scenario item).** `struggler_gives_up_001` (mean 0.63): the reply *"…angles around a point add up to 360°. First, add 70° + 50° + 90°. What's the sum?"* is competent but the scenario demands a **much simpler** entry ("try 10 − 7"); the down-shift item scored 0.2. The model holds the lesson difficulty constant when the student has disengaged.
3. **Over-strict rejection of equivalent answers (false-reject).** `false_reject_capable_001`: the model failed to accept *"90° in words = 90°"* as correct (scenario item 0.0) — it treated a format variant as wrong.
4. **Answer leakage on hint turns (reveals_answer = 4).** On `leaks_answer_guard_*` and `math_leaks_answer_guard_001` it revealed/again-stated the target value while hinting (S3 = 0.5).
5. **Naturalness (human_likeness = 7).** Some replies read as templated.

**Fixes (priority order).**
- **(P1) Specific-error rule** — *"On a wrong answer, first name the specific error (the wrong number, step, or misconception) in one clause, then give one hint. Do not open with a bare 'Not quite' / 'Let's walk through it.'"* + 1 exemplar. Targets S2 across all five models.
- **(P2) Down-shift rule** — *"If the student gives up, expresses distress, or fails twice, drop to a drastically simpler sub-problem (single operation, smaller numbers) before returning to the lesson item."*
- **(P3) Equivalent-answer tolerance** — *"Accept mathematically/semantically equivalent answers (90 = ninety = 90°); judge meaning, not format."*
- **(P4) Tighten no-reveal on hint turns** — reinforce S3 in the hint ladder.
- **(Cross) B3 rubric N/A fix** recovers ~3 scenarios with no model change.
- **Sampling/format unchanged** (Markdown, 0.6/0.95/20, 32k budget) — validated.
- **Projected:** 80% → **~85–88%** (B3 + P1/P2).

### 3.2 `qwen3-next-80b-instruct` — 75% (engine-bound, highest upside)

**Bottleneck inventory (15 failures).** **Tool-call-text leak: 10**; serialization crash: 2; N/A artifact: 5; residual genuine pedagogy ≈ 3.

**This model's score is gated by B1, not by teaching ability.** Ten of fifteen failures are the leak (`record_answer(...)` as the visible reply) on *easy* grade-and-advance scenarios it handled correctly underneath. Its genuine replies, when produced, are strong (e.g. `struggler_gives_up_001`: *"I get it — this can feel tricky… Imagine you're standing in the middle of a circle… How many degrees did you turn?"*).

**Residual genuine pedagogy (≈3).** Struggler down-shift (P2), non-responder/probe persona calibration, occasional S2.

**Fixes (priority order).**
- **(B1) Fix the tool-call-text leak** — engine parser extension + the "never write a tool call as your visible reply" Block-0 rule + one grade-turn exemplar. **This is the single highest-ROI fix in the entire cohort.**
- **(B2) Serialization fix** (2 occurrences here).
- **(P1) Specific-error rule**, **(P2) down-shift**, persona-calibration rules (shared with the others).
- **(Cross) B3 rubric N/A** recovers ~2.
- **Sampling/format unchanged** (Markdown, instruct 0.7/0.8/20).
- **Projected:** 75% → **~88–92%** (B1 alone ≈ +13 pp). This makes the *instruct* variant — the right choice for the latency-sensitive tool loop — a genuine 90% candidate.

### 3.3 `kimi-k2-thinking` — 73%

**Bottleneck inventory (16 failures).** Serialization crash: **28** (log); tool-call leak: 0; N/A artifact: 3; residual genuine pedagogy ≈ 13.

**Genuine pedagogy bottlenecks.**
1. **Generic diagnosis (S2 in 15/16 failures).** The dominant pattern. Verbatim: `error_prone_misreads_001` and `struggler_misreads_question_001` both reply **"Not quite — let's walk through it together."** — zero diagnostic content where the scenario requires naming "you used 270 instead of 360" / "you read the wrong number." `math_average_arithmetic_slip_001`: **"Let's keep going."** (advances without addressing the slip).
2. **Advance/​hint without an explicit verdict (S1).** Partly B2 (the verdict-aware Call-2 crashed) and partly genuine terseness.
3. **Guidance thinness (providing_guidance = 12).**

**Fixes (priority order).**
- **(B2) Serialization fix** — restores 28 verdict-aware second calls.
- **(P1) Specific-error rule** + **(P2) explicit-verdict rule** (*"State whether the answer is correct or incorrect, and name the error, before any hint or advance"*) — directly targets the "Not quite / Let's keep going" failure mode.
- **(Cross) B3 rubric N/A** (3).
- **Sampling unchanged** — keep temp **1.0 / top_p 0.95**, ≥16k budget (do **not** lower temperature; it is tuned to avoid degenerate reasoning).
- **Projected:** 73% → **~80–84%** (B2 + P1/P2).

### 3.4 `deepseek-v3.1` — 70% (most under-measured)

**Bottleneck inventory (18 failures).** Serialization crash: **52** (log) — the highest in the cohort; N/A artifact: **11/18** — also the highest; tool-call leak: 0; residual genuine pedagogy ≈ 5.

**`deepseek-v3.1` is the most under-measured model in the study.** Its observed replies are dominated by pre-verdict Call-1 fallbacks ("Let me check your answer." / "let me record that.") because the verdict-aware Call-2 crashed 52 times; and 11 of its 18 failures carry a spurious-0.0 N/A item. The model's *actual* teaching ability is largely untested by this run.

**Residual genuine pedagogy (≈5).** After B2+B3, the remainder are: the affirm-and-teach-canonical-method beat (`average_off_method_correct_001` — it must validate the trial-and-error method *and* model `360 ÷ 3 = 120`), and S2 specificity.

**Fixes (priority order).**
- **(B2) Serialization fix — top priority.** Expected to recover the bulk of the S1/S4/S5 failures.
- **(B3) Rubric N/A fix** — recovers an estimated 3–4 with no re-run.
- **(P1) Specific-error** + **canonical-method rule** (*"When a student reaches a correct answer by an inefficient method, affirm the method, then model the canonical one in one line"*).
- **Sampling/format unchanged** (XML, chat endpoint, temp 0.0 for math).
- **Projected:** 70% → **~82–88%** (B2 dominates). This is the second-strongest path to 90% after the instruct leak fix.

### 3.5 `qwen2.5:7b` — 62% (on-device; mostly genuine capability)

**Bottleneck inventory (23 failures).** Tool-call leak: 1; serialization crash: 0 (Ollama path); N/A artifact: 10; residual genuine pedagogy ≈ 15. **Unlike the cloud models, this model's failures are mostly genuine** — expected for a 7B on-device model.

**Genuine pedagogy bottlenecks.**
1. **Affirming wrong answers (false-accept) — the most serious.** `math_wrong_mcq_no_praise_001` (mean 0.18): the student chose a wrong option, yet the reply is **"Great! You've got it right. All angles around a point do indeed sum to 360°."** `incoherent_guard_001`: **"That's correct! The two angles add up to 180°…"** on an incoherent answer. The model is not reliably consuming/trusting the grader verdict — a *correctness* failure, not just a style one. (coherence dimension fails 13×, mistake_identification 10×.)
2. **Over-explaining / info-dump and self-contradiction** (human_likeness 15, coherence 13).
3. **Answer reveal (reveals_answer = 9)** — highest in the cohort; it states target values during hints.
4. **Instruction bleed into prose** — `off_topic_redirect_001`: *"…which type of map would you use… Record your answer."* (the tool instruction leaks into the visible sentence).

**Fixes (priority order).**
- **(P3) Verdict-adherence rule (top priority for this model)** — *"The platform's grader decides correctness. If the in-flight answer is graded incorrect, you must NOT say 'correct' or 'great job'. State it's not right, name the error, and hint."* Plus 1–2 few-shot exemplars (small instruct models benefit most from few-shot). Targets the dangerous false-accept pattern.
- **(P5) Conciseness rule** with a hard word budget + the info-dump exemplar; **no-reveal** reinforcement.
- **(B1) Leak rule** (1 occurrence) and "never write tool syntax in prose."
- **(Cross) B3 rubric N/A** recovers ~3.
- **Sampling/format unchanged** (Markdown, 0.7/0.8/20) — validated +10 pp vs XML.
- **Projected:** 62% → **~72–78%** (B3 + P3 + P5). The 7B capability ceiling makes 90% unlikely on-device; `qwen2.5:14b` or a distilled/fine-tuned 7B would be the route if a higher on-device bar is required.

---

## 4. Consolidated remediation roadmap

Fixes are ordered by total projected lift across the cohort. Engine and rubric fixes are **shared** (one change benefits multiple models); prompt fixes are largely shared rules added to the `simple_tutor` Block 0.

| # | Fix | Type | Models helped | Effort | Projected lift |
|--:|---|---|---|---|---|
| 1 | **B1 — tool-call-text leak** parser + "never emit tool syntax as reply" rule + exemplar | engine + prompt | qwen3-next-80b-instruct (+++), qwen2.5:7b | M | instruct +~13 pp |
| 2 | **B2 — Call-2 serialization** normalisation (adapted blocks → wire shape) | engine | deepseek-v3.1 (+++), kimi (++), instruct (+) | M | deepseek +~12 pp, kimi +~6 |
| 3 | **B3 — rubric N/A** verdict + exclude from mean; re-grade stored transcripts | rubric | all five (esp. deepseek, qwen2.5) | S | +2–4 pp each, no re-run |
| 4 | **P1 — specific-error diagnosis** rule + exemplar | prompt | all five (S2 is the #1 genuine gap) | S | +2–5 pp each |
| 5 | **P2 — down-shift on give-up/distress; P3 — explicit verdict / no false-accept** | prompt | thinking, instruct, kimi, qwen2.5 | S | +1–4 pp each |
| 6 | **P5 — conciseness/no-reveal** rule + exemplar | prompt | qwen2.5:7b, (Gemini/others) | S | +1–3 pp |

**Sequencing.** Do the **engine fixes (B1, B2) and the rubric fix (B3) first** — they are shared, high-impact, and require no prompt tuning or model change. Re-grade the existing `results2` transcripts for B3 immediately (free). Then add the shared prompt rules (P1–P3, P5) and re-run only the affected scenarios.

### 4.1 Projected post-remediation pass rates

Estimates are bounded by the count of recoverable failures (engine/rubric) plus conservative prompt-fix gains; they are projections to be confirmed by re-running, not measured results.

| Model | Eval 1 | After B1+B2+B3 (engine+rubric only) | After + prompt fixes (P1–P3, P5) | Reaches 90% target? |
|---|--:|--:|--:|:--:|
| qwen3-next-80b-instruct | 75% | ~88% | **~90–92%** | **likely** |
| deepseek-v3.1 | 70% | ~84% | **~86–90%** | **plausible** |
| qwen3-next-80b-thinking | 80% | ~84% | **~86–89%** | borderline |
| kimi-k2-thinking | 73% | ~80% | ~82–85% | unlikely (this iteration) |
| qwen2.5:7b (on-device) | 62% | ~70% | ~74–78% | no (7B ceiling) |

**Headline.** Two of the five — **`qwen3-next-80b-instruct`** and **`deepseek-v3.1`** — are realistic **90%+** candidates whose current scores are suppressed primarily by *engine and rubric defects*, not by teaching ability. The single most valuable engineering action is fixing the two-call tool-loop on the Vertex MaaS adapted path (B1 + B2); the single cheapest action is the rubric N/A fix (B3, re-grade only).

---

## 5. Validation plan

1. Implement B1, B2 (engine), add a regression test asserting a Vertex-MaaS grade turn completes Call 2 with a non-tool-syntax student reply.
2. Implement B3 (judge `n/a` + aggregation), **re-grade** the stored `results2` transcripts and record the de-biased pass rates (no model run).
3. Add the shared Block-0 rules (P1–P3, P5) + exemplars; re-run the five models on the full 60-scenario suite (`RESULTS_DIR=offline_eval/results3`).
4. Confirm: leak count → 0 for `qwen3-next-80b-instruct`; serialization errors → 0 in logs for `deepseek-v3.1`/`kimi`; S2 (specific-location) pass rate ↑; no regression on the previously-passing scenarios.
5. Re-aggregate and compare against this projection table.

---

# Part B — Extended analysis: Gemini family and `qwen2.5:14b` (appended 2026-06-28)

This part applies the same decomposition to five further Improved-Evaluation-1 models — `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-3.5-flash`, `gemini-3.1-pro`, and `qwen2.5:14b` — and produces a **material update to bottleneck B2**.

## B.0 — Update: B2 is a cross-provider engine bug, not a Vertex-MaaS-only one

Part A identified the Call-2 serialization crash (B2) on the OpenAI-compatible Vertex MaaS path (`TypeError: Object of type AdaptedToolUseBlock is not JSON serializable`). The Gemini logs reveal the **same root cause with a different crash signature**:

```
gemini-2.5-pro · generate_with_tools(google) failed: AttributeError: 'AdaptedToolUseBlock' object has no attribute 'get'   (×21)
gemini-2.5-pro · generate_with_tools(google) failed: AttributeError: 'AdaptedTextBlock'  object has no attribute 'get'    (×13)
```

The engine appends Call-1's `response.content` — a list of internal `AdaptedToolUseBlock`/`AdaptedTextBlock` objects — into the Call-2 message history. The OpenAI client chokes when JSON-serialising them; the Gemini client (`GeminiClient._build_contents`) chokes calling `.get()` on them. **Every non-Anthropic provider's verdict-aware second call is therefore broken by the same defect.** Incidence (`generate_with_tools failed` per 60-scenario run):

| Model | Call-2 failures (log) | Crash type |
|---|--:|---|
| gemini-3.5-flash | 43 | AttributeError (`.get`) |
| gemini-3.1-pro | 39 | AttributeError (`.get`) |
| gemini-2.5-pro | 34 | AttributeError (`.get`) |
| gemini-2.5-flash | 27 | AttributeError (`.get`) |
| deepseek-v3.1 (Part A) | 52 | TypeError (JSON) |
| kimi-k2-thinking (Part A) | 28 | TypeError (JSON) |

When Call 2 dies, the engine serves Call-1's **pre-verdict** text, which for Gemini is a terse holding phrase ("Let's keep going.", "Not quite — let's walk through it together.", "Got it — here's the next one:"). Much of what the preprint attributed to Gemini "under-teaching/terseness" is in fact this fallback. **The B2 fix (normalise Call-1 adapted content to each provider's wire shape before re-sending) now ranks as the single highest-leverage engineering action in the entire study — it touches Gemini and the MaaS families simultaneously.** Note `qwen2.5:14b` (Ollama path) shows 0 Call-2 failures — the bug is specific to the adapted (OpenAI/Gemini) response path.

## B.1 — `gemini-2.5-pro` — 68% (19 failures)

**Inventory.** Call-2 failure: **34**; tool-call leak: 0; N/A artifact: **9/19**. Top failing items: S4-guidance 18, S2-specific 17 (9 N/A), S1-affirm 15, S5-actionable 14. Dimension: providing_guidance 12.

**Genuine pedagogy (after B2/B3).**
1. **Under-teaching / dropped clarification.** `info_dump_guard_clarification_001` (mean 0.24): asked "what's the difference between scale and zoom?", the reply is **"Let's keep going."** — it neither answers nor teaches (S4=0, S1=0, S6=0). The model avoids the info-dump by saying *nothing*.
2. **Generic diagnosis (S2).** `false_reject_capable_001`, `incoherent_guard_001`: **"Not quite — let's walk through it together."** with no specific error named (these are also B2 fallbacks).
3. **Skips the teaching beat.** `average_off_method_correct_001` (mean 0.65): **"Got it — that's right. Here's the next one:"** — affirms and advances but does not model the canonical `360 ÷ 3 = 120` the scenario requires (S4=0).

**Fixes.** B2 (recovers verdict-aware replies); B3 (N/A, ~3 scenarios); **P4 — teach-beat rule** (*"Include at least one teaching sentence — the rule, a worked step, or the canonical method — before posing the next question"*); **answer-the-question rule** (*"When the student asks a genuine clarification, answer it in ≤2 sentences, then re-anchor — do not just advance"*); P1 specific-error. Format/temperature unchanged (XML, default 1.0). **Projected: 68% → ~80%.**

## B.2 — `gemini-2.5-flash` — 62% (23 failures)

**Inventory.** Call-2 failure: 27; leak 0; N/A 8/23. Items: S4-guidance 21, S1-affirm 20 (2 N/A), S2-specific 20 (6 N/A), S5-actionable 16. Dimension providing_guidance 14. Persona split is even (average 9, struggler 9) — it struggles equally with confirmation pacing and struggler scaffolding.

**Genuine pedagogy.** Same family signature as 2.5-pro: terse fallbacks, generic diagnosis, and dropped teaching beat, but with more S1 (affirmation) misses on struggler turns. **Fixes:** identical to B.1 (B2, B3, P4, P1). **Projected: 62% → ~74%.**

## B.3 — `gemini-3.5-flash` — 58% (25 failures)

**Inventory.** Call-2 failure: **43** (highest of all 10 models); leak 0; N/A 9/25. Items: **S4-guidance fails in all 25 failures**, S2-specific 23 (8 N/A), S1-affirm 21. Dimension providing_guidance 19.

**Genuine pedagogy.** The S4=25/25 pattern is the B2 fingerprint at scale — the guidance-bearing second call is failing 43 times, so almost every failure is a guidance-less fallback (`math_average_arithmetic_slip_001`: **"Not quite — let's walk through it together."**). After B2, the residual is the family under-teaching tendency. **Fixes:** B2 (largest beneficiary), B3, P4, P1. **Projected: 58% → ~72%.**

## B.4 — `gemini-3.1-pro` — 52% (29 failures, weakest Gemini)

**Inventory.** Call-2 failure: 39; leak 0; N/A 8/29. Items: **S2-specific fails in all 29**, S1-affirm 28, S4-guidance 27, S6-coherent 18. Dimensions: providing_guidance 15, **mistake_location 12, coherence 12** — markedly higher than the 2.5 line.

**Genuine pedagogy.** Beyond B2, the 3.1-pro shows a *distinct* weakness the 2.5 models do not: **mis-diagnosis and incoherence**. `average_off_method_correct_001` (mean 0.25): **"Not quite — let's walk through it together."** then poses a malformed follow-up (*"5 equal angles around a point… A) 355° B) 36° C) 365° D) 72°"* — distractors that don't reflect the misconception), and `math_average_wrong_mcq_001`: **"Let's keep going."** The 3.x line both mis-locates errors (mistake_location 12) and contradicts context (coherence 12). **Fixes:** B2 + B3 recover a portion, but the genuine mis-diagnosis/coherence gap is larger here — add P1 (specific error) and a coherence/grounding rule (*"Base the verdict and any follow-up only on the student's actual answer and the in-flight question"*). This model needs the most work of the five. **Projected: 52% → ~65%** (genuine gaps cap the gain).

## B.5 — `qwen2.5:14b` — 58% (25 failures, on-device; genuine capability)

**Inventory.** Call-2 failure: **0** (Ollama path — unaffected by B2); tool-call leak: 0; N/A artifact: 10/25. Items: S1-affirm 22 (1 N/A), S2-specific 19 (9 N/A), S6-coherent 17, S4-guidance 15. Dimensions: human_likeness 16, reveals_answer 12, coherence 12, mistake_identification 11. This is the same genuine-capability profile as `qwen2.5:7b` (Part A §3.5), slightly stronger.

**Genuine pedagogy.**
1. **Affirming wrong answers (false-accept) — most serious.** `math_wrong_mcq_no_praise_001` (mean 0.40): student picked a wrong option, reply is **"Great job! You correctly identified that all angles around a point sum to 360°…"** The model is not reliably consuming the grader verdict (mistake_identification fails 11×).
2. **Answer/rule leakage (reveals_answer 12 — highest in this cohort).** `no_unfounded_praise_001`: **"…Remember, smaller denominators indicate larger…"** — hands the rule that gives away the answer.
3. **Coherence / dismissive framing.** `incoherent_guard_001`: poses a new item with **"The answer should be straightforward… Go ahead and give it a try!"** — ignores the incoherence and mildly pressures.

**Fixes.** **P3 — verdict-adherence (top priority)** (*"The grader decides correctness; if the in-flight answer is graded incorrect you must not say 'correct'/'great job'"*) + 1–2 few-shot exemplars (small instruct models benefit most); **P5 — conciseness + no-reveal**; B3 (N/A, ~3). No engine fix applies. Format/sampling unchanged (Markdown, 0.7/0.8/20). **Projected: 58% → ~70%.** As with the 7B, the mid-size ceiling makes 90% unrealistic on-device; it is a strong *server-class* on-device option, not a frontier substitute.

## B.6 — Updated projections (all ten models)

| Model | Eval 1 | + engine/rubric (B1,B2,B3) | + prompt fixes | 90% candidate? |
|---|--:|--:|--:|:--:|
| qwen3-next-80b-instruct | 75% | ~88% | ~90–92% | **likely** |
| deepseek-v3.1 | 70% | ~84% | ~86–90% | **plausible** |
| qwen3-next-80b-thinking | 80% | ~84% | ~86–89% | borderline |
| kimi-k2-thinking | 73% | ~80% | ~82–85% | no (this iter) |
| **gemini-2.5-pro** | 68% | ~78% | **~80–84%** | no |
| **gemini-2.5-flash** | 62% | ~72% | ~74–78% | no |
| **gemini-3.5-flash** | 58% | ~70% | ~72–76% | no |
| **gemini-3.1-pro** | 52% | ~62% | ~64–68% | no |
| **qwen2.5:14b** (on-device) | 58% | ~66% | ~68–72% | no (size ceiling) |
| qwen2.5:7b (on-device) | 62% | ~70% | ~74–78% | no (size ceiling) |

**Conclusions from Part B.**
1. **B2 is now the highest-priority engineering fix overall** — confirmed across both the Gemini API path (AttributeError) and the MaaS path (TypeError), degrading the verdict-aware second call on **6 of the 10 models** (27–52 failures each). One normalisation fix at the two-call boundary lifts the entire Gemini family plus DeepSeek and Kimi.
2. **The Gemini family is B2-bound, then teaching-bound.** After B2, its residual gap is the genuine under-teaching/terseness tendency (drop-the-teaching-beat, answer-avoidance on clarification) — addressable with the P4 teach-beat and answer-the-question rules, but the family does not reach 90% this iteration on this task. `gemini-3.1-pro` additionally suffers mis-diagnosis/incoherence and is the weakest of the five.
3. **`qwen2.5:14b` mirrors `qwen2.5:7b`** — no engine bug, genuine false-accept/reveal/coherence gaps typical of its size class; a strong server-class on-device option, not a frontier substitute.
4. The 90%-track models remain the open-weight `qwen3-next-80b-instruct` and `deepseek-v3.1` (Part A), whose suppression is overwhelmingly B1/B2/B3, not capability.
