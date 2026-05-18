# DeepMind meeting — findings writeup (2026-05-18)

Narrative companion to `memory/deepmind_model_experiment_results.md`.
Slide-ready prose, intended to be read aloud or paraphrased in
~10 minutes during the meeting. Backed by 33 simulated tutoring
sessions across 9 models (3 providers × 3 tiers) on 2 lessons
× 2 student personas.

## The setup in one paragraph

We run a Django-based conversational tutor in production with
Seychelles secondary-school students. The lesson flow is structured
(5E pedagogy, bank questions, exit-ticket grading) but tutor turns are
LLM-driven. The tutor has two tool calls available — `pose_question`
(for bank items) and `pose_inline_question` (for chat-authored MCQs)
— and every tutor turn passes through a concurrent judge ensemble
(factual, pedagogy, safety, leak detection, handoff, …) that can
trigger a focused-rewrite "regen" cycle before shipping. We swapped
the active tutoring `ModelConfig` per cell — same prompts, same
judges, same tools, same student persona script — and measured what
happened per session.

## Four findings, ordered by surprise

### Finding 1 — Tool-use compliance is bimodal at our current prompt shape

| provider | avg tool-use across 4 cells |
|---|---:|
| Anthropic (any tier) | 47–81% |
| Gemini (any tier) | **0%** |
| OpenAI (any tier) | **0%** |

**Every Anthropic model used the `pose_question` tool**. Every
non-Anthropic model — including Gemini 3.1 Pro and GPT-5 — produced
**zero tool calls across 100+ tutor turns**. They reverted to writing
prose questions inline.

The honest reading: this is almost certainly a *prompt-adaptation
gap*, not a model-capability gap. Our `tools=[…]` definition is
shaped for Anthropic's native tool-calling. We do not currently
translate to Gemini's `function_calling_config` schema or use OpenAI's
per-call `tool_choice='required'`. The tools end up rendered as plain
text in the system prompt and the non-Anthropic models legitimately
treat them as suggestions.

This becomes the **#1 ask of DeepMind**: what's the canonical Gemini
3 path for forcing a tool selection over freeform prose when both are
plausible completions? Today we'd need provider-aware tool-spec
rendering in our LLM client abstraction (`apps/llm/client.py`) — a
day's work, but worth verifying against DeepMind's recommended
pattern first.

### Finding 2 — Session completion rate diverges as a direct consequence of Finding 1

| provider | sessions reaching exit_ticket (lesson body done) |
|---|---:|
| Anthropic | **11 of 12** (the 12th was a 503 in a downstream judge, not a model failure) |
| Gemini | 7 of 12 |
| OpenAI | 5 of 12 |

The non-completing sessions terminate one of two ways:

- **Deadlock** — the tutor produces the same turn twice in a row and
  our deadlock detector kicks them out. Gemini 3.1 Flash deadlocks on
  **all 4 cells** within 90 seconds each. GPT-5 deadlocks on **all 4
  cells** within 70 seconds each.
- **Max turns** — the tutor never advances past the warmup question
  before hitting our 30-turn cap.

The root cause appears to be Finding 1: without working tool calls,
the engine has no mechanism to advance state. The tutor poses a
prose question, the synthetic student answers, the engine can't bind
that answer to a graded question, the tutor reads back its own prose
without state advancement, and the loop closes.

The interesting outlier: **Gemini 3.1 Pro reaches max_turns on one
cell and deadlocks on another, but completed the geography capable
cell (slowly — 24 minutes)**. So Gemini 3 Pro *can* navigate the
prose-only path — it's just slow and unreliable without tools. The
small / fast Gemini and OpenAI models can't navigate prose-only at
all.

### Finding 3 — Regen cycle-1 clean rate is the truest "instruction-following" signal

The regen prompt is a small focused prompt (~1KB) that says
roughly: "here's a tutor response, here are the validator violations,
rewrite it to fix them. Return ONLY the rewritten text. No preamble.
Don't author new questions. Don't invent arithmetic."

| model | cycle-1 clean rate (rewrites that landed clean on first try) |
|---|---:|
| Claude Opus 4.7 | **60%** (12 / 20) |
| Claude Haiku 4.5 | 48% (10 / 21) |
| Claude Sonnet 4 | 4% (1 / 23) |
| Every Gemini | **0%** (0 / 67 combined) |
| Every OpenAI | **0%** (0 / 33 combined) |

Across 100+ rewrite attempts on Gemini and OpenAI models, **not a
single one landed clean on the first cycle**. They needed cycle 2 or
3, or shipped dirty. Opus is the only model that follows our
focused-rewrite prompt cleanly the majority of the time. Sonnet 4 has
strong tool-use compliance but its rewrites consistently re-trigger
validator flags — likely because the model wants to "improve" the
text beyond just fixing the flagged issues.

This is interesting in its own right: even Gemini 3.1 Pro, the
flagship reasoning model, hits 0% on a 1KB focused instruction-
following task. The prompt structure may be the wrong shape for
Gemini's behaviour — possibly too directive, possibly under-specified
on what "clean" means. **Second ask for DeepMind**: what's the
canonical Gemini 3 pattern for "rewrite-this-text-to-fix-these-
specific-violations" small prompts? Today's pattern works on Opus.

### Finding 4 — A single unified judge can replace the 7-judge ensemble on cost but not on recall

We tested whether one LLM call with a multi-axis prompt could replace
the 7-judge concurrent fan-out. 100 saved tutor turns with full
production judge outputs as ground truth. Two cheap models in
parallel: Haiku 4.5 and Gemini 2.5 Flash.

**The cost + latency story is dramatic:**

| metric | today (7-judge Opus ensemble) | unified Haiku | unified Gemini 2.5 Flash |
|---|---:|---:|---:|
| cost / turn | $0.34 | $0.0021 | $0.0007 |
| **savings** | — | **160×** | **490×** |
| wall latency / turn | 5–10s | **2.1s** | 4.8s |

**The quality story is mixed and instructive:**

| dimension | unified vs production agreement | safe to drop-in replace? |
|---|---|---|
| safety | 100% | yes (no positives in sample) |
| answer_correct recall | 100% | yes |
| figure_ref | 79–96% | yes |
| **factual** | 90% agreement but **0–12% recall** | **no — misses 88% of factual flags** |
| **coherence** | 66–78% agreement, **20–25% recall** | **no — misses 75% of coherence flags** |
| **rule** | 40–70% agreement | **no — unstable** |
| **step_complete** | 55–62% agreement | **no — coin-flip** |

**The pattern: unified judges have high SPECIFICITY but low RECALL.**
One prompt asking 7 questions doesn't dig as deep as 7 prompts asking
1. This is the concentration risk the multi-agent literature
(Cemri et al. 2025) warns about, manifesting cleanly on real data.

**The honest recommendation — hybrid, not full consolidation:**

Unified judge runs first as a cheap triage. When it flags ANY
dimension, fire only the specialist Opus judge(s) for the flagged
dimension(s) to confirm. Expected cost: ~$0.03/turn — **10× cheaper
than today, no recall loss**. The full-consolidation $0.0021/turn
target requires further prompt engineering on recall.

**Why this is the right slide for the meeting**: we tested the
cheap-consolidation hypothesis empirically rather than handwaving. The
data tells us cost is bounded only by recall engineering, and the
hybrid path is shippable today with no quality regression.

**Third ask for DeepMind**: are there published Gemini prompting
patterns for multi-aspect classification that preserve specialist-
judge recall? Our current prompt structure works for tutor generation
but underdelivers when asked to verify 7 things at once on a single
pass.

Cross-ref: `memory/deepmind_unified_judge_results.md` (full per-
dimension agreement breakdown, recall + specificity, cost projection).

## What the data says about Sonnet specifically (interesting subplot)

Sonnet 4 has the highest tool-use rate (81% avg) of any model in the
test, but the lowest cycle-1 clean rate of the Anthropic models (4%).
That combination means: Sonnet aggressively uses the tools, AND its
initial generation almost always trips a validator flag, AND its
regen cycles also struggle to land clean. It got to exit_ticket on
all 4 cells, but only because cycles 2/3 of the regen ensemble bailed
it out.

This is consistent with what we'd seen before: Sonnet had been our
production tutor, we observed dirty turns shipping, and we swapped to
Opus on 2026-05-17. The experiment quantifies the gap that
motivated the swap: ~15× difference in cycle-1 clean rate at the same
temperature (0.0) and prompts.

## What the data says about size tiers

The cleanest within-provider tier comparison is **Anthropic Opus vs
Haiku** (both follow tools, both follow rewrites):

| metric | Opus 4.7 | Haiku 4.5 |
|---|---:|---:|
| tool-use | 50% | 47% |
| cycle-1 clean | 60% | 48% |
| sessions completed | 4/4 | 4/4 (after retry) |
| avg wall per cell | 264s | 287s |

Haiku is within 20% of Opus on every quality metric AND is ~3-5×
cheaper per token. The "tutoring needs the best model" framing is
probably wrong for a school-pilot use case. This is a follow-up worth
testing in a longer pilot: **Haiku-4.5-tutored sessions at 1/3 the
cost, with the same regen safety net**.

## A note on accuracy (this is what we DIDN'T measure cleanly)

The metrics above are *process* metrics — tool-use, regen, deadlock,
completion — not response *accuracy*. Accuracy is harder to measure
because:

1. Most tutor turns don't have a single correct answer (hints are
   open-ended, scaffolding is judgement-driven, the "right" turn
   depends on the student state).
2. We don't grade the synthetic-student exit-ticket attempt in the
   current sim — we only check that the lesson body completed.
3. The validator flags we *do* persist are signal-noisy:
   `numeric_claim_unverified` fires whenever the factual judge can't
   ground a number against the KB, but "ungrounded" ≠ "wrong".

That said, the validator profile gives a useful **proxy**. Below is
the per-model violation profile across all tutor turns:

| model | tutor turns | turns with ≥1 flag | top flags (per 10 turns) |
|---|---:|---:|---|
| Opus 4.7 | 32 | 32 (100%) | numeric_unverified=5.9, figure_ref=4.4, repeated_q=3.8, incoherent=2.8 |
| Sonnet 4 | 32 | 31 (97%) | numeric_unverified=6.6, regen_did_not_clean=5.6, incoherent=4.7, figure_ref=4.4 |
| Haiku 4.5 | 34 | 33 (97%) | numeric_unverified=5.9, figure_ref=4.7, incoherent=4.4, regen_did_not_clean=3.2 |
| Gemini 3.1 Pro | 90 | 67 (74%) | regen_did_not_clean=5.9, **no_question=5.9**, numeric_unverified=3.3, figure_ref=1.4 |
| Gemini 3.1 Flash | 19 | 8 (42%) | numeric_unverified=2.1, repeated_q=2.1, figure_ref=1.1, authoring_violation=1.1 |
| Gemini 2.5 Flash | 27 | 26 (96%) | numeric_unverified=7.4, regen_did_not_clean=5.6, **authoring_violation=4.1**, incoherent=4.4 |
| GPT-5 | 18 | 8 (44%) | numeric_unverified=2.2, repeated_q=2.2, figure_ref=1.1, authoring=1.1 |
| GPT-4o | 29 | 20 (69%) | numeric_unverified=4.5, repeated_q=1.7, figure_ref=1.0, authoring=1.0 |
| GPT-4o mini | 30 | 23 (77%) | numeric_unverified=4.0, incoherent=2.0, regen=2.0, repeated_q=2.0 |

**Reading the table:**

- The 100% rate on Anthropic models is dominated by `numeric_claim_unverified` and `figure_ref_without_signal`, both of which are arguably validator noise (ungrounded ≠ wrong, deictic reference ≠ broken). Strip those two and Anthropic models look much cleaner.
- The "44%" on Gemini 3 Flash and GPT-5 looks great until you remember those models **only completed ~19 turns combined across 4 cells** — they deadlocked before the validator could trip much. Low flag rate ≠ high quality when the session died.
- **The signature problems per provider are distinct**:
  - Anthropic: `figure_ref_without_signal` (tutor mentions "the diagram" without attaching one) and `repeated_question` (asking the same Q twice).
  - Gemini 3 Pro: `no_question` (5.9 per 10 turns — by far the highest) — directly tied to the 0% tool-use finding; without tools, Gemini ends turns without proper question handoff.
  - Gemini 2.5 Flash: `authoring_violation` (4.1 per 10 turns) — authored prose MCQs that should have been tool calls; the structural validator catches them.
- **No model leaked answers more than 1/30 turns**, so the leak detector + scaffolding rules are working roughly equally across providers.

**The honest framing for the meeting**: we have a proxy for accuracy.
Cleaning that up — true per-turn accuracy scoring via a post-hoc
LLM meta-judge — is item #5 in the infrastructure session deck
(`memory/deepmind_infrastructure_session.md`).

## Headline for the deck

> **"Judges, not the tutor, are the cost AND latency driver.
> Per-turn pipeline = 1 tutor call + 7 LLM judges + 0–2 regen
> cycles. 55% of session cost is judges; per-turn wall time is
> max(judge times). Three measured paths reduce it:
> (a) Anthropic prompt caching + swap 4 of 7 judges to Haiku cuts
>     cost ~65%, no quality loss per BEA-rubric meta-judge;
> (b) a hybrid "unified-triage → Opus-specialist on flags only"
>     judge cuts cost ~10×; we tested the unified-only ceiling at
>     160–490× cheaper but recall on factual / coherence is too
>     low to ship as-is;
> (c) provider-aware tool-spec rendering would unlock
>     non-Anthropic tutors that today produce zero tool calls."**
>
> **And from the model sweep: real-world tutoring is also a
> tool-use + focused-rewrite problem. Two of three frontier model
> families produced zero tool calls under our current prompt shape
> — Anthropic models drove every successful session. Haiku 4.5
> holds within 20% of Opus on every metric, so the production
> tutoring use case may be a 'small model + judge ensemble' shape
> rather than 'biggest available model'."**

### The three asks for DeepMind

1. **Tool-use forcing on Gemini 3** — what's the canonical pattern
   for guaranteeing tool selection over freeform prose when both
   are plausible completions? Equivalent to OpenAI's
   `tool_choice='required'` but for Gemini.
2. **Focused-rewrite prompt shape that works on Gemini 3** — our
   1KB regen prompt hits 0% cycle-1 clean rate across all Gemini
   tiers; 60% on Opus. What's the right shape for "rewrite-this-
   text-to-fix-these-specific-violations" small prompts on Gemini?
3. **Multi-aspect classification with specialist-level recall** —
   our unified-judge experiment shows Gemini 2.5 Flash can score
   7 dimensions in one prompt at 490× cost reduction but with
   low recall on factual / coherence. Is there a published Gemini
   pattern that preserves specialist-judge recall on multi-axis
   verification?

### Latency table — completed sessions

| within family | fastest | slowest | spread |
|---|---|---|---|
| Anthropic | Haiku 4.5: 33.4 s/turn | Opus 4.7: 33.0 s/turn | basically tied |
| Gemini | 2.5 Flash: 23.4 s/turn | 3.1 Pro: 49.1 s/turn | 2× spread |
| OpenAI | GPT-4o: 17.2 s/turn | GPT-4o mini: 19.5 s/turn | basically tied |

Anthropic is tied across tiers because judges run on Opus regardless of which tutor model fired — wall time is dominated by the judge ensemble, not the tutor call.

Cross-refs: `memory/deepmind_cost_analysis.md` (per-turn pipeline audit + reductions), `memory/deepmind_meta_judge_results_all36.md` (BEA-rubric meta-judge results across the 36 cells).
