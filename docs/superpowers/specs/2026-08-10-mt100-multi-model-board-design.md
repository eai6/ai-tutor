# mt100 — 19-arm multi-turn board on 100 representative scenarios

**Date:** 2026-08-10
**Branch:** `offline-harness-copy`
**Status:** design approved, pending spec review

## Goal

Score 19 tutor models — 14 API, 5 local Qwen — on 100 multi-turn scenarios drawn
to be representative of the full 200. API arms run locally via `run_cloud.sh`;
the Qwen arms run in Colab against a Drive-backed results directory.

## 1. Scenario selection

### The set

`evals/dataset/multi_turn/` holds 200 scenarios. 100 is exactly half, so every
stratum can be halved without rounding pressure. The population splits on four
axes worth preserving:

| Axis | Levels | Population |
|---|---|---|
| `persona` | 6 | capable 34, average 34, struggler/probe_resistant/non_responder/error_prone 33 each |
| `subject` | 2 | math 104, geography 96 |
| `kind` | 3 | edge_case 49, baseline 32 (14 also edge_case → 18 pure), other 133 |
| `lesson_id` | 16 | — |

`kind` is derived, not a field: `edge_case` if the scenario carries that tag,
else `baseline` if it carries that one, else `other`. The two tags overlap on 14
scenarios; edge_case wins so the buckets stay disjoint.

### The algorithm

Force all 30 `v1`-tagged scenarios in, then fill the remaining 70 with a greedy
pass that repeatedly adds whichever candidate most reduces total marginal
deviation from the population's proportions across all four axes.

Forcing `v1` in is what keeps the existing mt30 boards readable: the old numbers
stay valid as a sub-board of the new one rather than becoming an orphaned
measurement. It costs a little representativeness, because `v1` is
baseline-heavy (9 baseline vs 4 edge_case), and the greedy fill cannot fully
undo that — see the residual below.

A pure proportional per-cell halving was tried first and rejected: six
persona×subject×kind cells contain more `v1` scenarios than their proportional
target, so it overshoots to 106 selected and lands `baseline` at 14 against an
ideal of 9. The greedy marginal fit hits 100 exactly and gets `baseline` to 10.

Seed: `20260810`, fixed in the script. The draw is deterministic and
re-runnable.

### Validated output

| Axis | Selected | Ideal |
|---|---|---|
| persona | 17/17/16/16/17/17 | 17/17/16.5/16.5/16.5/16.5 |
| subject | geography 48, math 52 | 48 / 52 |
| kind | baseline 10, edge_case 23, other 67 | 9 / 24.5 / 66.5 |
| lessons | 16 of 16 | — |
| v1 retained | 30 of 30 | — |

Total marginal deviation 19.0 summed across all four axes (lesson_id, with 16
levels, contributes most of it).

### How the set is addressed

Add the tag `v2` to the 100 selected YAML files. `run_eval.py:99` already
filters with `subset in s.tags`, so `--subset v2` selects them with no harness
change. This mirrors how `v1` works today rather than introducing a manifest
file and a new flag.

Deliverable: `evals/select_representative.py` — computes the draw, writes the
tag, and prints the distribution table above. Idempotent; re-running does not
duplicate tags.

## 2. Model roster — 19 arms

### Anthropic (5)

```
anthropic/claude-opus-5                    claude-opus-5
anthropic/claude-sonnet-5                  claude-sonnet-5
anthropic/claude-haiku-4-5-20251001        claude-haiku-4-5
anthropic/claude-opus-4-7                  claude-opus-4-7
anthropic/claude-sonnet-4-6                claude-sonnet-4-6
```

The 4.x pair carries continuity with `cloud_models_mt50.txt`; the 5-family gives
current frontier numbers.

### OpenAI (5)

```
openai/gpt-5.6-sol      openai/gpt-5.6-terra      openai/gpt-5.6-luna
openai/gpt-5.4-mini     openai/gpt-5.4-nano
```

All five verified present on the account's `models.list()`.

**GPT-5.6 runs with reasoning disabled.** The three 5.6 models reject function
tools on `/v1/chat/completions`:

> `Function tools with reasoning_effort are not supported for gpt-5.6-sol in
> /v1/chat/completions. To use function tools, use /v1/responses or set
> reasoning_effort to 'none'.`

The engine is entirely tool-driven, so this is a hard block. Two escapes were
verified working: `reasoning_effort='none'` on Chat Completions, and the
Responses API with reasoning intact. **Decision: `reasoning_effort='none'`.**

Read the resulting GPT-5.6 rows accordingly — they measure those models with
reasoning off, which is not how they would be deployed, and they may well score
below `gpt-5.4-mini`. A Responses API path remains the way to get a fair GPT-5.6
number if these rows look anomalous.

No `OpenAIClient` change is needed for token/temperature handling:
`_NEW_GEN_PREFIXES = ("gpt-5", ...)` already matches every `gpt-5.*` name and
routes to `max_completion_tokens` with no `temperature`, which is exactly what
the API demands. Probed: all five reject legacy `max_tokens`; the three 5.6
models also reject `temperature`; `5.4-mini`/`nano` accept it but the new-gen
branch drops sampling anyway.

The only client change is passing `reasoning_effort='none'` for `gpt-5.6*`.

### Google (4)

```
google/gemini-3.5-flash        google/gemini-3.1-pro-preview
google/gemini-2.5-flash        google/gemini-2.5-pro
```

`gemini-3.1-pro-preview` is the full ID — `gemini-3.1-pro` alone is not served.
All four already have or closely match existing `ModelProfile` entries.

### Qwen, local (5)

| Size | Tag | Status |
|---|---|---|
| 2b | `local_ollama/qwen3.5-2b-jetson` | Modelfile + profile exist |
| 4b | `local_ollama/qwen3-4b-jetson` | Modelfile + profile exist |
| 8b | `local_ollama/qwen3-8b-jetson` | **new** Modelfile, FROM `qwen3:8b` |
| 27b | `local_ollama/qwen3.6-27b-instruct` | Modelfile + profile exist |
| 30b | `local_ollama/qwen3-30b-a3b-jetson` | **new** Modelfile, FROM `qwen3:30b-a3b` |

Two corrections to the original request. `qwen3.6:30b-a3b` does not exist — the
current-generation MoE is `qwen3.6:35b-a3b`, and the genuine 30B is
`qwen3:30b-a3b` (Qwen3-30B-A3B), verified on the Ollama tags page. And 8b/30b
had no pinned tag, so they would have run bare; bare tags spawn a second
4096-ctx runner via the grader verifier's `/v1` endpoint and evict the tutor.
Both get a Modelfile pinning `num_ctx`, matching the existing jetson tags.

Mixed generations across sizes (3, 3.5, 3.6) mean the size ladder is a
size-and-generation confound, not a clean scaling curve. Stated here so the
board is not over-read.

## 3. Execution

### API arms — local

`offline_eval/cloud_models_mt100.txt` in the existing three-column format
(`spec  safe_name  region`), driven by:

```bash
RESULTS_DIR="$PWD/offline_eval/multi_turn_results/mt100" \
CLOUD_MODELS_FILE="$PWD/offline_eval/cloud_models_mt100.txt" \
MODE="--multi-turn --subset v2" \
bash offline_eval/run_cloud.sh
```

`run_cloud.sh` already provides per-model resume, a sweep lock, and
`EVAL_CHECKPOINT_DIR` pointed at the results dir so a killed run resumes from
its last scenario.

### Qwen arms — Colab

`offline_eval/_make_colab_nb_mt100.py`, mirroring `_make_colab_nb_qwen_mt30.py`:
builds the five Modelfile-pinned tags, symlinks
`offline_eval/multi_turn_results/mt100` to
`MyDrive/ai-tutor-eval-multiturn/mt100`, and runs the same `--subset v2` mode so
both legs write into one directory and `aggregate.py` boards them together.
Pins `OLLAMA_VERSION=0.30.7` per the known tool-parsing sensitivity.

Judge (Sonnet 4.6) and student-sim (Haiku) stay constant across all 19 arms.

### Cost

19 arms × 100 multi-turn sessions ≈ 1,900 judged sessions, each up to 15–30
turns with a judge and student-sim call per turn. Extrapolating from the mt50
notes (~1.5–2.5 h fast instruct, ~4–6 h Gemini Pro at *50* scenarios), the 14
API arms are plausibly 60–100+ hours sequential plus substantial token spend.
Accepted: build all, run all. Every arm is independently resumable, so the sweep
survives interruption and can be stopped between models without losing work.

## 4. Testing

- `evals/select_representative.py` — unit test asserting the draw is
  deterministic under the fixed seed, totals exactly 100, retains all 30 `v1`,
  and holds every axis within 2 of its proportional ideal.
- Tag write is verified idempotent: a second run leaves the YAML byte-identical.
- One smoke session per provider (`--subset v2 --sample 1`) before the sweep,
  to confirm each of the four provider paths completes a tool-driven turn. This
  catches the GPT-5.6 tool problem class before 60 hours of API time.
- New Modelfiles verified with `ollama show --modelfile` against the built tag,
  confirming `num_ctx` is baked, as was done for `qwen3-4b-thinking-jetson`.

## 5. Out of scope

- Responses API support in `OpenAIClient`.
- Retiring or renumbering the `v1` subset — it stays as-is.
- Any change to scenario content, rubrics, or `pass_threshold`.
- Judge or student-sim model changes.
