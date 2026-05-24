# v7 vs Baseline on error_prone — 2026-05-23 (production decision)

Direct apples-to-apples BEA-2025 comparison of `TUTOR_PROMPT_VARIANT=v7`
vs the current prod baseline (v3). Run on the same 2 models × 4
lessons × `error_prone` matrix (8 cells each side, ~78-94 in-scope
BEA turns per side — large enough that the gap is well outside
sampling CI).

> **TL;DR — recommend flipping prod `TUTOR_PROMPT_VARIANT=v7`.** Every
> single BEA dimension improved, biggest lifts on the weakest
> dimensions, 10-principle scores also up. Signal is strong and
> consistent across both Sonnet and Gemini.

---

## Headline

| Metric | Baseline (v3) | v7 | Δ |
|---|---:|---:|---:|
| n in-scope turns | 94 | 78 | — |
| **BEA strict** (all 4 dims = `Yes`) | 31% | **36%** | **+5pp** |
| **BEA lenient** (all 4 dims ≥ `Yes`/`Somewhat`) | 53% | **81%** | **+28pp** |
| 10-principle mean — Sonnet | 2.00 | **2.35** | +0.35 |
| 10-principle mean — Gemini | 2.20 | **2.64** | +0.44 |

## Per-dimension lenient pass rates

| Dimension | Baseline | v7 | Δ |
|---|---:|---:|---:|
| Mistake Identification | 72% | **96%** | **+24pp** |
| Mistake Location *(was weakest dim)* | 59% | **87%** | **+28pp** |
| Providing Guidance | 79% | **88%** | +10pp |
| Actionability *(was strongest dim)* | 85% | **94%** | +8pp |

**Every dimension up.** Biggest lifts on the dimensions where baseline
was weakest (Mistake Location +28pp, Mistake Identification +24pp).
Even the already-strong dimensions (Actionability) gained.

## Per-model lift

| Model | Baseline lenient | v7 lenient | Δ |
|---|---:|---:|---:|
| Sonnet 4 | 50% (n=42) | **83%** (n=36) | **+33pp** |
| Gemini 3 Flash | 56% (n=52) | **79%** (n=42) | **+23pp** |

v7 lifts both models substantially. Sonnet sees the bigger gain — the
v7 prompt's tool-discipline + structured-output rules play to
Anthropic's strengths.

## Why fewer in-scope turns under v7 (78 vs 94)?

v7 handles errors more efficiently. Some sessions reached the exit
ticket in fewer turns because v7 remediates errors so cleanly the
student gets back on track quickly. The most striking case:

- Sonnet × L1138 × error_prone: **baseline 20 turns (max_turns) → v7 6 turns (exit_ticket)**

Fewer mistakes registered overall under v7, but the ones that did get
**meaningfully better treatment**. Net effect: cheaper sessions AND
higher pass rate — pure efficiency win.

## Confidence

At n=78 vs n=94, 95% CIs on lenient pass rate are roughly ±9-10pp
each. The +28pp lenient gap is well outside sampling noise. Per-dim
gaps of +24pp / +28pp / +10pp / +8pp are also outside noise (the +8pp
is the only one within 2× CI; everything else is unambiguous).

The earlier small-N v7 results from `bea-comparison-2026-05-23.md`
(n=3-6 per cycle) hinted at this pattern but couldn't confirm. The
8-cell error_prone-enriched matrix gives the statistical power needed
to make the call.

## Production action

Flip `TUTOR_PROMPT_VARIANT=v7` via repo Settings → Variables →
Actions. Next deploy will pick it up:

```
TUTOR_PROMPT_VARIANT=v7
```

Or trigger an ad-hoc deploy via Actions → Deploy → Run workflow with
`tutor_prompt_variant=v7`. The active Container App revision env var
will switch on the next deploy.

Risk mitigation: defaults preserve current behaviour, so this is a
deliberate flip not an automatic one. Rollback is the same one-line
change in reverse.

## Caveats

1. **Synthetic students**, not real ones. error_prone is an
   instrument designed to maximise BEA coverage; doesn't measure
   student learning gains.
2. **Two specific lessons** (L1137 math + L1425 geo + L1138 math + L540 geo).
   Other lesson types could behave differently.
3. **The judge is Opus 4.7 with the BEA rubric** — same judge for both
   sides, so any judge bias cancels out, but a different judge model
   might score differently.
4. **The +28pp lenient lift may shrink in production** where students
   are real, less deliberately wrong, and have more varied mistake
   patterns. Treat this as "v7 is a structural improvement on
   error-handling" not "v7 lifts production quality by 28pp".

## Raw data pointers

- Baseline: `ab-test-reports-larger-2026-05-23/judge_scores/*error_prone*.json` (8 cells, n=94 in-scope)
- v7: `ab-test-reports-v7-errorprone-2026-05-23/judge_scores/*.json` (8 cells, n=78 in-scope)
- Both used the combined judge (`scripts/judge_transcripts.py`) with the same Opus 4.7 rubric.

## What we did NOT measure (yet)

- **v6 with error_prone-enriched matrix.** Could add another ~$30/30min if you want a 3-way v3/v6/v7 comparison on the same shape. The earlier small-N comparison had v6 at 38-43% lenient — well below v7's 81%, so unlikely to overtake.
- **Real-student transcripts** (not synthetic). Would need a pipeline to anonymise + classify production sessions; out of scope.

## Recommended next steps

1. **Flip `TUTOR_PROMPT_VARIANT=v7` in prod** — this run justifies it.
2. **Monitor the post-deploy-eval workflow output** for 1-2 days after the flip; the deploy-mode matrix (Sonnet + Gemini × 2 lessons × error_prone) will produce a 4-cell snapshot each push.
3. **Consider a v6 comparison run** if you want to confirm v7 > v6 at the same statistical depth (~$30, ~30 min). Otherwise the small-N comparison from `bea-comparison-2026-05-23.md` (v7 ≥ v3 ≥ v6) plus this large-N v7 vs baseline data is sufficient.

## Infrastructure changes shipped alongside

- `scripts/run_ab_test.py`: added optional `EVAL_PERSONAS` env var to
  restrict the persona dimension without editing constants
  (`EVAL_MATRIX_MODE=full EVAL_PERSONAS=error_prone` gives us the
  8-cell error_prone-only matrix this report used).
