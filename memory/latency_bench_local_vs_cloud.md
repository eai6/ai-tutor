# Latency benchmark — Qwen3-4B on the Jetson vs Sonnet 5

**Status**: first run complete, 2026-07-29 (N=5 per cell, +1 discarded warmup)
**Harness**: `scripts/bench_latency_local_vs_cloud.py`
**Raw output**: `offline_eval/latency_local_vs_cloud.json`

## Question

For the offline Jetson kiosk, how much tutoring latency do we pay by running
`qwen3-4b-jetson` locally instead of calling Sonnet 5? Specifically: is the
local model fast enough that the offline mode is usable on its own terms, or is
latency the thing that will make students abandon a turn?

This is a **latency-only** study. It says nothing about tutoring quality — that
lives in `memory/eval_benchmark_v2_simplified.md` and the `offline_eval/` sweeps.
The two must be read together before any routing decision.

## What gets measured

Per call, on a streaming request:

| metric | meaning | why it matters here |
|---|---|---|
| **TTFT** | time to first content token | what the student experiences as "did it hear me?" — the dominant UX term |
| **total** | wall time until the stream closes | the number production actually pays, since prod is buffered (no SSE on Azure Container Apps, and the kiosk path is `stream: False`) |
| **tok/s** | `tokens_out / (total - ttft)` | pure decode rate, isolated from prefill |
| **prefill_s** | Ollama's `prompt_eval_duration` | attributes local TTFT to prefill vs load vs queue |

Reported as **median** over N trials with min–max range. Median, not mean: a
single thermal-throttle or a cloud retry otherwise swamps a 5-trial mean.

## Design decisions

- **Three prompt sizes** (`short` ~150 tok, `medium` ~450 tok, `long` ~1500 tok).
  Prefill is where an edge GPU and a datacentre diverge most — a single prompt
  size would hide the term that actually decides whether long lesson context is
  affordable offline. All models see byte-identical prompts.
- **Prompts are synthetic but tutoring-shaped** — 5E framing, Newton's second law
  step, real misconception list, KB-excerpt padding. Self-contained so the run is
  reproducible without a DB. Not drawn from a live `TutorSession`.
- **`max_tokens=400` on both arms.** Capping output stops length variance from
  dominating the `total` column.
- **Temperature is 0.3 locally and unset on Sonnet 5.** 0.3 is the top of the
  production TUTORING clamp `[0.1, 0.3]` (CLAUDE.md invariant), but Sonnet 5
  **rejects non-default sampling parameters with a 400** — `temperature`,
  `top_p`, and `top_k` are all gone from that model's request surface. The arms
  are therefore not temperature-matched. This is a real asymmetry, but a minor
  one for a latency study: temperature moves *what* the model says, not how fast
  it decodes.
- **`thinking: {"type": "disabled"}` on the cloud arm.** On Sonnet 5, omitting
  the `thinking` field **runs adaptive thinking** — a silent default change from
  Sonnet 4.6, where omission meant no thinking. Left alone, every cloud turn
  would reason before answering, which is a different workload from the local
  model running `think: False`, and would inflate cloud TTFT for a reason that
  has nothing to do with local-vs-cloud. Both arms are non-thinking.
- **`think: False` on the local call.** `qwen3-4b-jetson` advertises the
  `thinking` capability and its Modelfile template has a think branch. Leaving it
  on would measure a reasoning trace the production tutoring path never emits,
  inflating local latency for the wrong reason.
- **`num_ctx: 16384` pinned on every local call.** Ollama keys a loaded runner on
  `(model, params)`; a request with a different `num_ctx` **evicts** the resident
  runner and reloads from scratch under `OLLAMA_MAX_LOADED_MODELS=1`. This is the
  same footgun documented at `apps/llm/client.py:1412-1423` — it cost 14 reloads
  across a handful of graded turns on 2026-07-28.
- **One discarded warmup per (model, size).** Pays model load, TLS handshake, and
  cold routing. Without it the first local trial measures a 3.9 GB VRAM load.
- **`ai-tutor.service` stopped during the run.** The Jetson has 7.4 GB unified
  memory with ~5.9 GB in use and the model holding 3.9 GB of it; gunicorn
  contending for the rest risks the unified-memory exhaustion that caused the
  BCCPLEXWDT lockups (see auto-memory `jetson_crash_memory_config.md`).

## Baselines already measured

- Network floor to `api.anthropic.com` from this Jetson: **~7 ms** TCP connect,
  **~100 ms** TLS handshake, **135–265 ms** for a full rejected round trip.
  So no cloud TTFT can beat ~150 ms here, and the SDK amortises the TLS term
  across calls on a reused connection.
- Local model resident at **3.89 GB VRAM**, `num_ctx` 16384, Q4_K_M, fully
  offloaded (`-ngl 99`), KV cache q8_0, flash-attn on.

## Results — 2026-07-29

Medians over 5 trials. `t@100tok` is the derived number to quote: predicted wall
time for a 100-token tutoring reply, `TTFT + 100/tok_s`. It normalizes away the
fact that the two models chose different reply lengths on the same prompt.

| model | size | tok in | prefill | TTFT | tok/s | t@100tok |
|---|---|---:|---:|---:|---:|---:|
| Qwen3-4B (Jetson) | short  |  145 | 0.09s | 0.75s | 17.7 | **6.4s** |
| Qwen3-4B (Jetson) | medium |  491 | 0.09s | 0.71s | 15.8 | **7.0s** |
| Qwen3-4B (Jetson) | long   | 1284 | 0.10s | 0.78s | 16.5 | **6.9s** |
| Sonnet 5 | short  |  205 | — | 0.73s | 75.3 | **2.1s** |
| Sonnet 5 | medium |  748 | — | 0.64s | 62.8 | **2.2s** |
| Sonnet 5 | long   | 1835 | — | 0.69s | 59.3 | **2.4s** |

### Findings

1. **Time-to-first-token is a tie — ~0.7s on both arms.** This was not expected.
   The Jetson is not slower to *start* answering than a datacentre round trip; a
   student watching the screen sees the first word at the same moment either way.
2. **The entire gap is decode rate: ~16 tok/s local vs ~60–75 tok/s cloud, a
   3.6–4.3× spread.** Normalized to a 100-token reply, that is **~6.9s local vs
   ~2.2s cloud — roughly 3×**.
3. **The prefill hypothesis was wrong.** The plan predicted prompt size would be
   where an edge GPU and a datacentre diverge most. It isn't: Ollama reports
   **0.09–0.10s of prefill even at 1284 tokens**, essentially flat, and both arms'
   TTFT is independent of prompt size across a 9× range. Long lesson context is
   close to free offline. Whatever budget pressure exists offline is on *output
   length*, not input length — which is a very different lever.
4. **Local TTFT is ~0.66s of fixed per-request overhead, not prefill.** Prefill
   accounts for 0.09s of the 0.75s. The residual sits in the Ollama request path;
   this run does not attribute it further, and it is the obvious thing to chase if
   TTFT ever needs to come down.
5. **The cloud arm has a fatter tail.** One `long` trial hit **4.59s** TTFT
   against a 0.69s median — a 6.6× outlier. The local arm's worst TTFT across all
   15 trials was 0.81s. Local is slower but far more predictable, which for a
   classroom kiosk may matter more than the median.
6. **Token counts are not comparable across arms.** The identical `short` prompt
   is 145 tokens to Qwen and 205 to Sonnet 5 (~41% more); at `long`, 1284 vs 1835.
   Sonnet 5 ships a new tokenizer. Never compare the two models' token counts, and
   don't reuse either's counts for the other's budgeting.

### What this does *not* say

Nothing about tutoring quality. A 3× latency penalty is only a good trade if the
local model's pedagogy holds up — that question lives in the `offline_eval/`
sweeps, and the two results have to be read together before any routing decision.

## Confounds to state in any writeup

1. **Sonnet 5 is a far larger model.** A latency win for the local model is
   partly a capability trade, not a free lunch.
2. **Cloud numbers are one client, one location, one time of day.** Anthropic
   load varies; treat the cloud row as indicative, not a SLA.
3. **The Jetson is thermally variable.** Sustained load will drift slower than a
   5-trial burst. This measures burst latency, which is the right model for a
   single student turn but not for a classroom of 20.
4. **No prompt caching on the cloud side.** Production tutoring reuses a large
   stable system prompt, so a cached-prefix run would show a materially better
   cloud TTFT at the `long` size. Worth a follow-up arm.

## Follow-ups worth running

- Cloud arm with `cache_control` on the system block — closes confound 4 and is
  the honest cloud configuration for production.
- Concurrency sweep on the local model (2, 4, 8 simultaneous turns) — the kiosk
  serves a classroom, and `-np 1` on the resident llama-server means turns
  serialize.
- `qwen3.5:4b` and `qwen3.5-2b-jetson` as local arms, once the 3.5 tags are
  qualified for tutoring.

Commit: 60916ec
