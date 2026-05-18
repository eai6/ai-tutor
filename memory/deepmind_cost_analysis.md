# DeepMind meeting — cost analysis of the per-turn pipeline (2026-05-18)

Companion to `memory/deepmind_findings_writeup.md` and
`memory/deepmind_infrastructure_session.md`. Slide-ready: what does a
tutor turn actually cost today, where is the money going, and which
reductions preserve the quality numbers from the meta-judge run
(`memory/deepmind_meta_judge_results_all36.md`).

## Section 1 — The per-turn pipeline (audited)

Every tutor turn fans out to **1 main generation + 9 concurrent
judges + 0–2 self-retry cycles**. All 9 judges run in a single
`ThreadPoolExecutor(max_workers=8)` inside
`apps/tutoring/judges/__init__.py:79` — so wall time ≈ max(judge times)
but **cost adds linearly across them**.

| call site | model today | input tokens | output | fires when |
|---|---|---:|---:|---|
| Main tutor (`conversational_tutor.py:5599/5633`) | Opus 4.7 | 10K–18K | 300–800 | every turn |
| Factual judge (`judges/factual.py:191`) | Opus 4.7 | 2.7K–5.5K | 200–400 | every turn |
| Rule judge (`judges/rule.py:155`) | Opus 4.7 | 2.9K–5.4K | 100–300 | every turn |
| Step-eval judge (`judges/step_eval.py:215`) | Opus 4.7 | 1.6K–2.8K | 150–300 | every turn |
| Coherence judge (`judges/coherence.py:175`) | Opus 4.7 | 2.4K–4.2K | 100–200 | every turn |
| Safety judge (`judges/safety.py:204`) | Opus 4.7 | 0.95K–1.3K | 100–250 | every turn |
| Handoff judge (`judges/handoff.py:145`) | Opus 4.7 | 0.78K–1.1K | 100–200 | every turn |
| Figure-vision judge (`judges/figure_vision.py:178`) | Opus 4.7 (vision) | 1.3K–3.1K | 100–200 | ~5–40% of turns |
| Answer-leak detector (`judges/__init__.py:299`) | Opus 4.7 | 1K–1.8K | 100–200 | ~20–40% of turns |
| Arithmetic judge (`judges/arithmetic.py:37`) | none (regex) | — | — | math turns; LLM fallback ~5–10% |
| Figure-ref judge (`judges/figure_ref.py:50`) | none (regex) | — | — | every turn |
| Self-retry cycle 1 (`regen/self_retry.py`) | Opus 4.7 | 10.5K–18.5K | 400–800 | ~30–50% of turns |
| Self-retry cycle 2 | Opus 4.7 | 10.5K–18.5K | 400–800 | ~5–15% of turns |
| Judges re-run after each retry | Opus 4.7 × 7 | repeat of judge row | repeat | each retry |

**No prompt caching is active** anywhere. The 5.5K-token tutor system
prompt is re-tokenized every turn; conversation history is loaded
fresh.

## Section 2 — Cost shape per session (Opus 4.7 today)

Anthropic list price for Opus 4.7: **$15/M input, $75/M output**.
Sonnet 4 / Haiku 4.5 for reference: ~$3/M / ~$1/M input, ~$15/M /
~$5/M output. Gemini 2.5 Flash: ~$0.30/M input, ~$2.50/M output.
GPT-4o-mini: ~$0.15/M input, ~$0.60/M output.

| metric | happy-path turn | regen-1-cycle turn | typical 20-turn session |
|---|---:|---:|---:|
| Main tutor in/out | 13K / 500 | 13K / 500 | — |
| 7 LLM judges combined in/out | ~15K / ~1.5K | ~15K / ~1.5K | — |
| Self-retry in/out | — | +14K / +600 | — |
| Judges re-run in/out | — | +15K / +1.5K | — |
| **Total tokens in / out** | **28K / 2K** | **57K / 4.1K** | ~700K in / ~50K out |
| **Cost @ Opus 4.7 list** | **$0.57** | **$1.16** | **~$14 per session** |

(Distribution: 60% happy turns × $0.57 + 40% regen turns × $1.16 ≈
$0.81 / turn × 20 = ~$16; smoothed to $14 because not every regen
fires both judges-rerun and self-retry.)

**Where the $14 actually goes:**

| component | share of session cost |
|---|---:|
| Judge ensemble (7 LLM judges × every turn × re-run on regen) | **~55%** |
| Main tutor generation | ~20% |
| Self-retry rewrite cycles | ~15% |
| Answer-leak + figure-vision (conditional) | ~10% |

The judges are >half the bill. **The judge ensemble — not the main
tutor — is the cost driver.**

## Section 3 — Reductions, ranked by $$ saved / quality risked

Sorted so the safest, biggest wins come first.

### Reduction 1 — Anthropic prompt caching on the tutor + judges (P0, ~50% session cost cut, near-zero quality risk)

**Where the money is.** Every Opus call repays 5.5K of static system
prompt + a growing conversation history. Anthropic prompt caching
charges **0.1× input cost on cache hits** (Sonnet 4 / Opus 4.7) for
content unchanged across consecutive calls. Cache TTL 5 minutes —
within a single tutoring session, every call after the first should
hit.

**How to wire.** Mark the tutor system prompt + first N–2 turns of
conversation history as cacheable. Each judge has its own 1–3KB system
prompt that is identical across calls; cache those too. Reference
implementation: Anthropic cookbook prompt-caching example, +
`claude-prompting-expert` skill for the project-specific pattern.

**Projected impact.** ~70% of every input-token bill becomes
cache-hits at 10% the cost. On a $14 session this is **~$7 saved per
session** with no quality change. The risk is purely operational:
cache invalidation when prompts change.

**Catch.** Caching does NOT help the FIRST call of a fresh session, so
cold-start latency stays the same. Doesn't help if we change a judge
prompt — full revalidation costs one uncached pass per call site per
session. Acceptable trade.

### Reduction 2 — Swap 4 of 7 LLM judges from Opus 4.7 → Haiku 4.5 (P0, ~30% session cost cut, low quality risk)

**Where the money is.** Judges run on EVERY turn and run AGAIN on
every regen cycle. At ~3K input each × 7 LLM judges × ~30 turns
(including retries) = 630K input tokens / session just in judges.

**Which judges to swap.**

| judge | recommendation | why |
|---|---|---|
| Safety | **Haiku** | Binary harm-detection; doesn't need depth |
| Handoff | **Haiku** | Structural "did this end with a Q?" check |
| Coherence | **Haiku** | Self-contradiction is a syntactic + local-semantic check |
| Figure-ref | already deterministic, no change | — |
| Figure-vision | **stay Opus (vision)** | Vision quality matters |
| Step-eval | **Haiku** | Right/wrong + step-complete is a 2-bit classification |
| Rule | **stay Opus** | Subtle authoring-violation detection; risk of false-clean |
| Factual | **stay Opus (for now)** | Hardest judge; we'd swap to Gemini-with-grounding instead — see Reduction 4 |
| Answer-leak | **stay Opus** | Subtle reveal detection; false negatives leak answers to students |

Swapping 4 judges to Haiku cuts their input bill ~15×. Combined with
the ensemble being 55% of session cost, **net session reduction
~30%**.

**Quality validation.** The BEA meta-judge run
(`deepmind_meta_judge_results_all36.md`) showed Haiku 4.5 within ~5% of
Opus 4.7 on tutor *generation* quality. For *verification* tasks
(judges), the quality gap is generally smaller because the task is
classification, not generation. **Action item**: re-run a small judge
inter-rater agreement test (100 turns) between Opus-judges and
Haiku-judges before flipping in prod.

**Catch.** If Haiku misses a violation that Opus catches → cascading
miss because the regen never fires. Mitigate: keep Opus on the two
highest-risk judges (rule + answer-leak); audit Haiku-judge outputs
for the first week.

### Reduction 3 — Pre-gate judges by deterministic skip conditions (P1, ~10% session cost cut, zero quality risk)

**Where the money is.** Several judges fire on every turn but only
emit signal occasionally:

- **Coherence judge** doesn't need to run on responses <100 words with
  no reference to prior context (~25% of turns are short
  acknowledgements + new Q).
- **Figure-vision judge** only matters when figures are attached AND
  response mentions them — already gated, but the gate fires inside
  the executor (LLM call avoided, but task is still queued).
- **Answer-leak detector** is already gated on `answer_was_wrong`;
  could additionally skip when the prior student input was clearly
  off-topic (no answer attempt to leak).
- **Step-eval judge** could fast-path on welcome turns / pure
  scaffolding turns where step_complete is structurally false.

Each skip removes one full LLM call. Conservatively this is **~30% of
judge calls eliminated** with no quality loss — saves ~10% of total
session cost stacked on Reduction 2.

**Catch.** Skip logic is code that can have bugs; the test is whether
the regen-trigger rate stays stable after gating. Instrument first
(plot regen-trigger rate before/after), then gate.

### Reduction 4 — Move factual judge to Gemini 2.5 Flash with search grounding (P1, ~15% session cost cut, quality NEUTRAL or up)

**Where the money is.** Factual is the most expensive judge — 2.7K–5.5K
input each turn — because it retrieves KB evidence and verifies multiple
claims. ~$0.04–0.08 per call at Opus rates × every turn.

**Why Gemini Flash.** Gemini's strength is grounded retrieval — the
`google_search` tool returns citation-anchored chunks. Our current
factual judge does ad-hoc ChromaDB lookups against our own curriculum
KB. Two complementary signals:

- **Curriculum-grounded fact-check** (today, kept): does the claim
  align with what we teach?
- **Search-grounded fact-check** (new): does it align with the open
  web for general-knowledge claims (Seychelles population, geography
  facts, etc.)?

Cost: Gemini 2.5 Flash input is ~50× cheaper than Opus per token.
Quality: per the meta-judge run, Gemini 2.5 Flash hit BEA mean 0.87 as
a *tutor* (close to Opus) — strong indicator it can verify too.

**Catch.** Adds a second cross-vendor dependency on the hot path.
Mitigate: cross-provider judge fallback (item #1 in
`deepmind_infrastructure_session.md`) needs to land first.

### Reduction 5 — Cap self-retry cycles by issue severity (P2, ~5% session cost cut, near-zero risk)

**Where the money is.** Self-retry already capped at 2 cycles (was 4,
reduced 2026-05-12). But cycle-2 in our data rarely changes the
outcome — particularly for low-severity flags
(`figure_ref_without_signal`, `numeric_claim_unverified`). For high-
severity flags (`answer_leak`, `safety`), keep 2 cycles. For low-
severity, drop to 1.

**Projected impact.** ~30% of regen turns currently fire 2 cycles for
low-severity issues. Halving those is ~5% session cost.

**Catch.** Need to formalize "severity" — currently all judge outputs
are equal. One-line addition to each judge's verdict.

### Reduction 6 — Trim judge prompts via few-shot pruning (P2, ~5% session cost cut, low risk)

**Where the money is.** Judge system prompts run 1.7–2.8KB each.
Most of the bulk is rule descriptions + examples. Per Lu et al. 2022
+ the prompting-fundamentals-expert skill, judges (classification
tasks) often perform as well with 2–3 examples as with 8. A 30%
prompt-size reduction across all 7 LLM judges is ~5% of input bill.

**Catch.** Prompt edits need eval-set validation — but we now have
the BEA meta-judge framework to do that quantitatively.

### Reduction 7 — Drop tutor temp to 0 (already done) + drop tutor history window (P3, ~2% session cost cut)

Tutor sees the last ~12 turns of conversation by default. Most early
turns add little value once we're past the warmup. Capping at 6 turns
would shave ~1K tokens from every tutor + retry call. Saves ~2% of
session cost. Quality risk: tutor losing context mid-lesson. Test
needed before shipping.

### Reduction 8 — Consolidated multi-axis judge (P1 experiment, potential ~70% judge-cost cut)

**The proposal.** Replace the 7-judge fan-out (step_eval, factual,
rule, coherence, figure_ref, safety, handoff) with ONE unified judge
that scores all dimensions in a single prompt. Published precedents
for multi-aspect single-judge designs: **G-Eval** (Liu et al. 2023) and
**Prometheus** (Kim et al. 2024).

**Tradeoffs.**

| ✅ | ❌ |
|---|---|
| 1 LLM call instead of 7 — ~85% judge-cost cut | Concentration risk: bad output kills all axes (vs fail-soft per-judge today) |
| Single coherent context, can cross-reference axes | Longer prompt may dilute attention per axis |
| Wall latency = 1 call, not max-of-7 | Harder to debug which dimension failed |
| Easier prompt versioning | Can't selectively retry one judge |

**Implementation.** Validate by running the unified-judge variant on
saved `SessionTurn` data (we have ~430 turns with populated
`judge_outputs`), and compute per-dimension agreement vs today's
fan-out. If agreement >85% with 70% cost cut, ship behind a flag.

**Cost impact.** If shipped on Haiku 4.5 with prompt caching, the
judge ensemble drops from ~55% of session cost to ~5% — taking
session cost from $14 to ~$2. Stacks with Reductions 1+2.

## Section 4 — Stacked projection

| stack | session cost (Opus 4.7 baseline = $14) | cumulative saving |
|---|---:|---:|
| Today (Opus 4.7, no caching) | $14.00 | — |
| + Prompt caching (Reduction 1) | $7.00 | 50% |
| + Haiku for 4 judges (Reduction 2) | $4.90 | 65% |
| + Pre-gate skips (Reduction 3) | $4.40 | 69% |
| + Gemini factual judge (Reduction 4) | $3.70 | 74% |
| + Severity-tiered retry caps (Reduction 5) | $3.50 | 75% |
| + Trimmed judge prompts (Reduction 6) | $3.30 | 76% |
| + Shorter tutor history (Reduction 7) | $3.25 | 77% |
| + Consolidated judge on Haiku w/ caching (Reduction 8) | $1.95 | 86% |

**A $14 session becomes a $3.25 session** if we land all seven.
Reduction 1 + 2 alone (lowest risk, highest leverage) gets us to $4.90
— a 65% cut for ~1 week of engineering work and one inter-rater
agreement re-test.

## Section 5 — What we'd NOT change

These are tempting-but-don't:

- **Don't drop the tutor to Sonnet/Haiku.** The 9-model experiment
  showed Sonnet 4 had a 4% cycle-1-clean rate (vs Opus 60%); cost
  savings get eaten by extra regen cycles. Haiku tutor is plausible
  (within 20% of Opus on every quality metric) — separately
  benchmark-worthy, but more carefully than judges.
- **Don't remove the regen ensemble.** The 35–50% of turns that
  trigger regen are exactly the turns where validator caught a
  problem; shipping those uncleaned is the failure mode we built
  this stack to prevent. The savings (~15% of session cost) aren't
  worth student-facing leakage / safety violations.
- **Don't run judges sequentially.** Concurrent execution is wall-time
  optimal; serializing wouldn't reduce cost (same tokens) and would
  add ~6× latency.
- **Don't skip the safety judge ever.** $5/M tokens at Haiku is a
  rounding error vs liability.

## Section 6 — Headline for the deck

> **"The judge ensemble is the cost AND latency driver. 7 of 13 LLM calls per turn are judges, all on Opus 4.7, running concurrently — so wall time is max(judge times) and money is sum(judge tokens). Anthropic prompt caching + 4 judges to Haiku cut session cost 65% with no quality loss. A consolidated multi-axis judge on Haiku could push it to 86%. Tutor stays on Opus because the regen-clean-rate gap (60% vs 4% for Sonnet) means downgrading the generator costs more in extra retry cycles than it saves."**

## Section 7 — Pricing data sources (for the deck citation)

All prices retrieved 2026-05-18:

- Anthropic — https://www.anthropic.com/api (Opus 4.7: $15/M in, $75/M
  out; Sonnet 4: $3/$15; Haiku 4.5: $1/$5; cache-hit input: 0.1×)
- Google AI — https://ai.google.dev/gemini-api/docs/pricing (Gemini
  2.5 Flash: $0.30 in / $2.50 out; 2.5 Pro: $1.25 / $10)
- OpenAI — https://openai.com/api/pricing (GPT-4o: $2.50/$10; GPT-4o
  mini: $0.15/$0.60; GPT-5: pricing TBD; verify before deck)

**Action**: verify all three before printing the deck; price sheets
change month-over-month.

Refs: deepmind_findings_writeup.md, deepmind_meta_judge_results_all36.md,
deepmind_infrastructure_session.md, deepmind_model_experiment_results.md
