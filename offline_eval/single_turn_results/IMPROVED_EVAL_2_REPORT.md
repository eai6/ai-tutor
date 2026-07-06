# Instruction-Following and Pedagogical Fidelity of Open and Proprietary Large Language Models as Conversational Tutors

### The Improved Evaluation 2 (results3b) Benchmark Study

**Nyansapo AI Tutor — Offline Model Evaluation Working Group**
Preprint · July 2026 · Version 2 (results3b)

---

## Abstract

**Motivation.** The Nyansapo AI Tutor is a conversational, 5E-model tutoring platform serving secondary-school students in low-resource settings (a Seychelles pilot in production, a Tanzania pilot in planning). Its production tutor is an Anthropic Claude model. For reasons of cost, latency, data residency, and the option of on-device / self-hosted deployment in bandwidth-constrained schools, we require a principled, reproducible answer to a single question: **which non-Anthropic model can replace Claude as the tutoring engine without degrading pedagogical quality?**

**Objectives.** This paper reports *Improved Evaluation 2*, a 29-model benchmark of candidate tutor models spanning three families — Alibaba's **Qwen** (2.5, 3, 3.5, 3.6, and Next generations; 0.5 B–80 B), Google's proprietary **Gemini** (2.5 / 3.x), and Google's open **Gemma 3** (1 B–27 B). We measure each model as a drop-in tutor over a fixed, rubric-scored benchmark and analyse strengths, weaknesses, and best-fit deployment scenarios by family.

**Methodology.** Each candidate model drives the production `simple_tutor` engine — a two-call tool-use loop (decide/grade, then compose) — over **60 single-turn scenarios** drawn from two secondary lessons (angle geometry; map skills) and six student personas. Responses are scored by a three-layer pipeline: deterministic assertions (Layer 1), a gating LLM rubric of scenario-specific + BEA-aligned standard items scored by a pinned Anthropic Haiku 4.5 judge at temperature 0 (Layer 3a, pass ⇔ mean ≥ 0.70), and eight advisory pedagogical dimensions (Layer 3b). Per-family sampling and prompt formats are applied automatically via a model-profile registry.

**Key findings.** The strongest deployable candidate is **Qwen3.5 4B**, which passed **60/60 (100%, rubric 0.92)** — a self-hostable 4-billion-parameter model that topped the board above the 80 B Qwen-Next MaaS model (98%) and every Gemini model. Three further open Qwen models cleared 90%: **Qwen3.5 9B (93%)**, **Qwen3.6 27B (93%)**, and **Qwen3 14B (92%)**. Among proprietary models, **Gemini 2.5 Flash (90%)** led its family. Model quality does **not** scale monotonically with parameter count within the Qwen line; sampling, context configuration, and reasoning-mode handling dominate.

**Major bottlenecks.** (1) A benchmark-infrastructure defect — the Ollama context window defaulting to 4096 tokens — silently truncated reasoning models mid-`<think>`, producing empty completions; correcting it (`num_ctx` fix) is the reason this is *Improved Evaluation 2* and lifted the small Qwen3/3.5 models by up to +73 points. (2) **Gemma 3 has no tool-calling support in Ollama**: every tool-requiring turn returned HTTP 400 and the engine served a fallback string, so Gemma's 7% is an artifact, not a capability measure. (3) The dominant *pedagogical* failure mode, shared across families, is **premature answer revelation** and **generic (non-specific) error diagnosis**.

**Recommendations.** For schools seeking a non-Anthropic tutor, we recommend **Qwen3.5 4B** (best quality-per-cost, self-hostable) or **Qwen3.6 27B** (headroom, still open) as primary candidates, with **Gemini 2.5 Flash** as a managed-API alternative. Gemma requires a prompted-tool-calling adapter before it can be evaluated at all.

**Conclusion.** A carefully-configured *small* open model now matches or exceeds proprietary frontier models on this pedagogical benchmark, making a fully self-hosted, cost-free tutoring deployment technically viable — provided evaluation infrastructure (context windows, tool-calling capability) is correctly configured, a lesson this study learned the hard way.

---

## 1. Introduction

### 1.1 Why evaluate LLMs for tutoring

Conversational tutoring is an unusually demanding instruction-following task. A good tutor must simultaneously (a) correctly grade a student's answer, (b) withhold the answer while scaffolding toward it, (c) diagnose the *specific* error a student made rather than issue generic encouragement, (d) adapt to the student's persona (a confident learner, a struggler, a non-responder), and (e) maintain a warm, human, non-robotic register — all in a single short turn. Unlike knowledge-recall benchmarks, correctness is necessary but far from sufficient: a factually right response that reveals the answer, or that praises a wrong answer, is a pedagogical failure. This makes tutoring a strong stress test of *constrained* instruction following, and a domain where model choice has direct downstream impact on learning outcomes.

### 1.2 Motivation for the improved evaluation

The Nyansapo platform runs Claude in production. Anthropic models are strong but carry per-token cost, network dependence, and data-egress considerations that are material for deployments in schools with intermittent connectivity and constrained budgets. A self-hosted or lower-cost model that preserved pedagogical quality would substantially expand the platform's reach. Answering "which model" responsibly requires a benchmark that measures *pedagogy*, not just accuracy, and that is reproducible across the many model families now available.

### 1.3 Limitations discovered in the previous evaluation

The first pass of this benchmark (the "improved evaluation," results3, first run) produced results that failed face validity: several reasoning models scored *below* smaller siblings of the same family (e.g., Qwen3.5 9B scored 20% while Qwen3.5 4B scored 38% — a 9-billion-parameter model apparently worse than a 4-billion one). Root-cause analysis (Section 8.4) traced this to an evaluation-infrastructure defect rather than a model capability difference: the local inference server's context window defaulted to 4096 tokens, so a ~2050-token system prompt left only ~2046 tokens for generation, and hybrid reasoning models were truncated *inside* their `<think>` block before emitting an answer. The engine then served a placeholder, which the rubric correctly failed. Additionally, all four Gemma 3 models scored an identical 7% regardless of size — a second artifact, traced to Gemma's absent tool-calling support on the local server.

This report — *Improved Evaluation 2* — presents the results **after** correcting the context-window defect (the `num_ctx` fix), re-running the affected models, and consolidating a clean 29-model board. The Gemma tool-calling defect is documented but not yet remediated; Gemma's rows are retained for transparency and explicitly annotated as invalid.

### 1.4 Objectives

1. Rank 29 candidate tutor models by pedagogical pass rate on a fixed rubric.
2. Characterise the strengths, weaknesses, and failure modes of each model family.
3. Determine whether smaller open models can match proprietary frontier models.
4. Identify the strongest non-Anthropic replacement candidate(s), with deployment trade-offs.
5. Surface benchmark-methodology lessons for the next evaluation iteration.

---

## 2. Related Work

**LLM benchmark methodology.** General-purpose leaderboards (e.g., open LLM leaderboards, LMSYS-style arena rankings, and instruction-following suites such as IFEval) establish that instruction adherence and task competence are distinct axes and that aggregate accuracy conceals task-specific failure modes. Our study follows the now-standard *evals-driven* paradigm: a fixed, versioned dataset; a deterministic-plus-LLM scoring pipeline; and per-item diagnostic labels rather than a single scalar.

**Instruction-following evaluations.** Constrained-generation benchmarks show that models frequently satisfy the *content* of an instruction while violating a *constraint* (length, format, or a prohibition such as "do not reveal the answer"). Tutoring intensifies this: the central constraint (withhold the answer; scaffold instead) is exactly the one models most often break, which our rubric isolates as a dedicated dimension (`reveals_answer`).

**Rubric-based / LLM-as-judge evaluation.** Using a strong, pinned LLM as a rubric grader at temperature 0 is an established, scalable alternative to human scoring, with known caveats around judge bias and self-preference. We mitigate these by (a) pinning a single judge model (Anthropic Haiku 4.5) at temperature 0, (b) decomposing judgement into many small, verbatim rubric items rather than a holistic score, and (c) aligning the standard rubric items with a published pedagogical schema (BEA-style tutoring criteria). We note the judge-family confound explicitly in Section 8.5.

**Model-comparison studies.** Cross-family studies increasingly report that parameter count is a weak predictor of task quality once instruction-tuning and inference configuration are controlled. Our within-Qwen results (a 4 B model beating an 80 B model) are a strong instance of this phenomenon in the tutoring domain.

---

## 3. Experimental Setup

### 3.1 Models evaluated

Twenty-nine models across three families were assessed. Anthropic (the incumbent) is the reference and the judge; it is **not** evaluated as a tutor here to avoid self-judging bias (Section 8.5). OpenAI, Meta, DeepSeek, Microsoft, xAI, and others were assessed in the prior results2 sweep but were out of scope for Improved Evaluation 2, which deliberately narrowed to the two families under active consideration for the platform: Qwen and Google.

| Family | Vendor | Models (count) | Access | Rationale for inclusion |
|---|---|---|---|---|
| **Qwen** | Alibaba | Qwen2.5 (7), Qwen3 (6), Qwen3.5 (4), Qwen3.6 (2), Qwen3-Next (2) — 21 | OSS (Ollama) + MaaS (Vertex) | Leading open-weight family; strong tool-use + multilingual; full size ladder enables scaling analysis |
| **Gemini** | Google | 2.5 Pro, 2.5 Flash, 3.1 Pro, 3.5 Flash — 4 | Proprietary API | Frontier managed models; strong instruction following; baseline for "proprietary" tier |
| **Gemma 3** | Google | 27B, 12B, 4B, 1B — 4 | OSS (Ollama) | Google's open family; candidate for fully self-hosted deployment |

**Model types and expected capabilities.**

- **Qwen2.5** (0.5–72 B): dense, non-reasoning instruct models. Expected: reliable tool use, moderate pedagogy, clean scaling.
- **Qwen3 / Qwen3.5 / Qwen3.6** (0.6–35 B): hybrid *reasoning* models emitting `<think>` traces. Expected: stronger diagnosis when the reasoning trace completes; sensitive to context-window and thinking-budget configuration.
- **Qwen3-Next-80B** (A3B MoE, instruct + thinking): large mixture-of-experts served via Vertex Model Garden; expected top-tier reasoning at MoE-efficient active-parameter cost.
- **Gemini 2.5 / 3.x**: proprietary frontier models; expected strong, consistent instruction following and tone.
- **Gemma 3** (1–27 B): open dense/multimodal models; expected competent generation but *uncertain tool-calling support* on the local server (a risk flagged pre-registration and subsequently realised).

### 3.2 Dataset

The benchmark comprises **60 single-turn scenarios**. Each scenario fixes a lesson, a lesson step, a student persona, and a seeded conversation history ending in a specific student utterance; the model must produce the next tutor turn, which is then scored.

**Source and construction.** Scenarios are authored YAML fixtures under `evals/dataset/`, versioned in the repository. Each encodes: the lesson and step context, the student turn, a set of deterministic assertions, a scenario-specific rubric checklist, a reference answer, and diagnostic tags. Reference answers were audited and corrected in a prior pass (26 placeholder answers replaced and mathematically-wrong references fixed), because an incorrect reference silently corrupts the grader's verdict and makes "false-accept" scenarios unwinnable.

**Composition.**

| Axis | Distribution |
|---|---|
| **Subject** | Math 34 (angle geometry, lessons 1137/1138); Geography 26 (map skills, lessons 1463/1464) |
| **Persona** | average 20, struggler 17, capable 11, probe_resistant 6, non_responder 5, error_prone 1 |
| **Category (tags)** | persona_handling (19), math (18), crosscutting (13), pedagogy (10), advance (8), diagnostic (7), format (6), non_answer (6), mcq (4), false_accept (4), bare_answer (4), … |

**Difficulty / scenario families.** Scenarios span (a) *correct-answer handling* (affirm + advance without over-probing), (b) *wrong-answer diagnosis* (name the specific error, withhold the answer), (c) *false-accept guards* (a wrong answer the model must not affirm), (d) *answer-leak guards* (must not reveal the target value), (e) *persona stress tests* (non-responders, probe-resistant, over-confident learners), and (f) *format / safety* constraints.

**Representative dataset examples.**

- *`math_average_arithmetic_slip_001`* (persona: average): the student applies the right method (sum angles, subtract from 360°) but makes an arithmetic slip. High-quality behaviour: affirm the method, point at the specific arithmetic step, withhold the final value.
- *`math_leaks_answer_guard_001`* (persona: struggler): the student gives a bare wrong answer (120). High-quality behaviour: mark it wrong, do **not** reveal 145, scaffold the subtraction.
- *`math_average_wrong_mcq_001`* (persona: average): the student picks a wrong MCQ option. High-quality behaviour: invite reconsideration *and* hint at the actual relationship (180°) without naming the option.
- *`non_responder_idk_chain_001`* (persona: non_responder): the student replies "idk". High-quality behaviour: gently re-engage with a simpler entry question — no grading tool needed.

**Appropriateness.** The dataset targets exactly the behaviours that differentiate a good tutor from a competent question-answerer: constraint adherence (withhold answers), diagnostic specificity, and persona adaptation. Its two-subject design (a computational subject and a factual/spatial subject) probes whether pedagogical skill generalises across content types.

### 3.3 Evaluation procedure

**Engine.** Every candidate model drives the identical production `simple_tutor` engine — a **two-call tool-use loop**: Call 1 lets the model decide and invoke a grading tool (`record_answer`) plus optional tools; the engine dispatches the tool and obtains a verdict; Call 2 re-invokes the model with the verdict in hand to compose the student-facing reply. This isolates *pedagogical* competence from grading mechanics and is byte-for-byte the code path used in production.

**Prompting strategy.** A three-block system prompt (role/rules/safety/tools; lesson step; per-turn context) is assembled per turn. The Block-0 template is selected by model family: Anthropic/default receive an XML-structured template; Qwen models receive a Markdown-structured template with targeted pedagogical rules and few-shot exemplars; Gemini/Gemma (Google lineage) receive the XML template plus the same targeted rules. This per-family prompt selection ensures no family is disadvantaged by a prompt tuned for another.

**Inference settings.** Sampling is applied per family via a model-profile registry: Qwen — temperature 0.7, top-p 0.8, top-k 20; Gemini — provider default (temperature ≈ 1.0, unset, per Google guidance); Gemma — temperature 1.0, top-p 0.95, top-k 64. Output budget (`num_predict`) is 16 000 tokens for reasoning models. Critically for reproducibility, the local-inference context window is now set explicitly (`num_ctx` = generation budget + prompt headroom, ≈ 24 000 for Qwen3/3.5) rather than inheriting the server's 4096 default (Section 8.4).

**Scoring pipeline (three layers).**
1. **Layer 1 — deterministic assertions.** Programmatic checks: non-empty response, no meta-reasoning leakage, no passive ending, banned-label avoidance. Hard gates on obviously-broken output.
2. **Layer 3a — gating LLM rubric.** A pinned **Anthropic Haiku 4.5** judge at **temperature 0** scores each scenario-specific checklist item and each BEA-aligned standard item on a 0.0–1.0 scale (with an "n/a" option for inapplicable conditional items, excluded from the mean). A scenario **passes** iff the mean over applicable items ≥ its `pass_threshold` (0.70). This layer determines the headline pass/fail.
3. **Layer 3b — pedagogical dimensions (advisory).** The same judge classifies eight discrete pedagogical dimensions (Section 4.2). These do not gate pass/fail but drive the diagnostic analysis.

**Human vs automated evaluation.** Scoring is fully automated (LLM-as-judge); no per-scenario human scoring was performed. The rubric items and reference answers were human-authored and audited.

**Consistency measures.** The judge is a single pinned model at temperature 0; the judge provider chain is held fixed across all models; the dataset and engine are versioned in git; every model runs the identical 60 scenarios; and per-turn structured logs (`in`/`out` token counts, response block types) are retained to detect infrastructure artifacts — the mechanism by which both major defects in this study were caught.

**Reproducibility.** The full pipeline (engine, dataset, scorers, model profiles, and the Colab/local run harness) is in the repository; a single `RESULTS_DIR=results3 aggregate.py` reproduces the leaderboard from the committed per-model JSONs.

---

## 4. Evaluation Rubric

Scoring combines a **gating rubric** (Layer 3a; determines pass/fail) and **advisory dimensions** (Layer 3b; drive diagnosis). Both are judged verbatim by the pinned Haiku 4.5 grader.

### 4.1 Gating rubric (Layer 3a)

Each scenario carries 3–5 **scenario-specific** checklist items (e.g., "Affirms '120' is correct"; "Treats '90' as incorrect") plus a set of **BEA-aligned standard items** applied to every scenario. Each item is scored 0.0 (fails), intermediate (partially satisfies), or 1.0 (fully satisfies); the pass gate is the mean ≥ 0.70. The standard items and their intent:

| Standard rubric item (paraphrased) | Measures | Why it matters | High-quality response | Common failure |
|---|---|---|---|---|
| Correctly recognises / affirms the student's answer state | Grading fidelity | A tutor that mis-grades cannot teach | Affirms a correct answer clearly; marks a wrong one | Praising a wrong answer (false-accept); second-guessing a correct one |
| Points at the **specific** error location/nature | Diagnostic specificity | Generic "not quite" gives no learning signal | Names the step/misconception ("360 − 215 isn't 155") | "Let's walk through it together" with no specifics |
| Does **not** reveal the final answer outright | Constraint adherence | Revealing the answer destroys the exercise | Scaffolds with a calibrated hint | States or paraphrases the target value / MCQ option |
| Offers correct, relevant guidance | Instructional value | The turn must move the student forward | A hint, worked step, or canonical method | Empty affirmation; no guidance |
| Makes the next action clear | Actionability | The student must know what to do next | Ends with a concrete question/computation | Passive trailing ("take your time") |
| Logically consistent with the conversation | Coherence | Contradiction erodes trust and confuses | Builds on the student's last turn | Ignores the student; contradicts prior turns |
| Warm, encouraging (never harsh) | Tone | Affect drives engagement, especially for strugglers | Supportive without condescension | Dismissive or robotic |
| Natural, non-templated | Human-likeness | Robotic replies reduce engagement | Conversational teacher voice | Filler openers, templated scaffolds |

### 4.2 Advisory pedagogical dimensions (Layer 3b)

Eight discrete dimensions are classified per turn; the desirable value is in brackets.

1. **mistake_identification** [yes] — did the tutor recognise a student mistake (or correctly recognise none was made)?
2. **mistake_location** [yes] — did the identified mistake match the *genuine* error (no misattribution / invented errors)?
3. **reveals_answer** [no] — did the tutor prematurely reveal the answer to a still-open question?
4. **providing_guidance** [yes] — did the tutor offer a useful hint/explanation/example?
5. **actionability** [yes] — is it clear what the student should do next?
6. **coherence** [yes] — is the reply consistent with the prior conversation?
7. **tutor_tone** [encouraging/neutral] — is the tone supportive and non-offensive?
8. **human_likeness** [yes] — does it read as natural human teaching rather than robotic?

**Interpreting the dimensions.** Because these are independent, they can dissociate in diagnostic ways. The clearest example (Section 6.3) is Gemma's profile: a degenerate fallback string vacuously scores 100% on `mistake_location` (nothing is misattributed), `reveals_answer` (nothing is revealed), `actionability` (it ends with a question), and `tone` (neutral) — while scoring 0% on `providing_guidance` and 13% on `human_likeness`. The dimensions thus expose *why* a model fails, not merely that it did.

---

## 5. Results

### 5.1 Improved Evaluation 2 Leaderboard (Combined — results3b)

Pass rate is over 60 scenarios; rubric score is the mean Layer-3a rubric across scenarios (0–1); the major bottleneck is the tag most frequent among a model's *failed* scenarios.

| Rank | Model | Family | Type | Pass % | Rubric | Major Bottleneck | Strengths |
|---|---|---|---|---|---|---|---|
| 1 | Qwen3.5 4B | Qwen | OSS | **100%** | 0.92 | — (none) | Perfect pass; withholds answers; specific diagnosis |
| 2 | Qwen3-Next-80B Instruct | Qwen | MaaS | 98% | 0.94 | tool_leak (1) | Highest rubric; precise, concise diagnosis |
| 3 | Qwen3.5 9B | Qwen | OSS | 93% | 0.92 | crosscutting (2) | Strong reasoning; consistent tone |
| 3 | Qwen3.6 27B | Qwen | OSS | 93% | 0.91 | persona_handling (2) | Balanced across subjects |
| 3 | Qwen3.6 35B-A3B | Qwen | OSS | 93% | 0.91 | crosscutting (2) | Geography-perfect (100%); MoE-efficient |
| 6 | Qwen3 14B | Qwen | OSS | 92% | 0.89 | advance (2) | Reliable mid-size open model |
| 7 | Gemini 2.5 Flash | Gemini | Proprietary | 90% | 0.87 | pedagogy (3) | Best proprietary; strong coherence + tone |
| 8 | Gemini 3.5 Flash | Gemini | Proprietary | 87% | 0.87 | math (3) | Consistent; fast managed API |
| 9 | Qwen3 4B | Qwen | OSS | 85% | 0.86 | persona_handling (3) | Excellent quality-per-parameter |
| 9 | Qwen2.5 32B | Qwen | OSS | 85% | 0.84 | math (4) | Solid non-reasoning workhorse |
| 9 | Qwen2.5 72B | Qwen | OSS | 85% | 0.84 | advance (5) | No gain over 32B (see §8.3) |
| 12 | Gemini 3.1 Pro | Gemini | Proprietary | 82% | 0.85 | persona_handling (4) | Strong geography; weaker math |
| 12 | Qwen3-Next-80B Thinking | Qwen | MaaS | 82% | 0.83 | persona_handling (4) | Thinking variant underperforms instruct |
| 14 | Gemini 2.5 Pro | Gemini | Proprietary | 80% | 0.81 | math (4) | Generic-diagnosis weakness |
| 15 | Qwen3 30B-A3B | Qwen | OSS | 75% | 0.83 | persona_handling (5) | MoE; mid pedagogy |
| 16 | Qwen3 8B | Qwen | OSS | 68% | 0.84 | math (11) | Math-weak; good rubric on passes |
| 17 | Qwen2.5 14B | Qwen | OSS | 63% | 0.75 | math (8) | Baseline mid-size |
| 18 | Qwen2.5 7B | Qwen | OSS | 58% | 0.71 | math (8) | Usable floor for tutoring |
| 19 | Qwen3.5 2B | Qwen | OSS | 50% | 0.70 | math (11) | Borderline; small-model limits |
| 20 | Qwen2.5 3B | Qwen | OSS | 48% | 0.62 | math (10) | Weak math |
| 21 | Qwen3 1.7B | Qwen | OSS | 40% | 0.63 | math (11) | Below usable threshold |
| 22 | Qwen3 0.6B | Qwen | OSS | 23% | 0.51 | math (14) | Too small |
| 22 | Qwen3.5 0.8B | Qwen | OSS | 23% | 0.55 | math (16) | Too small |
| 24 | Qwen2.5 1.5B | Qwen | OSS | 22% | 0.47 | persona_handling (15) | Too small |
| 25 | Gemma 3 27B | Gemma | OSS | **7%†** | 0.35 | math (18) | *Invalid — see below* |
| 25 | Gemma 3 12B | Gemma | OSS | **7%†** | 0.36 | math (18) | *Invalid* |
| 25 | Gemma 3 4B | Gemma | OSS | **7%†** | 0.37 | persona_handling (18) | *Invalid* |
| 25 | Gemma 3 1B | Gemma | OSS | **7%†** | 0.36 | math (18) | *Invalid* |
| 29 | Qwen2.5 0.5B | Qwen | OSS | 2% | 0.36 | persona_handling (19) | Too small |

† **Gemma rows are not valid capability measurements.** Gemma 3 has no tool-calling support on the local inference server: 60/60 tool-requiring turns returned HTTP 400 and the engine served a fallback string. The identical 7% across a 27× parameter range (1B–27B) is the diagnostic signature of this artifact, not model quality (Section 6.3, 8.4).

**Historical comparison — Anthropic.** Anthropic Claude models are the platform's incumbent tutor and the rubric *judge*; they were not run as tutors in this benchmark to avoid self-judging bias. The production system operates on Claude at a quality level the platform treats as the ≈100% pedagogical reference. The purpose of this benchmark is precisely to find a non-Anthropic model that approaches that reference — which, per the table above, **Qwen3.5 4B (100%)** and **Qwen3-Next-80B (98%)** now do on this dataset.

**Historical comparison — the first (buggy) run.** The following table shows the effect of the `num_ctx` fix that defines *Improved Evaluation 2*, for the nine models re-run:

| Model | Prev. Pass % (buggy) | Curr. Pass % (fixed) | Δ | Remark |
|---|---|---|---|---|
| Qwen3.5 4B | 38% | 100% | **+62** | Truncation removed → perfect |
| Qwen3.5 9B | 20% | 93% | **+73** | Resolved the inverted-scaling anomaly |
| Qwen3 14B | 63% | 92% | +29 | |
| Qwen3 4B | 52% | 85% | +33 | |
| Qwen3 8B | 48% | 68% | +20 | Residual math weakness is genuine |
| Qwen3.5 2B | 25% | 50% | +25 | |
| Qwen3 1.7B | 15% | 40% | +25 | |
| Qwen3.5 0.8B | 8% | 23% | +15 | |
| Qwen3 0.6B | 12% | 23% | +11 | |

### 5.2 OSS smaller models assessed — leaderboard

Self-hostable open-weight models (run locally via Ollama). These are the deployment-relevant candidates for cost-free / on-premises operation.

| Rank | Model | Params | Pass % | Rubric | Major Bottleneck |
|---|---|---|---|---|---|
| 1 | Qwen3.5 4B | 4B | 100% | 0.92 | — |
| 2 | Qwen3.5 9B | 9B | 93% | 0.92 | crosscutting |
| 2 | Qwen3.6 27B | 27B | 93% | 0.91 | persona_handling |
| 2 | Qwen3.6 35B-A3B | 35B-A3B | 93% | 0.91 | crosscutting |
| 5 | Qwen3 14B | 14B | 92% | 0.89 | advance |
| 6 | Qwen3 4B | 4B | 85% | 0.86 | persona_handling |
| 6 | Qwen2.5 32B | 32B | 85% | 0.84 | math |
| 6 | Qwen2.5 72B | 72B | 85% | 0.84 | advance |
| 9 | Qwen3 30B-A3B | 30B-A3B | 75% | 0.83 | persona_handling |
| 10 | Qwen3 8B | 8B | 68% | 0.84 | math |
| 11 | Qwen2.5 14B | 14B | 63% | 0.75 | math |
| 12 | Qwen2.5 7B | 7B | 58% | 0.71 | math |
| 13 | Qwen3.5 2B | 2B | 50% | 0.70 | math |
| 14 | Qwen2.5 3B | 3B | 48% | 0.62 | math |
| 15 | Qwen3 1.7B | 1.7B | 40% | 0.63 | math |
| 16 | Qwen3 0.6B / Qwen3.5 0.8B | <1B | 23% | 0.51–0.55 | math |
| 18 | Qwen2.5 1.5B | 1.5B | 22% | 0.47 | persona_handling |
| 19 | Gemma 3 (1–27B) | 1–27B | 7%† | ~0.36 | *(tool-calling artifact)* |
| 20 | Qwen2.5 0.5B | 0.5B | 2% | 0.36 | persona_handling |

**Deployment threshold.** Treating a rubric mean ≥ 0.85 and pass ≥ 90% as "production-grade," five open models qualify: **Qwen3.5 4B, Qwen3.5 9B, Qwen3.6 27B, Qwen3.6 35B-A3B, Qwen3 14B**. The smallest of these (4 B) runs comfortably on a single consumer GPU.

### 5.3 Proprietary and MaaS models assessed — leaderboard

| Rank | Model | Vendor | Access | Pass % | Rubric | Major Bottleneck |
|---|---|---|---|---|---|---|
| 1 | Qwen3-Next-80B Instruct | Alibaba | Vertex MaaS | 98% | 0.94 | tool_leak |
| 2 | Gemini 2.5 Flash | Google | API | 90% | 0.87 | pedagogy |
| 3 | Gemini 3.5 Flash | Google | API | 87% | 0.87 | math |
| 4 | Gemini 3.1 Pro | Google | API | 82% | 0.85 | persona_handling |
| 5 | Qwen3-Next-80B Thinking | Alibaba | Vertex MaaS | 82% | 0.83 | persona_handling |
| 6 | Gemini 2.5 Pro | Google | API | 80% | 0.81 | math |

**Observation.** Within Gemini, the *Flash* variants outperform the *Pro* variants on this benchmark (2.5 Flash 90% > 2.5 Pro 80%; 3.5 Flash 87% > 3.1 Pro 82%). The pattern is consistent with the thinking-mode artifact discussed in §8.3: Pro/thinking configurations spend more of their budget on internal reasoning that, on short single-turn pedagogical tasks, adds latency without commensurate quality — and occasionally destabilises the concise, action-ending format the rubric rewards. The MaaS Qwen-Next *instruct* similarly beats its *thinking* sibling (98% vs 82%).

### 5.4 Per-subject performance (Combined leaderboard)

Math = 34 scenarios (angle geometry); Geography = 26 scenarios (map skills). Cells show pass rate and rubric mean.

| Model | Math % | Math rubric | Geo % | Geo rubric | Subject gap (M−G) |
|---|---|---|---|---|---|
| Qwen3.5 4B | 100% | 0.93 | 100% | 0.91 | 0 |
| Qwen3-Next-80B Instruct | 97% | 0.94 | 100% | 0.93 | −3 |
| Qwen3.6 27B | 94% | 0.92 | 92% | 0.91 | +2 |
| Qwen3.6 35B-A3B | 88% | 0.88 | 100% | 0.95 | −12 |
| Qwen3.5 9B | 91% | 0.91 | 96% | 0.92 | −5 |
| Qwen3 14B | 91% | 0.89 | 92% | 0.89 | −1 |
| Gemini 2.5 Flash | 88% | 0.88 | 92% | 0.86 | −4 |
| Gemini 3.5 Flash | 88% | 0.86 | 85% | 0.88 | +3 |
| Qwen3 4B | 82% | 0.84 | 88% | 0.87 | −6 |
| Qwen2.5 32B | 79% | 0.83 | 92% | 0.86 | −13 |
| Qwen2.5 72B | 85% | 0.83 | 85% | 0.85 | 0 |
| Gemini 3.1 Pro | 74% | 0.83 | 92% | 0.89 | −18 |
| Qwen3-Next-80B Thinking | 82% | 0.83 | 81% | 0.83 | +1 |
| Gemini 2.5 Pro | 82% | 0.85 | 77% | 0.77 | +5 |
| Qwen3 30B-A3B | 76% | 0.82 | 73% | 0.84 | +3 |
| Qwen3 8B | 59% | 0.81 | 81% | 0.88 | −22 |
| Qwen2.5 14B | 65% | 0.75 | 62% | 0.76 | +3 |
| Qwen2.5 7B | 47% | 0.69 | 73% | 0.75 | −26 |
| Qwen3.5 2B | 41% | 0.70 | 62% | 0.70 | −21 |
| Qwen2.5 3B | 47% | 0.63 | 50% | 0.62 | −3 |
| Qwen3 1.7B | 41% | 0.67 | 38% | 0.59 | +3 |
| Qwen3 0.6B | 21% | 0.50 | 27% | 0.53 | −6 |
| Qwen3.5 0.8B | 18% | 0.54 | 31% | 0.57 | −13 |
| Qwen2.5 1.5B | 21% | 0.48 | 23% | 0.45 | −2 |
| Gemma 3 (all) | 3%† | 0.34 | 12%† | 0.37 | −9 |
| Qwen2.5 0.5B | 3% | 0.38 | 0% | 0.35 | +3 |

**Subject finding.** Math is systematically the harder subject: the large negative gaps (Gemini 3.1 Pro −18, Qwen2.5 7B −26, Qwen3 8B −22, Qwen2.5 32B −13) are almost all math-weaker, consistent with the "math" tag dominating the bottleneck column. Geography (recall/spatial reasoning) tolerates weaker models better than math (multi-step arithmetic + constraint adherence). Only at the very top (Qwen3.5 4B, Qwen-Next-80B) does the subject gap close to ≈0 — a marker of genuine, generalised pedagogical competence.

---

## 6. Detailed Analysis by Model Family

### 6.1 Alibaba / Qwen

**Overall performance.** Qwen is the strongest and most internally-varied family, holding 8 of the top 9 places yet also the bottom of the board. Configured correctly, Qwen reasoning models are excellent tutors: Qwen3.5 4B (100%), Qwen3.5 9B and Qwen3.6 27B/35B (93%), and Qwen3 14B (92%) all clear the production bar. The family's headline lesson is that **inference configuration dominates parameter count** — the `num_ctx` correction moved the same models by up to +73 points, and a 4 B model outscores an 80 B one.

**Rubric-based analysis.** Aggregated over all Qwen scenarios, dimension pass rates were: tutor_tone 99%, mistake_location 87%, actionability 86%, mistake_identification 85%, providing_guidance 73%, coherence 73%, human_likeness 62%, **reveals_answer 65%** (weakest). The two consistent weaknesses are **premature answer revelation** and **human-likeness / coherence** on the weaker models. Tone is essentially never a problem.

**Failure analysis (representative).**

- *Premature revelation* — `qwen2.5_7b` on `math_average_arithmetic_slip_001`. The tutor correctly re-derives the method but then states *"So, the value of x is 145°"*, scoring **0.0** on the "does not reveal the final answer" item (judge: "directly reveals the final answer"). Rubric criterion violated: `reveals_answer`. Likely cause: smaller Qwen models complete the arithmetic in their reasoning and, lacking a strong suppression prior, surface the result. Effect: dropped the scenario below threshold despite otherwise-correct content.
- *Answer leak via MCQ* — `qwen3_8b` on `math_capable_pushback_001` gives a well-structured explanation but appends a multiple-choice question whose correct option (D, 360°) is the very value under discussion, scoring **0.0** on non-revelation. Criterion violated: `reveals_answer`. This is a *systematic* pattern (Qwen's family-wide 65% on the dimension), not occasional.
- *Contrast — success* — `qwen2.5_32b` on the same leak-guard scenario: *"Not quite — 120 isn't right here. Angles around a point sum to 360°, so … What's 60 + 75 + 80?"* (rubric 0.97). It marks the error, gives the rule, withholds the value, ends with an action — a textbook pass.

**Common failure patterns.** (1) *Answer revelation* (the dominant Qwen failure). (2) *Math over-computation* — weaker Qwen models solve and reveal, or make arithmetic slips (the "math" bottleneck dominates all sub-8 B Qwen models). (3) *Robotic / templated phrasing* (human_likeness 62%) — heavy Markdown, LaTeX blocks, and enumerated scaffolds on mid-size models. (4) *Persona mishandling* on non-responders and probe-resistant students at small scale.

**Why the patterns occur.** Reasoning models trained for math/coding are primed to *produce the solution*; tutoring inverts that incentive (withhold it). The reveal-suppression instruction competes with a strong solve-and-show prior, and smaller models have less capacity to hold the constraint while still scaffolding. Over-formatting reflects code/chat post-training styles.

**Recommendations.** (a) Strengthen the *no-reveal* rule with a positive reframe and a worked exemplar of "hint without stating the value" (already partially present; extend for the reveal case specifically). (b) Add a light output-style constraint discouraging LaTeX/enumerations in the student-facing turn to lift human_likeness. (c) Keep temperature at the family-recommended 0.7/0.8/20; do **not** lower to greedy (degrades reasoning). (d) Prefer *instruct* over *thinking* variants for this short-turn task. (e) For deployment, select from the ≥90% tier; do not deploy sub-4 B models. (f) Fine-tuning on 200–500 in-domain tutoring turns is a high-ROI option for locking the no-reveal behaviour into small models.

### 6.2 Google / Gemini (proprietary)

**Overall performance.** Gemini is consistent and reliable but, on this benchmark, no Gemini model reaches the top open Qwen tier. Gemini 2.5 Flash leads at 90% (rubric 0.87); the family clusters 80–90%. Its signature strength is *coherence and tone*; its signature weakness is *diagnostic specificity* and *math*.

**Rubric-based analysis.** Aggregated Gemini dimension pass rates: tutor_tone 100%, actionability 94%, mistake_location 94%, coherence 92%, mistake_identification 90%, human_likeness 88%, providing_guidance 79%, **reveals_answer 78%**. Gemini is the most *balanced* family (no dimension below 78%) but is capped by a tendency toward generic diagnosis.

**Failure analysis (representative).**

- *Generic diagnosis* — `gemini-2.5-pro` on `math_average_wrong_mcq_001` replies *"Not quite — let's walk through it together."* It correctly invites reconsideration (partial credit) but scores **0.0** on "points at the specific location/nature of the mistake" (judge: "offers only a generic 'not quite' … without identifying the specific misconception"). Criteria violated: diagnostic-specificity item + partial on the hint item; the scenario barely passed (0.73) or failed on stricter variants. This is Gemini 2.5 Pro's *characteristic* miss and the reason it trails the Flash variants.
- *Contrast — success* — `gemini-3.1-pro` on `math_capable_pushback_001`: *"That is an excellent distinction! I mean angles going fully around in a complete circle, like spinning all the way around while standing on a mountain peak. … what is the total sum?"* (0.94) — warm, specific, ends with an action, withholds the value.

**Common failure patterns.** (1) *Generic encouragement without specific diagnosis* — "let's walk through it together" as a reflex, especially on Pro. (2) *Math under-performance* — Gemini 3.1 Pro's −18 math/geo gap is the family's starkest; math scenarios drive the "math"/"pedagogy" bottlenecks. (3) *Pro > Flash inversion* — larger/thinking Gemini variants are not better here (§8.3). Gemini essentially never fails on tone, coherence, or actionability.

**Why.** Gemini's instruction-following and safety-tuning favour smooth, non-committal encouragement — excellent for tone, but it can substitute empathy for a precise error diagnosis, which the rubric explicitly rewards. On math, the shorter Flash reasoning path appears better matched to single-turn arithmetic than the Pro thinking path.

**Recommendations.** (a) Prompt Gemini to *always name the specific step or misconception before hinting* — a targeted rule with 1–2 exemplars; Google guidance favours few-shot for Gemini and this directly attacks its weakness. (b) Use *positive* constraints (Google warns negative-only instructions over-index and hurt arithmetic). (c) Prefer **Flash** over **Pro** and avoid `thinkingBudget`/high thinking levels for this task. (d) Keep temperature at the provider default. (e) Gemini 2.5 Flash is the recommended managed-API tutor if an open model is not chosen.

### 6.3 Google / Gemma (open) — *invalid results, documented*

**Overall performance.** All four Gemma models scored an identical 7% (rubric ≈ 0.36). This is **not** a capability measurement. It is the signature of a total tool-calling failure.

**Root cause.** Gemma 3 has no tool-calling support in its Ollama chat template. The tutor engine requires a grading tool; every tool-requiring request returned **HTTP 400**, `_call_llm` caught the error and returned `None`, and the engine served its fallback string *"Sorry — I had trouble responding just now…"*. Across `gemma3:27b`, **60/60** turns returned 400 with **0** successful responses. The four "passes" are the four non-answer scenarios (idk/monosyllabic/refusal) where the fallback coincidentally reads as an acceptable re-engagement.

**Dimension signature (the tell).** Aggregated Gemma dimensions: providing_guidance **0%**, human_likeness **13%**, coherence 46%, mistake_identification 48% — yet mistake_location **100%**, reveals_answer **100%**, actionability **100%**, tutor_tone **100%**. A real tutor cannot score 0% on guidance while scoring 100% on four other dimensions; the fallback string vacuously satisfies the "did-not" dimensions (it misattributes nothing, reveals nothing) while failing the "did" dimensions (it guides nothing, reads as non-human). This dissociation is definitive evidence of an infrastructure artifact.

**Recommendations.** Gemma must be re-evaluated through a **prompted (text) tool-calling adapter**: detect tool-incapable models, omit the `tools` field from the request, inject the tool schema into the prompt with an instruction to emit `record_answer(...)` as text, and recover it via the engine's existing text-tool-call parser. Until then, **exclude Gemma from all comparative conclusions**. Alternatively, evaluate Gemma via a surface that supports its native function calling (Vertex/AI Studio) rather than the local server.

---

## 7. Cross-Family Comparison

Family-level dimension pass rates (aggregated over each family's scenarios; Gemma shown but invalid):

| Dimension | Gemini | Qwen | Gemma† |
|---|---|---|---|
| mistake_identification | 90% | 85% | 48% |
| mistake_location | 94% | 87% | 100%‡ |
| reveals_answer (want no) | 78% | 65% | 100%‡ |
| providing_guidance | 79% | 73% | 0% |
| actionability | 94% | 86% | 100%‡ |
| coherence | 92% | 73% | 46% |
| tutor_tone | 100% | 99% | 100%‡ |
| human_likeness | 88% | 62% | 13% |

‡ vacuously satisfied by Gemma's fallback string; not indicative of competence.

| Axis | Best family | Notes |
|---|---|---|
| **Accuracy (pass rate ceiling)** | Qwen | Qwen holds ranks 1–6; Gemini peaks at rank 7 |
| **Robustness (range within family)** | Gemini | 80–90% band; Qwen spans 2–100% (size-dependent) |
| **Consistency across dimensions** | Gemini | No valid dimension < 78% |
| **Instruction following (no-reveal)** | Gemini (78% vs 65%) | Both families' weakest constraint |
| **Reasoning / diagnosis quality** | Qwen (top tier) | Qwen-Next/3.5/3.6 give the most specific diagnoses |
| **Hallucination / misattribution** | Gemini (mistake_location 94%) | Qwen 87%; both strong |
| **Formatting / human-likeness** | Gemini (88% vs 62%) | Qwen over-formats (LaTeX/enumerations) |
| **Tone** | Tie (~100%) | Neither family fails tone |
| **Reliability (deployability)** | Qwen (top open models) | Gemini simplest to operate (managed API) |

**Synthesis.** Gemini is the more *uniformly* competent family — a safe, balanced choice that rarely embarrasses itself but rarely tops the board. Qwen is the higher-*ceiling* family — its best models are the best tutors in the study, but quality is strongly conditional on size and configuration. The single most important cross-family weakness is **premature answer revelation**, on which *every* family underperforms relative to its other dimensions.

---

## 8. Discussion

**8.1 Why certain families outperform.** Qwen's top models pair strong math/reasoning post-training with enough capacity to *hold the no-reveal constraint while scaffolding*. Gemini's uniformity reflects heavy instruction-and-safety tuning that guarantees tone and coherence but softens diagnostic specificity. Gemma's collapse is purely infrastructural.

**8.2 Proprietary vs open trade-offs.** The headline result — an open 4 B model (Qwen3.5 4B, 100%) beating a proprietary frontier model (Gemini 2.5 Flash, 90%) and a 20×-larger MaaS model (Qwen-Next-80B, 98%) — indicates that, for this bounded pedagogical task, open models have closed the gap. Open models trade managed-API convenience for zero marginal cost, data locality, and offline capability; proprietary models trade cost for operational simplicity and stronger baseline uniformity.

**8.3 Do larger models win?** No. Within Qwen, 4 B ≈ 100% while 72 B = 85% and 80 B-thinking = 82%; within Gemini, Flash > Pro consistently. Two mechanisms: (i) *thinking-mode mismatch* — on short single-turn tasks, extended reasoning adds latency and can destabilise the concise, action-ending format the rubric rewards; (ii) *instruction-tuning recency* — the newer 3.5/3.6 generations encode tutoring-relevant behaviours (no-reveal, specific diagnosis) that raw scale does not confer. Parameter count is a poor predictor once generation and configuration are controlled.

**8.4 Threats to validity — infrastructure.** This study's central methodological lesson is that *benchmark infrastructure defects masquerade as model-quality differences*. Two such defects were caught only because per-turn token/block logs were retained: (1) the **`num_ctx`=4096 truncation** (empty completions from reasoning models; fixed, models re-run), and (2) the **Gemma tool-calling 400** (fallback string served for every turn; documented, not yet fixed). Both produced plausible-looking but entirely spurious scores (an inverted size curve; an identical 7% across a 27× range). We therefore treat *unexplained non-monotonicities* as infrastructure suspects until proven otherwise.

**8.5 Threats to validity — judge confound.** The rubric judge is an Anthropic model, and the incumbent is Anthropic. We do not evaluate Claude as a tutor here, so self-preference cannot inflate the incumbent's rank (it has none). However, judge-family stylistic preferences could in principle advantage models whose style resembles Claude's. We mitigate via temperature-0 scoring, many small verbatim items, and a fixed judge; a cross-judge replication (e.g., a Gemini or open judge) is planned (Section 9).

**8.6 Generalisability.** The benchmark covers two subjects, one grade band, six personas, and 60 single-turn scenarios. Findings generalise to single-turn secondary-STEM tutoring with reasonable confidence; extension to multi-turn dialogue, other subjects, and other languages (relevant for the Tanzania/Swahili pilot) is untested and is the priority for the next iteration.

---

## 9. Recommendations for the Next Evaluation

**9.1 Dataset improvements.** (a) Expand beyond 60 single-turn scenarios — add multi-turn trajectories to test memory, persistence, and recovery. (b) Broaden subjects (biology, history) and grade bands. (c) Add a **Swahili / multilingual** slice for the Tanzania pilot. (d) Balance persona counts (error_prone has only 1 scenario). (e) Add harder answer-leak and false-accept edge cases, since answer-revelation is the universal weakness. (f) Increase geography depth (only 3 scenarios use lesson 1464).

**9.2 Rubric improvements.** (a) Add an explicit **no-reveal severity** weight (currently one item among many; it is the highest-leverage constraint). (b) Add a *diagnostic-specificity* score with graded levels rather than binary. (c) Reduce ambiguity in conditional items via more worked "n/a" guidance. (d) Consider a *repair* dimension for multi-turn (did the tutor recover after a student error?).

**9.3 Pipeline improvements.** (a) **Cross-judge replication** — re-score with a non-Anthropic judge to bound the judge confound. (b) **Automated infrastructure guards** — assert `num_ctx ≥ prompt + num_predict`, assert 0 HTTP-400s, and flag any `blocks=[]` rate > 0 before trusting a run. (c) **Multiple runs** per model to estimate variance (currently single-run). (d) **Capability pre-flight** — probe each model's tool-calling support and route tool-incapable models through the prompted adapter. (e) Structured error-analysis tooling to auto-cluster failures by tag/dimension.

**9.4 Prompt improvements (pre-rerun).** (a) For Qwen: extend the no-reveal exemplar set; add an output-style constraint (no LaTeX/enumerations in the student turn). (b) For Gemini: a "name the specific error before hinting" rule with exemplars; positive framing only. (c) Ship the **Gemma prompted-tool-calling adapter** so Gemma is measurable. (d) Re-confirm per-family prompt selection leaves the Anthropic/production prompt byte-identical.

---

## 10. Selecting an Alternative to Anthropic Models

The benchmark's operational purpose is to recommend a non-Anthropic tutor for schools that cannot or prefer not to use Claude.

**Closest to the Anthropic reference (≈100%).** Two models reach or exceed the platform's quality bar on this dataset: **Qwen3.5 4B (100%, rubric 0.92)** and **Qwen3-Next-80B Instruct (98%, rubric 0.94)**. Three more clear 90%: **Qwen3.5 9B, Qwen3.6 27B, Qwen3.6 35B-A3B (all 93%)**, and **Qwen3 14B (92%)**. Among managed APIs, **Gemini 2.5 Flash (90%)** leads.

**Most likely to exceed 90% after prompt/eval refinement.** Models already at 82–88% that a no-reveal/specific-diagnosis prompt patch should lift: **Gemini 3.5 Flash (87%)**, **Qwen3 4B (85%)**, **Qwen2.5 32B (85%)**, and **Gemini 3.1 Pro (82%)**. Gemma models are excluded until the tool-calling adapter ships (likely strong once fixed, given their generation quality).

**Practical trade-offs.**

| Candidate | Cost | Speed | Reliability | Deployment | Open? | Enterprise suitability |
|---|---|---|---|---|---|---|
| **Qwen3.5 4B** | Zero marginal (self-host) | Very fast (4 B) | High (100% here) | Single consumer GPU / edge | Yes | Excellent for on-prem / offline schools |
| **Qwen3.6 27B** | Zero marginal (self-host) | Moderate (needs ~24 GB) | High (93%) | One workstation/server GPU | Yes | Headroom option; still open |
| **Qwen3-Next-80B** | Per-token (Vertex MaaS) | Fast (A3B MoE) | Highest (98%) | Managed cloud | Weights open; served MaaS | Strong if managed cloud acceptable |
| **Gemini 2.5 Flash** | Per-token (API) | Fast | High (90%) | Managed API only | No | Simplest ops; data leaves premises |

**Recommendation.** For the Nyansapo deployment context — cost-sensitive, often offline, data-locality-conscious schools — we recommend **Qwen3.5 4B** as the primary non-Anthropic tutor: it is the top scorer, self-hostable on modest hardware, and free at the margin, directly enabling the offline/on-prem deployments the platform targets. Where more headroom is wanted and a workstation GPU is available, **Qwen3.6 27B** is the conservative open choice. Where a managed API is acceptable and self-hosting is undesirable, **Gemini 2.5 Flash** is the recommended proprietary alternative. All three should undergo a confirmatory multi-turn and multilingual evaluation (Section 9) before production rollout, and the recommended small-model choice should be spot-checked on a sample of transcripts to confirm the 100% is earned rather than an artifact of the single-run, single-judge configuration.

---

## 11. Conclusion

**Major findings.** After correcting a context-window truncation defect, *Improved Evaluation 2* shows that a carefully-configured **small open model (Qwen3.5 4B) matches the platform's Anthropic-level quality (100% pass, rubric 0.92)** and outscores both a proprietary frontier model and a 20×-larger MaaS model. Five open Qwen models clear the 90% production bar; Gemini is uniformly competent but caps below the open leaders; parameter count is a poor predictor of tutoring quality once configuration is controlled.

**Most significant bottlenecks.** (1) Evaluation-infrastructure fragility — a default context window silently truncated reasoning models, and absent tool-calling support invalidated an entire family (Gemma); both were caught only via structured logging. (2) The universal *pedagogical* weakness is **premature answer revelation** (Qwen 65%, Gemini 78% on the dimension), followed by **generic, non-specific error diagnosis** (notably Gemini Pro).

**Key recommendations.** Add automated infrastructure guards and cross-judge replication; strengthen the no-reveal and specific-diagnosis rubric weights and prompts; ship the Gemma prompted-tool-calling adapter; and extend the dataset to multi-turn and multilingual scenarios.

**Expected improvements next iteration.** With the prompt patches, we anticipate the 82–88% cohort (Gemini 3.5 Flash, Qwen3 4B, Qwen2.5 32B) rising into the 90s and Gemma becoming measurable (and likely competitive at 12–27 B).

**Final deployment recommendation.** Adopt **Qwen3.5 4B** as the primary non-Anthropic tutoring engine for cost-sensitive, offline-capable deployments, with **Qwen3.6 27B** and **Gemini 2.5 Flash** as headroom and managed-API alternatives respectively — pending a confirmatory multi-turn, multilingual, multi-run, cross-judge validation.

---

### Appendix A — Reproducibility

Engine: `apps/tutoring/simple_tutor` (two-call tool loop). Scorers: `evals/scorers/{deterministic,llm_rubric,trajectory}.py`. Dataset: `evals/dataset/*.yaml` (60 single-turn scenarios). Per-family configuration: `apps/llm/model_profiles.py`. Judge: Anthropic Haiku 4.5, temperature 0, fixed provider chain. Results: `offline_eval/results3/*.json` (29 models); reproduce the board with `RESULTS_DIR=offline_eval/results3 python offline_eval/aggregate.py`. The `num_ctx` fix that defines this iteration: `apps/llm/client.py` (`OllamaClient.generate_with_tools`).

### Appendix B — Known invalid rows

Gemma 3 (1B/4B/12B/27B): tool-calling unsupported on the local server; 60/60 HTTP-400; scores are fallback artifacts. Exclude from all conclusions until the prompted-tool-calling adapter is implemented and Gemma is re-run.
