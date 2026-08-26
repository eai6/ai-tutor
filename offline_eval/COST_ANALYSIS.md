# Cost

Cloud tutoring is metered per token. On-device tutoring is not metered at all —
once the card is bought, the marginal cost of a session is the electricity to
produce it. The two are therefore reported in the units they actually have,
rather than forced onto a common denominator.

All figures are from the geography board, 34 sessions per arm, computed by
`offline_eval/cost_table.py` from the per-response traces.

---

## Table 1 — Published unit prices, USD per million tokens

| model | input | output | cache read | cache write |
|---|---:|---:|---:|---:|
| claude-opus-4-7 | 5.00 | 25.00 | 0.500 | 6.250 |
| gemini-3.5-flash | 0.30 | 2.50 | 0.030 | 0.375 |
| gpt-5.4-mini | 0.25 | 2.00 | 0.025 | 0.312 |

Cache reads bill at ~10% of the input rate, cache writes at ~125%. The tutor
prompt is deliberately layered so its static prefix — role, rules, tools, then
step content — can be cached, which is why the read rate matters as much as the
headline input rate.

---

## Table 2 — Tokens consumed, geography board

| model | fresh input | cached input | cache writes | output (est.) |
|---|---:|---:|---:|---:|
| claude-opus-4-7 | 1,504,857 | 4,124,127 | 415,837 | 41,884 |
| gemini-3.5-flash | 5,557,243 | 244,212 | 0 | 34,996 |
| gpt-5.4-mini | 3,613,357 | 2,209,408 | 0 | 31,254 |

The three input buckets are **disjoint**: a provider's `input_tokens` excludes
cached tokens, so the prompt's true size is their sum. Output tokens are not
recorded by the tracer and are estimated from reply length; they are 6–8% of
each bill.

---

## Table 3 — What the run cost

| model | billed | per session | if uncached | saved by caching |
|---|---:|---:|---:|---:|
| claude-opus-4-7 | $13.23 | $0.389 | $31.27 | 58% |
| gemini-3.5-flash | $1.76 | $0.052 | $1.83 | 4% |
| gpt-5.4-mini | $1.02 | $0.030 | $1.52 | 33% |
| **total** | **$16.02** | | **$34.62** | **54%** |

**Per session is the number that scales.** A school pays it again for every
session, every year; it does not fall with volume.

Gemini's caching barely engages — 4% saved against Opus's 58%. On a workload
that re-sends the same static prefix on every tutor response, cache reads
should dominate after the first response of a session. It costs little at Flash
prices, but the same behaviour on a more expensive model would re-bill the
whole prefix every time.

---

## On-device — capital, not a per-token price

| item | cost |
|---|---:|
| NVIDIA RTX 3090, 24 GB | **$2,500**, one-off |

Both on-device models run on this single card, so the hardware cost is the same
whichever is deployed. There is no per-token charge.

That is the whole difference. A metered service is paid for again with every
session; a bought card is paid for once. Any comparison beyond this point
requires assumptions about how much the school uses the system, how long the
card lasts, and what a teacher's time costs — none of which this study
measured, and all of which would drive the answer more than the figures above.
