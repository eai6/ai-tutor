# Tutor + judge unification — research proposal (2026-05-18)

## Goal

Reduce the per-turn LLM call budget further. Today's pipeline is **1 tutor call + 1 unified judge call** (after the unification work landed today). The judge cost is now ~75% lower than the 7-specialist baseline, but it's still a full second LLM call per turn.

Can we collapse it into the tutor call itself?

## Three patterns, with cost / quality trade-offs

### Pattern A — Judge-as-tool

**Shape:** Add a `self_check` tool to the tutor's tool list. The tutor calls it after composing a draft, gets the 10-axis verdict back, and decides whether to revise or ship.

```python
# Pseudo: tutor system prompt has 3 tools now
tools = [pose_question, pose_inline_question, self_check]
# On each turn, tutor may emit:
#   1. text + pose_question  (current flow, no judge call)
#   2. text + self_check     (tutor opted to verify; tool returns verdict)
#   3. text + self_check + revised_text  (tutor saw bad verdict, fixed)
```

**Cost change vs today:**
- Best case: tutor doesn't call `self_check` (ships untested) → savings = 100% of judge cost
- Worst case: tutor calls `self_check` and revises → 1 extra tool round-trip per turn, **costs MORE than today** because the tutor model is Opus (expensive) and now does 2-3 round trips in one turn

**Quality risk:** the tutor decides when to self-check. Models historically under-call self-check tools when they "feel confident". Adversarial selection: the turns most likely to need a judge are the ones the tutor is most confident shipping.

**Verdict:** ❌ Don't pursue. Selection bias is the killer.

---

### Pattern B — Self-Refine in a single extended-thinking call

**Shape:** Tutor generates a candidate response, then in the same call evaluates against the rubric and emits a revised final response. All inside one LLM invocation using extended thinking / structured output.

```xml
<turn_output>
  <draft>...the tutor's first attempt...</draft>
  <self_eval>
    factual: pass | "none seen"
    rule: fail | "If angles are 100°, 120°, 80°..." NO_AUTHORING
    coherence: pass | ...
    ...
  </self_eval>
  <final>...rewritten if any axis failed, else same as draft...</final>
</turn_output>
```

**Cost change vs today:**
- 1 LLM call instead of 2
- Output ~doubles (draft + eval + final), so output cost ~2x
- Net: tutor input cost stays same, judge input cost goes to 0, tutor output ~2x
- For Opus 4.7 (tutor) — per-turn cost: today $0.42 (tutor) + $0.0025 (Haiku unified judge) = $0.42. Self-refine: $0.42 → ~$0.42-0.50 (depending on output growth). **Net change: roughly break-even.**

**Cost actually shifts when:**
- Tutor was using a cheap model (Haiku) for self-refine, judge model was also cheap → no savings
- The big savings come from removing the second API round-trip latency (~3s saved per turn), not the token cost

**Research precedent:** Self-Refine (Madaan et al. 2023, NeurIPS), Constitutional AI (Anthropic 2022), Self-Critique pattern. **Documented finding: models have self-bias — they under-flag their own outputs ~30% more than they flag others.**

**Quality risk:** the tutor judging its own output. Per the v3 disagreement audit, even cross-vendor judges have non-trivial disagreement; same-model self-judgment is consistently worse.

**Mitigation:** wrap the self-eval in a separate "auditor persona" — same model but with a fresh context asked to be adversarial. Some empirical work shows persona switching recovers most of the self-bias gap. Untested in our codebase.

**Verdict:** ⚠️ Interesting but the cost win is mostly latency, not $$. Quality risk is real and well-documented in the literature. Skip unless latency is the binding constraint.

---

### Pattern C — Trained-in rubric (no runtime judge)

**Shape:** The rubric IS the system prompt. The tutor has been trained (or fine-tuned, or aggressively prompted) to internalize all 10 dimensions during generation. No post-hoc check.

```python
# System prompt explicitly includes rubric:
"""
You are a tutor. ALWAYS check yourself against these 10 rules before
emitting. If ANY rule would fail, fix the text before sending. ...
[full rubric here]
"""
# Output: one tutor turn, no separate judge call.
```

**Cost change:** judge cost → 0. Tutor system prompt grows by ~5KB (the rubric), so input cost per call goes up slightly.

**Quality risk:** trusting the model to police itself with no observability. We can't audit "did the rubric fire on this turn?" because there's no separate verdict to log. Regression possible.

**Research precedent:** This is essentially what RLHF/RLAIF training does — the rubric gets baked into the model's preference function. We don't have a custom-trained model, so prompting is the only option.

**Verdict:** ❌ Don't pursue. Loses the auditability the judge stack provides — and audit is half the value of the judge stack (it feeds regen, dashboards, benchmark analysis).

---

### Pattern D — Hybrid: cheap-tutor-with-judge vs expensive-tutor-no-judge

**Shape:** Move the spend differently. Instead of trying to merge tutor + judge, ask: is the judge stack a proxy for "tutor isn't quite reliable enough"?

If a more expensive / better tutor (e.g. Opus 4.7 vs Sonnet 4) reduces the need for a judge, the math might already favour "Opus tutor + no judge" over "Sonnet tutor + judge ensemble".

**Today's data:**
- Sonnet tutor: 4% cycle-1 clean rate (every turn needs regen)
- Opus tutor: 60% cycle-1 clean rate
- Opus per-turn cost: ~$0.42
- Unified Haiku judge: ~$0.0025

Even if we eliminated the judge entirely, the SAVINGS would be $0.0025 — rounding error vs the $0.42 tutor cost. **The judge is no longer a meaningful cost lever** now that it's on Haiku via the chain.

**Verdict:** ✅ This is the realisation that matters. **The judge is already cheap enough.** The next dollar of cost reduction should target the TUTOR side (prompt caching, history window trimming, Haiku tutor E2E test from `deepmind_findings_writeup.md` Finding 4).

---

## Recommended next steps

In priority order, by expected $ saved / risk ratio:

### 1. **Anthropic prompt caching on the tutor system prompt** (P0)
- Tutor system prompt is ~30KB and re-tokenized every turn
- Anthropic caching: cached input is 10% of full price
- Conservative estimate: **30-50% reduction in tutor input cost** per session
- Effort: ~half-day (Anthropic SDK wrapper change)
- Risk: zero (cache is transparent — same model output, just billed differently)
- This is **Reduction 1** from `memory/deepmind_cost_analysis.md` and the biggest unrealised win.

### 2. **Haiku tutor E2E test** (P1)
- Per the model experiment, Haiku 4.5 BEA mean was 0.83 vs Opus 0.94 — within range
- Haiku is ~$1/M in vs Opus $15/M — **15× tutor cost cut** if quality holds
- Effort: 1 day (drive 8-10 lessons, compare regen-clean-rate + completion + BEA scores)
- Risk: medium (acceptance criteria need to be tight; defined in `memory/deepmind_infrastructure_session.md` item #7)
- If Haiku works, prompt caching on Haiku is even cheaper.

### 3. **Trim tutor conversation-history window** (P2)
- Tutor sees 12 prior turns by default; many early turns add little value
- Reducing to 6 turns saves ~1K input tokens per call × ~20 turns = 20K tokens/session
- Effort: 2 hours (config change + verify no behaviour regression in E2E)
- Risk: low

### What NOT to do

**Don't pursue tutor+judge unification (Patterns A, B, C).** The judge is now Haiku at ~$0.0025/turn. Even eliminating it entirely saves <1% of session cost. The combined complexity of self-judging + loss of auditability isn't worth the rounding-error savings. The right cost lever is the tutor model + prompt caching.

## Cost trajectory if we do #1 + #2 + #3 (no tutor+judge unification)

| state | tutor $/turn | judge $/turn | total $/session (20 turns) |
|---|---:|---:|---:|
| Today (Opus tutor + unified Haiku judge) | $0.42 | $0.0025 | $8.45 |
| + Prompt caching (tutor cache-hit ~70%) | $0.13 | $0.0025 | $2.65 |
| + Haiku tutor (if E2E confirms) | $0.009 | $0.0025 | $0.23 |
| + 6-turn history window | $0.007 | $0.0025 | $0.19 |

**~98% session-cost reduction**, all without touching the judge/tutor separation that gives us the auditability + regen safety net.

## TL;DR

We just made the judge ~75% cheaper. Now the judge is rounding error in the cost analysis. **Stop optimizing the judge. Move to prompt caching and the Haiku tutor E2E test.**

Refs: `memory/deepmind_cost_analysis.md`, `memory/deepmind_unified_judge_v3_interpretation.md`, `memory/unified_judge_rollout_plan.md`, `memory/deepmind_findings_writeup.md`.
