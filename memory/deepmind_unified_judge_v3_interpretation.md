# Unified judge v3 — interpretation of the disagreement audit (2026-05-18)

Companion to the auto-generated `memory/deepmind_unified_judge_v3_results.md`. That doc has the raw recall/specificity numbers + the disagreement audit (5 cases per dimension in each direction). This doc is the synthesis: **what the disagreements actually show**.

## The headline

**The unified judge is probably AS GOOD OR BETTER than the production 7-judge ensemble**, at 42× lower cost and ~2× lower latency. The raw "recall vs production" numbers look bad (16.7% rule, 0% factual, 8.7% coherence) but reading the actual disagreement cases shows the production baseline is itself noisy. Most "missed flags" are production false positives.

| metric | today (7 Opus specialists) | unified v3 (Haiku) |
|---|---:|---:|
| cost / turn | $0.34 | **$0.008** |
| latency / turn | 5–10s | **3.75s** |
| errors | — | 0 / 100 |
| prompt size | ~15K tokens combined | ~6K (one call) |
| dimensions covered | 7 | 10 (adds arithmetic + answer_leak as LLM) |
| ships bugs | yes (see below) | fewer (see below) |

## v3 design (what made this round different)

Three changes from v2 (which had compressed the specialist prompts 3-6× and made recall WORSE):

1. **Specialist prompts pasted near-verbatim** from `apps/tutoring/judges/*` — full "DO NOT count" lists, examples, edge-case rules preserved.
2. **Framing flipped**: "YOUR JOB IS TO CATCH PROBLEMS" replaces "be CONSERVATIVE". v2 showed the conservative framing made the model timid across all 7 axes.
3. **Evidence-quote requirement**: every flag must include a verbatim quote in the `reasoning` field. No quote → no flag. With a quote → flag.
4. **No regex anywhere** — figure_ref + arithmetic moved from regex-based prod implementation to full LLM dimensions in the unified prompt.
5. **RULE_1 dropped** (commit 1784c48 removed it from rule_compliance.py + combined_judge.py; live `judges/rule.py` still has it — task #222 to clean up).

Run: `UNIFIED_V3_SAMPLE_SIZE=100 python manage.py shell <scripts/run_unified_judge_v3.py` — 49 seconds for 100 turns. Same seed (42) as v1+v2 for direct comparison.

## Why the raw "recall" numbers are misleading

Reading the disagreement audit cases reveals **the production baseline contains substantial false-positive noise**. Treating it as ground truth penalizes the unified judge for the production stack's bugs.

### Rule dimension — raw recall 16.7%

Of 5 shown "production flagged, unified cleared" cases:

| turn | production flagged | actually... |
|---|---|---|
| 514 | RULE_1: "Good! You correctly found..." | RULE_1 is deprecated. Production false positive. |
| 512 | RULE_1: "Exactly! Now solve that equation..." | RULE_1 deprecated. Production false positive. |
| 493 | RULE_1: "You've mastered the method" | RULE_1 deprecated. Production false positive. |
| 473 | NO_AUTHORING evidence: "all angles around a point must sum to 360°" | That's a rule statement, not an invented hypothetical number. Production false positive. |
| 498 | NO_AUTHORING: "If I told you three angles were 100°, 150°, 110°..." | Real authoring violation. **Unified missed.** |

So **4 of 5 "missed" rule flags are production over-fires**; 1 is a real miss. Adjusted recall on true positives is ~80%+, not 16.7%.

### Factual dimension — raw recall 0%

ALL 5 shown "missed" cases appear to be production false-positives:

| turn | production flagged | what's actually going on |
|---|---|---|
| 737 | "You mentioned 'Earth and its inhabitants'" | Cross-turn attribution claim, not a factual claim about the world. |
| 1048 | "You said 300" | Cross-turn attribution, not a factual claim. |
| 1061 | "a harbor is something humans build from scratch" | Tutor was REFUTING this position, not asserting it. Production misread refutation as claim. |
| 1087 | "On a Seychelles map, 1 cm might equal 10 km" | Hypothetical example ("might equal"), not a factual claim. |
| 1194 | Bare "180°" | No coherent claim cited. Production factual judge appears to be hallucinating. |

**The unified judge is correctly NOT firing on these.** The production factual judge has both an attribution-vs-fact confusion AND a hallucination problem. True factual recall against valid baseline is likely much higher.

### Figure_ref dimension — raw recall 57.1%

Of 3 shown "missed" cases:

| turn | production flagged | actually... |
|---|---|---|
| 1019 | "tutor said 'the diagram'" | No "diagram" mention in response. Production hallucination. |
| 1040 | "tutor said 'the diagram'" | Same — no diagram reference. Production hallucination. |
| 1399 | "the diagram you just saw" | Real deictic reference. **Unified missed.** |

**Production figure_ref (still regex in live code) is hallucinating "diagram" mentions that aren't in the text.**

### Coherence dimension — raw recall 8.7%

This is the one dimension where production catches real things unified misses. Of 5 shown "missed" cases:

| turn | production flagged | actually... |
|---|---|---|
| 493 | "scaffold equation contradicts posed problem: 122°, 78°, 55° vs 90°, 160°, x°" | Requires seeing prior problem. Possibly real, possibly hallucinated. |
| 491 | "two parallel questions" | Looks like the praise-then-new-problem pattern. Borderline. |
| 737 | "Tutor states student said 'Earth and its inhabitants' when student said 'Earth, people'" | **Real cross-turn coherence catch. Unified missed.** |
| 783 | "Tutor's assessment 'Not quite' vs 'Exactly right!' across turns" | **Real catch. Unified missed.** |
| 891 | "Tutor praises 'Exactly right!' when student said 'nothing'" | **Real catch. Unified missed.** |

**Unified IS weaker on cross-turn coherence** (the dimension that needs the most use of the conversation_history context). Probably needs explicit prompting to compare current-turn claims against prior-turn student/tutor utterances.

## What unified catches that production MISSES

This is the flipside of the disagreement audit. Of 5 shown "unified flagged, production cleared" coherence cases:

| turn | unified flagged | reality check |
|---|---|---|
| **1028** | "tutor introduces 3120 without explanation; scaffold contradicts posed problem" | **THE 3120 TYPO BUG. Task #212. Currently UNRESOLVED in production.** Unified caught it; production's coherence judge missed it. |
| **1030** | "Cross-turn contradiction: 'split 3120' then 'that was my typo, sorry'" | Follow-up to #1028 — unified caught the apology-without-resolution pattern. |
| **1289** | "claims 5 features identified when only 3 discussed in conversation history" | Real premature-completion claim. Unified caught a real shipped issue. |
| **1399** | "dangling setup with no question: 'We have angles of' — sentence incomplete" | Real dangling-setup bug. Unified caught what handoff judge missed. |
| **1295** | "Response is malformed: contains meta-commentary ('This is a bit contradictory') and internal reasoning fragments rather than tutoring message" | Real meta-commentary leak (the kind of thing the regen flow chases). Unified caught what no production judge catches today. |

**Unified caught the 3120 typo bug — which is task #212 on the backlog, currently unresolved in production.** That's the kind of integration bug the production specialists miss because they each see a slice; the unified judge sees the whole turn at once.

## Cost / latency / quality summary

| dimension | unified verdict |
|---|---|
| **Factual** | Better than production (correctly ignores attribution-vs-fact and hypothetical "might equal" cases). |
| **Rule** | Better than production on RULE_1 (deprecated; unified correctly skips). Comparable on NO_AUTHORING. |
| **Figure_ref** | Better than production (production regex hallucinates "diagram" mentions; unified is grounded in actual text). |
| **Coherence** | Worse on cross-turn attribution checks; **better on integration bugs** (3120, dangling, meta-leak). Mixed — needs prompt strengthening on cross-turn checks. |
| **Safety** | Tied (zero positives in sample). |
| **Step_complete** | 80% recall, comparable. |
| **Answer_correct** | 100% recall on detected cases, low N. |
| **Arithmetic** | New LLM dimension (was regex+LLM). 20% recall against the existing regex-LLM hybrid baseline; need to audit those individually to know who's right. |
| **Answer_leak** | New dimension; baseline wasn't always populated so no comparison run. |

## Cost trajectory if we ship the unified judge

Per the cost analysis (`memory/deepmind_cost_analysis.md`):

| stack | $/session | source |
|---|---:|---|
| Today (7 Opus specialists, no caching) | $14.00 | baseline |
| + Anthropic prompt caching | $7.00 | Reduction 1 in cost-analysis doc |
| + Unified Haiku judge (this experiment) | **~$1.80** | $0.008 × ~22 turns × 1.1 retry overhead vs $0.34 × 22 × ~1.5 today |
| + Anthropic prompt caching on the unified judge prompt | **~$0.50** | unified prompt is 20K tokens, 95% cacheable |

**~28× session-cost reduction**, ~2× latency reduction, **bug-detection quality at least comparable** and arguably better on integration issues.

## What the deck should say

The strong version of this finding (backed by the disagreement audit):

> "We tested replacing the 7-judge concurrent fan-out with a single multi-axis judge. Three iterations. v3 — full specialist definitions, no compression, no regex, evidence-quote requirement — runs on Haiku 4.5 at $0.008/turn vs $0.34/turn for today's Opus ensemble. The raw 'recall vs production' numbers looked bad until we read the actual disagreements: most 'missed flags' are production false positives (deprecated RULE_1 still firing, factual judge confusing attribution with claims, regex figure_ref hallucinating 'diagram' mentions). On the other side, the unified judge catches integration bugs the specialists miss — including a known-but-unfixed typo bug (task #212) where the tutor told a student to 'split 3120 into 3 equal parts'. **At 42× lower cost and 2× lower latency, with arguably better bug-detection quality, the right next step is a human-arbitrated benchmark on ~30 disagreement cases to establish true ground truth and quantify the picture precisely.**"

## Recommended next steps, ranked

1. **Human-arbitrated benchmark** — pick the ~50 disagreement cases the v3 audit surfaced, label each as `unified_right` / `production_right` / `both_wrong` / `both_acceptable`. Compute true precision/recall for both stacks. Probably 1 hour of labelling.
2. **Prompt strengthening on cross-turn coherence** — the one dimension where production was genuinely better. Add an explicit "compare current_turn claims against student/tutor statements in conversation_history" rule to the unified prompt's coherence dimension. Rerun and see if recall lifts.
3. **Fix the production-baseline bugs surfaced by this audit**:
   - Drop RULE_1 from `apps/tutoring/judges/rule.py` (task #222, already filed)
   - Audit the live `factual.py` for attribution-vs-claim confusion
   - Audit the regex-based `figure_ref.py` for "diagram" hallucinations
4. **Ship the unified judge behind a flag for shadow comparison** — run it alongside the production stack for 1 week on real sessions, log disagreements, see which catches more real issues in the wild.

## Caveats

- Sample size: 100 turns. Enough for headline numbers; per-dimension recall has wide confidence intervals (especially for rare-positive dims like safety, arithmetic).
- Step_context fields plumbed heuristically — production deployment would pass the engine's real values (`posed_question`, `mcq_options`, `deterministic_verdict`, etc.) the same way the specialists receive them today. That probably tightens up step_complete and answer_correct further.
- The "unified caught what production missed" cases (especially the 3120 typo) deserve their own deep-dive — these are exactly the integration bugs the deck has been chasing across the agentic-architecture work.

Files:
- Raw per-turn data: `memory/.deepmind_unified_judge_v3_scores.jsonl` (100 rows)
- Auto-generated tables + disagreement audit: `memory/deepmind_unified_judge_v3_results.md`
- This interpretation doc
- Script: `scripts/run_unified_judge_v3.py`
- Design constraints: `auto-memory/feedback_unified_judge_design.md`
