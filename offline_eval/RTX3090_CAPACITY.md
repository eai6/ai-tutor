# Concurrent tutoring capacity on one RTX 3090

**Question.** How many students can one RTX 3090 (24 GB) tutor at the same
time, on the 4B or the 27B model?

**Answer.** Concurrent students, by response-time budget:

| Model | 30s budget (median) | 30s (95th pct) | 60s budget (median) | 60s (95th pct) |
|---|---|---|---|---|
| **qwen3-4B** (A–D picker tier) | **48** | 32 | **80** | 80 |
| **qwen3.8-27B** (free-text tier) | **4** | 4 | **12** | 8 |

A **10–12x gap** between the tiers. That gap, not the absolute numbers, is the
planning fact: one card serves a class on the 4B and serves one small group on
the 27B.

**These are GPU-inference numbers and they are optimistic — see "The
platform gap" below before planning against them.** A real tutor response on the same
box measured 14.9s where the benchmark measures 8.9s.

---

## Method

| Setting | Value |
|---|---|
| GPU | NVIDIA RTX 3090, 24 GB (rented, vast.ai) |
| Server | ollama, flash attention on, KV cache `q8_0` |
| Prompt size | 5,153 input tokens — matched to the boards' measured median of 4,916 |
| Reply cap | 90 tokens — the boards' measured median reply |
| Sampling | 3 rounds per level, first discarded as warm-up; p50/p95 over the rest |

**A tutor response is TIMED, not derived.** Each simulated student issues call 1 (the
short tool pick) and then call 2 (the full reply), with one timer across both,
because the engine runs `TUTOR_CALL_MODE=two`. Every number below is a whole
response.

An earlier version of this document estimated a response as `2 x one call`. That
was wrong, and wrong by more as load rose — call 1 queues too, which a fixed
multiplier cannot express. At N=32 doubling predicted 15.0s where the measured
response is 20.0s; at N=64 it predicted 26.2s against 39.4s. The capacity figures
it produced were 30-40% too generous.

Measured from the traces, 95% of 27B responses and 100% of 4B responses really are two
calls, so timing the pair is right for almost every response. The 5% single-call
minority makes responses slightly *cheaper*, so this is conservative.

**Three different numbers, kept separate:**

- **Slots** (`OLLAMA_NUM_PARALLEL`) — how many requests the GPU batches at
  once. A server setting, bounded by VRAM, since `num_ctx` is allocated PER
  SLOT.
- **N** — how many students are waiting at the same instant. Requests beyond
  the slot count **queue** rather than fail, so N can exceed slots. This is the
  measured capacity number, and it is what the headline table reports.
- **Students in a lesson** — larger than N, because a student spends most of a
  response reading and typing, using no GPU. Derived, and it inherits a think-time
  assumption, so it is a range and never one figure.

---

## Full results — whole tutor responses, measured

### qwen3-4B — 24 slots at `num_ctx` 8192 (18.2 GB used)

| N | response p50 | response p95 | tok/s | students @15s think | @30s | @60s |
|---|---|---|---|---|---|---|
| 1 | 2.6s | 2.5s | 24 | 7 | 13 | 24 |
| 32 | 20.0s | 28.6s | 81 | 56 | 80 | 128 |
| **48** | **28.8s** | 36.3s | 81 | 73 | 98 | 148 |
| 64 | 39.4s | 51.1s | 76 | 88 | 113 | 161 |
| **80** | **44.0s** | 59.9s | 80 | 107 | 135 | 189 |

### qwen3.8-27B — 4 slots at `num_ctx` 32768 (19.1 GB used)

| N | response p50 | response p95 | tok/s | students @15s | @30s | @60s |
|---|---|---|---|---|---|---|
| 1 | 8.9s | 8.8s | 13 | 3 | 4 | 8 |
| **4** | **22.6s** | 28.3s | 16 | 7 | 9 | 15 |
| 6 | 31.5s | 42.8s | 16 | 9 | 12 | 17 |
| 8 | 39.0s | 52.6s | 17 | 11 | 14 | 20 |
| **12** | **55.6s** | 77.6s | 17 | 15 | 18 | 25 |

---

## The platform gap — read this before planning

The benchmark issues only model calls. A real tutor response also runs the platform's own
work between the two calls: grading the answer, DB writes, embedding lookups.
The student waits through all of it.

Measured on the **same box**, for the 27B:

| | value |
|---|---|
| benchmark response at N=1 | 8.9s |
| `math_27b_v3` board response, 641 real responses | **14.9s** |
| unmodelled platform work | **~6.0s per response** |

So the headline numbers are a **GPU ceiling, not a deployment figure**. Two
readings bracket the truth:

| | 27B at N=4 | reading |
|---|---|---|
| inference only | 22.6s | what the GPU does |
| board-anchored, `board x slowdown(N)` | 37.7s | what a student waits |

The board-anchored reading puts the 27B at roughly **N=2** for a 30s budget
rather than 4. The honest statement is that the real answer sits between the
two columns, closer to the second, and this sweep does not resolve where. To
resolve it, instrument a production response end-to-end under concurrency rather
than benchmarking inference alone.

The same caveat applies to the 4B, where the gap could not be measured
cleanly: its boards ran on an earlier instance, so 2.6s benchmark against 5.1s
board is a cross-box comparison and cannot be attributed to platform work with
confidence.

---

## Three findings worth carrying into deployment

**1. Halving the context doubles capacity, for free.** `num_ctx` is allocated
**per slot**, not shared. Dropping the 4B from 16384 to 8192 fits 24 slots in
the same 18.2 GB instead of 12, and it wins at every load level. The boards'
median prompt is 5,153 tokens, so 8192 clears prompt and reply comfortably;
16384 was never needed for this workload.

**2. The 27B is compute-bound; the 4B is not.** The 27B holds ~16 tok/s
whatever the load — saturated at 4 concurrent requests, so more students only
queue. The 4B climbs to ~81 tok/s and holds it, still filling the GPU rather
than fighting for it. This is why the gap is ~10x and not the ~6x the parameter
counts suggest.

**3. The tail breaks before the median.** At a 30s budget the 4B's median
allows 48 but its 95th percentile allows 32 — one student in twenty waits
noticeably longer than the average. Pick the column that matches whether the
budget is a promise or an average.

---

## Reproducing

```bash
# whole responses (call1 + call2), which is what a student waits
python offline_eval/concurrency_bench.py --model qwen3-4b-ctx8k \
    --levels 1,32,48,64,80 --repeat 3 --turn-mode --acceptable 30

python offline_eval/capacity_report.py --budget=30 --turn-mode \
    offline_eval/sweep_rtx3090_turnmode.log
```

Raw data: `offline_eval/sweep_rtx3090_turnmode.log` (whole responses, authoritative)
and `offline_eval/sweep_rtx3090.log` (single calls, superseded).

**Traps this measurement hit, recorded so a re-run does not repeat them.**

- **`num_ctx` is per slot.** Sixteen slots at 16384 pushed the card to 22.9 of
  24.5 GB and made a *single* request 4.6x slower than at 4 slots — slower than
  production. Nothing errors; it quietly degrades. Check N=1 against a latency
  already trusted.
- **Single-shot levels are not reproducible.** One sweep timed the 4B slower at
  N=8 than at N=12, and the 27B faster at N=2 than at N=1. Latency cannot fall
  as load rises — that is a slot paying first-request costs. Hence three rounds
  per level with the first discarded. Warm-up contamination also made the 27B's
  N=1 look like 10.3s when three rounds put it at 4.1s, which inflated every
  derived figure until it was caught.
- **A response is not two identical calls.** See the Method section.
