# qwen3.8-27B — math30 arm

**Question this answers:** is Qwen3.8-27B a better local tutor than Qwen3.6-27B,
which tied the best cloud model on the mt100 board (94% pass, rubric 0.86)?

**Status:** wired and tested, not yet run. Branch `offline-harness-copy`.

---

## 1. The measurement

**30 scenarios, `--subset v2,math --sample 30 --seed 0`.**

Baseline to beat — **qwen3.6-27b-instruct: 25/30** on the identical draw. Every
arm on the mt100 board has full coverage of these 30, so the whole board is a
free reference:

| arm | on these 30 |
|---|---|
| claude-sonnet-4-6 | 28/30 |
| claude-opus-4-7 | 26/30 |
| **qwen3.6-27b-instruct** | **25/30** |
| claude-haiku-4-5 | 24/30 |
| qwen3-8b-jetson | 19/30 |
| qwen3.5-2b-jetson | 1/30 |

### Why math and not geography

Geography is saturated: 13 of 18 arms score 100%, every cloud arm ≥98%, and
qwen3.6 went **30/30** on the geography draw. That slice cannot distinguish 3.8
from 3.6, from haiku, or from gpt-5.4-nano. Math spreads 1–28 of 30 with no
ceiling.

### What n=30 can and cannot resolve

Both arms run the identical 30 scenarios, so this is a paired comparison — more
sensitive than two independent samples. It still cannot resolve a 1–2 scenario
difference. **Read 23–27 as "same class as 3.6".** A result outside that band is
the interesting outcome, in either direction.

---

## 2. Branch: stay on `offline-harness-copy`

`aws_deployment` **cannot run this** without a substantial port. Verified:

| needed | `offline-harness-copy` | `aws_deployment` |
|---|---|---|
| `v2` scenario tags | 100 files | **0 files** |
| `evals/select_representative.py` | present | **absent** |
| `Modelfile.qwen3.6-27b-instruct` | present | **absent** |
| `local_ollama/qwen3.6-27b-instruct` profile | present | **absent** |
| tree layout | `apps/…` | `ai_tutor/apps/…` |

The blocker is the `v2` tags: without them `--subset v2,math` selects nothing.
Regenerating them via `select_representative.py` is not equivalent — unless the
draw reproduces the same 100 scenarios byte-for-byte, `--sample 30 --seed 0`
lands on a different 30 and the 25/30 baseline no longer applies.

The branches are 120/30 commits apart from merge-base `bffb760`, so this is a
real port, not a cherry-pick. **Run the eval where it already works; port
afterwards only if the result justifies it.** See the appendix.

---

## 3. What is already in place

Uncommitted on `offline-harness-copy`:

| file | change |
|---|---|
| `infra/ollama/Modelfile.qwen3.8-27b-instruct` | new — `FROM qwen3.8:27b`, num_ctx 32768, card's non-thinking sampling (0.7 / 0.80 / 20, presence_penalty 1.5) |
| `apps/llm/model_profiles.py` | new exact key `local_ollama/qwen3.8-27b-instruct`, knob-for-knob twin of the 3.6 entry |
| `apps/tutoring/management/commands/run_eval.py` | `--subset` accepts comma-separated tags, ANDed; extracted as `filter_by_subset` |
| `evals/test_subset_filter.py` | new — 9 tests on the AND semantics and the real slice sizes |
| `offline_eval/run_matrix.sh` | identity probe follows `OLLAMA_API_BASE`/`OLLAMA_HOST` instead of hardcoded localhost |
| `apps/llm/models.py` | synthesized ollama fallback honours `OLLAMA_API_BASE` |
| `offline_eval/seed_ollama_configs.py` | writes `OLLAMA_API_BASE` onto the seeded row |
| `offline_eval/models_qwen38.txt` | new — single-arm model list |
| `offline_eval/MT100_RUNBOOK.md`, `_make_colab_nb_mt100.py` + tests | updated for a 20-arm / 6-Qwen board |

Verified: profile resolves to the exact key (not the `r"qwen3"` cloud fallback
with `num_ctx=None`); `FROM qwen3.8:27b` returns 200 from the Ollama registry;
101 Django tests and 58 eval tests pass.

**Already done:** the active `purpose='judge'` row (`google/gemini-2.5-flash`)
is deactivated. Re-activate it after the run — it is the normal dev config.

---

## 4. Run it

### 4.1 Rent the box

Vast.ai, **offer `38607381`** — RTX 3090 24 GB, $0.122/hr, 167 GB disk,
rel 0.998, Quebec. Balance is $10.

```bash
pip install vastai
vastai set api-key "$VAST_API_KEY"
vastai create instance 38607381 --image ollama/ollama --disk 60 --ssh
vastai show instances          # ssh host + port
```

Offer IDs go stale in minutes. If gone:

```bash
vastai search offers 'gpu_name=RTX_3090 disk_space>=60 rentable=true reliability>0.98' -o dph
```

**Why a 3090 and not the $0.029 V100:** the V100 is Volta, so flash attention is
unavailable and `OLLAMA_KV_CACHE_TYPE=q8_0` silently does not apply — KV lands at
f16 (~6.4 GB instead of ~3.2 GB). The 3090 is Ampere, both apply, and 24 GB is
the same class as the L4 the 3.6 baseline arm was provisioned on.

```
weights + vision projector   17.74 GB
KV @ 32k, q8_0               ~3.2 GB
                             ~21 GB of 24 GB
```

### 4.2 Serve

On the instance (`ollama/ollama` image — already installed):

```bash
export OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 \
       OLLAMA_KEEP_ALIVE=1h OLLAMA_MAX_LOADED_MODELS=1
ollama serve
```

On the Mac — **tunnel, do not expose the port.** Ollama has no authentication;
binding `0.0.0.0:11434` publishes an open inference endpoint. The tunnel also
means everything defaults to `localhost:11434` and needs no env vars.

```bash
ssh -N -L 11434:localhost:11434 root@<host> -p <port>
```

### 4.3 Smoke — one scenario, throwaway results dir

```bash
TUTOR_CALL_MODE=two \
MODELS_FILE="$PWD/offline_eval/models_qwen38.txt" \
MODE="--multi-turn --subset v2,math --sample 1 --seed 0" \
RESULTS_DIR=/tmp/qwen38_smoke \
bash offline_eval/run_matrix.sh
```

Never point a sample run at `math30/`. `run_matrix.sh` treats "a JSON exists at
this path" as "done" with no check on its contents, so a 1-scenario file
permanently blocks the real run unless re-run with `FORCE=1`.

### 4.4 Two gates — check both before the long run

**Gate 1 — is it actually in instruct mode?**

```bash
cat /tmp/qwen38_smoke/identity.log     # want: thinking_chars=0 -> answers directly
```

This is the one genuinely unverified thing on this arm. 3.6's hybrid template
demonstrably gated on Ollama's top-level `think` flag; Qwen3.8 moved its primary
control to `reasoning_effort` and routes `enable_thinking` through
`chat_template_kwargs`, which is vLLM-only — Ollama drops it. The tag's baked-in
defaults are the card's *thinking* numbers (temp 1.0, top_p 0.95, pp 0), so if
the flag misses, the arm runs a reasoning model at instruct sampling and the row
is meaningless. **`THINKS` → stop, do not spend the session.**

**Gate 2 — did the whole model land on the GPU?**

```bash
ollama ps      # PROCESSOR column — want "100% GPU"
```

The profile deliberately does not set `num_gpu` (mirroring 3.6, for
comparability), so Ollama autofits to *free* memory at load. This codebase has
measured that going wrong: 17–31 of 34 layers on GPU depending on what else was
resident, decode tracking it directly at 6.7 vs 13.5 tok/s. With ~3 GB of
headroom it is a live risk. If it spilled, fix at **runtime** — `OLLAMA_NUM_GPU=99`
or a lower `num_ctx` — not by editing the profile, so the arm stays comparable.
A spill does not corrupt the result, it just turns 2 hours into 6.

### 4.5 The run

```bash
TUTOR_CALL_MODE=two \
MODELS_FILE="$PWD/offline_eval/models_qwen38.txt" \
MODE="--multi-turn --subset v2,math --sample 30 --seed 0" \
RESULTS_DIR="$PWD/offline_eval/multi_turn_results/math30" \
bash offline_eval/run_matrix.sh
```

Resume-safe per model — interrupt and re-run to continue. Expect well under the
2.5 h the 3.6 arm's pace implies: 3090 memory bandwidth (936 GB/s) is roughly 3×
an L4's.

Cost: ~$0.30 of GPU, plus Anthropic spend for the student-sim and rubric judge.

### 4.6 Read it

```bash
RESULTS_DIR="$PWD/offline_eval/multi_turn_results/math30" \
  ./venv/bin/python offline_eval/aggregate.py
```

Then **destroy the instance** — Vast bills storage even while stopped:

```bash
vastai destroy instance <id>
```

And re-activate the judge row.

---

## 5. Why the judge is deactivated

Two different graders, easily confused:

- **The rubric judge** produces the board's pass/rubric numbers. It is
  **hardcoded** at `evals/runner.py:54` as `claude-sonnet-4-6` for multi-turn —
  it does not come from `ModelConfig`. Every arm on the board shares it, so the
  25/30 baseline is sound.
- **The in-session free-text grader** resolves through
  `get_judge_provider_chain`, which picks from *active* `ModelConfig` rows.

The 3.6 baseline was measured on Colab, where a fresh migrated DB has **zero**
`ModelConfig` rows (verified) and `_local_verifier_chain` finds nothing either —
it filters `is_active=True` while `seed_ollama_configs.py` writes inactive rows.
So that arm ran with no LLM grader tier at all.

Running on the local DB, the grader *would* be active — giving 3.8 something 3.6
never had. Deactivating it restores the baseline's conditions. This is the
opposite of the right call for a fresh sweep, where all arms should share one
cloud judge; it is correct **only** because we are matching an existing row.

---

## 6. Open risks

1. **Thinking suppression unverified on 3.8** — gate 1 above. Highest-impact
   unknown.
2. **~3 GB VRAM headroom** — gate 2 above.
3. **n=30 resolution** — 23–27 is noise. Do not over-read a 1–2 scenario delta.
4. **Quantization** — q4_K_M, same as every other Qwen row. A different host
   (OpenRouter serves this model at fp8/bf16) would not be comparable.
5. **Vast interruption** — the arm is resume-safe, but a single-arm 30-scenario
   run reclaimed midway means restarting that arm.

---

## Appendix — porting to `aws_deployment` later

Only worth doing if the result justifies carrying the arm forward. Needed:

1. The 100 `v2` scenario tags (copy the files; do **not** regenerate — a
   different draw invalidates the baseline) and `evals/select_representative.py`.
2. `infra/ollama/Modelfile.qwen3.6-27b-instruct` and `.qwen3.8-27b-instruct`.
3. Both 27b profile entries → `ai_tutor/apps/llm/model_profiles.py`.
4. The `filter_by_subset` change → `ai_tutor/apps/tutoring/management/commands/run_eval.py`.
5. `evals/test_subset_filter.py` — its `ROOT / 'apps'` paths become
   `ROOT / 'ai_tutor' / 'apps'`.
6. The `OLLAMA_API_BASE` changes in `run_matrix.sh`, `models.py`,
   `seed_ollama_configs.py`.

The notebook generator and `MT100_RUNBOOK.md` do **not** need porting — the Vast
path does not use Colab.
