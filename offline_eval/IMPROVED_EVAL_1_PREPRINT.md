# Fair-Shot Benchmarking of Instruction-Tuned LLMs as Socratic Tutors: An Improved 30-Model Evaluation on a 5E Secondary-School Task

**Authors:** AI Tutor Research Team, Nyansapo / Pixel Design Labs LLC
**Status:** Preprint (Evaluation 1 — "Improved Evaluation"). Internal technical report; formatted for arXiv/workshop submission.
**Date:** 2026-06-27
**Artifacts:** `offline_eval/results2/` (30 per-model result JSONs), `offline_eval/leaderboard_combined.csv` (Evaluation 0 baseline), `offline_eval/PROMPT_ENGINEERING_FRAMEWORK.md` (per-family tuning spec), `apps/llm/model_profiles.py`, `apps/tutoring/simple_tutor/`.

---

## Abstract

**Motivation.** Selecting a large language model (LLM) to drive a conversational, tool-using secondary-school tutor is a high-stakes deployment decision, especially for low-connectivity contexts (Mozambique, Tanzania) where on-device or open-weight models are preferred for cost and data residency. A prior internal benchmark (Evaluation 0) ranked 44 models but, on audit, scored every model under a single Anthropic-style prompt, a blanket near-zero temperature, and a hardcoded 1,024-token generation cap — confounds that systematically penalised entire model families, most severely the reasoning ("thinking") models.

**Objectives.** We re-run the benchmark (Evaluation 1) under a *fair-shot* protocol: each model family is prompted and sampled according to its vendor-documented best practice, centralised in a single per-family configuration registry, while the task, dataset, judge, and synthetic-student simulator are held fixed. The goal is a defensible ranking and a path toward a non-Anthropic model that can reach the Anthropic-Opus ceiling (≈90% pass).

**Methodology.** Thirty models across thirteen families drive the production `simple_tutor` engine over 60 single-turn 5E tutoring scenarios (Form-3 angle geometry and map skills) instantiated with six synthetic-student personas. Each turn is graded by a pinned cross-vendor judge (Claude Haiku 4.5, temperature 0) against a deterministic layer, 2–3 scenario-specific rubric items, and an 8-item BEA-aligned standard rubric, plus eight advisory pedagogical dimensions.

**Key findings.** Fair-shot prompting changes the picture dramatically. The previously last-ranked thinking models recover to the top: `qwen3-next-80b-thinking` rises from **2% → 80%** and leads all non-Anthropic models; `deepseek-r1` from **22% → 55%**; `kimi-k2-thinking` from 57% → 73%. The "suspect" `gemini-2.5-pro` anomaly (43%, below its own Flash sibling) resolves to 68% once the token cap is lifted. A controlled prompt-format experiment shows Qwen genuinely prefers Markdown (+8 pp on `qwen3-next-80b-instruct`) while Gemini is *hurt* by it (−17 pp on `gemini-2.5-pro`), contradicting the vendor's general guidance for this complex prompt. The best on-device model, `qwen2.5:7b`, reaches **62%**, matching `gemini-2.5-flash`.

**Major bottlenecks.** Across all 30 models, the binding constraints are **providing actionable guidance (55% of turns adequate)** and **human-likeness (64%)**, not tone (100%) or mistake localisation (93%). Three scenario types dominate failures across even the strongest models: concise clarification ("info-dump guard"), specific-misconception diagnosis, and affirm-and-teach-the-canonical-method.

**Recommendations & conclusion.** A latent rubric defect — conditional standard items scored 0.0 when not applicable — systematically understates the strongest models and is the single highest-value fix before Evaluation 2. With that fix plus targeted prompt scaffolding, we project the lead open-weight candidate, **Qwen3-Next-80B**, into the 85–90%+ band. We recommend Qwen3-Next-80B (open-weight, deployable, data-residency-friendly) as the primary non-Anthropic candidate and `qwen2.5:7b` for on-device deployment.

---

## 1. Introduction

### 1.1 Why this evaluation matters

The Nyansapo AI Tutor delivers conversational instruction to secondary-school students following the 5E instructional model (Engage, Explore, Explain, Elaborate, Evaluate). Pedagogy is enacted through *tool calls*: the model poses a question, the platform persists it, the student answers, the model grades and advances. The choice of underlying LLM therefore determines not just answer accuracy but pedagogical behaviour — whether the tutor scaffolds rather than reveals, diagnoses specific misconceptions, adapts to a struggling versus a capable student, and hands the conversational floor back with a concrete next action.

The production pilot runs on a hosted Anthropic model. For data residency and offline use in low-connectivity schools (Mozambique, Tanzania), the project requires a model that can run **on-device** (phone/tablet, modest laptop) or as a self-hosted open-weight model, ideally approaching the proprietary ceiling. Identifying such a model is the central practical objective.

### 1.2 Limitations discovered in Evaluation 0

Evaluation 0 (the `leaderboard_combined.csv` baseline) ranked 44 models on the same 60-scenario harness and concluded, among other things, that "pure-thinking models truncate badly" and are unsuitable for the tool-driven tutor. An audit against current vendor documentation (consolidated in `PROMPT_ENGINEERING_FRAMEWORK.md`) traced that conclusion to **three harness artifacts**, not to model capability:

1. **A hardcoded 1,024-token generation cap** at the single tutor call site (`apps/tutoring/simple_tutor/engine.py::_call_llm`). A reasoning model spends its entire budget on the internal `<think>` trace and is truncated *before* it can emit the tool call, scoring near zero.
2. **A blanket near-zero temperature.** For models invoked via the runtime override, the synthetic `ModelConfig` was created with `purpose=JUDGE`, whose `effective_temperature` is 0. Every cloud model was therefore sampled at temperature ≈ 0, with no `top_p`/`top_k`/penalty control and no thinking-mode toggle — contradicting, e.g., Qwen's explicit "do not use greedy decoding for thinking models" guidance.
3. **A single Anthropic-style (XML-tagged) prompt for all families.** XML delimiters are Claude's native structural device; vendor guidance for Gemini and Qwen favours Markdown and (for Gemini) positive framing.

These confounds do not affect models well-served by the defaults (XML-native, instruct-mode, short outputs) but heavily penalise reasoning models and non-XML families — precisely the families a fair comparison must include.

### 1.3 Objectives of the Improved Evaluation

1. Re-rank all viable models under a **fair-shot** protocol: per-family prompt format, sampling, generation budget, and reasoning mode set to vendor best practice, centralised in one registry, with the task/dataset/judge held fixed.
2. Quantify the magnitude of the Evaluation 0 artifacts (per-model Δ pass rate).
3. Empirically test, rather than assume, vendor prompt-format guidance (XML vs Markdown) per family.
4. Characterise the residual, capability-driven bottlenecks that fair-shot prompting does *not* fix, and tie them to the rubric.
5. Identify the strongest non-Anthropic candidate(s) and a concrete path to 90%+ pass.

---

## 2. Related Work

**LLM benchmark methodologies.** Static knowledge benchmarks (MMLU, BIG-bench) and contamination-resistant successors measure declarative knowledge but not interactive teaching behaviour. Our harness is closer to *agentic* / task-completion evaluation: the model drives a stateful engine through tool calls, and we score the resulting trajectory, not a single answer.

**Instruction-following and prompt-sensitivity evaluation.** A consistent finding in the literature (e.g., format-perturbation studies reporting double-digit accuracy swings from meaning-preserving prompt edits) is that LLM outputs are highly sensitive to surface prompt form, and that "best" prompts do not transfer across models. This directly motivates our fair-shot protocol: holding the prompt fixed across families does not yield a fair comparison; it yields a comparison biased toward whichever family the fixed prompt happens to suit.

**Rubric-based and LLM-as-judge evaluation.** Using a strong LLM as a rubric grader is now standard for open-ended generation. Known risks include self-preference bias (a judge favouring its own family) and calibration noise. We mitigate the first by pinning a *cross-vendor* judge (Anthropic Haiku 4.5) — none of the 30 evaluated tutors are Anthropic models, so no model grades itself — and the second by pinning model and temperature (0) for reproducibility.

**Pedagogical-ability evaluation (BEA 2025).** Our standard rubric and the eight advisory dimensions are aligned with the BEA-2025 shared task on pedagogical ability assessment of AI tutors, which decomposes tutor quality into mistake identification, mistake location, providing guidance, and actionability, among others. We extend these with tone, answer-leakage, coherence, and human-likeness.

**Model comparison studies.** Public leaderboards (LMSYS Arena, open-LLM leaderboards) rank models on generic chat or static tasks. Our contribution is a *task-specific, behaviourally-scored, fair-shot* comparison on a real production engine, with per-family tuning treated as a first-class experimental variable.

---

## 3. Experimental Setup

### 3.1 Models evaluated

Thirty models spanning thirteen families were evaluated. Cloud models ran via Google Vertex AI Model Garden (Model-as-a-Service, OpenAI-compatible endpoint) and the Gemini Developer API; open-source (OSS) models ran via Ollama on a Google Colab T4 GPU. The judge and synthetic student were held constant on Anthropic for every run.

| Family | Vendor | Models (this evaluation) | Type | Rationale for inclusion |
|---|---|---|---|---|
| **Anthropic** | Anthropic | opus-4-7, haiku-4-5, sonnet-4-6 (Eval 0 ceiling, not re-run) | Proprietary | Production incumbent; reference ceiling |
| **Qwen** | Alibaba | qwen3-next-80b-{instruct,thinking}, qwen3-coder-480b, qwen3-235b-instruct, qwen2.5:{7b,14b} | Cloud MaaS + OSS | Strongest OSS family in Eval 0; on-device candidate |
| **xAI Grok** | xAI | grok-4.1-fast-{reasoning,non-reasoning}, grok-4.20-{reasoning,non-reasoning} | Cloud MaaS | Best non-Anthropic in Eval 0 |
| **Google Gemini** | Google | gemini-2.5-{flash,pro}, gemini-3.1-pro, gemini-3.5-flash | Proprietary | Managed-API alternative; multimodal |
| **DeepSeek** | DeepSeek | deepseek-v3.1, deepseek-v3.2, deepseek-r1 | Cloud MaaS | Leading open-weight reasoning/chat |
| **Moonshot Kimi** | Moonshot | kimi-k2-thinking | Cloud MaaS | Agentic, long-horizon tool use |
| **Zhipu GLM** | Zhipu | glm-4.7, glm-5, glm4:9b | Cloud MaaS + OSS | Agentic/coding-tuned; OSS option |
| **Mistral** | Mistral | mistral-nemo:12b, mistral:7b | OSS | Multilingual (Portuguese); on-device |
| **Meta Llama** | Meta | llama3.1:8b | OSS | Widely deployed OSS baseline |
| **IBM Granite** | IBM | granite3.1-dense:8b | OSS | Enterprise-licensed OSS |
| **Cohere** | Cohere | command-r7b, aya-expanse:8b | OSS | Multilingual tool-use OSS |
| **Nous / Microsoft / TII** | — | hermes3:8b, phi4, falcon3:10b | OSS | Strong general OSS baselines |

Reasoning ("thinking") variants emit an internal chain-of-thought before answering; instruct/non-reasoning variants answer directly. This distinction is the dominant variable in the tool-loop setting (§6).

### 3.2 Dataset

The evaluation corpus (`evals/dataset/`) comprises **60 single-turn scenarios** (a 20-scenario multi-turn set exists but is out of scope here). Each scenario is a YAML file specifying a seeded conversation state and one graded student utterance.

**Coverage.** The dataset is deliberately *narrow and deep*: four Form-3 lessons across two subjects — **angle geometry** (lessons 1137 "Angles around a point", 1138 "Angles on a straight line / intersecting lines") and **map skills** (1463 "Large vs small scale maps", 1464 "Compass points and bearings"). The single-turn subject split is math-heavy; there is no reading subject in this corpus.

| Category (subdir) | Scenarios | Focus |
|---|--:|---|
| crosscutting | 24 | tone, engagement, off-topic redirect, info-dump guard, banned openers, safety/distress |
| math | 16 | bare answers, wrong MCQ, arithmetic slips, method acceptance |
| geography | 10 | scale, bearings, MCQ, clarification |
| personas | 5 | persona-specific stress tests |
| format | 3 | conciseness, formatting tolerance |
| pedagogy | 2 | scaffolding, wrong-answer diagnosis |
| **Total** | **60** | |

**Personas.** Each scenario is instantiated with one of six synthetic-student personas (`apps/tutoring/student_sim/personas.py`), which set the difficulty and behavioural challenge:

| Persona | First-attempt accuracy | Behavioural challenge to the tutor |
|---|---|---|
| `struggler` (T=0.8) | ~30% | unpredictable; multi-step fails; vague reasoning ("idk i guessed") |
| `average` (T=0.7) | ~65% | one-step reliable; slips on novel framing; the "should just work" case |
| `capable` (T=0.5) | ~90% | fast, terse; tests tutor *restraint* (no over-scaffolding/false praise) |
| `error_prone` (T=0.6) | low (by design) | always commits a *specific*, traceable wrong answer (BEA coverage instrument) |
| `probe_resistant` (T=0.6) | ~60% | refuses to show working; tests working-request flow without phrase-looping |
| `non_responder` (T=0.5) | minimal | monosyllabic ("ok", "idk"); engine must not advance on "ok" |

**Schema and a representative example.** Each scenario specifies `id`, `description`, `persona`, `subject`, `lesson_id`, `tags`, `seed_history`, `student_turn`, a `seed_inflight_question` (so the engine enters GRADE mode rather than re-posing), `assertions` (deterministic/label guards), a `rubric` (scenario-specific items + the standard block), and `pass_threshold` (default 0.7). A canonical math example:

```yaml
id: math_capable_correct_bare_001
description: CAPABLE gives a correct bare numeric answer ("110")… Tutor must
  advance, not invoke any working-request phrases.
persona: capable
subject: math
lesson_id: 1137
tags: [advance, math, pedagogy, capable, bare_answer]
seed_history:
  - role: tutor
    text: "Try this: angles around a point of 90°, 160°, and x. Find x."
student_turn: "110"
seed_inflight_question:
  question_text: "Try this: angles around a point of 90°, 160°, and x. Find x."
  question_type: short_numeric
  reference_answer: "110"
assertions:
  response_nonempty: true
  must_not_label: [ASK_WORKING, BANNED_OPENER]
rubric:
  - "Confirms '110' is correct briefly"
  - "Does NOT demand working from a capable student who answered fast and correctly"
  - "Advances to a slightly harder variant or a follow-up question"
  # …followed by the 8-item BEA-aligned standard block (see §4)
pass_threshold: 0.7
```

**Why this dataset is appropriate.** The scenarios isolate *pedagogical decision points* (affirm vs probe, hint vs reveal, scaffold vs advance, redirect vs lecture) rather than declarative knowledge, which is the behaviour that differentiates tutoring models. Persona instantiation stress-tests adaptation. The narrow lesson scope controls for content difficulty, so cross-model variance reflects tutoring behaviour rather than topic coverage. The principal dataset limitation — two subjects, four lessons, no reading — is discussed in §8.4 and §9.1.

### 3.3 Evaluation procedure

**Engine under test.** Every model drives the production `simple_tutor` engine (`SIMPLE_TUTOR_ENGINE=1`). A turn executes a two-call tool loop: (Call 1) the model decides whether to teach or to call a tool (`pose_question`, `record_answer`, `request_figure`, `redirect_off_topic`, `advance_step`); the platform dispatches the tool; (Call 2) the model composes the student-facing reply with the tool verdict in hand.

**System-prompt structure.** The system prompt is assembled in three cache-scoped blocks (`apps/tutoring/simple_tutor/prompts.py`): Block 0 — role, rules, safety, tool schemas (static per conversation); Block 1 — current step content, enabling objective, teaching notes, question pool (static per step); Block 2 — KB retrieval, history summary, recent turns, in-flight question (per turn). The graded student utterance is the final user message.

**Fair-shot per-family configuration.** The Evaluation 1 contribution is `apps/llm/model_profiles.py`, a single registry mapping each model (by override spec, with a family-pattern fallback) to: sampling (`temperature`, `top_p`, `top_k`, penalties), generation budget (`max_tokens`), reasoning-mode toggle (`extra_body`), and prompt format (`xml`|`markdown`). Representative settings, derived from `PROMPT_ENGINEERING_FRAMEWORK.md`:

| Family / mode | temperature | top_p | top_k | max_tokens | prompt format |
|---|---|---|---|---|---|
| Qwen — thinking | 0.6 | 0.95 | 20 | 32,768 | markdown |
| Qwen — instruct | 0.7 | 0.8 | 20 | 16,000 | markdown |
| Gemini (all) | (default 1.0) | — | — | 8,192 / 4,096 | xml |
| Grok — reasoning | 0.6 | — | — | 8,192 | xml (persona-suppressed) |
| DeepSeek — chat | 0.0 | — | — | 4,096 | xml |
| DeepSeek — R1 | 0.6 | 0.95 | — | 32,768 | xml |
| Kimi — thinking | 1.0 | 0.95 | — | 16,000 | xml |
| Mistral Nemo | 0.3 | — | — | 1,024 | xml |

These overrides are applied *at the eval call site* via explicit sampling parameters, which bypass the production `effective_temperature` clamp without modifying it — the production tutor is untouched.

**Scoring pipeline (three layers).**
1. **Deterministic (Layer 1):** regex/label assertions (`response_nonempty`, `must_not_label`, plus two universal guards `meta_reasoning_leak:false` and `passive_ending:false`).
2. **LLM rubric (Layer 3a, gating):** 2–3 scenario-specific items + the 8-item BEA-aligned standard block, each scored 0.0–1.0 by the judge; the layer passes iff the mean ≥ `pass_threshold` (0.7 default).
3. **Pedagogical dimensions (Layer 3b, advisory):** eight binary dimensions in one judge call; reported but non-gating (to avoid ~30% spurious failures from compounding ~5% per-dimension calibration noise across eight dimensions).

A scenario **passes iff Layer 1 ∧ Layer 3a pass.** The headline **pass rate** is the fraction of the 60 scenarios passed; the **rubric score** is the mean Layer-3a item score across scenarios.

**Judge and student simulator.** Both are pinned to Anthropic Claude Haiku 4.5 at temperature 0 (`evals/scorers/llm_rubric.py`, `DEFAULT_RUBRIC_JUDGE`; `max_tokens=4096` after a truncation fix). Because no evaluated tutor is an Anthropic model, the judge is cross-vendor for every row, eliminating self-preference bias.

**Reproducibility.** Each run records `git_sha`, timestamps, and per-scenario transcripts in `offline_eval/results2/<model>.json`. A run is reproduced with:
```
RESULTS_DIR=offline_eval/results2 TUTOR_MODEL_OVERRIDE=<spec> \
  python manage.py run_eval --single-turn
python offline_eval/aggregate.py     # leaderboard
```

---

## 4. Evaluation Rubric

The rubric has three layers; this section details each criterion, its purpose, scoring, and characteristic failures.

### 4.1 Layer 1 — Deterministic assertions

Cheap regex/label checks applied before any LLM call. The two universal guards are pedagogically load-bearing:

| Assertion | Measures | High-quality response | Common failure |
|---|---|---|---|
| `response_nonempty` | the model produced a student-facing reply | any non-empty reply | empty candidate (Gemini thinking-mode), tool-only turn with no text |
| `meta_reasoning_leak: false` | reasoning did not leak into the reply | reasoning kept internal | "The student has only named one business… Let me prompt them." |
| `passive_ending: false` | the turn hands back a concrete action | ends with a question/task | "Take your time and let me know when you're ready." |
| `must_not_label: [...]` | scenario-specific banned behaviours | none of the labels present | `ASK_WORKING` on a capable bare-correct answer; `INFO_DUMP` on a clarification |

### 4.2 Layer 3a — Scenario-specific + BEA-aligned standard rubric (gating)

Each item is scored on a continuous 0.0–1.0 scale (1.0 fully satisfies; 0.7 minor issue; 0.5 partial; 0.0 fails). Every scenario appends the same **8-item BEA-aligned standard block** (verbatim):

| # | Standard rubric item (abridged) | Measures | Characteristic failure |
|--:|---|---|---|
| S1 | Affirms a correct answer clearly / recognises a mistake — no false hedging | correctness acknowledgement | "Let me check your answer." (verdict left open) |
| S2 | Points at the *specific* location/nature of the mistake | diagnostic specificity | "Not quite — let's walk through it together." |
| S3 | Does NOT reveal the final answer outright; hints OK | scaffolding discipline | naming the correct option/value |
| S4 | Offers correct, calibrated guidance (hint/example/explanation) | instructional value | generic restatement; no forward motion |
| S5 | Makes the next action clear (question/MCQ/calculation) | actionability | passive ending |
| S6 | Logically consistent with the conversation | coherence | ignores the student's actual question |
| S7 | Warm, encouraging, never dismissive | tone | dismissiveness ("Let's keep going." to a real question) |
| S8 | Natural, not robotic/templated/filler | human-likeness | "Great question! Let me think about this carefully…" |

Scenario-specific items precede the block and encode the decision point under test, e.g. *"Models the canonical method (360 ÷ 3 = 120) without disparaging the trial-and-error"* or *"Stays under ~40 words."*

**Pass threshold.** A scenario's rubric layer passes iff the mean item score ≥ 0.7. Because the block contributes 8 of ~11 items, a model that is fluent and well-toned but pedagogically thin (high S6–S8, low S2/S4) can still fail — by design.

### 4.3 Layer 3b — Eight pedagogical dimensions (advisory)

A single judge call returns one categorical verdict per dimension (`evals/scorers/llm_rubric.py::PEDAGOGICAL_DIMENSIONS`):

| Dimension | Question (abridged) | Desirable | Why it matters |
|---|---|---|---|
| `mistake_identification` | did the tutor catch (or correctly not-flag) the student's mistake? | yes | false-accepts corrupt the learning signal |
| `mistake_location` | does the identified mistake match the real error? | yes | mis-attribution misleads the student |
| `reveals_answer` | did the tutor give away an unanswered question's answer? | no | premature reveal short-circuits learning |
| `providing_guidance` | useful hint/explanation/example that moves forward? | yes | the core teaching act |
| `actionability` | is the next student action clear? | yes | passivity stalls the session |
| `coherence` | consistent with the conversation? | yes | contradiction erodes trust |
| `tutor_tone` | encouraging / neutral / offensive | encouraging or neutral | affect drives persistence |
| `human_likeness` | natural vs robotic/templated | yes | templated tutoring disengages |

These are advisory (non-gating) but provide the clearest cross-family behavioural profile (§5.2, §7).

---

## 5. Results

### 5.1 Improved Evaluation 1 leaderboard (30 models) with historical comparison

Pass = fraction of 60 scenarios passed; Rubric = mean Layer-3a score (0–1). "Eval 0" is the previous-evaluation pass rate (confounded baseline). Anthropic rows are the Evaluation 0 ceiling (not re-run in Evaluation 1) and are shown for reference.

| Rank | Model | Family | Type | Eval 0 % | **Eval 1 %** | Δ | Rubric | Major bottleneck | Notable strength |
|--:|---|---|---|--:|--:|--:|--:|---|---|
| — | claude-opus-4-7 | Anthropic | Prop. | 90 | *90\** | — | 0.88 | persona handling | ceiling |
| — | claude-haiku-4-5 | Anthropic | Prop. | 82 | *82\** | — | 0.86 | math | strong/cheap |
| — | claude-sonnet-4-6 | Anthropic | Prop. | 78 | *78\** | — | 0.82 | persona handling | balanced |
| 1 | qwen3-next-80b-thinking | Qwen | MaaS | 2 | **80** | **+78** | 0.83 | crosscutting (conciseness) | best non-Anthropic |
| 2 | grok-4.1-fast-reasoning | xAI | MaaS | 72 | **78** | +6 | 0.82 | math diagnosis | reasoning depth |
| 3 | qwen3-next-80b-instruct | Qwen | MaaS | 65 | **75** | +10 | 0.78 | math | robust tool-loop |
| 4 | kimi-k2-thinking | Moonshot | MaaS | 57 | **73** | +16 | 0.78 | math | clean tool calls |
| 5 | deepseek-v3.1 | DeepSeek | MaaS | 45 | **70** | +25 | 0.73 | math | strong chat |
| 6 | gemini-2.5-pro | Google | Prop. | 43 | **68** | +25 | 0.77 | math/diagnostic | anomaly resolved |
| 7 | qwen3-coder-480b | Qwen | MaaS | 68 | 67 | −1 | 0.74 | diagnostic | agentic coding |
| 8 | qwen3-235b-instruct | Qwen | MaaS | 63 | 65 | +2 | 0.74 | diagnostic | consistent |
| 9 | glm-4.7 | Zhipu | MaaS | 67 | 63 | −4 | 0.73 | diagnostic | agentic |
| 9 | grok-4.1-fast-non-reasoning | xAI | MaaS | 57 | 63 | +6 | 0.73 | math | fast |
| 11 | gemini-2.5-flash | Google | Prop. | 65 | 62 | −3 | 0.75 | math | fast/cheap |
| 11 | **qwen2.5:7b** | Qwen | **OSS** | 52 | **62** | **+10** | 0.73 | math | **best on-device** |
| 13 | glm-5 | Zhipu | MaaS | 57 | 60 | +3 | 0.74 | math | — |
| 14 | gemini-3.5-flash | Google | Prop. | 50 | 58 | +8 | 0.71 | math | — |
| 14 | qwen2.5:14b | Qwen | OSS | 55 | 58 | +3 | 0.72 | math | on-device |
| 16 | deepseek-v3.2 | DeepSeek | MaaS | 58 | 57 | −1 | 0.66 | math | — |
| 17 | deepseek-r1 | DeepSeek | MaaS | 22 | **55** | +33 | 0.83 | math | high rubric quality |
| 18 | gemini-3.1-pro | Google | Prop. | 58 | 52 | −6 | 0.70 | math | — |
| 19 | grok-4.20-reasoning | xAI | MaaS | 45 | 50 | +5 | 0.67 | math | — |
| 20 | mistral-nemo:12b | Mistral | OSS | 53 | 45 | −8 | 0.65 | persona handling | multilingual |
| 21 | grok-4.20-non-reasoning | xAI | MaaS | 48 | 43 | −5 | 0.64 | crosscutting | — |
| 22 | glm4:9b | Zhipu | OSS | 43 | 33 | −10 | 0.57 | persona handling | — |
| 23 | granite3.1-dense:8b | IBM | OSS | 33 | 32 | −1 | 0.55 | math | enterprise |
| 24 | llama3.1:8b | Meta | OSS | 33 | 30 | −3 | 0.51 | persona handling | ubiquitous |
| 25 | command-r7b | Cohere | OSS | 15 | 20 | +5 | 0.43 | math | multilingual |
| 25 | hermes3:8b | Nous | OSS | 22 | 20 | −2 | 0.41 | math | — |
| 27 | mistral:7b | Mistral | OSS | 32 | 15 | −17 | 0.49 | persona handling | — |
| 28 | aya-expanse:8b | Cohere | OSS | 10 | 8 | −2 | 0.38 | persona handling | multilingual |
| 29 | falcon3:10b | TII | OSS | 0 | 0 | 0 | 0.32 | tool protocol | — |
| 29 | phi4 | Microsoft | OSS | 0 | 0 | 0 | 0.32 | tool protocol | — |

\* Anthropic rows carried over from Evaluation 0; not re-run under the fair-shot protocol (see §8.5).

**Reading the table.** The largest movements are the thinking models (+78, +33, +16) and the previously-suspect `gemini-2.5-pro` (+25). Cloud instruct models and the on-device Qwen2.5 models improve modestly (+2 to +10). Several models move down (−1 to −17); §5.3 and §8 attribute these to variance/environment rather than the prompt change.

### 5.2 Cross-model pedagogical-dimension profile

Aggregated over all 30 models × 60 scenarios (n = 1,799 dimension judgements):

| Dimension | Pass rate | Interpretation |
|---|--:|---|
| tutor_tone | **100%** | tone is a solved problem — no model is dismissive/offensive |
| mistake_location | 93% | when a model flags an error, it usually flags the right one |
| actionability | 84% | most turns end with a concrete next action |
| reveals_answer | 80% | answer-leakage is contained but not eliminated (1 in 5 turns leaks) |
| coherence | 77% | ~1 in 4 turns drifts from the student's actual move |
| mistake_identification | 76% | ~1 in 4 mistakes is missed or mis-affirmed |
| human_likeness | 64% | robotic/templated phrasing is common |
| **providing_guidance** | **55%** | **the binding constraint: nearly half of turns fail to move the student forward** |

This profile is the central qualitative result: the field's weakness is not safety or tone but **substantive guidance and naturalness**. (The aggregate is dragged down by the bottom OSS models; the top models clear these dimensions far more often, but the *rank order* of dimension difficulty holds within strong models too — see §6.)

### 5.3 The prompt-format experiment (XML vs Markdown)

For the two families whose vendor guidance prescribes a non-XML format (Qwen, Gemini), we ran both formats head-to-head (full 60-scenario runs; `results2` XML baseline vs `results2_md` Markdown):

| Model | XML % | Markdown % | Δ | Decision |
|---|--:|--:|--:|---|
| qwen3-next-80b-instruct | 67 | **75** | **+8** | Markdown |
| qwen3-235b-instruct | 62 | 65 | +3 | Markdown |
| qwen3-coder-480b | 65 | 67 | +2 | Markdown |
| qwen3-next-80b-thinking | 82 | 80 | −2 | Markdown (family-consistent; within noise) |
| gemini-3.5-flash | 58 | 63 | +5 | XML (see below) |
| gemini-2.5-flash | 62 | 57 | −5 | XML |
| gemini-3.1-pro | 52 | 52 | 0 | XML |
| gemini-2.5-pro | 68 | **52** | **−17** | XML |

**Finding.** Qwen genuinely benefits from Markdown (mean +3.25 pp; the instruct flagship +8). Gemini is *net harmed* by Markdown — `gemini-2.5-pro` loses 17 pp — **contradicting the vendor's general "prefer Markdown" guidance** for this complex, multi-section tutoring prompt, where XML's explicit delimiters appear to help Gemini parse the rule hierarchy. We therefore set Qwen → Markdown and retain Gemini → XML. This is the clearest demonstration in the study that prompt-format guidance must be *tested per task*, not assumed.

---

## 6. Detailed Analysis by Model Family

For each family we report overall performance, rubric-criterion behaviour, in-depth failure analysis with real excerpts, recurring patterns, and recommendations. Failure excerpts are drawn from the family's *best* model, so they reflect the capability ceiling, not the floor.

### 6.1 Qwen (Alibaba) — best non-Anthropic family

**Overall.** Six models, best 80%, family mean 68%, rubric mean 0.76 — the strongest family in the study. `qwen3-next-80b-thinking` (80%) leads all non-Anthropic models; the instruct sibling (75%) is the most robust tool-loop driver; `qwen2.5:7b` (62%) is the best on-device model. The Evaluation 0 verdict that Qwen-thinking was unusable (2%) was entirely a truncation artifact.

**Rubric-based analysis.** Qwen consistently satisfies tone (S7), coherence (S6), actionability (S5), and answer-discipline (S3). Its failures concentrate in **S2 (specific mistake location)** and the scenario-specific *diagnosis* and *conciseness* items.

**Failure analysis.**
- *Specific-misconception diagnosis* — `banned_opener_loop_guard_001` (math, probe_resistant). Worst item S2 (score 0.0). Judge: *"The tutor does not identify the specific error in the student's reasoning or calculation."* The model produced a fluent, well-toned MCQ walk-through but never named the student's actual slip. Effect: a single 0.0 on S2 pulls the rubric mean below 0.7 despite high scores elsewhere → scenario fails.
- *Conditional-item artifact* — `safety_distress_signal_001` (geography, struggler, "i hate this i'm so stupid"). Worst item (score 0.0): *"If the student made a mistake … affirm …"* The judge itself notes *"the student did not attempt the question … so this rubric item does not apply,"* yet scored it 0.0. Qwen's actual reply was warm and appropriate ("It's completely normal to feel frustrated … let's take a step back …"). This is a **rubric defect, not a model failure** (§8.3).

**Common patterns.** (i) Generic rather than specific error diagnosis; (ii) occasional over-length on clarification turns; (iii) thinking variant's quality depends on an adequate token budget (the core Eval 0 fix).

**Recommendations.** Add a system-prompt directive to *name the specific error* before hinting; few-shot one diagnosis exemplar; keep Markdown format and mode-specific sampling (instruct 0.7/0.8/20). For on-device, `qwen2.5:7b` at Markdown/0.7 is the deployment pick.

### 6.2 xAI Grok

**Overall.** Four models, best 78% (`grok-4.1-fast-reasoning`), family mean 58%. Reasoning helps the 4.1 line (+15 pp over non-reasoning) but the 4.20 line is weaker (50/43) — newer is not better. Persona suppression (added to neutralise Grok's default personality) is applied to all Grok runs.

**Rubric-based analysis.** Grok-4.1-reasoning is strong on guidance (S4) and coherence (S6) but fails S2 on diagnostic scenarios and the engagement-calibration items.

**Failure analysis.**
- *Specific misconception* — `error_prone_misreads_001` (math, error_prone). Worst item (0.0): *"Surfaces the SPECIFIC misconception (used 270 instead of 360)."* Grok replied *"Not quite — let's walk through it together."* — correct sentiment, zero diagnostic content. Violates S2 and the scenario item; the generic opener also risks the `banned_opener` guard.
- *Engagement misread* — `geo_non_responder_first_turn_001` (non_responder replies "k"). Worst item (0.0): *"Treats 'k' as passive acknowledgement, not engaged interest."* Grok opened *"Great!"* and launched into content, misreading a disengagement signal as enthusiasm.

**Common patterns.** Generic "not quite" diagnosis; misreading low-engagement personas; version regression (4.20 < 4.1).

**Recommendations.** Pin to `grok-4.1-fast-reasoning`; keep persona suppression; add explicit "name the error, then hint" and a non-responder-handling rule. Penalties remain unset (rejected by the reasoning endpoint).

### 6.3 Google Gemini

**Overall.** Four models, best 68% (`gemini-2.5-pro`, after the token-cap fix resolved the Eval 0 anomaly), family mean 60%. Counter-intuitively, the 2.5 line outperforms the 3.x line here, and Gemini is harmed by Markdown (§5.3).

**Rubric-based analysis.** Gemini scores well on tone and conciseness but its dominant failure is *over-terseness that drops the teaching content* — the opposite of the OSS info-dump problem.

**Failure analysis.**
- *Affirm + teach canonical method* — `average_off_method_correct_001` (math, average). Worst item (0.0): *"Models the canonical method (360 ÷ 3 = 120) without disparaging the trial-and-error."* `gemini-2.5-pro` replied only *"Got it — that's right. Here's the next one:"* — it affirmed and advanced but skipped the teaching beat the scenario requires. High actionability, zero instructional value on this item → S4 and the scenario item fail.
- *Dismissing a legitimate question* — `info_dump_guard_clarification_001` (geography). The model replied *"Let's keep going."* to a genuine "what's the difference between scale and zoom?" — avoiding the info-dump but failing to answer at all (item 0.0; also a tone/coherence risk).

**Common patterns.** Under-teaching (terseness sacrifices guidance), dismissiveness on side-questions, sensitivity to prompt format (XML required).

**Recommendations.** Retain XML; add a "always include one teaching sentence before advancing" rule; tune toward slightly more elaboration on Explain/clarification turns; leave temperature at the default (1.0), per vendor guidance and our results.

### 6.4 DeepSeek

**Overall.** Three models, best 70% (`deepseek-v3.1`), family mean 61%. `deepseek-r1` recovered from 22% → 55% and posts the **highest rubric quality in the entire study (0.83)** despite a middling pass rate — a revealing dissociation (below).

**Rubric / failure analysis.**
- *Open verdict* — `no_banned_opener_001` (geography, struggler). `deepseek-v3.1` replied *"I see you've chosen option A. Let me check your answer."* and stopped — never delivering the verdict. Worst item (0.0): *"Recognises that 'A' is wrong WITHOUT affirming it."* Violates S1 (correctness acknowledgement). This reflects the two-call tool loop occasionally ending after the grade tool fires but before the verdict text is composed.
- *Affirmation gap* — `average_off_method_correct_001`: *"merely acknowledges the student said it without confirmation"* (S1, 0.0).

**The rubric/pass dissociation.** R1's 0.83 rubric mean with a 55% pass rate indicates that when R1 answers, its turns are excellent, but it fails a subset of scenarios outright (often the tool-protocol/verdict-composition cases), and Layer-1 or a single 0.0 item tips those below threshold. R1 also remains a structural mis-fit for the tool loop (function-calling is documented as non-thinking-mode only).

**Common patterns.** Verdict left implicit after the grade tool; R1's tool-loop fit ceiling.

**Recommendations.** Prefer `deepseek-v3.1`/chat for the tool-driven tutor; reserve R1 for non-tool single-shot grading; add a "state the verdict explicitly in the reply" rule.

### 6.5 Moonshot Kimi

**Overall.** One model, `kimi-k2-thinking`, 73%, rubric 0.78 — the strongest single thinking model after Qwen, and the cleanest demonstration that a reasoning model *can* drive the tool loop when given budget. Its lowest failure category is `math` (8), much lower than peers.

**Failure analysis.** Same S2 weakness — `error_prone_misreads_001`: generic "Not quite — let's walk through it together" without naming the 270-vs-360 error; `no_banned_opener_001`: generic "Not quite" with no specific location (S2, 0.0).

**Recommendations.** Strong candidate; keep temperature 1.0 / top_p 0.95 and ≥16k budget (do not lower temperature); add specific-error-naming scaffolding. Preserve historical `reasoning_content` across turns if extended to multi-turn.

### 6.6 Zhipu GLM

**Overall.** Three models, best 63% (`glm-4.7`), family mean 52%. `glm-4.7` > `glm-5` (version regression). The OSS `glm4:9b` (33%) underperformed its Eval 0 (43%) — see §8.2.

**Failure analysis.**
- `average_off_method_correct_001`: identical pattern to Gemini — *"Got it — that's right. Here's the next one:"* (skips canonical method; S4 item 0.0).
- `off_topic_redirect_001`: a conditional-item artifact — judge marks the affirm item "not applicable" yet scores 0.0, plus the reply *"Let's keep going."* risks dismissiveness.

**Recommendations.** Pin `glm-4.7`; XML format; thinking enabled via the object parameter; add teaching-beat and off-topic-handling rules.

### 6.7 Mistral

**Overall.** Two models, best 45% (`mistral-nemo:12b`), family mean 30% — the weakest of the mid-tier families and a net regression from Eval 0 (Nemo 53→45; 7B 32→15). The environment changed (Eval 0 ran Nemo on a different runtime) and these are high-variance weak models; we do not attribute the drop to the prompt change.

**Failure analysis.**
- `average_off_method_correct_001`: Nemo *pivoted to a new problem without affirming* the correct answer (S1, 0.0).
- `info_dump_guard_clarification_001`: ~180-word reply against a ~40-word limit (scenario item 0.0) — the classic OSS info-dump.

**Recommendations.** Retain the official 0.3 temperature; for the multilingual (Portuguese) mandate Nemo remains relevant, but it needs strong conciseness and affirmation constraints, and likely fine-tuning to be deployment-grade.

### 6.8 Meta Llama, IBM Granite, and small OSS (Cohere, Nous, Microsoft, TII)

**Overall.** This tier (8 models, 0–32%) is not deployment-viable for the tool-driven tutor as-is.

**Failure analysis — two distinct failure modes.**
1. *Tool-protocol failure* — `falcon3:10b` and `phi4` score **0%** with rubric ≈ 0.32. They do not emit parseable tool calls, so the engine never receives a question/grade; replies are empty or off-protocol. This is **not** a teaching failure and is **unaffected by prompt/sampling tuning** — it requires the tool-call parser to be extended to their formats. (`phi4` is a capable model otherwise and is worth recovering.)
2. *Engine-confusion / capability* — `llama3.1:8b` returns meta-commentary about tool outputs ("I notice that the response is a tool call output…"); `hermes3:8b` emits figure-id errors; `command-r7b`/Granite produce 180–280-word info-dumps against 40-word limits and generic diagnosis. These violate S2/S4/S5 and the conciseness items repeatedly.

**Recommendations.** Extend the tool-call parser for falcon3/phi4 (recover under-measured capability); otherwise this tier is a poor fit for an agentic tutor without fine-tuning. Granite/Llama may suit non-tool fallbacks.

---

## 7. Cross-Family Comparison

Family-level summary (best model and family mean), ordered by best model:

| Family | Best % | Mean % | Rubric (mean) | Dominant failure category | Format | Deployment |
|---|--:|--:|--:|---|---|---|
| Qwen | 80 | 68 | 0.76 | math / diagnosis | markdown | MaaS + on-device |
| xAI | 78 | 58 | 0.71 | diagnosis / engagement | xml | MaaS (closed) |
| Moonshot | 73 | 73 | 0.78 | math | xml | MaaS (open-weight) |
| DeepSeek | 70 | 61 | 0.74 | verdict/affirmation | xml | MaaS (open-weight) |
| Google | 68 | 60 | 0.73 | under-teaching | xml | API (closed) |
| Zhipu | 63 | 52 | 0.68 | teaching beat | xml | MaaS + OSS |
| Mistral | 45 | 30 | 0.57 | conciseness / affirmation | xml | on-device |
| IBM/Meta | 32/30 | — | 0.55/0.51 | conciseness / engine fit | xml | OSS |
| Cohere/Nous | 20 | — | 0.43/0.41 | info-dump / capability | xml | OSS |
| Microsoft/TII | 0 | 0 | 0.32 | tool protocol | xml | OSS (blocked) |

**Comparative observations.**
- **Instruction-following / tool reliability** separates the top tier (Qwen, Kimi, DeepSeek-chat, Grok) from the rest more than raw knowledge does: the bottom failures are protocol and conciseness violations, not wrong facts.
- **Reasoning quality vs pass rate dissociate** (DeepSeek-R1: rubric 0.83, pass 55%): a model can produce excellent individual turns yet fail scenarios on a single gating item or a protocol slip.
- **Tone and mistake-location are universally strong; guidance and human-likeness are universally weak** — the bottleneck is consistent across families (§5.2), implying a shared, addressable cause (prompt scaffolding + the rubric's diagnosis emphasis) rather than family-specific deficits.
- **Open-weight parity:** the top open-weight models (Qwen3-Next, Kimi, DeepSeek-v3.1) match or beat the proprietary non-Anthropic option (Gemini) on this task.

---

## 8. Discussion

### 8.1 Why some families outperform

The top families (Qwen, Kimi, DeepSeek-chat, Grok-reasoning) share strong agentic/tool-use post-training and benefit most from the fair-shot fixes (adequate token budget for reasoning, mode-appropriate sampling). The decisive variable is not model size — `qwen2.5:7b` (62%) beats `gemini-3.1-pro` (52%) and ties `gemini-2.5-flash` — but *task-fit*: reliable structured tool-calling plus calibrated, concise pedagogy.

### 8.2 Proprietary vs open-source trade-offs

The best open-weight models now match the best non-Anthropic proprietary model (Gemini) on this task, while offering data residency, no per-token cost, and offline deployment — decisive advantages for the project's low-connectivity mandate. The cost is operational: self-hosting, quantisation tuning, and the per-family configuration burden this study formalises.

### 8.3 Does scale help? Mixed.

Within Qwen, 80B-thinking > 80B-instruct > 480B-coder > 235B > 14B > 7B *roughly* tracks capability but not monotonically (the 80B-A3B MoE beats the 480B coder). Across the OSS tier, family matters far more than size (`mistral-nemo:12b` 45% > `llama3.1:8b` 30%; `qwen2.5:7b` 62% > everything below 14B). Newer-within-family is *not* reliably better (`glm-4.7` > `glm-5`; `grok-4.1` > `grok-4.20`).

### 8.4 Limitations

- **Narrow content domain.** Two subjects, four Form-3 lessons, no reading. Generalisation to other subjects/levels is unverified.
- **Single-turn focus.** The 60 graded scenarios are single-turn; long-horizon behaviours (state tracking, exit-ticket gating) are under-tested here.
- **OSS environment shift.** Eval 0 OSS numbers came from mixed hardware; Eval 1 OSS ran on Colab T4. OSS old-vs-new deltas are therefore confounded and not strictly comparable (only the Qwen Markdown signal, which is internally controlled, is clean).
- **Single run per (model, format).** Per-model deltas of ±2–6 pp are within run-to-run/judge variance and should not be over-interpreted.

### 8.5 Threats to validity

- **Conditional-rubric artifact (highest impact).** The 8-item standard block includes conditional items ("If the student made a mistake … affirm …"). When a scenario has *no* mistake (off-topic, distress, clarification), the judge sometimes scores the item **0.0** while explicitly noting it "does not apply." This drags the mean below 0.7 and **fails scenarios the model handled correctly** — disproportionately on the strongest models (whose remaining failures are concentrated in these edge scenarios). True pass rates for the top tier are therefore *understated*. This is the single most important fix before Evaluation 2 (§9.2).
- **Anthropic ceiling not re-run.** The 90/82/78 Anthropic rows are Evaluation 0 numbers carried forward; they were generated under the old harness (1,024-token cap, XML). They likely *also* understate Anthropic, but the comparison to non-Anthropic Eval 1 numbers is conservative (it favours the incumbent), so conclusions about candidates *approaching* the ceiling are not inflated.
- **Judge monoculture.** A single judge family (Anthropic) grades all models. Although cross-vendor for every tutor here, a multi-judge ensemble would harden the scores.
- **LLM-judge calibration.** ~5% per-dimension noise motivated making Layer 3b advisory; Layer 3a gating inherits some of this.

### 8.6 Generalisability

Findings about *protocol* (fair-shot prompting, token budget for reasoning models, per-task format testing, the conditional-rubric pitfall) generalise broadly to any LLM-as-agent evaluation. Findings about *specific model ranks* are task-specific to 5E tutoring on this content and should be re-validated per deployment domain.

---

## 9. Recommendations for the Next Evaluation (Evaluation 2)

### 9.1 Dataset improvements
- Broaden beyond two subjects/four lessons; add at least one **reading/comprehension** lesson and one additional STEM topic to test generalisation.
- Add harder **multi-turn** scenarios to the gated set (state tracking, remediation loops, exit-ticket competency), not just single-turn.
- Balance personas and difficulty; add more `error_prone` coverage (currently n=1 single-turn) to strengthen mistake-diagnosis measurement.
- Replace remaining `reference_answer: "PLACEHOLDER_REF"` rows with human-verified references.

### 9.2 Rubric improvements
- **Add explicit N/A handling** to the conditional standard items (judge returns `n/a`, excluded from the mean). *Highest-priority fix* — directly de-biases the top tier (§8.5).
- Weight items (e.g., guidance/diagnosis higher than filler-avoidance) instead of an unweighted mean.
- Disambiguate overlapping items (S2 specific-location vs the `mistake_location` dimension).
- Consider a small **human-rated calibration set** to validate judge scores and estimate judge–human agreement.

### 9.3 Evaluation-pipeline improvements
- **Multiple runs per model** (≥3) with mean ± CI, so ±5 pp noise no longer masquerades as signal.
- **Multi-judge ensemble** (add a non-Anthropic judge) to reduce judge monoculture.
- Record per-rubric-item pass rates per model in the aggregate (not just the mean) for finer error analysis.
- Re-run the **Anthropic ceiling** under the fair-shot harness for a like-for-like comparison.
- Harden the harness: treat judge connectivity failures as `errored` (not 0), and add tool-call-format auto-detection so capable models (phi4) are not scored 0 for a parser gap.

### 9.4 Prompt improvements (to raise scores before re-running)
- Add a universal **"name the specific error, then hint"** instruction (targets S2/`mistake_identification` — the top recurring failure).
- Add **"include one teaching sentence before advancing"** (targets Gemini/GLM under-teaching on S4).
- Tighten the **clarification/info-dump** rule with a concrete word budget and one exemplar (targets the OSS info-dump and the ~40-word scenario items).
- Add a **non-responder / low-engagement** handling rule (targets the "k" misread).
- Keep the validated per-family settings (Qwen→Markdown; Gemini→XML; mode-appropriate sampling; reasoning token budgets).

---

## 10. Selecting an Alternative to Anthropic

### 10.1 Closest current performers

Against the Anthropic ceiling (Opus 90, Haiku 82, Sonnet 78), the leading non-Anthropic models are:

| Candidate | Eval 1 % | Rubric | Openness | Notes |
|---|--:|--:|---|---|
| qwen3-next-80b-thinking | 80 | 0.83 | open-weight | leads all non-Anthropic; needs adequate token budget |
| grok-4.1-fast-reasoning | 78 | 0.82 | proprietary (xAI) | strong but closed; default-personality risk |
| qwen3-next-80b-instruct | 75 | 0.78 | open-weight | most robust tool-loop driver |
| kimi-k2-thinking | 73 | 0.78 | open-weight | clean agentic tool use |
| deepseek-v3.1 | 70 | 0.73 | open-weight | strong chat; reliable |

`qwen3-next-80b-thinking` already **exceeds the Anthropic Sonnet (78)** ceiling row and approaches Haiku (82) on pass rate, with a rubric (0.83) near Opus (0.88).

### 10.2 Which models can plausibly exceed 90% after improvements

Two compounding factors make 90%+ realistic for the lead candidates:
1. **Rubric de-biasing (§9.2).** The conditional-item artifact understates the top tier specifically. Several top-model "failures" are scenarios the judge itself flags as N/A; fixing this is expected to recover multiple points on Qwen3-Next/Grok-reasoning/Kimi without any model change.
2. **Targeted prompt scaffolding (§9.4).** The two dominant residual failures — specific-error diagnosis (S2) and the affirm-and-teach beat (S4) — are addressable with one or two universal rules and a single exemplar each; both are *behavioural*, not capability, gaps for the top models (which already reason well).

Projection: **`qwen3-next-80b` (thinking for hardest items, instruct for the tool loop)** is the most likely non-Anthropic family to reach the **85–90%+** band in Evaluation 2, followed by `grok-4.1-fast-reasoning` and `kimi-k2-thinking`.

### 10.3 Practical trade-offs

| Factor | Qwen3-Next-80B | Grok-4.1-fast | Kimi-K2 | Gemini-2.5 | qwen2.5:7b (on-device) |
|---|---|---|---|---|---|
| Pass (Eval 1) | 75–80% | 78% | 73% | 62–68% | 62% |
| Openness | open-weight | closed | open-weight | closed | open-weight |
| Data residency / offline | ✓ (self-host) | ✗ | ✓ (self-host) | ✗ | ✓ (on-device) |
| Per-token cost | infra only | paid API | infra only | paid API | none |
| Deployment ease | moderate (GPU) | easy (API) | moderate (GPU) | easy (API) | easy (CPU/edge) |
| Enterprise suitability | high | high | high | high | high (edge) |

### 10.4 Recommendation

- **Primary non-Anthropic recommendation for schools: the Qwen3-Next-80B family** — highest pass + rubric among non-Anthropic models, open-weight (data residency, no per-token cost, offline-capable), and the clearest path to 90%+ after the rubric fix and prompt scaffolding. Use the **instruct** variant for the latency-sensitive tool loop and the **thinking** variant for hard items, with a per-item token budget.
- **On-device / low-connectivity recommendation: `qwen2.5:7b`** (62%, Markdown, temp 0.7) — matches Gemini 2.5 Flash with zero connectivity or per-token cost; `qwen2.5:14b` (58%) where a larger footprint is acceptable.
- **Managed-API fallback (if open-weight hosting is undesirable): `grok-4.1-fast-reasoning`** (78%) or `gemini-2.5-pro` (68%, XML).

All recommendations are supported by the Evaluation 1 results (§5) and the failure analysis (§6); the 90%+ projection is conditioned on the Evaluation 2 improvements in §9.

---

## 11. Conclusion

**Major findings.** Fair-shot prompting overturns the central conclusion of the prior evaluation: reasoning ("thinking") models are *not* unsuitable for the tool-driven tutor — they were truncated by a harness artifact. With per-family token budgets, sampling, and (tested) prompt formats, `qwen3-next-80b-thinking` rises from 2% to **80%**, leading all non-Anthropic models, and the best on-device model reaches **62%**. A controlled experiment shows prompt-format guidance must be validated per task: Markdown helps Qwen (+8) but hurts Gemini (−17).

**Most significant bottlenecks.** Across all families the binding constraints are **substantive guidance (55%)** and **human-likeness (64%)**, manifested as three recurring scenario failures: concise clarification, specific-misconception diagnosis, and affirm-and-teach-the-canonical-method. Tone and mistake-location are effectively solved.

**Key recommendations.** Fix the conditional-rubric N/A artifact (which understates the strongest models), add multi-run/multi-judge robustness and dataset breadth, and apply two universal prompt rules ("name the specific error, then hint"; "teach one sentence before advancing").

**Expected improvement in Evaluation 2.** With the rubric fix and targeted scaffolding — no model change — we project the lead non-Anthropic candidate into the **85–90%+** band.

**Final deployment recommendation.** Adopt **Qwen3-Next-80B** (open-weight) as the primary non-Anthropic candidate and **`qwen2.5:7b`** for on-device deployment; re-validate both under the improved Evaluation 2 harness before production rollout.

---

### Appendix A — Reproducibility checklist
- Code: `apps/tutoring/simple_tutor/` (engine, prompts, family prompts), `apps/llm/model_profiles.py` (per-family registry), `apps/llm/client.py` (sampling plumbing).
- Data: `evals/dataset/*.yaml` (60 single-turn scenarios), `evals/fixtures/{institution,lessons}.json`.
- Results: `offline_eval/results2/*.json` (per-model, per-scenario transcripts + rubric/dimension scores), `results2_md/` (Markdown-format experiment).
- Judge/sim: Claude Haiku 4.5 @ temp 0 (`evals/scorers/llm_rubric.py`).
- Commands: `RESULTS_DIR=offline_eval/results2 TUTOR_MODEL_OVERRIDE=<spec> python manage.py run_eval --single-turn`; `python offline_eval/aggregate.py`.

### Appendix B — Glossary
**5E** — Engage/Explore/Explain/Elaborate/Evaluate instructional model. **Fair-shot** — per-family prompt/sampling/format/mode tuned to vendor best practice. **MaaS** — Model-as-a-Service (Vertex Model Garden). **Thinking/reasoning model** — emits internal chain-of-thought before answering. **BEA** — Building Educational Applications shared task on pedagogical ability. **Layer 1/3a/3b** — deterministic / gating-rubric / advisory-dimensions scoring layers.
