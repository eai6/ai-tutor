# Offline tutor-model evaluation — findings

**Date:** 2026-06-15 · **Status:** 23 open-source models scored (laptop + Colab T4). Cloud benchmarks (Gemini / Claude) in progress.
**Author:** AI Tutor team · **Context:** model selection for offline / low-connectivity deployment (Mozambique, Tanzania)

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
  meaningfully better (52%) at the best teaching quality measured.
- **These are stock models with no tutor-specific tuning** — a starting baseline, not a
  ceiling. The cloud benchmarks below establish the target to close toward.

## Cost & footprint
- Small models ran on a **local 8 GB laptop, no GPU** (zero infra cost). The 7–14B tier
  ran on a **free Colab T4**. Only spend was Anthropic API calls for scoring.

## Next steps
1. **Cloud benchmarks (in progress):** Gemini (2.5/3.x Pro & Flash) and Claude
   (Opus / Sonnet / Haiku) run through the *same* harness to mark the quality ceiling
   the offline candidates should aim for. (Caveat: when a Claude model is the *tutor*,
   the pass/fail grader stays cross-family, but the Anthropic rubric judge is
   same-family — read those rubric numbers with that in mind.)
2. **Recover phi4 / falcon3** via the tool-leak diagnostic, then re-score.
3. **Prompt-tune the leading offline candidate** (qwen2.5:7b / :3b) on math + persona.
4. **Pick the deployment tier** once the cloud ceiling and the tuned numbers are in.

*All work is local/Colab; nothing has been deployed to production.*
