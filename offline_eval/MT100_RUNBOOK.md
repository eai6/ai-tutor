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

    TUTOR_CALL_MODE=two \
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

    TUTOR_CALL_MODE=two \
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

**Resolved 2026-08-10: `TUTOR_CALL_MODE=two` is pinned board-wide.** Both
legs set it — the `run_cloud.sh` invocations in steps 1 and 2 above, and Cell 9
of the Colab notebook. `_call_mode()`
(`apps/tutoring/simple_tutor/engine.py:2417`) returns the pinned value
verbatim before it ever consults `family`, so all 19 arms run the same
protocol and the per-family `'auto'` resolution below is bypassed entirely.

Two-call means Call 1 picks tools, the platform grades, and Call 2 writes the
reply *knowing the verdict*. One-call means the model writes its reply in
Call 1, before grading, and `_align_reply_polarity` cleans up contradictions
afterward. Those are different task shapes, which is why mixing them across
rows would not have been defensible.

Why `two` and not `one`: it is the configuration production actually ships for
Anthropic, so the Anthropic rows stay production-representative, and it matches
the `TUTOR_CALL_MODE=two` pin the mt30/mt50 OSS boards used — so the 30 `v1`
scenarios inside this 100 remain comparable to those earlier boards on protocol
as well as on scenario set. That continuity is the whole reason the selection
algorithm forces all 30 `v1` scenarios into the draw.

The cost is real and worth planning for: the nine arms that `'auto'` would have
run one-call (4 Google + 5 Qwen) now make two LLM calls per turn, so budget
roughly double the per-turn latency and token spend on those rows — including
`gemini-3.1-pro-preview`, the most expensive arm on the board.

Had it been left unpinned, `'auto'` would have resolved to `'two'` for the 5
Anthropic arms (via `_FORCE_POSE_EXEMPT_FAMILIES == frozenset({'anthropic'})`)
and the 5 OpenAI arms (via the `not family` fallback, before they had
profiles), and `'one'` for the 4 Google and 5 Qwen arms — a 10/9 split with no
principle behind it.

### The five OpenAI arms now have ModelProfile entries

**Resolved 2026-08-10.** `apps/llm/model_profiles.py` previously had no exact
key and no `FAMILY_PATTERNS` regex matching `gpt`/`openai`, so
`get_model_profile()` returned `None` for all five OpenAI specs and `family`
was `None`. Exact entries now exist for `openai/gpt-5.6-sol`, `-terra`,
`-luna`, `openai/gpt-5.4-mini` and `-nano`, all `family="openai"`,
`max_tokens=8192`. What that fixed:

- **`max_tokens` was 1024, against 8192 for Gemini and 2048 for Anthropic.**
  `engine.py:2153` — `max_tokens = profile.max_tokens if profile else 1024` —
  fell back to 1024 whenever `profile` was `None`. `gpt-5.4-mini` and
  `gpt-5.4-nano` send no `reasoning_effort` (only the gpt-5.6-* arms do, and
  only to satisfy the tool-calling restriction), and reasoning tokens bill
  against that same budget, so mini/nano could spend the whole 1024 thinking
  and return `finish_reason=length` with empty content and no tool call —
  reading as a near-zero tutoring score for a purely harness reason. This was
  the most likely way the OpenAI rows would have come back uninterpretable.
- **They skipped the family-gated eval-repair paths.** `_should_force_pose`,
  `_should_force_grade`, polarity alignment, auto-grade fallback, stuck-slot
  pivot and ensure-posed-question all early-return on
  `if not family or family in _FORCE_POSE_EXEMPT_FAMILIES` — which `family=None`
  satisfied. The OpenAI arms therefore ran an unscaffolded configuration while
  Gemini and Qwen arms received all six repairs. With `family="openai"` they
  now get the same treatment as every other non-Anthropic family.

Two things deliberately did NOT change:

- **No sampling is set on these profiles.** `_build_completion_kwargs` routes
  every `gpt-5*` name through its new-generation branch, which sends only
  `max_completion_tokens` and drops temperature/top_p/top_k. The API enforces
  it too — gpt-5.6-* reject a custom temperature outright. Sampling here would
  be inert and would misrepresent how these arms run.
- **The prompt block is unchanged.** `build_family_block_0` branches only for
  `qwen` (Markdown), `gemini`/`gemma` and `kimi` (targeted XML appendices);
  every other family, including `openai` and the previous `None`, gets the base
  XML template. So adding the family did not alter what these models are
  prompted with — only their budget and their repair coverage.

Call mode is no longer affected either way: `TUTOR_CALL_MODE=two` is pinned
board-wide, so the `family`-driven `'auto'` resolution never runs.

**Still true: no OpenAI arm has ever been run through this harness before.**
Every other vendor here (Anthropic, Gemini, Qwen) has prior eval runs to
sanity-check against; the five OpenAI rows are first-contact. Treat early
anomalies as "could be the harness" until a second run confirms them, and lean
on the step-1 smoke pass rather than assuming a clean first sweep.

### gpt-5.6-luna: one dropped tool call observed in development

During implementation, `gpt-5.6-luna` returned no tool call (`finish=stop`)
on 1 of 7 live preflight-style attempts; a follow-up burst was 6/6. This
reads as sampling variance, not a systematic tool-adherence failure, but it
is worth watching specifically in luna's row if its scores look like an
outlier — a dropped tool call mid-sweep would show up as a missing or
malformed turn rather than a low rubric score.
