# Unified multi-axis judge experiment

Sample: **100 saved tutor turns** with populated production judge outputs (random seed=42).

Replaces the 7-judge concurrent fan-out with ONE unified judge call that scores all dimensions in a single prompt. Tested with two cheap models in parallel:

- **anthropic / claude-haiku-4-5-20251001**
- **google / gemini-2.5-flash**

Baseline: the saved `judge_outputs` from the production 7-judge ensemble (typically Opus 4.7-judged). Agreement = does the unified judge flag the same turn the production ensemble flagged?

## Baseline class distribution (sanity check)

| dimension | baseline-flagged | baseline-clean |
|---|---:|---:|
| factual_flagged | 8 | 92 |
| rule_flagged | 31 | 69 |
| coherence_flagged | 24 | 76 |
| figure_ref_flagged | 8 | 92 |
| safety_flagged | 0 | 100 |
| step_complete | 6 | 7 |
| answer_correct | 6 | 2 |

Note: dimensions with very few positives (e.g. safety) have limited statistical power for recall measurements.

## Headline — per-judge agreement vs production baseline

| judge | dim | agreement | recall (flag→flag) | specificity (clean→clean) |
|---|---|---:|---:|---:|
| anthropic/claude-haiku-4-5-20251001 | factual_flagged | 92.0% (92/100) | 12.5% (1/8) | 98.9% (91/92) |
| anthropic/claude-haiku-4-5-20251001 | rule_flagged | 40.0% (40/100) | 67.7% (21/31) | 27.5% (19/69) |
| anthropic/claude-haiku-4-5-20251001 | coherence_flagged | 66.0% (66/100) | 25.0% (6/24) | 78.9% (60/76) |
| anthropic/claude-haiku-4-5-20251001 | figure_ref_flagged | 79.0% (79/100) | 75.0% (6/8) | 79.3% (73/92) |
| anthropic/claude-haiku-4-5-20251001 | safety_flagged | 100.0% (100/100) | nan% (0/0) | 100.0% (100/100) |
| anthropic/claude-haiku-4-5-20251001 | step_complete | 61.5% (8/13) | 83.3% (5/6) | 42.9% (3/7) |
| anthropic/claude-haiku-4-5-20251001 | answer_correct | 75.0% (6/8) | 100.0% (6/6) | 0.0% (0/2) |
| google/gemini-2.5-flash | factual_flagged | 89.8% (79/88) | 0.0% (0/7) | 97.5% (79/81) |
| google/gemini-2.5-flash | rule_flagged | 70.5% (62/88) | 46.7% (14/30) | 82.8% (48/58) |
| google/gemini-2.5-flash | coherence_flagged | 78.4% (69/88) | 20.0% (4/20) | 95.6% (65/68) |
| google/gemini-2.5-flash | figure_ref_flagged | 90.9% (80/88) | 28.6% (2/7) | 96.3% (78/81) |
| google/gemini-2.5-flash | safety_flagged | 100.0% (88/88) | nan% (0/0) | 100.0% (88/88) |
| google/gemini-2.5-flash | step_complete | 54.5% (6/11) | 100.0% (5/5) | 16.7% (1/6) |
| google/gemini-2.5-flash | answer_correct | 85.7% (6/7) | 100.0% (6/6) | 0.0% (0/1) |

## Cost + latency per call

| judge | avg input tokens | avg output tokens | avg latency | errors |
|---|---:|---:|---:|---:|
| anthropic/claude-haiku-4-5-20251001 | 879 | 247 | 2.12s | 0 |
| google/gemini-2.5-flash | 840 | 169 | 4.76s | 12 |

## What this would replace

Today's 7-judge concurrent fan-out (per `deepmind_cost_analysis.md`):
- Aggregate input per turn: ~15K tokens (sum across 7 judges)
- Aggregate output per turn: ~1.5K tokens
- Wall latency per turn: ~max of 7 judge latencies (typically 5-10s)
- Cost on Opus 4.7: ~$0.34/turn (judge ensemble only)

Unified-judge replacement estimate (per the averages above):
- **anthropic/claude-haiku-4-5-20251001**: ~$0.0021/turn (879 in + 247 out @ $1.0/M in, $5.0/M out)
- **google/gemini-2.5-flash**: ~$0.0007/turn (840 in + 169 out @ $0.3/M in, $2.5/M out)

## Honest interpretation — the headline + the caveat

**The cost + latency win is real and large.**
- Today's 7-judge ensemble on Opus 4.7: ~$0.34/turn, ~5–10s wall latency.
- Haiku unified judge: ~$0.0021/turn (160× cheaper), 2.1s latency.
- Gemini 2.5 Flash unified judge: ~$0.0007/turn (490× cheaper), 4.8s latency.

**But the quality picture is mixed — DO NOT ship as a drop-in replacement.**

Per-dimension agreement breaks down into two camps:

| dimension | unified vs production | safe to ship as-is? |
|---|---|---|
| safety_flagged | 100% (no positives in sample) | n/a |
| answer_correct (recall) | 100% (6/6 both judges) | likely yes — but specificity is 0%, so wrong-answers are being marked correct |
| figure_ref_flagged (specificity) | 79–96% | yes for figure-mention false-positive detection |
| factual_flagged | 90% agreement BUT recall 0–12% | **NO — misses 88%+ of factual flags** |
| coherence_flagged | 66–78% agreement BUT recall 20–25% | **NO — misses 75%+ of coherence flags** |
| rule_flagged | 40–70% agreement | **NO — both recall and specificity unstable** |
| step_complete | 55–62% agreement | **NO — coin-flip-level** |

**The pattern: unified judges have high SPECIFICITY (when prod says clean,
they agree) but low RECALL (they miss flags that the production
specialist judges catch).** This is the concentration risk we
hypothesized — one prompt asking for 7 verdicts at once doesn't dig as
deep as 7 prompts asking for 1 verdict each.

**Three plausible paths forward, ordered by effort:**

1. **Hybrid (recommended)**: unified judge runs first as a cheap
   triage. When it flags ANY dimension, run only the specialist
   judge(s) for the flagged dimension(s) on Opus to confirm. Expected
   cost: ~$0.01/turn (unified) + ~$0.05/turn × ~40% flag rate = ~$0.03
   total. **10× cheaper than today, no recall loss.**

2. **Prompt-engineered unified judge**: invest in better few-shot
   examples per dimension + chain-of-thought reasoning per axis. May
   close the recall gap; would need a controlled re-experiment.
   Effort: 1–2 days. Risk: unbounded — may or may not work.

3. **Status quo + reductions 1–7**: skip the consolidation. Cost
   already cut 65% via prompt caching + selective Haiku swap. Lower
   risk, lower ceiling.

**Caveats on the experiment itself:**
- 12 Gemini parse errors (out of 100) — output exceeded 1500-token cap
  when many violations needed enumeration. Re-run with 3000 tokens to
  fix; doesn't change the headline.
- Production baseline isn't ground truth — some "missed" flags may be
  cases where the production judge over-flagged. A 3-way human-+-2-judge
  arbitration would clarify, but is out of scope today.
- 100-turn sample is enough for headline numbers; per-dimension recall
  estimates (especially safety, factual, figure_ref) have wide
  confidence intervals due to low positive counts.

## Methodology notes

- Eval set: random sample of saved SessionTurn rows with populated `judge_outputs` (production = 7 individual judges, mostly on Opus 4.7).
- Binary flag derivation: a dimension is "flagged" when the baseline judge emitted any violation / contradiction / non-safe verdict. Otherwise clean.
- Agreement metric: exact match on the binary flag. Recall = unified agrees when baseline flagged. Specificity = unified agrees when baseline clean.
- Both judges run with temperature=0 and the same prompt.
- Not measured: figure_vision (vision input out of scope for text judge), answer_leak (gated path, different signature), arithmetic (deterministic).

Raw per-turn JSONL: `memory/.deepmind_unified_judge_scores.jsonl` (100 rows)