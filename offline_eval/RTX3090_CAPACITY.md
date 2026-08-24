# Concurrent tutoring capacity on one RTX 3090

**Question.** How many students can one RTX 3090 (24 GB) tutor at the same
time, using the 4B or the 27B model?

**Answer.** With a 60-second budget per tutor turn:

| Model | Concurrent students (median turn < 60s) | Concurrent students (95th-pct turn < 60s) |
|---|---|---|
| **qwen3-4B** (A–D picker tier) | **144** | **64** |
| **qwen3.8-27B** (free-text tier) | **12** | **none of the levels tested** |

A **12× gap** between the tiers. That gap, not the absolute numbers, is the
planning fact: one 3090 serves a class on the 4B and serves one small group on
the 27B.

Both crossings are bracketed by measurement, not extrapolated. The 4B is inside
budget at N=144 (51.2s turn) and outside at N=160 (68.0s); the 27B is inside at
N=12 (56.8s) and outside at N=16 (70.8s).

---

## Method

| Setting | Value |
|---|---|
| GPU | NVIDIA RTX 3090, 24 GB (rented, vast.ai) |
| Server | ollama, flash attention on, KV cache `q8_0` |
| Prompt size | 5,153 input tokens — matched to the eval boards' measured median of 4,916 |
| Reply cap | 90 tokens — the boards' measured median reply |
| Sampling | 3 rounds per level, first discarded as warm-up; p50 over the rest |
| Budget | 60 s per **turn** |

**A turn is two model calls,** not one. The engine runs `TUTOR_CALL_MODE=two`:
call 1 picks the tool, the platform grades the answer, call 2 writes the reply.
The benchmark times one call, so every latency below is `2 x call`. Reading a
call as a turn would double the reported capacity.

**Three different numbers, kept separate:**

- **Slots** (`OLLAMA_NUM_PARALLEL`) — how many requests the GPU batches at
  once. A server setting, bounded by VRAM.
- **N** — how many students are waiting at the same instant. Requests beyond
  the slot count **queue** rather than fail, so N can exceed slots. This is the
  measured, assumption-free capacity number, and it is what the headline table
  reports.
- **Students in a lesson** — larger than N, because a student spends most of a
  turn reading and typing, using no GPU at all. Derived, and it inherits a
  think-time assumption, so it is given as a range and never as one figure.

---

## Full results

### qwen3-4B — 24 slots at `num_ctx` 8192 (18.2 GB used)

| N | turn p50 | turn p95 | tok/s | students @15s think | @30s | @60s |
|---|---|---|---|---|---|---|
| 16 | 9.6s | 13.2s | 91 | 41 | 66 | 116 |
| 32 | 15.0s | 24.2s | 94 | 64 | 96 | 160 |
| 64 | 26.2s | 50.6s | 94 | 101 | 137 | 211 |
| 128 | 40.2s | 80.4s | 114 | 176 | 224 | 319 |
| **144** | **51.2s** | 100.2s | 104 | 186 | 228 | 313 |
| 160 | 68.0s | 129.0s | 93 | over budget | — | — |
| 176 | 64.4s | 123.2s | 104 | over budget | — | — |
| 192 | 67.6s | 133.0s | 107 | over budget | — | — |
| 256 | 87.0s | 171.4s | 111 | over budget | — | — |
| 384 | 128.0s | 243.0s | 113 | over budget | — | — |

N=160 and N=176 land within noise of each other (68.0s and 64.4s) — past
saturation the curve flattens and ordering between adjacent levels stops being
meaningful. Both are over budget, which is what the bracket needs.

### qwen3-4B — 12 slots at `num_ctx` 16384 (18.2 GB used)

| N | turn p50 | turn p95 | tok/s | students @15s | @30s | @60s |
|---|---|---|---|---|---|---|
| 16 | 11.0s | 21.8s | 63 | 38 | 60 | 103 |
| 32 | 19.0s | 33.0s | 75 | 57 | 83 | 133 |
| 64 | 31.4s | 54.8s | 84 | 95 | 125 | 186 |

### qwen3.8-27B — 4 slots at `num_ctx` 32768 (19.1 GB used)

| N | turn p50 | turn p95 | tok/s | students @15s | @30s | @60s |
|---|---|---|---|---|---|---|
| 8 | 39.2s | 65.6s | 21 | 11 | 14 | 20 |
| **12** | **56.8s** | 97.0s | 21 | 15 | 18 | 25 |
| 16 | 70.8s | 127.2s | 22 | over budget | — | — |
| 24 | 111.6s | 198.4s | 21 | over budget | — | — |
| 32 | 142.6s | 266.4s | 20 | over budget | — | — |

---

## Three findings worth carrying into deployment

**1. Halving the context doubles capacity, for free.** `num_ctx` is allocated
**per slot**, not shared. Dropping the 4B from 16384 to 8192 fits 24 slots in
the same 18.2 GB instead of 12, and it wins at every load level — at N=64,
26.2s against 31.4s, and 94 tok/s against 84. The boards' median prompt is
5,153 tokens, so 8192 clears prompt and reply comfortably; 16384 was never
needed for this workload.

**2. The 27B is compute-bound; the 4B is not.** The 27B holds ~21 tok/s
whatever the load — it is saturated at 4 concurrent requests, so more students
only queue. The 4B climbs from 91 to 114 tok/s as N rises, meaning it is still
filling the GPU rather than fighting for it. This is why the gap is 12x and not
the ~6x the model sizes suggest.

**3. The tail breaks well before the median does.** At N=128 the 4B has a 40s
median turn but an 80s 95th percentile: one student in twenty waits over a
minute while the average student is fine. If the 60s budget is a promise rather
than an average, the safe number for the 4B is **64** (50.6s p95), less than
half the median-based 144.

For the 27B there is no such number. Its 95th percentile is already 65.6s at
N=8, the lowest level tested, so **no measured configuration keeps the 27B's
tail inside 60s** — a strict-tail deployment of the free-text tier needs fewer
than 8 concurrent students, or a larger budget, and this sweep does not say
which.

---

## Reproducing

```bash
python offline_eval/concurrency_bench.py --model qwen3-4b-ctx8k \
    --levels 16,32,64,128 --repeat 3 --acceptable 30
python offline_eval/capacity_report.py offline_eval/sweep_rtx3090.log
```

Raw sweep: `offline_eval/sweep_rtx3090.log`.

**Two traps this measurement hit, recorded so a re-run does not repeat them.**
Sixteen slots at `num_ctx` 16384 pushed the card to 22.9 of 24.5 GB and made a
*single* request 4.6x slower than at 4 slots — slower than production. Nothing
errors; it quietly degrades, and the only way to catch it is to check N=1
against a latency already trusted. Separately, single-shot levels are not
reproducible: one sweep timed the 4B slower at N=8 than at N=12, and the 27B
faster at N=2 than at N=1. Latency cannot fall as load rises — that is a slot
paying first-request costs. Hence three rounds per level with the first
discarded.
