# Cloud arms — Opus, Gemini Flash, GPT mini

**Question:** how do the local Qwen tiers compare to a cloud tutor on the same
scenarios, same tutor build, same instrument?

**Status:** prepared, not started. Runs after math-27b finishes and the GPU
instance is destroyed.

---

## 1. The arms

| arm | model | surface | why this one |
|---|---|---|---|
| Anthropic | `claude-opus-4-7` | free text | the tutoring model this platform actually ships (`project_tutor_model_choice`) |
| Google | `gemini-3.5-flash` | free text | the cheap-fast tier; already the judge vendor |
| OpenAI | `gpt-5.4-mini` | free text | first-contact for this harness — see the caveat |

One model per provider, deliberately. This is a **ceiling reference for the
local arms**, not a cloud leaderboard.

**All three are free text**, matching qwen3.8-27b. `answer_surface` is unset on
their profiles, so `_uses_answer_picker` falls through to the provider rule and
only `local_ollama` gets the A–D buttons. The picker exists for the tier that
cannot read prose options; these are not it.

## 2. Cost — measured, not guessed

From the geography 27b arm: **584 calls, 3.02M input tokens, 21k output** for 34
sessions. Math sessions run ~16 turns against geography's ~6, so both subjects
per model ≈ **11.1M in / 0.08M out**.

Output is genuinely tiny because tutor replies are short (median 352 chars).
Input dominates, and it is ~5.2k tokens per call because every call carries the
system prompt, question pool and recent turns.

| arm | both subjects |
|---|---|
| `claude-opus-4-7` **without** caching | ~$57 |
| `claude-opus-4-7` **with** caching | **~$20** |
| `gemini-3.5-flash` | ~$1.30 |
| `gpt-5.4-mini` | ~$1.70 |

Opus 4.7 is $5/M in, $25/M out; cache reads bill at ~0.1x, writes ~1.25x.

**Prompt caching is already implemented** and the prompt is deliberately
layered for it — `prompts.py` splits Block 1 (role/rules/tools, static per
conversation), Block 2 (step content, static per step) and Block 3 (KB chunks,
history, recent turns — never cached), with two breakpoints. So the ~$20 figure
is the expected one, not an aspiration.

**Verify it rather than assume it.** `LLMResponse` carries
`cache_read_tokens`; if that is zero across a run, a silent invalidator is at
work and the bill is the $57 column. Check after the first few scenarios, not
at the end.

Add Anthropic student-sim and Google grader spend on top — same for every arm,
and already being paid on the local boards.

## 3. Sequencing

1. math-27b finishes on the box
2. `box_fetch.sh` — **results live only on the rented host until this runs**
3. **destroy the instance** — cloud arms need no GPU, and the meter is
   ~$0.17/h
4. cloud arms locally via `run_cloud.sh` (API only)

## 4. The run

```bash
TUTOR_CALL_MODE=two EVAL_SKIP_RUBRIC=1 TUTORING_QUESTION_TYPES=mcq DEBUG=True \
CLOUD_MODELS_FILE="$PWD/offline_eval/cloud_models_3arm.txt" \
MODE="--multi-turn --subset hg1" \
RESULTS_DIR="$PWD/offline_eval/multi_turn_results/geo_cloud" \
  bash offline_eval/run_cloud.sh

# then the same with MODE="--multi-turn --subset math --sample 34 --seed 0"
# and RESULTS_DIR=.../math_cloud
```

Same flags as every post-fix local arm, so the boards are comparable:
`TUTOR_CALL_MODE=two`, `EVAL_SKIP_RUBRIC=1` (hand-graded, no LLM rubric),
`TUTORING_QUESTION_TYPES=mcq` (production's own default).

`run_cloud.sh` now checkpoints per scenario and writes a per-arm trace — it had
neither until 0accc21, and cloud arms are the most exposed to a mid-sweep kill
because a provider overload window outlasts any retry ladder.

## 5. Risks

**The OpenAI arm is first contact.** No OpenAI model has ever run through this
harness. Its profile was ported in 0accc21 — without an exact key,
`max_tokens` falls back to 1024 while GPT-5.x bills reasoning against the same
budget, so the model can spend the whole allowance thinking and return
`finish_reason=length` with no tool call. That reads as a near-zero tutoring
score for a purely harness reason. **Smoke one scenario per arm into a
throwaway results dir before the full sweep** — the mt100 runbook learned this
the expensive way.

**Rate limits and overload.** The cloud ladder is `[2,5,12,30,60]`; a longer
window will still exhaust it. Checkpoints mean a kill costs one scenario.

**Cache verification** — see §2. This is the difference between $20 and $57.

**MCQ depth on math** — lessons 1141 and 1138 hold 4 and 5 MCQs against the ~6
a session needs. Both local tiers hit their turn cap there. The cloud arms will
too; it is a content gap, not a model result, and should be discounted the same
way on every arm.

## 6. What this answers, and what it does not

It gives the local arms a ceiling on identical scenarios, with the same
instrument and the same tutor build.

It does **not** answer whether the tutor teaches well — `EVAL_SKIP_RUBRIC=1`
means `passed` is deterministic assertions only. The Grade tab is the
instrument, and these arms add 6 × 34 = 204 more sessions to hand-grade on top
of the local ones.

Worth deciding before running: whether all three arms are needed on **both**
subjects, or whether geography alone settles the comparison at a third of the
cost and a third of the annotation load.
