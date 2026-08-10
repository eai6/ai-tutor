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

## 1. Smoke pass — every arm, one scenario, throwaway results dir

    MODE="--multi-turn --subset v2 --sample 1" \
    RESULTS_DIR=/tmp/mt100_smoke \
    CLOUD_MODELS_FILE="$PWD/offline_eval/cloud_models_mt100.txt" \
    bash offline_eval/run_cloud.sh

`RESULTS_DIR=/tmp/mt100_smoke` is throwaway scratch — **do not point it at the
real results dir**, or a 1-scenario smoke run becomes a permanent 1/100 row.

Preflight (step 0) only exercises 4 of the 14 API-leg arms, and does so
outside the engine — it calls `generate_with_tools` directly, not
`manage.py run_eval`. This step runs all 14 API arms (the Qwen arms are
covered separately by Cell 8.5 of the Colab notebook) through the real engine
path, one scenario each, so it catches what preflight structurally cannot:
- a mistyped or retired model ID on any of the 10 arms preflight never touches
- auth/permission failures specific to a model (not just a provider)
- engine plumbing breaks (prompt assembly, tool schema, judge call) that only
  show up inside `run_eval`, not a bare `generate_with_tools` call
- gross truncation or empty-content failures (e.g. an OpenAI arm hitting its
  1024-token cap and returning `finish_reason=length` with no tool call —
  see the OpenAI ModelProfile gap below)

This matters because of how `run_cloud.sh`'s resume-skip works: it treats
"a JSON file already exists at this path" as "done," with no check on
*what's in it*. A model that fails mid-sweep still writes a run JSON — just
one with 0 (or near-0) of 100 scenarios complete — and that file then
permanently blocks a retry in step 2 unless you notice the 0%-ish row and
rerun with `FORCE=1`. Catching the failure here, against a throwaway dir,
costs one scenario per arm instead of a silently-stuck row discovered only
when the board is aggregated.

## 2. API leg — 14 arms, locally

    RESULTS_DIR="$PWD/offline_eval/multi_turn_results/mt100" \
    CLOUD_MODELS_FILE="$PWD/offline_eval/cloud_models_mt100.txt" \
    MODE="--multi-turn --subset v2" \
    bash offline_eval/run_cloud.sh

Resume-safe per model and guarded by a sweep lock: a second concurrent sweep
against the same results dir refuses to start. Interrupt and re-run to resume.

## 3. OSS leg — 5 Qwen arms, in Colab

**Push the branch first.** The notebook clones `--depth 1 -b
offline-harness-copy` straight from GitHub — it does not see local commits.
All the mt100 profiles, Modelfiles, and `v2` scenario tags are local-only
until you push:

    git push origin offline-harness-copy

Running the Colab leg before pushing clones a tree with none of that, and
every arm fails from the first `ollama create` (missing Modelfile) onward.

    ./venv/bin/python offline_eval/_make_colab_nb_mt100.py

Upload `offline_eval/colab_mt100_qwen.ipynb` to Colab and run it. It builds all
five tags from their Modelfiles and writes to
`MyDrive/ai-tutor-eval-multiturn/mt100/`. Download that folder into the local
results dir when it finishes.

**Check `identity.log` after the first Qwen arm completes.** Cell 9 runs
`run_matrix.sh`, which probes each arm's model identity (thinks vs. answers
directly) and appends the result to `identity.log` in the results dir before
it scores a single scenario. Read that line as soon as the first arm's build
finishes — don't wait for all five. If any arm reports `THINKS`, **stop the
run**: thinking was supposed to be suppressed (`ollama_think=False`) for the
hybrid-template arms (2b/8b/27b), and a `THINKS` result means the identity
probe is advisory only — `run_matrix.sh` logs it but does not abort — so a
misconfigured arm will otherwise burn its full 100-scenario budget confounded
before anyone notices.

**The 30B arm may not fit a T4.** `qwen3-30b-a3b-jetson`'s base
(`qwen3:30b-a3b-instruct-2507-q4`) is roughly 18 GB, and its profile sets
`num_gpu=99` (force full GPU offload) — do not change that. A Colab T4 has
16 GB VRAM, so a T4 runtime is likely to OOM on this arm specifically. It is
also last in `models.txt`, so on a T4 it fails only after the other four arms
have already consumed most of the session. Two ways to make that cheap:
either request an **A100 or L4** runtime (Runtime → Change runtime type) for
this notebook, which fits the model comfortably, or reorder `models.txt` so
`qwen3-30b-a3b-jetson` runs **first**, so an OOM is discovered in minutes
rather than at the end of a multi-hour session.

## 4. Board

    RESULTS_DIR="$PWD/offline_eval/multi_turn_results/mt100" \
    ./venv/bin/python offline_eval/aggregate.py

`offline_eval/aggregate.py` has no argparse and ignores CLI flags entirely —
it only reads the `RESULTS_DIR` env var (falling back to
`offline_eval/single_turn_results/results`, the SINGLE-turn board, if unset).
A `--results ...` flag is silently swallowed: the script would either print
"no results" or, worse, board stale single-turn data as if it were the mt100
multi-turn results — and that failure surfaces only after both legs have
already burned their paid API hours. Use the `RESULTS_DIR=...` env-var form
above, matching the pattern Leg 1 already uses.

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
- Sampling overrides would be moot even if a profile existed:
  `_build_completion_kwargs` discards sampling parameters for new-gen models.
- **`max_tokens` is 1024, not 8192/2048/1024 like everyone else.**
  `engine.py:2153` — `max_tokens = profile.max_tokens if profile else 1024` —
  falls back to 1024 whenever `profile` is `None`, which is every OpenAI arm.
  For comparison: Gemini arms run at 8192, Anthropic at 2048, Qwen at 1024 (a
  deliberate choice for the Qwen arms; accidental for OpenAI). `gpt-5.4-mini`
  and `gpt-5.4-nano` send no `reasoning_effort` at all (only the gpt-5.6-*
  arms do, and only to satisfy the tool-calling restriction), and reasoning
  tokens are billed against that same 1024-token budget — so mini/nano can
  spend the whole budget thinking and come back with `finish_reason=length`,
  empty content, and no tool call, which reads as a harness failure rather
  than a model failure unless you know to look for it.
- **They skip the family-gated eval-repair paths.** `_should_force_pose` and
  `_should_force_grade` (`engine.py`) only engage `if not family or family in
  _FORCE_POSE_EXEMPT_FAMILIES: return False` — with `family=None`, OpenAI
  arms hit that same early return and never get Call 1 forced onto
  `pose_question`/`record_answer` the way Gemini and Qwen arms do (the fix
  built for Gemini's "question narrated as prose, nothing to grade" failure
  mode and extended to every non-Anthropic family). They also render Block 0
  from the unmodified base XML template — `build_family_block_0` gives Qwen a
  Markdown variant and Gemini a targeted XML variant, but `family=None` falls
  into the same "everyone else, incl. Anthropic/production" branch as
  Anthropic itself, so OpenAI arms are prompted with an Anthropic-shaped
  system block, not a family-matched one.
- **No OpenAI arm has ever been run through this harness before.** Every
  other vendor on this board (Anthropic, Gemini, Qwen) has prior eval runs to
  sanity-check against; the five OpenAI rows are first-contact — treat early
  anomalies as "could be the harness" until a second run confirms them.

Taken together, **an OpenAI-vs-Gemini comparison on this board is not valid
until OpenAI `ModelProfile` entries exist** — the OpenAI rows differ from
every other row on call mode, `max_tokens` budget, prompt family, and
eval-repair coverage, all for the same underlying reason (no profile), not
because of anything about the models being compared.

Adding OpenAI `ModelProfile` entries would flip these five arms to one-call
and start applying a `family_prompt_delta` — i.e. it would change what the
board measures, not just clean up a gap. Treat it as a decision that needs to
be made deliberately, not a pre-sweep chore. **Do not add these profile
entries or pin `TUTOR_CALL_MODE` as part of running this board** — both are
pending human decisions, not implementation gaps to close along the way.

### gpt-5.6-luna: one dropped tool call observed in development

During implementation, `gpt-5.6-luna` returned no tool call (`finish=stop`)
on 1 of 7 live preflight-style attempts; a follow-up burst was 6/6. This
reads as sampling variance, not a systematic tool-adherence failure, but it
is worth watching specifically in luna's row if its scores look like an
outlier — a dropped tool call mid-sweep would show up as a missing or
malformed turn rather than a low rubric score.
