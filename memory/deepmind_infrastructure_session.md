# DeepMind meeting — infrastructure improvements session (2026-05-18)

Companion to `memory/deepmind_findings_writeup.md`. The findings doc
covers MODEL behaviour. This doc covers ENGINEERING the experiment
itself surfaced — what we need to build, in priority order, to make
the platform robust at production scale across providers.

Each item below references concrete evidence from the experiment
(file paths, log lines, cell IDs) so you can show, not tell.

## #1 — Cross-provider judge fallback (P0)

### The problem

Today every judge call (factual, pedagogy, safety, leak, …) is
configured to use a single ModelConfig per `Purpose`. The factual
content judge defaults to Gemini-2.5-flash. When Gemini went 503 mid-
sweep:

- **3 cells crashed outright** (haiku × L638 × capable, gemini-3-pro
  × L638 × struggler, gemini-25-flash × L540 × capable).
- The Haiku cell is the smoking gun: the *tutor* was Anthropic, the
  session was healthy, but a downstream Gemini judge call 503'd and
  the entire session failed.
- In production this would manifest as: a student is mid-lesson, the
  tutor turn completes, but the judge pipeline crashes → the engine
  ships an unvetted response OR drops the turn.

### The current code

`apps/curriculum/content_judges/_providers.py::get_judge_provider_chain`
already returns a chain of distinct providers and uses
`call_judge_with_fallback` to walk it on failure. But the chain is
populated from active `ModelConfig` rows, and in our local DB only
one provider is active per judge purpose. The plumbing exists; the
config doesn't.

### The fix

1. Seed multiple active `ModelConfig` rows per
   `content_judge_*` purpose — one per provider (Gemini, Anthropic
   Haiku, OpenAI 4o-mini). This is migration + admin work, not
   code work. The chain builder already picks distinct providers.
2. Tighten the fail-soft behaviour: when ALL providers fail (true
   outage), the judge should return `skipped=True + skip_reason='all_providers_failed'`
   so the validator marks the turn unjudged but doesn't crash the
   tutor response.
3. Surface fallback events to telemetry — count `[Judge] fellback
   provider=X→Y` lines in the upload log. Today the fallback is
   silent.

### Effort

Half a day. The chain logic is in place; we just need configs +
better failure-mode handling at the orchestrator level
(`run_all_judges` in `apps/tutoring/judges/__init__.py`).

### Why DeepMind cares

This is a generic LLM-platform pattern, but most teams don't have
cross-provider judge fallback because they single-source their
inference. If we get this right, Gemini outages don't take down our
service. The Gemini team specifically benefits from fewer "the API
is broken, we can't use it" stories.

## #2 — Provider-aware tool-spec rendering (P0)

### The problem

The Finding 1 / 0% tool-use rate on non-Anthropic models. Our
`apps/llm/client.py::BaseLLMClient` exposes `generate()` that takes
`tools=[…]` but only the Anthropic adapter routes them as a real
`tools=` API parameter. The OpenAI and Gemini adapters likely render
them into the system prompt as text (or worse, drop them entirely).
Result: Gemini and OpenAI see "you have these tools available" as
prose, not as a structured calling spec, and don't invoke them.

### The fix

1. **Anthropic** — already correct.
2. **OpenAI** — pass `tools=` + `tool_choice='required'` (or `'auto'`
   with stronger prompt instruction). The Responses API accepts the
   same shape.
3. **Gemini** — pass `tools=[Tool(function_declarations=[…])]` +
   `tool_config={'function_calling_config': {'mode': 'ANY', 'allowed_function_names': […]}}`.
   Mode `ANY` forces tool selection when both tool calls and freeform
   text are plausible.

### Effort

One day per provider for the adapter work + integration tests. The
hard part is verifying that the model picks the *right* tool, not
that it picks *a* tool — but we have the judge ensemble to flag
mismatches.

### Why DeepMind cares

Gemini's `function_calling_config.mode='ANY'` is the killer feature
here. If it works as documented, we go from 0% → ~90% tool-use on
Gemini 3 Pro overnight. That's the single highest-leverage change
in this entire experiment. It'd be valuable to have a 20-minute
working session with someone on the Gemini API team to confirm we're
shaping the call correctly.

## #3 — Long-context system prompt caching (P1)

### The problem

Our tutor system prompt is **~30 KB** rendered every turn. It carries
the lesson's full curriculum context, the current student's
competency snapshot, the active question + 4 options for MCQ, the
hint-vs-reveal rules, the active-question scaffolding, and a media
catalog. Every turn pays this token cost. At 10–15 turns per
session × thousands of sessions per month per school, that's the
dominant line in our LLM bill.

### What we have

- Anthropic prompt caching — works, ~75% cost reduction on cached
  tokens. Confirmed in production.
- OpenAI auto-prompt-caching — partial (only beyond a fixed prefix
  size; system_prompt is included).
- Gemini context caching — documented but we haven't wired it up.

### The fix

Implement a per-provider caching abstraction in the LLM client.
Anthropic-style `cache_control` blocks for the static portions of
the system prompt (curriculum + media catalog), per-call dynamic
tail for student-specific context.

### Effort

Two days. Existing implementation works for Anthropic; needs
extension to OpenAI's silent caching contract + Gemini's explicit
caching API.

### Why DeepMind cares

If Gemini caching works at our prompt sizes (30 KB system prompt
re-sent ~12 times per session), it's the cost lever. Real testimony
on Gemini at this scale would be a good slide for the meeting and a
practical input from us.

## #4 — Better retry strategy on 503 / 429 (P1)

### The problem

The Gemini 503 we hit during the sweep was not transient — it
persisted across many cells (the run spanned ~3 hours, errors hit
across hours, not minutes). Our current retry posture is:

- LLM client retries 3× with exponential backoff (factor 2) before
  raising.
- Caller catches the exception and surfaces as `skipped=True` (for
  judges) or `error` (for the tutor itself).

This is reasonable but doesn't help when:

- The failure window is longer than the retry envelope (~30s total).
- A judge fails after another judge already succeeded → orchestrator
  ships with partial verdicts.

### The fix

1. Per-purpose retry config (`Purpose.JUDGE` can wait longer because
   judges run async to the user; `Purpose.TUTORING` has a tight SLA).
2. Circuit-breaker on the BaseLLMClient — if provider X returns 503
   for >N consecutive requests in M seconds, skip provider X for the
   next K minutes and let downstream fallback take over (depends on
   #1).
3. Surface circuit-breaker state to the dashboard so an operator
   knows "Gemini is currently being skipped, fallback Anthropic
   active."

### Effort

One day.

### Why DeepMind cares

Real production teams need circuit breakers around provider APIs.
This is a generic LLM-infra ask — useful framing for any cloud
provider team.

## #5a — Post-hoc accuracy meta-judge (P1)

### What we have today

The experiment captures *process* metrics (tool-use, regen
convergence, deadlock) and *validator-flag* counts, which proxy for
accuracy but mix signal with noise. The biggest noise contributors
are `numeric_claim_unverified` (any number not in KB triggers it,
regardless of correctness) and `figure_ref_without_signal` (the tutor
mentioning "the diagram" without a media attachment, which is often
a stylistic choice not a bug). Anthropic models hit 100% flag rate on
this signal even though they completed every session cleanly. The
signal is too noisy to put on a slide as "model X is more accurate
than model Y".

### The fix — post-hoc LLM meta-judge

Add a second-pass evaluator that re-rates every saved tutor turn
across saved sessions on three axes (1–5 scale):

| axis | what it measures |
|---|---|
| **factual accuracy** | Does the turn make any claims that are factually wrong against curriculum-grade knowledge? |
| **pedagogical appropriateness** | Is this turn the right move at this point in the lesson? (Right hint level, right transition, right tone.) |
| **leak / reveal risk** | Did this turn give away the answer or pre-empt student reasoning? |

Implementation:
- Read each tutor turn from the saved `SessionTurn` rows (already
  persisted with full content + metadata).
- Send `{lesson_context, prior_turn, this_turn, student_input_after}`
  to a strong meta-judge LLM with a tightly-scoped rubric prompt.
- Use Opus-4.7 OR claude-3.5-sonnet OR Gemini-3-Pro as the meta-judge
  (cross-validate with at least 2 to avoid same-vendor bias).
- Aggregate per (model × lesson × persona) for the slide table.

Cost: 36 sessions × ~10 turns × 1 meta-judge call = 360 calls
(~$5–10). Re-runnable any time over saved sessions — doesn't require
re-doing the tutoring.

### Why DeepMind cares

This is the "LLM-as-judge" pattern at production scale, with a
specific tutoring rubric. Cross-vendor judges remove same-vendor
bias. If we publish the meta-judge prompts + the rubric as part of
the open-source bench (item #5b below), the field gets a reusable
shape for educational-AI quality evaluation.

### Effort

1.5 days for the eval pipeline + an Opus / Gemini 3 Pro double-judge
sanity check. Two months earlier we hand-rolled a smaller
post-hoc judge (`memory/eval_benchmark_v2_simplified.md`) — same
pattern, smaller scope.

## #5b — Synthetic-student benchmark hardening (P2)

### What we built today

`apps/tutoring/student_sim/` drives a full session end-to-end via a
persona-driven student LLM (we used Anthropic Claude for the student
side across all cells to keep that variable fixed). The
`run_model_experiment` management command iterates a matrix and
writes per-cell metrics + a markdown report. This is the apparatus
that produced the data in this deck.

### What's missing for a real bench

1. **Exit-ticket pass rate** as a quality metric. Today we measure
   tutor-side process metrics (tool-use, regen, validator flags) but
   not "did the student actually learn anything?". The exit-ticket
   layer exists; we don't currently complete it in the sim.
2. **Multi-trial averaging.** Each cell is N=1. Temperature is 0.0
   for the tutor and 0.5–0.8 for the student persona, so there's
   variance. We need N=3 minimum for confidence.
3. **Per-trial cost capture.** We track student-side tokens. Tutor-
   side tokens + per-call cost estimate would let us produce the
   cost-vs-quality slide directly.
4. **Persona realism.** Today's "struggler" and "capable" are
   prompt-driven; pilot transcripts would let us fingerprint a
   real student profile and reproduce it.

### Effort

1 week to harden + open-source. Could be a joint paper with DeepMind
if there's interest in publishing a tutor-quality benchmark.

### Why DeepMind cares

There is no public benchmark that measures multi-turn LLM behavior
under remediation pressure in a real educational context. HuggingFace
MT-Bench, AlpacaEval, etc., are single-turn or unstructured. The
existing tutoring benchmarks (MathQA, etc.) measure single-shot
correctness, not the pedagogical loop. We have the apparatus
already; productizing it is achievable and would benefit the field.

## #6 — Tutoring as a multi-agent versus single-agent question (P3, research)

The current architecture is single-agent with post-hoc judges. Per
`memory/agentic_platform_architecture_plan.md` we've explicitly
deferred multi-agent decomposition until benchmark evidence demands
it (Cemri et al. 2025's "17× error amplification" finding bias the
priors away from multi-agent).

The data from this experiment is bias-confirming:
- Opus single-agent + judge ensemble → 60% cycle-1 clean, 100% session completion.
- The judge ensemble already provides what multi-agent would: pre-
  flight checks, regeneration, leak detection.

The interesting research question for DeepMind: at what model scale
does single-agent + judge ensemble become the *worse* pattern than
multi-agent decomposition? We've seen no such crossover in our data
yet. Worth asking what their internal evidence says.

## #7 P1 — E2E benchmark Haiku 4.5 as tutor

Today's BEA meta-judge shows Haiku scored 0.83 vs Opus 0.94, but that
was on synthetic-student replays of just 4–9 mistake turns per cell.
Before downgrading prod tutor to Haiku, run a controlled E2E: pick 4
lessons (geography + math, struggler + capable), run 8 full sessions
with Haiku tutor vs the existing Opus baseline. Score with BEA
meta-judge + cycle-1 regen-clean rate + completion rate. Ship Haiku
iff BEA mean stays within 5% AND regen-clean-rate stays >50%. Effort:
1 day. Why: ~3-5× cost savings on the tutor call if it holds up.
Currently Opus pulls ~20% of session cost.

## #8 P2 — E2E benchmark Gemini 2.5 Flash as tutor

2.5 Flash hit BEA mean 0.87 on the model sweep AND was 1.4× faster
than Anthropic models per turn (23s vs 33s). Both numbers warrant a
controlled E2E. Same protocol as #7. Bonus: if Gemini 2.5 Flash
tutoring works, the factual judge could trivially share the same
client + add search grounding (Reduction 4 in cost analysis). Effort:
1.5 days (provider-aware tool-spec rendering needed first per Finding
1 of `deepmind_findings_writeup.md`).

## Headline for the engineering slide

> **"Three P0 infrastructure improvements before we run this experiment again:**
>
> 1. **Cross-provider judge fallback** so a single-provider outage doesn't crash sessions (we lost 3 cells today to a Gemini 503 that should have rolled over to Anthropic Haiku as the judge).
> 2. **Provider-aware tool-spec rendering** to get Gemini and OpenAI from 0% → working tool use without changing models (probably a 1-day fix and a 5× improvement on session completion).
> 3. **Long-context caching for Gemini** at our 30 KB prompt size — would be the largest single line on the inference bill if we run at pilot scale."

## What we'd ask DeepMind specifically (engineering)

1. **Confirm the `function_calling_config.mode='ANY'` pattern** — is our reading of the docs correct? Worked example for a tutor-shape prompt with two tool definitions would be ideal.
2. **Gemini context caching at ~30 KB system prompt + ~5 KB per-turn variable tail** — pricing tier guidance, gotchas, recommended TTL.
3. **Recommended retry / backoff envelope for `gemini-3.1-pro-preview`** — the 503s we hit were sustained, not transient. What's the official guidance for production retries during preview-model demand spikes?
4. **Gemini Flash as a judge model** at our volume (~10 judge calls per tutor turn × ~12 turns per session × ~thousand sessions per school per month). Is there a "small batch" or "judge tier" pricing path worth exploring?
