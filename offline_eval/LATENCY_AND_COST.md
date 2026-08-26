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

---

## 6. Cost per student per year — capital against metered

The two tiers are priced in different units, and the comparison only means
something once both are per student per year.

- **Cloud is metered.** Every session bills tokens. The school pays again for
  every lesson, every year, forever.
- **Offline is capital.** One RTX 3090 (~$2,500) is bought once and amortised
  over the roll and the card's life. Cost per student *falls* as the school
  grows — the opposite shape.

Assumption: **80 sessions per student per year** (2/week x 40 weeks), and the
mean of the two boards' per-session cost, since a real timetable mixes
subjects.

### Cloud

| model | $/session | $/student/yr | 150 students | 300 students |
|---|---:|---:|---:|---:|
| claude-opus-4-7 | 0.456 | **$36.44** | $5,466 | $10,932 |
| gemini-3.5-flash | 0.072 | **$5.76** | $864 | $1,728 |
| gpt-5.4-mini | 0.040 | **$3.20** | $480 | $960 |

### Offline — one RTX 3090

| roll | card life | $/student/yr | total/yr |
|---:|---:|---:|---:|
| 150 | 1 year | **$17.20** | $2,580 |
| 150 | 2 years | **$8.87** | $1,330 |
| 300 | 1 year | **$8.60** | $2,580 |
| 300 | 2 years | **$4.43** | $1,330 |

($2,500 capital plus ~$80/yr electricity at 350W, 6h/day, 190 school days,
$0.20/kWh.)

### The result that matters, and it is not the convenient one

**At a 150-300 student roll, the cheap cloud tier is cheaper than the GPU.**
GPT-5.4-mini costs $3.20 per student per year against the card's $4.43 at its
most favourable assumption (300 students, 2-year life) and $17.20 at its least.

The crossover — the roll at which the card becomes cheaper:

| model | 1-year card life | 2-year card life |
|---|---:|---:|
| claude-opus-4-7 | **71 students** | **36 students** |
| gemini-3.5-flash | 448 | 231 |
| gpt-5.4-mini | 806 | 416 |

So the honest summary is split:

- **Against a frontier model, the GPU wins easily.** Opus is beaten by the card
  at any roll above ~71 students, and a 300-student school pays $10,932/yr for
  Opus against $1,330 for the card.
- **Against a cheap cloud tier, the GPU does not win on cost at this scale.**
  GPT-5.4-mini beats one 3090 until roughly 400-800 students.

**The offline case therefore does not rest on cost alone at 150-300 students.**
It rests on what the metered tiers cannot offer: working without connectivity,
no per-use bill that grows with adoption, no data leaving the school, and no
exposure to price or availability changes. Those are the arguments the
deployment stands on; cost parity with the cheap cloud tier is a supporting
fact, not the headline.

Quality changes this again, though — see below.

---

## 7. Quality, from 169 human grades (geography)

Graded in the viewer's Grade tab against the 8 pedagogical dimensions of
`ai_tutor/apps/benchmark/pedagogy.py`, all-or-nothing pass rule, zero sessions
peeked.

| arm | pass | rate | $/student/yr |
|---|---:|---:|---:|
| claude-opus-4-7 | 34/34 | **100%** | $36.44 |
| **qwen3.8-27B (local)** | 33/34 | **97%** | **$4.43** |
| gpt-5.4-mini | 29/33 | **88%** | $3.20 |
| gemini-3.5-flash | 24/34 | **71%** | $5.76 |
| **qwen3-4B (local)** | 23/34 | **68%** | **$4.43** |

### Where each tier fails

| dimension | Opus | Gemini | GPT | 4B | 27B |
|---|---:|---:|---:|---:|---:|
| revealing_answer | 0% | 3% | 9% | **32%** | 0% |
| providing_guidance | 0% | **24%** | 3% | 0% | 0% |
| actionability | 0% | 0% | 0% | **15%** | 0% |
| coherence | 0% | 6% | 0% | 0% | 3% |

Every arm scored 100% on mistake identification, mistake location, tutor tone
and human-likeness. The tiers separate on two or three specific behaviours, not
on general competence.

**The 27B is the result.** At 97% it is within one session of Opus, beats both
cheaper cloud models, and costs $4.43 per student per year against Opus's
$36.44 — an eighth of the price for statistically indistinguishable teaching.

**The 4B's weakness is specific.** Its dominant failure is *revealing the
answer* (32%) — giving the answer instead of guiding to it. That is a prompt
and policy behaviour, not a capability ceiling, and it is the same family as
the low explanation-uptake finding. It is worth one targeted attempt before
concluding the 4B cannot teach.

### The capacity tension

| arm | quality | concurrent students (30s turn budget) |
|---|---:|---:|
| qwen3.8-27B | 97% | **4** |
| qwen3-4B | 68% | **48** |

This is the deployment decision in one table. The 27B teaches nearly as well as
a frontier model but serves four students at a time; the 4B serves forty-eight
but reveals the answer in a third of sessions. Fixing the 4B's revealing-answer
behaviour is worth more than any model swap available here — it is the only
change that would give a full class frontier-adjacent tutoring on one card.

**Caveat: this is geography only.** The maths grading is outstanding, and the
maths board carries a known content ceiling (lessons 1141 and 1138) that will
depress every arm.

---

## Figures

| file | shows |
|---|---|
| `figures/fig1_latency.png` | per-turn latency by arm and subject, median with 95th-percentile whisker |
| `figures/fig2_quality_vs_cost.png` | human pass rate against $/student/year |
| `figures/fig3_crossover.png` | $/student/year vs roll, GPU curves against cloud lines |
| `figures/fig4_dimensions.png` | per-dimension failure rate by arm |

Regenerate with `python offline_eval/make_figures.py`; cost model in
`offline_eval/cost_model.py`.
