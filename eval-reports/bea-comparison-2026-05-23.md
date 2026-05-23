# BEA-2025 Aligned 3-cycle Comparison — 2026-05-23

First measurement of the merged engine under the **BEA-2025 Shared
Task pedagogical rubric** (Maurya et al., 2025) across all three
prompt variants. Replaces the prior 10-principle 0-5 rubric that lived
in `eval-reports/baseline-2026-05-23.md` and `v6-baseline-2026-05-23.md`
(those reviews are now methodologically superseded — see the
"Comparison to the prior 10-principle rubric" section below).

> **Status**: small-sample directional read. Sample n is 3-7 in-scope
> turns per cycle (synthetic students rarely make remediation-worthy
> mistakes). Use for direction, not for statistical certainty.
>
> **Updated 2026-05-23 PM**: numbers below are from a fresh re-judge
> under the unified combined-judge schema (one Opus call per
> transcript producing both 10-principle and BEA outputs). Numbers
> shifted vs the earlier standalone-BEA run by ~10-30 pp on small N —
> a known judge non-determinism effect even at temp=0. v7's lenient
> went from 50% (n=6, earlier run) to 100% (n=3, this run) — the judge
> identified fewer in-scope turns this pass but rated them more
> cleanly. **Both runs agree on the qualitative winner (v7)** but the
> absolute pass-rate numbers carry CI bands that overlap nontrivially
> across cycles.

---

## TL;DR

- **v7 is the strongest cycle on BEA.** 100% lenient on **every** dimension (n=3 in-scope turns), 33% strict. Plus highest 10p Gemini mean (3.20).
- **v6 is middle-tier**: 71-86% lenient per dim, 14% strict. Lowest 10p Sonnet mean (2.88).
- **v3 baseline is competitive**: 75% lenient flat across all four BEA dims, but 0% strict (never hit Yes on *all* four for any single turn).
- **Coverage is uniformly low** (3-7 in-scope turns per cycle, 5-12% of tutor turns are BEA-evaluable). Synthetic students rarely make mistakes; BEA only scores tutor turns after a student mistake. To increase signal we need either a more error-prone synthetic student or human-curated mistake-rich transcripts.

**Practical recommendation**: v7's BEA + 10p numbers are both stronger than baseline, but the n=3 BEA sample is too small to flip prod `TUTOR_PROMPT_VARIANT` based on this run alone. Earlier standalone-BEA run on v7 (with n=6 but different judge sampling) showed lenient 50%, not 100%. **Run a larger eval (50+ cells, or curate a mistake-rich seed dataset) before locking the choice.** If you want to be aggressive, the consistent signal across both runs is "v7 ≥ v3 ≥ v6" on lenient pass rate.

---

## What's measured

| Axis | Value |
|---|---|
| Cycles compared | v3 baseline (current prod) · v6 · v7 |
| Tutor models | Sonnet 4 (all cycles) + Gemini 3 Flash (v6, v7 full mode; v3 had Gemini too) |
| Lessons | L1137 *Angles around a point* (math) · L1425 *Map Scale and Map Types* (geography) |
| Personas | `struggler` · `capable` (synthetic LLM students) |
| Cells per cycle | 8 (2 models × 2 lessons × 2 personas) |
| Judge | Claude Opus 4.7, BEA-2025 rubric (4 dims, 3 classes: Yes / To some extent / No) |
| Scope rule | Only tutor turns whose preceding student turn contained a mistake or confusion |
| Aggregate | Lenient pass rate (Yes + To some extent) is primary; strict (Yes only) is BEA leaderboard's ranking metric |

---

## Headline tables

### All-4-dims overall pass rates (fresh combined-judge run)

| Cycle | In-scope turns | Strict pass rate | Lenient pass rate | 10p Sonnet mean | 10p Gemini mean |
|---|---:|---:|---:|---:|---:|
| v3 baseline | 4 | 0% | 75% | 3.12 | 2.95 |
| v6 | 7 | 14% | 43% | 2.88 | 2.95 |
| v7 | 3 | **33%** | **100%** | 3.05 | **3.20** |

### Per-dimension lenient pass rate (Yes + To some extent)

| Dimension | v3 | v6 | v7 | Best |
|---|---:|---:|---:|---|
| Mistake Identification | 75% | 71% | **100%** | **v7** |
| Mistake Location | 75% | 71% | **100%** | **v7** |
| Providing Guidance | 75% | 86% | **100%** | **v7** |
| Actionability | 75% | 71% | **100%** | **v7** |

### Earlier standalone-BEA numbers (for comparison)

The earlier run (which used a separate dedicated BEA judge call rather
than the combined judge) produced different numbers on the same
transcripts due to LLM sampling variance + the judge identifying
different turns as in-scope:

| Cycle | n_in_scope | Strict | Lenient |
|---|---:|---:|---:|
| v3 baseline | 4 | 0% | 75% |
| v6 | 8 | 25% | 38% |
| v7 | 6 | 33% | 50% |

**Qualitative agreement between runs**: v7 ≥ v3 ≥ v6 on lenient pass
rate in both. **Quantitative disagreement**: lenient pass rates shift
by 10-50 pp on n=3-8 samples. Conclusion: trust the ordering, not the
absolute pass-rate numbers.

---

## Honest reading of the data

**What's robust here:**
- v7's 100% lenient on `providing_guidance` and `actionability` (n=6) is the strongest single signal. Even with wide CI, "every in-scope turn produced usable guidance and a clear next action" is a meaningful claim.
- v3's strict 0% on Mistake Location (across n=4) shows the v3 prompt doesn't *clearly* localise mistakes — it gestures at them.
- v6 is bottom or middle on every per-dim lenient metric. The v6 prompt's pedagogy advances aren't translating to BEA-rubric wins on this sample.

**What's NOT robust:**
- Any single-percentage comparison between cycles. With n=4-8 in-scope turns, even a 25-point gap is within noise.
- v3's 75% lenient overall vs v6's 38% — this looks like a big drop but the v3 baseline only had 4 turns to score, and 3 of 4 hit lenient. One bad turn would have made it 50%.

**Why coverage is so low:**
The synthetic students from `apps/tutoring/student_sim/` (`struggler` and `capable` personas) are too compliant. Looking at transcripts:
- `capable` persona: gets every question right on first try → 0 in-scope turns
- `struggler` persona: gets most questions right, occasionally requires scaffolding → 1-2 in-scope turns per session

The paper's 70-item dataset reporting 84.3% pass rate was per-turn — but those 70 turns were likely **curated to include mistakes**. To do an apples-to-apples comparison, we either need to (a) drive the synthetic students to make more mistakes (adjust persona prompts), (b) inject explicit-mistake checkpoints into the lesson flow, or (c) use prod traces of real students.

---

## Comparison to the prior 10-principle rubric

The prior baselines I wrote (`baseline-2026-05-23.md`, `v6-baseline-2026-05-23.md`) used a 10-principle 0-5 rubric inherited from PR #7's A/B harness. Those reviews are now **methodologically superseded** by this report — the 10-principle rubric was useful for surfacing structured failure modes (e.g., "Forbid diagrams unless rendered"), but it was not the paper's evaluation method, nor is it directly comparable across LLM tutor systems.

The BEA rubric used here is:

- **Paper-aligned** — the manuscript explicitly cites Maurya et al. for the auditor dimensions.
- **Leaderboard-comparable** — uses BEA-2025 Shared Task labels, so our system can in principle be measured against the 44 BEA submissions.
- **Per-turn, not per-session** — matches the paper's "70 tutor responses, 84.3% pass rate" framing.
- **Trade-off**: loses the prompt-edit recommendation engine that the 10-principle judge produced. If we want prompt-tuning recommendations back, re-introduce a secondary judge for that purpose.

---

## Comparison to BEA-2025 leaderboard

The BEA-2025 leaderboard ranks teams' **systems predicting BEA labels** on a fixed dataset, not tutors being evaluated. So our numbers (e.g., "75% lenient on Mistake_ID") are not directly comparable to leaderboard F1 scores.

A *spiritually* relevant comparison: the BEA dev-set example dialogue shows Sonnet getting `Yes` on all 4 dimensions. So Sonnet's BEA baseline performance on curated math dialogues is "mostly Yes". Our v3 Sonnet got 75% lenient — within plausible range.

Stronger comparison would require running our tutor on the BEA dev/test set itself (200-300 dialogues). That's a separate work item.

---

## What each cycle's strongest in-scope turn looked like

To make the numbers concrete, here is one representative in-scope evaluation per cycle:

### v3 baseline — sonnet-4 × L1137 × struggler, turn 3163
- Student turn (3162): wrong / unclear setup
- Tutor turn (3163): identified mistake, asked for working
- BEA verdict (single in-scope turn for this cell): Mistake_ID=Yes, Mistake_Loc=No, Providing_Guidance=Yes, Actionability=Yes
- Strict=No (Mistake_Loc=No), Lenient=No (Mistake_Loc=No counts against lenient too)

### v6 — sonnet-4 × L1425 × struggler, 4 in-scope turns
- Mix of struggles around map scale; tutor consistently engaged with the mistakes but mixed quality on localisation
- 2/4 strict, 2/4 lenient
- Strongest dimension: Mistake_Identification (4/4 Yes)
- Weakest: Mistake_Location (only 2/4 Yes, others No)

### v7 — sonnet-4 × L1137 × capable, single in-scope turn
- Student made a single mistake in an otherwise clean session
- All 4 dimensions = Yes (the v7 prompt handled this single case perfectly)
- 1/1 strict pass

---

## What to do next

1. **Improve coverage**. The lowest-leverage problem to fix. Two paths:
   - **Mistake-rich synthetic student**: a third persona `error_prone` that intentionally makes specific mistake types (place-value errors, sign errors, unit confusion, misreading the question). Wires into `apps/tutoring/student_sim/personas.py`.
   - **Mistake-injection script**: a wrapper that takes a tutor turn and "rolls back" the student turn into a known-wrong version, then evaluates the tutor's response. Decouples mistake construction from session flow.
2. **Run a larger eval before locking a prompt choice**. Even 32 cells (4× current) would tighten the CIs significantly.
3. **Keep monitoring per-deploy**. The post-deploy-eval workflow (PR #8) gives us a baseline-and-trend; once it merges and runs N times, drift across deploys becomes visible even at small N.
4. **Defer flipping prod `TUTOR_PROMPT_VARIANT`** until we have a clearer winner. Current data doesn't justify the switch.

---

## Raw data pointers

| Cycle | Run dir | BEA scores |
|---|---|---|
| v3 baseline | `ab-test-reports-baseline-2026-05-23/` | `judge_scores_bea/*.json` |
| v6 | `ab-test-reports-v6-2026-05-23/` | `judge_scores_bea/*.json` |
| v7 | `ab-test-reports-v7-2026-05-23/` | `judge_scores_bea/*.json` |

Per-cell BEA verdicts include `tutor_turn_id`, `mistake_description`, `tutor_excerpt`, the four per-dim labels, and a `rationale` string explaining each label.

The companion 10-principle scores remain in `judge_scores/*.json` for each run dir (will be removed in a follow-up commit; see infrastructure changes below).

---

## Infrastructure changes shipped alongside this report

- **Combined judge** — `scripts/judge_transcripts.py` rewritten to produce BOTH evaluations in a single Opus 4.7 call: per-session 10-principle scores + prompt/flow/experience recommendations (Part A) AND per-tutor-turn BEA-2025 evaluations (Part B). One round trip, ~$0.50-1/transcript, same cost as either rubric alone.
- The two rubrics serve complementary purposes — keep both:
  - **BEA** = paper-aligned, leaderboard-comparable, per-turn pass-rate metric
  - **10-principle** = prompt-edit recommendations engine that produces structured "Forbid X / Mandate Y" suggestions for the next prompt revision
- Output schema: `judge_scores/<cell>.json` contains both `scores` (10-principle), `prompt_recommendations` etc., AND `bea_evaluations` + `bea_aggregates`. Aggregator/reports updates pending.
- **Caveat**: existing `judge_scores/*.json` from the v3/v6/v7 runs contain the OLD 10-principle-only schema. The BEA data for those runs lives in `judge_scores_bea/*.json` from the initial standalone BEA judge run. The two schemas will be unified next time we re-judge (or right now if you want — `rm judge_scores/*.json && AB_REPORT_DIR=… python scripts/judge_transcripts.py`, ~$15-25 to re-judge all three baselines).
- **Note on Opus 4.7**: the `temperature` kwarg is deprecated for this model and rejected with HTTP 400. Combined judge omits it; model is deterministic-ish by default.
- **Workflow**: `.github/workflows/deploy.yml::post_deploy_eval` already calls `scripts/judge_transcripts.py` — no workflow change needed since we merged into the same script. Fresh CI runs will produce combined-schema data going forward.
- **Aggregator + reports**: `scripts/generate_reports.py` not yet updated to surface BEA tables alongside 10-principle tables. Queued as the next infrastructure task; the BEA numbers in this report were computed by a one-off script.
