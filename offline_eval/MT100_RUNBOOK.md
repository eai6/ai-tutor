# mt100 runbook

19 arms x 100 multi-turn scenarios (`--subset v2`). Both legs write into
`offline_eval/multi_turn_results/mt100/`, one JSON per model.

## 0. Preflight (always)

    ./venv/bin/python offline_eval/preflight_mt100.py

Prints `PREFLIGHT OK` and exits `0` on success; on failure it prints
`PREFLIGHT FAILED` plus one `FAIL ...` line per broken arm and exits `1` — it
is a real gate, safe to use as a CI/script precondition (`&& bash
offline_eval/run_cloud.sh`, or check `$?`). It fires one tool-driven turn per
vendor (Anthropic, two OpenAI models, Google) and catches per-vendor tool-call
refusals before the sweep spends hours. It is not a substitute for the
call-mode and profile caveats below — those apply even when preflight is
clean.

## 1. API leg — 14 arms, locally

    RESULTS_DIR="$PWD/offline_eval/multi_turn_results/mt100" \
    CLOUD_MODELS_FILE="$PWD/offline_eval/cloud_models_mt100.txt" \
    MODE="--multi-turn --subset v2" \
    bash offline_eval/run_cloud.sh

Resume-safe per model and guarded by a sweep lock: a second concurrent sweep
against the same results dir refuses to start. Interrupt and re-run to resume.

## 2. OSS leg — 5 Qwen arms, in Colab

    ./venv/bin/python offline_eval/_make_colab_nb_mt100.py

Upload `offline_eval/colab_mt100_qwen.ipynb` to Colab and run it. It builds all
five tags from their Modelfiles and writes to
`MyDrive/ai-tutor-eval-multiturn/mt100/`. Download that folder into the local
results dir when it finishes.

## 3. Board

    ./venv/bin/python offline_eval/aggregate.py \
      --results offline_eval/multi_turn_results/mt100

## Reading the board

- **gpt-5.6-sol / terra / luna ran with reasoning DISABLED.** Chat Completions
  refuses function tools otherwise. They may score below gpt-5.4-mini; that is
  the setting, not necessarily the model. A fair number needs the Responses API.
- **The Qwen size ladder mixes generations** (3, 3.5, 3.6), so it is a
  size-and-generation confound, not a scaling curve.
- **The 30B arm is not Jetson-viable** — it is a capacity ceiling, not a
  deployment candidate.
- **Any pre-mt100 qwen3:8b number is mislabelled**: that profile claimed
  instruct while running thinking. mt100 is the first correct 8B measurement.

### Engine call mode is not uniform across the board

`TUTOR_CALL_MODE` is unset everywhere in this runbook, so every arm falls
back to its default, `'auto'`. `_call_mode()`
(`apps/tutoring/simple_tutor/engine.py:2417`) resolves `'auto'` to `'two'`
when `not family` **or** when `family` is in `_FORCE_POSE_EXEMPT_FAMILIES`
(`== frozenset({'anthropic'})`), and to `'one'` otherwise.

Measured across the 19 mt100 arms, that split is:

| Call mode | Count | Arms |
|---|---|---|
| **two-call** | 10 | the 5 Anthropic arms (deliberate exemption) + the 5 OpenAI arms (**accidental** — see below) |
| **one-call** | 9 | the 4 Google arms + the 5 local Qwen arms |

Two-call means Call 1 picks tools, the platform grades, and Call 2 writes the
reply *knowing the verdict*. One-call means the model writes its reply in
Call 1, before grading, and `_align_reply_polarity` cleans up contradictions
afterward. **These are two different protocols, and rows are not directly
comparable on this axis** — a two-call Anthropic row and a one-call Gemini row
are not measuring the same task shape, independent of anything about the
models themselves.

Pinning `TUTOR_CALL_MODE=one` or `TUTOR_CALL_MODE=two` board-wide is an
available, unexercised option that would remove this confound — neither
`run_cloud.sh` nor the Colab notebook sets it. Nobody has decided whether to
pin it; that decision was escalated during implementation and intentionally
left for whoever runs the sweep. If board results need to be defensible
cross-row, decide this before running Leg 1.

### The five OpenAI arms have no ModelProfile entry at all

`apps/llm/model_profiles.py` has no exact key and no `FAMILY_PATTERNS` regex
matching `gpt`/`openai`, so `get_model_profile()` returns `None` for all five
OpenAI specs in `cloud_models_mt100.txt` (`gpt-5.6-sol`, `gpt-5.6-terra`,
`gpt-5.6-luna`, `gpt-5.4-mini`, `gpt-5.4-nano`), and `family` is `None` for
all five. Consequences, stated plainly:

- They land in two-call mode via the `not family` fallback in `_call_mode()`
  above — **by accident**, not by the same deliberate choice that exempts
  Anthropic.
- No `family_prompt_delta` is applied to them, unlike every Claude/Gemini/Qwen
  arm on the board.
- Sampling overrides would be moot even if a profile existed:
  `_build_completion_kwargs` discards sampling parameters for new-gen models.

Adding OpenAI `ModelProfile` entries would flip these five arms to one-call
and start applying a `family_prompt_delta` — i.e. it would change what the
board measures, not just clean up a gap. Treat it as a decision that needs to
be made deliberately, not a pre-sweep chore.

### gpt-5.6-luna: one dropped tool call observed in development

During implementation, `gpt-5.6-luna` returned no tool call (`finish=stop`)
on 1 of 7 live preflight-style attempts; a follow-up burst was 6/6. This
reads as sampling variance, not a systematic tool-adherence failure, but it
is worth watching specifically in luna's row if its scores look like an
outlier — a dropped tool call mid-sweep would show up as a missing or
malformed turn rather than a low rubric score.
