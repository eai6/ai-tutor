# Offline tutor-model evaluation — findings

**Date:** 2026-06-12 · **Status:** Laptop tier complete (11 models); 14B+ tier pending (Colab)
**Author:** AI Tutor team · **Context:** model selection for offline / low-connectivity deployment (Mozambique, Tanzania)

## Why we ran this
The pilot runs on a hosted Anthropic model. For data-residency and offline use in
low-connectivity schools, we need a tutor model that runs **locally on-device**
(phone/tablet and modest laptop). This evaluation ranks open-source models on how
well they drive our **real production tutoring engine** — not a toy benchmark.

## How we measured it
- **Tutor under test:** each open-source model, run locally via Ollama, driving the
  production `simple_tutor` engine (it controls pedagogy through tool-calls:
  pose question → grade answer → advance step).
- **Scorers held constant on Anthropic** (our top-tier, trusted reference): the
  **judge** and the **student-simulator** are the same Anthropic models we use today,
  so every model is graded on an identical, high-quality yardstick.
- **Test set:** 60 single-turn lesson scenarios spanning math and reading, across
  several student personas, scored on pass/fail + a 0–1 quality rubric.
- **One engine change** was required and is done: the tutor engine previously
  hard-coded the Anthropic SDK; it now goes through our pluggable client layer, so
  any provider (incl. local models) can drive it. The Anthropic path is unchanged.

## Results (60 scenarios each, 0 errors)

| Rank | Model | Device tier | Pass rate | Quality (rubric) |
|---:|---|---|---:|---:|
| 1 | **qwen2.5:3b** | phone/tablet | **45%** | **0.61** |
| 2 | llama3.2:3b | phone/tablet | 28% | 0.46 |
| 2 | qwen2.5:1.5b | phone/tablet | 28% | 0.50 |
| 4 | llama3-groq-tool-use:8b | laptop | 22% | 0.46 |
| 5 | granite3.1-dense:2b | phone/tablet | 15% | 0.41 |
| 5 | granite3.1-moe:3b | phone/tablet | 15% | 0.37 |
| 7 | llama3.2:1b | phone/tablet | 8% | 0.29 |
| 7 | nemotron-mini | phone/tablet | 8% | 0.39 |
| 9 | qwen2.5:0.5b | phone/tablet | 7% | 0.37 |
| 10 | hermes3:3b | phone/tablet | 3% | 0.34 |
| 11 | gemma2:2b | phone/tablet | 0% | 0.32 |

## Key findings
1. **`qwen2.5:3b` is the clear leader** — 45% pass / 0.61 quality, nearly double the
   next-best, and it's small enough to run on a **tablet/phone**. This is our lead
   candidate for offline deployment.
2. **Bigger did not mean better.** The largest model that fit our 8 GB test laptop
   (the 8B tool-tuned Llama) came **4th**, beaten by three sub-3B models. Picking the
   right model family matters far more than raw size for on-device tutoring.
3. **One model is unusable:** `gemma2:2b` scored 0% — it cannot make the tool-calls
   the engine needs.
4. **Common weak spot:** adapting tone/register to each student persona
   ("persona_handling") is the most frequent failure across nearly all models, and
   the math reasoning is the 8B model's specific weakness. Both are addressable with
   targeted prompt tuning on the chosen model.

## What this means for deployment
- A **~3B model is viable on-device today** at meaningfully better quality than the
  smaller options — promising for the phone/tablet tier.
- **45% is a starting baseline, not the ceiling.** These are stock models with no
  tutor-specific tuning. The hosted Anthropic baseline is far higher; the goal is to
  close that gap with the best offline candidate via prompt/fine-tuning.

## Cost & footprint
- Run **entirely on a local laptop** (8 GB RAM, no GPU) — no infrastructure spend.
  Only cost was Anthropic API calls for the judge/student-simulator scoring.

## Next steps
1. **Larger models (14B–70B)** can't run on the 8 GB laptop. A ready-to-run Google
   Colab (free T4 GPU) notebook is prepared to test `qwen2.5:14b`, `phi4`, etc. — to
   see whether scaling Qwen past 3B is worth the heavier hardware.
2. **Prompt-tune the leading candidate** on its two weak areas (persona handling,
   math) and re-measure.
3. **Decide the deployment tier** (3B on-device vs. a larger model on a school
   server) once the 14B+ numbers are in.

*All work is local; nothing has been committed or deployed.*
