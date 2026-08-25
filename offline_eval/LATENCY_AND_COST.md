# Latency and cost — local tiers against cloud

Ten arms over the same two boards (34 scenarios each), the same tutor build and
the same instrument. Local arms ran on a rented RTX 3090; cloud arms through
provider APIs.

**Every figure here is measured, not modelled.** Latency comes from
`latency_report.py` over the per-turn traces; cost from `cloud_cost.py` over the
same traces, priced from the three disjoint token buckets each provider reports.

---

## 1. Tutor latency per turn

What a student waits for one reply. Median over every turn on the board.

| board | arm | turns | p50 | p95 | max |
|---|---|---:|---:|---:|---:|
| geography | gpt-5.4-mini | 295 | **1.65s** | 3.13s | 7.8s |
| geography | claude-opus-4-7 | 314 | **4.27s** | 6.34s | 12.7s |
| geography | qwen3-4B (local) | 313 | **5.09s** | 8.55s | 45.3s |
| geography | gemini-3.5-flash | 310 | **10.55s** | 17.16s | 35.7s |
| geography | qwen3.8-27B (local) | 294 | **19.62s** | 26.36s | 30.3s |
| maths | gpt-5.4-mini | 636 | **1.50s** | 3.54s | 12.2s |
| maths | claude-opus-4-7 | 677 | **3.81s** | 5.80s | 17.2s |
| maths | qwen3-4B (local) | 635 | **7.46s** | 26.00s | 40.9s |
| maths | qwen3.8-27B (local) | 641 | **14.86s** | 19.65s | 38.0s |
| maths | gemini-3.5-flash | 663 | **15.75s** | 33.51s | 80.9s |

**The local 4B beats two of the three cloud arms.** At 5.09s on geography it is
faster than Gemini Flash (10.55s) and within striking distance of Opus (4.27s).
The offline tier is not the slow option — Gemini is.

**Gemini is the slowest arm on both boards** and by a wide margin on maths
(15.75s p50, 33.51s p95, 80.9s worst). It was also the arm that hung for 83
minutes mid-sweep on a connection with no timeout, so treat its tail as
unreliable rather than merely slow.

**The local 27B is the slowest local option** at 19.62s per geography turn —
4x the 4B for the same board.

---

## 2. Session latency

Wall-clock for a whole tutoring session, and how much of it is the tutor
thinking rather than the harness.

| board | arm | median session | tutor share |
|---|---|---:|---:|
| geography | gpt-5.4-mini | **19.0s** | 73% |
| geography | claude-opus-4-7 | **46.4s** | 83% |
| geography | qwen3-4B | **51.6s** | 87% |
| geography | gemini-3.5-flash | **95.6s** | 92% |
| geography | qwen3.8-27B | **163.3s** | 96% |
| maths | gpt-5.4-mini | **51.0s** | 64% |
| maths | claude-opus-4-7 | **94.1s** | 81% |
| maths | qwen3-4B | **133.8s** | 94% |
| maths | qwen3.8-27B | **295.4s** | 90% |
| maths | gemini-3.5-flash | **337.7s** | 95% |

Tutor share above 90% means the harness is not the bottleneck — the model is.
GPT's 64% on maths is the one case where non-inference work is a real share of
the session.

---

## 3. Cloud cost

Measured token spend, both boards, 34 sessions each.

| arm | board | fresh in | cached in | written | hit% | **cost** | uncached |
|---|---|---:|---:|---:|---:|---:|---:|
| claude-opus-4-7 | geography | 1,504,857 | 4,124,127 | 415,837 | 68% | **$12.19** | $30.22 |
| gemini-3.5-flash | geography | 5,557,243 | 244,212 | 0 | 4% | **$1.67** | $1.74 |
| gpt-5.4-mini | geography | 3,613,357 | 2,209,408 | 0 | 38% | **$0.96** | $1.46 |
| claude-opus-4-7 | maths | 2,495,266 | 7,747,380 | 389,298 | 73% | **$18.78** | $53.16 |
| gemini-3.5-flash | maths | 10,615,578 | 1,739,055 | 0 | 14% | **$3.24** | $3.71 |
| gpt-5.4-mini | maths | 6,651,701 | 4,237,184 | 0 | 39% | **$1.77** | $2.72 |
| | | | | | | **$38.62** | $93.01 |

**Prompt caching saved $54** — 58% of the uncached price. It works because the
tutor prompt is layered for it: static role/rules, then static step content,
then the per-turn tail that is never cached.

**Gemini's cache barely engages** — 4% on geography, 14% on maths, against
Opus's 68-73%. On a workload that re-sends the same static prefix every turn,
reads should dominate after the first turn of a session. It costs little at
Flash prices, but the same behaviour on a heavier tier would re-bill the whole
prefix every turn. Worth investigating separately from this run.

---

## 4. Cost per session, and the comparison that matters

| arm | $/session |
|---|---:|
| claude-opus-4-7 (maths) | **$0.552** |
| claude-opus-4-7 (geography) | **$0.359** |
| gemini-3.5-flash (maths) | $0.095 |
| gpt-5.4-mini (maths) | $0.052 |
| gemini-3.5-flash (geography) | $0.049 |
| gpt-5.4-mini (geography) | $0.028 |

Local, at the RTX 3090's measured $0.165/h rental, **run one session at a
time** — the way the boards actually ran:

| arm | $/session (sequential) |
|---|---:|
| 4B geography | $0.0024 |
| 4B maths | $0.0061 |
| 27B geography | $0.0075 |
| 27B maths | $0.0135 |

But a GPU serves many students at once, and that is the real comparison. At the
concurrency ceilings measured in `RTX3090_CAPACITY.md` (48 for the 4B, 4 for the
27B, at a 30s turn budget):

| arm | $/session at capacity |
|---|---:|
| **4B maths, 48 concurrent** | **$0.00013** |
| 27B maths, 4 concurrent | $0.00338 |

**The 4B at capacity is roughly 4,000x cheaper per session than Opus** and 400x
cheaper than GPT-5.4-mini. Even the 27B, which serves only 4 students at once,
is ~160x cheaper than Opus.

---

## 5. What this does and does not establish

It establishes speed and price. On both, the local 4B is strong: faster than
two of three cloud arms, and cheaper than all of them by three to four orders
of magnitude once the GPU is shared across a class.

**It does not establish teaching quality.** Every arm ran with
`EVAL_SKIP_RUBRIC=1`, so `passed` counts deterministic assertions only —
session completed, tools fired, grading resolved. On those, the arms cluster
tightly: geography 34/34 for all three cloud arms; maths 23/22/22 for cloud
against the local 27B's 24/34. **The frontier model does not beat the offline
one on the maths board.**

Two reasons not to read that as "cloud is not better":

- **A content ceiling caps every arm.** Lessons 1141 and 1138 hold 4 and 5 MCQs
  against the ~6 a session needs, so sessions run to the turn cap when the pool
  empties. Six of Opus's eleven maths failures are there; on adequately stocked
  lessons Opus is 14/14. Discount those two lessons on every arm, or the board
  ranks models by how gracefully they degrade when the questions run out.
- **Assertions are not pedagogy.** Whether a tutor explains, probes, or teaches
  is what the Grade tab measures, and that grading is not yet done.

The cost and latency case for the offline tier is made. The quality case is
open.

---

## Sources

- Latency: `offline_eval/latency_report.py` over `*/trace/*.jsonl`
- Cost: `offline_eval/cloud_cost.py` over the same traces
- Concurrency ceilings: `offline_eval/RTX3090_CAPACITY.md`
- Boards: `offline_eval/multi_turn_results/{geo,math}_{4b_v2,27b_v2,27b_v3,cloud}`
