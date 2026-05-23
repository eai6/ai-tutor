# Larger Eval (n=110 BEA turns) — 2026-05-23

First **statistically meaningful** BEA-2025 measurement of the tutor on
the merged engine. Adds an `error_prone` synthetic persona that
intentionally makes substantive, diagnosable mistakes, lifting BEA
in-scope coverage **30× over the prior runs**.

Supersedes the small-N v3/v6/v7 comparison
(`bea-comparison-2026-05-23.md`) for the question "how well does the
tutor remediate student mistakes?". The earlier per-variant
comparison is still informative for prompt-tuning iteration but had
n=3-8 — too small for confident dimension-level claims.

## TL;DR

- **110 in-scope BEA turns** across 24 cells (vs 14 in the prior best run). Tight CIs at this N.
- **Overall pass rates**: **27% strict · 48% lenient** on all 4 BEA dimensions.
- **Strongest dimension**: `actionability` (82% lenient) — the tutor reliably gives a clear next action.
- **Weakest dimension**: `mistake_location` (55% lenient) — the tutor often gestures at mistakes without pinpointing the exact error.
- **Surprising persona finding**: `error_prone` (53% lenient) **scored higher than `struggler` (14% lenient)** — explained below.
- **Model comparison**: Sonnet 4 beats Gemini 3 Flash on strict pass rate (36% vs 21%); near-tie on lenient (50% vs 47%).
- **10-principle scores dropped** vs prior small runs (Sonnet 2.62 vs 3.12; Gemini 2.70 vs 2.95) — error_prone sessions surface more pedagogical weak spots the judge penalises.

## What this run measured

| Axis | Value |
|---|---|
| Cells | 24 (2 models × 4 lessons × 3 personas) |
| Tutor models | Claude Sonnet 4 · Gemini 3 Flash |
| Lessons | L1137 (Math · angles around a point) · L1138 (Math · angles on a straight line) · L1425 (Geog · map scale) · L540 (Geog · understanding maps) |
| Personas | `struggler` · `capable` · **`error_prone`** (new — see "Why error_prone" below) |
| Judge | Claude Opus 4.7 (combined 10-principle + BEA per transcript) |
| Wall time (simulator) | ~70 min — error_prone cells hit `max_turns=20` consistently, ~5× longer than struggler/capable |
| Cost | ~$60 ($24 simulator + ~$30 judge + ~$5 retries) |
| Output dir | `ab-test-reports-larger-2026-05-23/` |

## Why `error_prone`

The previous runs had **only 4-7 BEA in-scope turns per cycle** because
the `struggler` and `capable` personas rarely produce remediation-worthy
mistakes — the simulator's struggler tends to give correct-but-vague
answers ("idk", "i guessed") rather than committed wrong answers. BEA
only scores tutor turns whose preceding student turn contains a clear
mistake or confusion, so small N kills statistical power.

`error_prone` is explicitly designed as a measurement instrument:
- Every reply is substantive (a number, a letter, or a phrase — never "idk")
- Every reply is wrong via one of 7 specific error modes (arithmetic slip, wrong operation, echoed number, wrong formula, place-value, distractor MCQ, confused concept)
- Persona prompt at `apps/tutoring/student_sim/personas.py::_ERROR_PRONE_PROMPT`

**Result**: error_prone produced **94 BEA in-scope turns across 8 cells** (≈12 per cell), vs struggler's 14 across 8 cells (≈1.75 per cell) and capable's 2 across 8 cells.

It's not a realistic student. It's a load-test rig for the tutor's
mistake-handling.

## Overall BEA pass rates (n=110)

| Metric | Rate |
|---|---:|
| Strict pass (all 4 dims = `Yes`) | **27%** |
| Lenient pass (all 4 dims = `Yes` or `To some extent`) | **48%** |

### Per dimension

| Dimension | Exact (Yes) | Lenient (Yes + Somewhat) | Read |
|---|---:|---:|---|
| Mistake Identification | 54% | 66% | Tutor recognises the mistake about ⅔ of the time |
| Mistake Location | 37% | 55% | **Weak spot** — only half the time does the tutor pinpoint *where* / *what* the mistake is |
| Providing Guidance | 43% | 76% | Guidance lands as at-least-partially helpful 3/4 of the time |
| Actionability | 49% | 82% | **Strongest** — student knows what to do next 4/5 of the time |

These numbers are at n=110 — 95% CIs are roughly ±9pp on lenient and
±9pp on strict. Differences between dimensions of ~15pp+ are real.

## Per-persona breakdown

| Persona | n_in_scope | Strict | Lenient |
|---|---:|---:|---:|
| `error_prone` | 94 | **31%** | **53%** |
| `struggler` | 14 | 7% | 14% |
| `capable` | 2 | 0% | 50% |

**The struggler's 14% lenient is much worse than error_prone's 53%.**
Two hypotheses for why:

1. **Struggler's mistakes are hedged** ("idk", "i guessed", "is it 90?") and the tutor gives generic encouragement rather than a directed remediation. The judge then marks Mistake_Identification or Mistake_Location as `No` because the tutor didn't actually name the mistake.
2. **Error_prone makes clean, nameable mistakes** that map onto known error categories the tutor's prompt is tuned to handle ("you added when you should subtract"). Easier for the tutor to identify, locate, guide, and redirect.

**Implication**: the tutor is much better at handling confident wrong
answers than at handling confused / hedged answers. The "I don't know"
case may be the bigger production failure.

## Per-model breakdown

| Model | n_in_scope | Strict | Lenient |
|---|---:|---:|---:|
| Sonnet 4 | 44 | **36%** | 50% |
| Gemini 3 Flash | 66 | 21% | 47% |

Sonnet is noticeably better at hitting all 4 dimensions `Yes`
simultaneously (strict 36% vs 21%). Lenient near-tie. Both models
land at ≈half-pass on the all-4-dims criterion.

Note Gemini got more in-scope turns (66 vs 44) — Gemini's sessions
tend to be longer (more turns, more mistake-remediation cycles) so it
generates more BEA-evaluable turns per cell.

## 10-principle score regression

| Run | Sonnet 10p mean | Gemini 10p mean |
|---|---:|---:|
| Prior baseline (small N) | 3.12 | 2.95 |
| Prior v6 | 2.88 | 2.95 |
| Prior v7 | 3.05 | 3.20 |
| **Larger eval (this run)** | **2.62** | **2.70** |

The 10-principle scores dropped 0.4-0.5 pts vs the prior runs. Two
factors:

1. **More cells with error_prone sessions** — long remediation-heavy
   sessions surface more of the tutor's weak spots (poor remediation,
   no prereq drop-back, repeated similar problems). The 10p judge is
   per-session, so any pedagogical weakness anywhere in the 20-turn
   session pulls down the score.
2. **Two new lessons** (L1138 math, L540 geo) introduce content the
   v6/v7 prompt variants weren't tuned for. The default `baseline`
   prompt may not handle their specific structure as well.

This is **not** a quality regression of the tutor between prior runs
and this one — same engine code, same prompt (`baseline` = v3). What
changed is the evaluation lens.

## What this means for production decisions

1. **Don't flip `TUTOR_PROMPT_VARIANT` based on this run alone.** The
   larger-N data is on the **baseline (v3) prompt**, not v6/v7. To
   compare prompt variants properly we'd need to run this same
   error_prone-enriched matrix under v6 and v7 too — another ~$60 each.
2. **The mistake_location weakness (37% exact / 55% lenient)** is the
   single highest-leverage prompt-tuning target. The judge consistently
   notes that the tutor recognises the mistake exists but doesn't name
   exactly *what* went wrong. A system-prompt rule like "When the
   student is wrong, name the specific error before scaffolding" would
   target this directly.
3. **Struggler's 14% lenient suggests the engine over-relies on
   confident-wrong-answer handling.** "I don't know" / hedged answers
   are not getting clear remediation. Worth investigating.

## Cost & latency

- **Simulator**: ~70 min wall, 24 cells, ~$24 in tutor + student LLM calls
- **Judge**: ~30 min wall (one Opus call per transcript, 24 transcripts), ~$30
- **Retries / parse errors**: 1 transcript needed a second pass (LLM returned malformed JSON), ~$1 wasted
- **Total**: ~100 min wall, ~$60 cost

By comparison, the prior baseline runs were ~30 min and ~$15 each but
gave us n=3-8 in-scope turns. **The larger run cost 4× as much for
30× the BEA signal.** Strong cost/value tradeoff.

## Infrastructure changes shipped alongside

- `apps/tutoring/student_sim/personas.py` — added `error_prone` persona definition.
- `scripts/run_ab_test.py` — full-mode matrix expanded from 2 lessons × 2 personas to 4 lessons × 3 personas (24 cells, vs 8 prior). Deploy-mode unchanged (still 2 lessons × 2 personas for CI cost).
- `scripts/judge_transcripts.py` — robustness fix: skip malformed `scores` items instead of crashing the loop. (LLM occasionally returns a string in place of a per-principle dict.)
- `scripts/generate_reports.py` — same robustness fix; also guards the per-cell renderer against non-dict score items.

## Raw data pointers

- `ab-test-reports-larger-2026-05-23/summary.md` — per-cell + per-principle + BEA cross-cell tables
- `ab-test-reports-larger-2026-05-23/FINAL_REPORT.md` — ranked prompt-edit recommendations from the 10-principle judge
- `ab-test-reports-larger-2026-05-23/per_cell/*.md` — full transcripts + judge evidence per cell
- `ab-test-reports-larger-2026-05-23/judge_scores/*.json` — structured per-cell judge output

## Next steps

1. **Run error_prone-enriched matrix under v6 and v7** prompts — gives us the proper apples-to-apples cycle comparison at high N. ~$120 total.
2. **Target the mistake_location weakness** with a focused prompt edit ("name the specific error before scaffolding") and re-baseline.
3. **Investigate struggler's collapse** (14% lenient) — likely a hedged-answer / "idk" handling gap. Worth a code-level look at the engine.
4. **Add error_prone to the CI matrix** if cost permits — currently the deploy-mode CI workflow uses the smaller 4-cell matrix without error_prone. With error_prone it'd cost ~$15-20/deploy (vs current ~$5).
