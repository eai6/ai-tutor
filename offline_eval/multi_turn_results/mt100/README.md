# mt100 — 18-arm multi-turn board

Run 2026-08-10 → 2026-08-12. ~47 h of API time, 1,800 judged sessions.
Method and commands: `offline_eval/MT100_RUNBOOK.md`.
Design: `docs/superpowers/specs/2026-08-10-mt100-multi-model-board-design.md`.

Reproduce the board:

    RESULTS_DIR="$PWD/offline_eval/multi_turn_results/mt100" \
    ./venv/bin/python offline_eval/aggregate.py

## Board

| # | model | pass | rubric | err | wall-clock |
|---|---|---|---|---|---|
| 1 | claude-opus-4-7 | 94% | 0.91 | 0 | 2.16 h |
| 1 | qwen3.6-27b-instruct | 94% | 0.86 | 0 | — |
| 3 | claude-sonnet-4-6 | 93% | 0.90 | 1 | 2.37 h |
| 3 | gemini-3.5-flash | 93% | 0.90 | 0 | 4.53 h |
| 5 | gemini-2.5-flash | 92% | 0.88 | 0 | 2.95 h |
| 5 | gemini-3.1-pro | 92% | 0.87 | 0 | 16.44 h |
| 5 | gpt-5.4-mini | 92% | 0.88 | 0 | 1.73 h |
| 5 | gpt-5.6-luna | 92% | 0.89 | 0 | 1.85 h |
| 5 | gpt-5.6-sol | 92% | 0.90 | 0 | 1.95 h |
| 10 | claude-opus-5 | 91% | 0.94 | 0 | 3.03 h |
| 11 | claude-haiku-4-5 | 89% | 0.88 | 0 | 1.89 h |
| 11 | claude-sonnet-5 | 89% | 0.91 | 0 | 3.01 h |
| 11 | gpt-5.6-terra | 89% | 0.91 | 0 | 1.88 h |
| 14 | gpt-5.4-nano | 88% | 0.86 | 0 | 2.31 h |
| 15 | qwen3-8b-jetson | 74% | 0.78 | 0 | — |
| 16 | qwen3-4b-jetson | 65% | 0.70 | 0 | — |
| 17 | qwen3-30b-a3b-jetson | 61% | 0.70 | 0 | — |
| 18 | qwen3.5-2b-jetson | 19% | 0.49 | 0 | — |

Qwen wall-clock is not recorded: those arms ran in three concurrent Colab tabs
and the result JSONs carry no duration field, so elapsed time would measure
session scheduling rather than model speed. The per-arm elapsed is in each
Colab tab's cell output if it is ever needed.

## Run configuration

- **Scenarios**: the 100 `v2`-tagged multi-turn scenarios — a stratified draw
  from the 200, containing all 30 `v1` scenarios so the older mt30 boards
  remain readable as a sub-board. See `evals/select_representative.py`.
- **Call mode**: `TUTOR_CALL_MODE=two`, pinned on both legs. Unpinned, `auto`
  resolves per-family and would have split the board 10/9.
- **Rubric judge**: `claude-sonnet-4-6` (constant, all arms).
- **Student-sim**: `claude-haiku-4-5` (constant, all arms).
- **In-session free-text grader**: `claude-sonnet-4-6` on the cloud leg, via an
  active `purpose='judge'` ModelConfig. See the caveat below.
- **Cloud leg**: `offline_eval/cloud_models_mt100.txt` + `run_cloud.sh`.
- **OSS leg**: `colab_mt100_qwen.ipynb`, three concurrent tabs, all five arms
  Modelfile-pinned. `identity.log` records the instruct-mode probe: every arm
  reported `thinking_chars=0` → answers directly.

## Caveats — read before comparing rows

1. **The three gpt-5.6 arms ran with reasoning DISABLED.** They reject function
   tools on `/v1/chat/completions` unless `reasoning_effort='none'`, and the
   engine is tool-driven. A fair gpt-5.6 number needs the Responses API, which
   this client does not speak. Do not read 89–92% as their reasoning-on ability.
2. **The Qwen ladder mixes generations** — 2b is Qwen3.5, 4b/8b are Qwen3,
   27b is Qwen3.6. It is a size-and-generation comparison, not a scaling curve.
3. **gemini-2.5-pro is absent by choice**, stopped at 2/100. Its absence is not
   a failure. Re-run with `FORCE=1` if wanted.
4. **The 30B arm is not Jetson-viable** — ~18.6 GB at q4. Capacity ceiling, not
   a deployment candidate.
5. **Any pre-mt100 qwen3:8b number is mislabelled**: that profile declared
   instruct while running thinking. This board is the first correct 8B measure.
6. **One errored scenario** across 1,800: `claude-sonnet-4-6` on
   `error_cascade_struggler_1142_09`, an Anthropic-side HTTP 500. Transient,
   not a harness fault. That arm is effectively 93/99.

## Two results worth a second look

- **qwen3.6-27b-instruct ties the best cloud arm** (94%) on a lower rubric
  (0.86 vs 0.91) — clears the bar as often, less polished per turn.
- **The 30B MoE lands below the dense 4B and 8B** (61% vs 65%/74%) despite a
  verified-correct profile and instruct mode. A3B means ~3B active parameters,
  which explains why it is not 30B-class, but not why it trails a dense 4B.
- **claude-opus-5 has the board's best rubric (0.94) but ranks 10th on pass
  rate** — the two metrics disagree, which usually means it teaches well but
  trips assertions. Worth reading its 9 failures before concluding it is worse
  than opus-4-7.

## The judge misconfiguration, and why arm 1 was re-run

The first attempt at this sweep graded free-text answers with a LOCAL
`qwen3-4b-jetson`, not a cloud judge. There were no `purpose='judge'`
ModelConfig rows, so `get_judge_provider_chain` fell through to `tutoring` and
picked up the desktop-seeded local Ollama row. It was doing real work — 15
`POST localhost:11434/v1/chat/completions` calls in the first 24 scenarios.

Fixed by seeding an active `purpose='judge'` row (`claude-sonnet-4-6`), which
puts Sonnet first and demotes the local model to a last-resort tail. The whole
sweep was restarted so every arm shares one judge.

`../mt100_localjudge_archive/` keeps the discarded `claude-opus-5` arm from
that first attempt. It is a direct control on the same 100 scenarios:

| judge | claude-opus-5 |
|---|---|
| local qwen3-4b | 95/100 |
| claude-sonnet-4-6 | 91/100 |

The 4B grader was ~4 points more lenient. Had the run continued, every row
would have carried that inflation invisibly.

**Note on the log line**: `grader.py::_local_verifier_chain` emits
`"no cloud judge available"` whenever it builds the last-resort tail, even when
a cloud judge then does the grading. It appears ~40×/arm in these logs and is
NOT evidence of local grading — count `POST localhost:11434` instead, which is
0 for every cloud arm here. The wording is worth fixing.
