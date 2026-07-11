# Multi-Turn Tutor Evaluation — Sweep 2

*Post-remediation benchmark of 16 models on the core15 subset. Covers per-model performance, sweep-1 comparison, bottlenecks and fixes (prompt vs. engine), the Anthropic advantage, the single-turn vs. multi-turn gap, and the feasibility of offline tutoring.*

*AI Tutor — Nyansapo · Evaluation harness (simple_tutor engine) · core15 stratified subset (15 scenarios)*

---

## 1. Executive summary

Sweep 2 re-ran the multi-turn benchmark after a round of engine and prompt fixes aimed at the non-Anthropic models. The data is clean: zero judge outages and zero errored scenarios across all 16 models, so every score is the model's own behaviour, not an infrastructure artefact.

- **Anthropic still leads decisively.** Claude Opus 15/15 (100%), Claude Haiku 12/15 (80%). Every non-Anthropic model scores 53% or below. The gap is large and consistent.
- **The fixes were net positive.** 8 of the 13 models with a sweep-1 baseline improved, most sharply the OSS Qwen family and the flagship qwen3-next-80b-instruct (3→8 / +5). The control, Claude Opus, held at 15/15 — confirming the changes never touched the Anthropic path.
- **But two Gemini models regressed.** gemini-3.5-flash fell 13→8 and gemini-3.1-pro 6→3. The uniform tool-forcing that rescued weak models fights strong ones that were already composing good combined turns.
- **One failure mode dominates.** Across every model the failing sessions fail the same way — they run out of turns without the lesson ever advancing. Pass rate tracks lesson-completion almost perfectly. The bottleneck is protocol and state discipline, not teaching quality.
- **Single-turn ≠ multi-turn.** Single-turn scores overstate real tutoring ability. Claude barely drops (82%→80%); several Gemini models collapse (gemini-3.1-pro 58%→20%). Multi-turn is the deployment-relevant number.

## 2. What sweep 2 tested

The benchmark drives the production `simple_tutor` engine through complete tutoring sessions. A simulated student (Anthropic Haiku) plays six personas across four lessons in maths and geometry; a separate rubric judge (also Haiku, temperature 0) scores each finished trajectory. A scenario "passes" when the session reaches its exit ticket with the expected trajectory and clears the rubric threshold.

**Subset — core15.** Sweep 2 ran the stratified 15-scenario subset rather than the full 30. core15 holds all six personas, both subjects, all four lessons and the turn-budget extremes, and reproduces the full-30 ranking (Spearman 0.97) at half the wall-clock. All sweep-1 comparisons below are computed on exactly the same 15 scenario IDs, so the two sweeps are directly comparable.

**The 16 models fall in three tiers.**

- **Anthropic (2):** Claude Opus 4.8 and Claude Haiku 4.5. These are the control — the engine and its prompts were authored and tuned on Claude.
- **Cloud non-Anthropic (6):** gemini-2.5-flash / 2.5-pro / 3.5-flash / 3.1-pro, plus Vertex Model-Garden qwen3-next-80b (instruct and thinking). Cloud APIs.
- **Open-source Qwen (8):** qwen2.5 (32b, 72b), qwen3 (4b, 14b, 30b-a3b), qwen3.5 (4b, 9b), qwen3.6 (35b-a3b) — run locally through Ollama. This is the offline-deployment tier.

**What changed since sweep 1.** Between the two sweeps we shipped, for non-Anthropic models only: tool-choice forcing (pose a question when one is due, grade when an answer is in flight), a per-turn duplicate-tool cap, a repair path folded into the second model call, recovery of tool calls emitted as plain text, per-family pedagogy prompts, and a raised output-token ceiling for qwen2.5. The Anthropic template was left byte-for-byte unchanged.

## 3. Sweep 2 leaderboard (core15, out of 15)

| Model | Tier | Pass | Rate | Rubric | Complete / Timeout / Deadlock |
|---|---|---|---|---|---|
| **claude-opus-4-8** | Anthropic | **15 / 15** | 100% | 0.92 | 12 / 3 / 0 |
| **claude-haiku-4-5** | Anthropic | **12 / 15** | 80% | 0.69 | 12 / 3 / 0 |
| gemini-2.5-pro | Gemini | 8 / 15 | 53% | 0.64 | 10 / 5 / 0 |
| gemini-3.5-flash | Gemini | 8 / 15 | 53% | 0.71 | 10 / 5 / 0 |
| qwen3-next-80b-instruct | Qwen-MaaS | 8 / 15 | 53% | 0.60 | 7 / 7 / 1 |
| gemini-2.5-flash | Gemini | 7 / 15 | 47% | 0.61 | 11 / 4 / 0 |
| qwen3.6:35b-a3b | OSS Qwen | 6 / 15 | 40% | 0.59 | 4 / 10 / 1 |
| qwen3.5:9b | OSS Qwen | 5 / 15 | 33% | 0.58 | 8 / 7 / 0 |
| gemini-3.1-pro | Gemini | 3 / 15 | 20% | 0.51 | 3 / 12 / 0 |
| qwen2.5:72b | OSS Qwen | 3 / 15 | 20% | 0.46 | 4 / 11 / 0 |
| qwen3:14b | OSS Qwen | 3 / 15 | 20% | 0.55 | 5 / 8 / 2 |
| qwen3:30b-a3b | OSS Qwen | 3 / 15 | 20% | 0.53 | 2 / 13 / 0 |
| qwen2.5:32b | OSS Qwen | 2 / 15 | 13% | 0.50 | 7 / 8 / 0 |
| qwen3.5:4b | OSS Qwen | 2 / 15 | 13% | 0.49 | 3 / 12 / 0 |
| qwen3:4b | OSS Qwen | 1 / 15 | 7% | 0.49 | 0 / 15 / 0 |
| qwen3-next-80b-thinking | Qwen-MaaS | 0 / 15 | 0% | 0.29 | 0 / 1 / 14 |

*Rubric = mean rubric-judge score (0–1) on completed trajectories. The last column is the session-outcome split: "Complete" = reached the exit ticket; "Timeout" = ran out of turns (max_turns); "Deadlock" = the engine detected the tutor and student stuck in a loop. Note how tightly the pass count tracks the "Complete" count — that is the whole story of this benchmark.*

## 4. Sweep 1 → Sweep 2: what moved

Measured on the identical 15 scenario IDs. Positive = the fixes helped.

| Model | Sweep 1 | Sweep 2 | Δ | Notes |
|---|---|---|---|---|
| qwen3-next-80b-instruct | 3 / 15 | 8 / 15 | **+5** | Gained all 5; biggest single win. The flagship offline-capable model. |
| gemini-2.5-pro | 5 / 15 | 8 / 15 | **+3** | +4 gained, 1 lost. Clear net improvement. |
| qwen3.5:9b | 2 / 15 | 5 / 15 | **+3** | Best truly-local mid-size gain. |
| qwen2.5:72b | 0 / 15 | 3 / 15 | **+3** | Off the floor — forcing gave it a protocol it now follows. |
| qwen3:14b | 0 / 15 | 3 / 15 | **+3** | Off the floor. |
| claude-haiku-4-5 | 11 / 15 | 12 / 15 | +1 | Control tier — essentially flat, within noise. |
| qwen2.5:32b | 1 / 15 | 2 / 15 | +1 | Small gain. |
| qwen3.5:4b | 1 / 15 | 2 / 15 | +1 | Small gain. |
| claude-opus-4-8 | 15 / 15 | 15 / 15 | 0 | Control held at ceiling — changes did not touch Anthropic. |
| gemini-2.5-flash | 7 / 15 | 7 / 15 | 0 | Net flat (+3 / −3): the set of passing scenarios churned. |
| qwen3.6:35b-a3b | 6 / 15 | 6 / 15 | 0 | Net flat (+4 / −4): churn, not stability. |
| qwen3:4b | 1 / 15 | 1 / 15 | 0 | Too small to benefit. |
| gemini-3.1-pro | 6 / 15 | 3 / 15 | **−3** | Regressed — lost 4 geometry scenarios. See §8. |
| gemini-3.5-flash | 13 / 15 | 8 / 15 | **−5** | Regressed hardest, from the top of the non-Anthropic pack. See §8. |

*Two new models had no sweep-1 baseline: qwen3:30b-a3b (3/15) and qwen3-next-80b-thinking (0/15).*

Net across the 14 comparable models: eight improved, two regressed, four flat, for **+12 scenario-passes overall**. The direction is right; the two Gemini regressions are the caveat and are analysed in §8.

## 5. Per-model performance

### Anthropic — the control tier

Opus completed 12 of 15 lessons and passed all 15 (three sessions legitimately ran long but still traced correctly). Haiku completed 12 and passed 12. Their rubric scores (0.92, 0.69) are the two highest in the field. Crucially, both call the right tool at the right time and then act on the result — Opus makes the compliant two-call turn 95% of the time and never spams. Anthropic is the ceiling every other tier is measured against; §9 explains why the edge is real and partly structural.

### Gemini — capable single-shot, uneven across turns

- **gemini-2.5-pro:** 8/15, rubric 0.64, 10 lessons completed. The strongest Gemini on trajectory. Improved +3 over sweep 1.
- **gemini-3.5-flash:** 8/15, rubric 0.71 (the best Gemini rubric), 10 completed — but this is down from 13/15 in sweep 1. Its content is good; the forcing cost it completions.
- **gemini-2.5-flash:** 7/15, rubric 0.61, 11 completed — the most completions of any Gemini, yet a middling pass rate because several completed sessions missed the rubric bar. Flat vs sweep 1.
- **gemini-3.1-pro:** 3/15, rubric 0.51, only 3 lessons completed and 12 timeouts. It kept the protocol (94% two-call turns) but used it wrongly — it sprayed the pose-a-question tool ~29 times per turn (6,807 attempts dropped by the cap) and almost never advanced. The clearest case of forcing amplifying bad behaviour. Regressed −3.

### Vertex Model-Garden Qwen (cloud, hosted)

- **qwen3-next-80b-instruct:** 8/15, rubric 0.60, 7 completed. Tied for best non-Anthropic. Up from 3/15 — the single biggest beneficiary of the fixes, because the hosted endpoint honours forced tool-calls natively.
- **qwen3-next-80b-thinking:** 0/15, rubric 0.29, 14 of 15 sessions deadlocked. The "thinking" variant returns its reasoning in a channel the adapter does not read, so no usable tool call ever reaches the engine and every lesson stalls. A pure integration failure, not a capability one — see bottleneck B4.

### Open-source Qwen (Ollama, the offline tier)

This tier is the offline-deployment candidate, so its numbers matter most for the connectivity-constrained pilots.

- **qwen3.6:35b-a3b (6/15):** 40% — the best truly-local model, 0.59 rubric. Still times out on 10 of 15.
- **qwen3.5:9b (5/15), qwen2.5:72b / qwen3:14b / qwen3:30b-a3b (3/15):** 33% and 20% — the mid tier. All improved off sweep 1 but still time out on roughly half.
- **qwen2.5:32b, qwen3.5:4b, qwen3:4b:** 7–13% — the small models. qwen3:4b times out on all 15. Too small to sustain a 15-turn protocol; a couple still leak tool syntax into visible text.

Read the rubric column across this tier (0.46–0.59): when these models do keep a lesson moving, their teaching is mediocre-to-fair, not disastrous. Their problem is reaching the end of the lesson at all — again a protocol ceiling, not a pedagogy floor.

## 6. The dominant bottleneck: lessons that never advance

One pattern explains the leaderboard. Sessions end for one of three reasons — they reach the exit ticket (success), they run out of turns (max_turns), or the engine detects a deadlock. Sort the models by how often they reach the exit ticket and you have reproduced the pass-rate ranking almost exactly:

- Opus completes 12/15 → passes 15. Haiku completes 12 → passes 12.
- gemini-3.1-pro completes 3, times out 12 → passes 3.
- qwen3:4b times out on all 15 → passes 1. qwen3-next-thinking deadlocks 14 → passes 0.

**The mechanism.** The engine advances a lesson only through two tools: one registers a question (with its reference answer) into a server-side slot; the other grades the student's reply against that slot. A question merely written in prose is invisible to the engine. So when a model asks a question without calling the register tool — or grades against a slot that was never filled — the correct answer is scored as ungraded, the step never advances, the tutor re-asks, and the session burns its turn budget until max_turns. One missed tool call cascades into a whole failed lesson.

This is why forcing helped the weak models so much: models like qwen2.5:72b and qwen3-next-80b-instruct simply were not calling the tools, and compelling the call unblocked whole lessons (0→3, 3→8). It is also why it is protocol, not pedagogy: the empty-slot grading event still fired 42 times on gemini-3.5-flash, 31 on gemini-2.5-pro and 52–60 on the small qwen3.5 models even in sweep 2.

## 7. Bottlenecks and their fixes — prompt vs. engine

Each bottleneck below is tagged by where the fix belongs. The pattern is that the heavy hitters are engine/integration problems; prompt tuning is real but secondary.

| # | Bottleneck | Fix belongs to | Solution / status |
|---|---|---|---|
| **B1** | Lesson never advances — model doesn't call the register/grade tool at the right moment, so steps stall and the session times out. | **Engine (dominant)** | Server-authoritative state: auto-advance on a correct answer; treat a clearly correct bare answer as gradeable; make one missed call non-fatal. Tool-forcing (shipped) is the first increment. |
| **B2** | Over-forcing strong models — uniform ANY-mode forcing makes gemini-3.1-pro spray the pose tool ~29×/turn and never advance. | Engine + Prompt | Gate the forcing: let compliant models run free (AUTO) and only force after an observed missed call; for Gemini use ANY constrained to the one allowed function rather than blanket ANY. Prompt: see B6. |
| **B3** | Empty-slot grading — grade tool called with no question in flight; the guard rejects it but the turn is wasted. | Engine | Fold the register step into the same turn when the slot is empty (partly shipped), then grade — so a mis-ordered turn self-heals instead of stalling. |
| **B4** | Hidden-reasoning models — qwen3-next-thinking returns reasoning in an unread channel, so no tool call arrives; 14/15 deadlock. | Engine / adapter | Read the model's reasoning/thinking channel in the adapter, or disable thinking for the tutoring call. Reasoning models also need no CoT scaffolding in the prompt. |
| **B5** | Tool-call-as-text leak — small OSS models emit `grade(answer=…)` as the visible reply (qwen3:14b 3×, qwen3.5:9b 1×). | Adapter + Prompt | Text-call recovery in the adapter (shipped for Ollama) parses and strips it; prompt rule "your visible reply is never a function call". Mostly solved; residual on the smallest models. |
| **B6** | Repetition and Gemini-3 prompt sensitivity — repeated-phrase failures on Gemini; negative/flowery rules hurt Gemini 3 specifically. | Prompt | Rewrite the Gemini family rules positively (Gemini 3 over-indexes on "do not…"), strip persona flourish, and add 2–3 worked combined-turn few-shot examples (Google recommends few-shot for Gemini). |
| **B7** | Mediocre teaching when the lesson does move — OSS rubric 0.46–0.59 vs 0.69 / 0.92 for Anthropic. | Prompt / pedagogy | Per-family pedagogy rules (name the specific error, teach before advancing, accept equivalent answers) — shipped in `family_prompts`, needs eval-driven iteration. |

## 8. Why the two Gemini models regressed

The forcing that rescued the weak models is applied uniformly to every non-Anthropic family, and it makes a wrong assumption for strong tool-users: that the problem is a missing call. For gemini-3.5-flash and gemini-3.1-pro the problem was never a missing call — they were already composing good combined turns. Forcing a call every turn changed their behaviour for the worse.

gemini-3.1-pro is the clean illustration. It kept 94% two-call compliance yet completed only 3 lessons, because under ANY-mode forcing it emitted the pose tool dozens of times per turn (6,807 dropped by the cap) — opening new questions instead of closing the current one and advancing. **Forcing guarantees a tool is called; it does not guarantee it is called correctly.**

The prompting evidence points the same way. gemini-3.1-pro is a Gemini-3 model, and Google's own guidance is explicit that Gemini 3 over-indexes on negative instructions ("do not…") and flowery, Claude-style rules — they degrade its logic. Our shared family prompt is written in that register. So the regression has two compounding causes: an engine policy (blanket forcing) that suits weak models, and a prompt register that suits Claude but not Gemini 3.

**Important caveat.** At 15 scenarios the standard error on a pass rate is roughly ±13 points, and sweep 2 changed several variables at once (forcing, repair, prompts, token ceiling). A single run cannot cleanly attribute a −3 or −5 to one cause. Before we act on the Gemini regressions they should be reproduced — ideally on the full 30 scenarios and with the forcing change isolated. Treat §8 as the leading hypothesis, not a settled result.

## 9. Why Anthropic models have an edge

The gap is real and it is largest exactly where it matters — sustaining a multi-turn lesson. Several factors compound.

1. **Home-field prompt design.** The two-tool protocol and the whole tutor prompt were authored and tuned on Claude. Every other family is being asked to conform to conventions shaped around Anthropic's tool-use behaviour. This is genuine home-field advantage and part of the gap is our instrumentation, not the models.
2. **Structured-prompt adherence.** The prompt is long and heavily structured with XML-style sections. Claude was trained to follow exactly this shape and does so more reliably than models that prefer flatter or example-driven prompts (Gemini) — which is also why a one-size prompt penalises the others.
3. **Tool and state discipline.** Claude calls the right tool at the right time and then acts on the result, turn after turn, without spamming or drifting. This state discipline — track the in-flight question, grade it, advance — is precisely the skill multi-turn rewards, and it is where the other families break down.
4. **Shared-vendor evaluation (a caveat, not the cause).** The simulated student and the rubric judge are also Anthropic (Haiku). The judge runs at temperature 0 on a behavioural rubric and most of the pass/fail signal comes from whether the lesson actually completed (engine state, not judge taste), so this is a minor confound rather than the main effect — but intellectual honesty requires naming it. A fully neutral evaluation would use a third-party judge.

Net: most of the edge is a real capability difference in multi-turn tool/state discipline, amplified by a prompt and protocol built on Claude. Both halves are actionable — per-family prompts and a more forgiving engine narrow it.

## 10. Why multi-turn scores are far below single-turn — and what it means

The same models look much stronger in the single-turn benchmark. The comparison is stark:

| Model | Single-turn | Multi-turn | Δ | Reading |
|---|---|---|---|---|
| claude-haiku-4-5 | 82% | 80% | −2 | Holds — maintains state across turns. |
| gemini-2.5-pro | 43% | 53% | +10 | Actually better multi-turn (small N). |
| gemini-3.5-flash | 50% | 53% | +3 | Roughly flat. |
| qwen3-next-80b-instruct | 65% | 53% | −12 | Drops — protocol burden across turns. |
| gemini-2.5-flash | 65% | 47% | −18 | Drops materially. |
| gemini-3.1-pro | 58% | 20% | **−38** | Collapses. |

**Why the gap exists.** The single-turn harness hands the model a question that is already registered in the engine and asks for one good reply; it does not require the model to drive the register→grade→advance loop or to carry state across turns. Multi-turn requires all of that, every turn, for ~15 turns — and errors compound: one missed grade stalls the lesson, the tutor repeats, and the session times out. A model can be excellent at "produce one good tutoring reply" and still fail at "run a whole lesson".

**What it means.**

- Single-turn numbers overstate deployable tutoring ability. The multi-turn score is the one that predicts what a student actually experiences.
- The differentiator between models is not knowledge or single-reply quality — it is sustained tool/state discipline. Claude has it (82→80); most others lose it (gemini-3.1-pro 58→20).
- The largest lever is therefore the engine, not the model. Making state server-authoritative and advancement forgiving would lift every non-Anthropic model's multi-turn score without changing the model at all.

## 11. Is offline tutoring possible?

"Offline" means running the open-source Qwen tier locally (Ollama, no cloud API) — the path for low-connectivity pilots. The honest answer is: **not yet at Claude quality for autonomous lessons, but plausibly feasible for structured, supervised use with engine changes.**

**Where it stands today.**

- Best truly-local model (qwen3.6:35b-a3b) reaches 40%; qwen3.5:9b 33%. Small models (≤4b) sit at 7–13%. None is near Opus (100%) or Haiku (80%).
- But the OSS failures are dominated by the protocol/timeout bottleneck, not by bad teaching: the rubric scores on completed lessons (0.46–0.59) are fair, not broken. The models can teach; they cannot reliably finish a lesson under the current engine.

**What would make it feasible.**

1. **Engine hardening.** Server-authoritative state and forgiving advancement (B1/B3) remove most of the timeouts that are sinking these models. This is the highest-leverage change and it helps every OSS model at once.
2. **Right-size the model.** A model in the qwen3.6:35b class (~20–30 GB, runs on a single workstation GPU) is the realistic offline target — big enough to teach, small enough to deploy. The sub-10b models are not viable for autonomous multi-turn.
3. **Close the tool-protocol gap.** Even a small amount of fine-tuning on the two-tool protocol, or the thinking-channel fix (B4) for the reasoning variants, would convert protocol failures into completions.
4. **Hybrid deployment.** Where connectivity exists, fall back to cloud Claude; offline mode is the degraded-but-usable tier, not the default.

**Bottom line.** Fully offline, autonomous, Claude-quality tutoring is not achievable with today's open models on today's engine. Offline tutoring that is good enough for structured, supervised lessons is a realistic 2–3 step target: harden the engine so completion no longer depends on perfect tool discipline, standardise on a ~35b Qwen, and close the protocol gap. The evidence that this will work is that the OSS models already teach acceptably when they reach the end of a lesson — we mostly need to help them get there.

## 12. Recommendations and next steps

1. **Gate the tool-forcing (engine).** Rather than force every non-Anthropic family every turn, force only after an observed missed call, and let compliant models (the strong Geminis) run free. This is the direct fix for the sweep-2 regressions while keeping the Qwen gains.
2. **Harden the engine.** Move state to the server and make advancement forgiving (B1/B3) — the single highest-leverage change for every non-Anthropic model, offline tier included.
3. **Fix the Gemini and thinking-model prompts.** Rewrite the Gemini family prompt positively, strip flourish, add few-shot combined-turn examples (B6); read the thinking channel for reasoning variants (B4).
4. **Confirm the regressions.** Before acting on the −3/−5 Gemini regressions, reproduce them on the full 30 scenarios with the forcing change isolated. At N=15 the noise band is wide and several variables moved at once.
5. **Sweep 3 discipline.** Isolate one variable at a time, keep Opus as the fixed control, and track the exit-ticket completion rate as the primary metric — it is what actually moves the score.

## 13. Caveats on reading these numbers

- **Small N.** 15 scenarios per model → roughly ±13-point standard error on a pass rate. Single-scenario flips are inside the noise; trends across many models are the trustworthy signal.
- **Multiple variables moved.** Forcing, repair, prompts and token limits all changed between sweeps, so a per-model delta cannot be attributed to one cause. Sweep 3 should move one lever at a time.
- **Shared-vendor evaluation.** Simulated student and rubric judge are both Anthropic Haiku at temperature 0. The dominant pass signal is engine completion, so this is a minor confound — but a fully neutral run would use a third-party judge.
- **Data integrity confirmed.** Zero judge outages and zero errored scenarios across all 16 models — the scores are the models' own behaviour.
