# Offline tutor-model evaluation — findings

**Date:** 2026-06-18 · **Status:** 44 models scored — 23 open-source (laptop + Colab T4) + 7 proprietary benchmarks (Claude / Gemini) + 14 large open-weight via Vertex Model Garden MaaS (DeepSeek / Kimi / Qwen3 / Grok / GLM).
**Author:** AI Tutor team · **Context:** model selection for offline / low-connectivity deployment (Mozambique, Tanzania)

## TL;DR
The proprietary ceiling is **Claude Opus 4.7 at 90%**. The best offline model,
**qwen2.5:14b at 55%**, reaches ~61% of that — a real but closeable gap. For
on-device use, **qwen2.5:3b (45%)** is the phone/tablet pick and **qwen2.5:7b
(52%, best rubric 0.71 of any open model)** the laptop/server pick. A 7B open model
already rivals Gemini 3.5 Flash (50%). Stock models, no tutor-specific tuning yet.
Cloud-hosted open weights go much further: via Vertex Model Garden, **Grok 4.1-fast
(reasoning) hits 72%** — the best non-Anthropic model in the whole benchmark, behind
only Claude (Opus/Haiku/Sonnet) and ahead of every Gemini — with **Qwen3-Coder 480B
at 68%** and **GLM-4.7 at 67%** close behind. See the two Vertex Model Garden sections.

## Proprietary benchmark ceiling (same harness, 60 scenarios)

| Model | Vendor | Pass rate | Rubric |
|---|---|---:|---:|
| claude-opus-4-7 | Anthropic | **90%** | 0.88 |
| claude-haiku-4-5 | Anthropic | 82% | 0.86 |
| claude-sonnet-4-6 | Anthropic | 78% | 0.82 |
| gemini-2.5-flash | Google | 65% | 0.74 |
| gemini-3.1-pro-preview | Google | 58% | 0.71 |
| gemini-3.5-flash | Google | 50% | 0.66 |
| gemini-2.5-pro | Google | 43%* | 0.64 |

\* **Gemini Pro anomaly:** the Pro models score *below* the Flash models
(2.5-flash 65% > 3.1-pro 58% > 2.5-pro 43%), which is backwards. The Pro models
returned empty responses in id-probing (thinking-mode), so this is likely a harness
interaction, not true capability — treat the Gemini-Pro rows as **suspect pending a
diagnostic** (same playbook that recovered GLM-4). Haiku 4.5 (82%) also beats
Sonnet 4.6 (78%), which is plausible for this recent, strong small model.

## Vertex Model Garden — large open-weight models via MaaS (new, 2026-06-18)

Same 60-scenario single-turn harness, same Anthropic judge + student-sim; the
tutor is served by **Google Vertex AI Model Garden** as Model-as-a-Service
(pay-per-token, OpenAI-compatible endpoint — no GPU endpoints deployed). All four
ran with **0 harness errors and 0 empty-response retries**.

| Model | Vendor | Mode | Pass rate | Rubric |
|---|---|---|---:|---:|
| deepseek-v3.2 | DeepSeek | non-thinking | **58%** | 0.71 |
| kimi-k2-thinking | Moonshot | thinking | 57% | 0.68 |
| deepseek-v3.1 | DeepSeek | non-thinking | 45% | 0.62 |
| deepseek-r1-0528 | DeepSeek | thinking | 22% | 0.41 |

**Findings:**
1. **DeepSeek V3.2 (58%, rubric 0.71) is the strongest open-weight model tested** —
   it ties Gemini 3.1 Pro, beats Gemini 3.5 Flash (50%) and *every* locally-run open
   model (best was qwen2.5:14b at 55%). A genuine cloud-hosted open-weight option in
   the Gemini-Flash tier.
2. **Newer beats older within DeepSeek:** V3.2 (58%) clearly outscores V3.1 (45%).
3. **Kimi K2 Thinking (57%) is competitive** — its structured tool-calls drove the
   tutor cleanly despite being a reasoning model.
4. **DeepSeek R1 (22%) is poorly suited to this task** — R1 is reasoning-only, and
   DeepSeek tool-calling is documented as non-thinking-mode only, so the tool-driven
   tutor underperforms. The low score is model/task fit, **not** a harness artifact
   (0 errors, 0 empty-response retries). Math + persona remain the dominant failure
   categories, consistent with the rest of the field.

*Integration: `apps/llm/client.py::VertexModelGardenClient` (OpenAI-compatible
Vertex MaaS endpoint, ADC auth, per-region routing). See
`docs/superpowers/specs/2026-06-17-vertex-model-garden-eval-design.md`.*

## Vertex Model Garden — batch 2: Qwen3 / Grok / GLM (2026-06-18)

Ten more MaaS models on the same 60-scenario harness (all `global`). All scored
**0 harness errors**.

| Model | Vendor | Mode | Pass rate | Rubric |
|---|---|---|---:|---:|
| grok-4.1-fast-reasoning | xAI | reasoning | **72%** | 0.80 |
| qwen3-coder-480b-a35b | Qwen | instruct | 68% | 0.76 |
| glm-4.7 | Zhipu | — | 67% | 0.75 |
| qwen3-next-80b-a3b-instruct | Qwen | instruct | 65% | 0.71 |
| qwen3-235b-a22b-instruct-2507 | Qwen | instruct | 63% | 0.74 |
| glm-5 | Zhipu | — | 57% | 0.73 |
| grok-4.1-fast-non-reasoning | xAI | non-reasoning | 57% | 0.72 |
| grok-4.20-non-reasoning | xAI | non-reasoning | 48% | 0.64 |
| grok-4.20-reasoning | xAI | reasoning | 45% | 0.65 |
| qwen3-next-80b-a3b-thinking | Qwen | thinking | 2% | 0.34 |

**Findings:**
1. **Grok 4.1-fast (reasoning) at 72% is the best non-Anthropic model in the entire
   44-model benchmark** — behind only Claude Opus/Haiku/Sonnet, ahead of every Gemini,
   DeepSeek, Qwen and GLM. A strong cloud-hosted frontier option.
2. **Qwen3-Coder 480B (68%) and GLM-4.7 (67%)** are the next tier — both beat
   Gemini 2.5 Flash (65%) and every locally-run open model. Qwen3 instruct models are
   uniformly strong (Coder 68%, Next-80B 65%, 235B 63%).
3. **Newer ≠ better, twice:** GLM-4.7 (67%) > GLM-5 (57%), and Grok 4.1-fast
   (72%/57%) > Grok 4.20 (45%/48%) on both reasoning and non-reasoning variants.
4. **Reasoning mode is model-dependent for the tool-driven tutor:** it *helps* Grok
   4.1-fast (72% reasoning vs 57% non-reasoning) but *hurts* Grok 4.20 (45% vs 48%).
5. **Pure-thinking models truncate badly:** qwen3-next-80b-**thinking** scored 2%
   (vs 65% for its **instruct** sibling) — reasoning tokens exhaust the tutor's
   budget before it emits the tool call, the same failure as deepseek-r1 (22%). For
   this tool-loop tutor, **instruct/non-thinking variants are the right default.**

**Data-integrity note:** the first pass of this batch was corrupted by a **client-side
internet outage** mid-sweep — the Anthropic rubric-judge calls returned
`APIConnectionError` for 7 of the 10 models, scoring capable models as 0% (a judge
outage, not model capability; tool-calls verified working throughout). Those 7 were
re-run once connectivity returned; **all 10 rows above are from runs with
`judge_failed=0`.** Follow-up worth doing: harden the harness so a judge
connectivity failure is recorded as `errored` (or retried) rather than silently
scored as a model failure.

## Why we ran this
The pilot runs on a hosted Anthropic model. For data-residency and offline use in
low-connectivity schools, we need a tutor model that runs **locally on-device**
(phone/tablet and modest laptop). This evaluation ranks open-source models on how
well they drive our **real production tutoring engine** — not a toy benchmark.

## How we measured it
- **Tutor under test:** each model drives the production `simple_tutor` engine
  (it controls pedagogy through tool-calls: pose question → grade answer → advance).
- **Scorers held constant on Anthropic** (our trusted reference): the **judge** and
  **student-simulator** are the same Anthropic models we use in production, so every
  model is graded on an identical, high-quality yardstick. The pass/fail grader is
  **cross-family** (it excludes the tutor's own vendor), so a model never grades itself.
- **Test set:** 60 single-turn lesson scenarios (math + reading, multiple personas),
  scored pass/fail + a 0–1 quality rubric.
- **Two engine changes** were required and are done: (1) the engine was hard-wired to
  the Anthropic SDK — now it routes any provider through our pluggable client layer;
  (2) some open models emit tool-calls as text rather than via the structured channel —
  the client now parses those leaks. The Anthropic path is unchanged.
- **Hardware:** small models (≤9B) on an 8 GB CPU laptop; 7–14B models on a free
  Google Colab T4 GPU. Same harness, same scenarios → directly comparable scores.

## Results — 23 open-source models (60 scenarios each, 0 errors)

| Rank | Model | Params | Device tier | Pass rate | Rubric |
|---:|---|---|---|---:|---:|
| 1 | **qwen2.5:14b** | 14B | GPU laptop/server | **55%** | 0.66 |
| 2 | mistral-nemo:12b | 12B | GPU laptop/server | 53% | 0.67 |
| 3 | **qwen2.5:7b** | 7B | GPU laptop | 52% | **0.71** |
| 4 | **qwen2.5:3b** | 3B | **phone/tablet** | 45% | 0.61 |
| 5 | glm4:9b | 9B | GPU laptop | 43% | 0.60 |
| 6 | granite3.1-dense:8b | 8B | GPU laptop | 33% | 0.56 |
| 6 | llama3.1:8b | 8B | GPU laptop | 33% | 0.53 |
| 8 | mistral:7b | 7B | GPU laptop | 32% | 0.54 |
| 9 | llama3.2:3b | 3B | phone/tablet | 28% | 0.46 |
| 9 | qwen2.5:1.5b | 1.5B | phone/tablet | 28% | 0.50 |
| 11 | hermes3:8b | 8B | GPU laptop | 22% | 0.43 |
| 11 | llama3-groq-tool-use:8b | 8B | GPU laptop | 22% | 0.46 |
| 13 | command-r7b | 7B | GPU laptop | 15% | 0.42 |
| 13 | granite3.1-dense:2b | 2B | phone/tablet | 15% | 0.41 |
| 13 | granite3.1-moe:3b | 3B | phone/tablet | 15% | 0.37 |
| 16 | aya-expanse:8b | 8B | GPU laptop | 10% | 0.38 |
| 17 | llama3.2:1b · nemotron-mini | 1–4B | phone/tablet | 8% | 0.29–0.39 |
| 19 | qwen2.5:0.5b | 0.5B | phone/tablet | 7% | 0.37 |
| 20 | hermes3:3b | 3B | phone/tablet | 3% | 0.34 |
| 21 | falcon3:10b · gemma2:2b · phi4 | 2–14B | — | 0% | 0.32 |

## Key findings
1. **Qwen2.5 sweeps every size tier** — 3B (45%) → 7B (52%) → 14B (55%). Scaling
   helps but with **diminishing returns**, and the **7B has the best teaching-quality
   score (0.71) of all 23 models**. Recommendations by tier:
   - **Phone/tablet:** `qwen2.5:3b` (45%) — the clear on-device champion.
   - **Laptop / school server:** `qwen2.5:7b` — near-top pass rate at half the size of
     14B, and the best rubric quality. The value pick.
2. **Model family matters far more than size.** `mistral-nemo:12b` (53%) beats
   `llama3.1:8b` (33%) by 20 points; the largest model that fit our 8 GB laptop (an 8B)
   sits mid-pack. Picking the right family beats simply going bigger.
3. **GLM-4:9b (43%) is a viable alternative** — once the engine parsed its tool-call
   format, it scored solidly.
4. **Three models scored 0% (falcon3:10b, phi4, gemma2:2b)** — a tool-protocol failure,
   not a teaching failure. gemma2 has no tool capability; falcon3/phi4 likely leak
   tool-calls in a format the parser doesn't yet handle. **phi4 (a strong 14B) is worth
   recovering** — a follow-up diagnostic run will capture its format.
5. **Universal weak spots:** math reasoning and persona/tone adaptation are the most
   common failure categories across the board — both addressable with targeted prompt
   tuning on the chosen model.

## What this means for deployment
- A **~3B model is viable on-device today** (45%), and a **7B on a school server** is
  meaningfully better (52%) at the best teaching quality of any open model.
- The **frontier ceiling is Opus 4.7 at 90%**; the best offline model reaches ~61% of
  that pass rate. **These are stock models with no tutor-specific tuning** — a starting
  baseline. The gap to the ceiling is the target to close with prompt/fine-tuning.

## Cost & footprint
- Small models ran on a **local 8 GB laptop, no GPU** (zero infra cost). The 7–14B tier
  ran on a **free Colab T4**. Cloud benchmarks ran via API (paid). Scoring spend was
  Anthropic/Gemini judge calls throughout.

## Methodology note (for the benchmark rows)
The headline **pass rate is cross-family judged for every model** — the grader excludes
the tutor's own vendor, so no model grades itself. Only the secondary rubric/label
layers can be same-family for a Claude or Gemini tutor; read those columns with that in
mind. All 30 models ran the identical 60-scenario single-turn set.

## Next steps
1. **Diagnose the Gemini-Pro anomaly** (Pro scoring below Flash; likely thinking-mode /
   tool-handling) and the **phi4 / falcon3** 0% (tool-leak format), then re-score — three
   capable models currently under-measured by harness artifacts, not ability.
2. **Prompt-tune the leading offline candidate** (qwen2.5:7b / :3b) on math + persona,
   the two universal weak spots, and re-measure against the ceiling.
3. **Pick the deployment tier** (3B on-device vs 7–14B on a school server) once the
   tuned numbers are in.

*All work is local / Colab / API; nothing has been deployed to production.*
