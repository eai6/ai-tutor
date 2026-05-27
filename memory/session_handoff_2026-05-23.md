# Session handoff — 2026-05-23 (eval pivot + BEA rubric)

For the next Claude. Read this first to pick up where the prior session
left off. Most context is captured in commits + PR bodies + the plan
files already in `memory/`. This doc points to the live state.

## TL;DR

The user pivoted from "build CI eval workflow" → "evaluate the merged
PR empirically against the paper" → "the rubric should align with
BEA-2025 Shared Task". Two PRs landed this session. The current open
work is **rewriting the judge from a 10-principle 0–5 rubric to the
BEA-2025 4-dimension Yes/Somewhat/No taxonomy**. The user has signed
off on **replace + re-judge all baselines** but is currently reviewing
the rubric draft before I build.

## What happened, in order

1. **PR #7 merged** (Manzia's Science-of-Learning Tier 1 + A/B cycles + deploy-time prompt/model env vars). I resolved migration conflict (renamed PR's `0029` → `0031`, pointed at `0030`) and fixed an `institution_id=12` hardcode bug in my own `0030`. Commit `2788b68`.
2. **Deployed**: run `26337546649` succeeded. Prod ModelConfig now: Opus tutoring, **Sonnet regen** (PR's choice, was Opus on my path), Gemini Flash Lite judge → OpenAI gpt-4o-mini → Haiku 4.5 cascade.
3. **Local Postgres question** → user pushed back on Docker → built `apps/curriculum/management/commands/load_prod_dump.py` that parses pg_dump COPY blocks via Django ORM into SQLite. Loaded `prod_content_dump.sql` (~12k rows) cleanly into local SQLite.
4. **Phase 1 baseline run** — 8 cells (Sonnet+Gemini × L1137+L1425 × struggler+capable). Output `ab-test-reports-baseline-2026-05-23/`. Synthesised review at `eval-reports/baseline-2026-05-23.md`. Overall judge mean 3.09/5 (Sonnet 3.20, Gemini 2.98).
5. **PR #8 opened** (`post-deploy-eval-workflow` branch) — wires the eval pipeline as a tail job on `deploy.yml`. Commits results to a long-lived `eval-reports` branch under `runs/<sha>-<mode>/`. Not yet merged. Pre-merge: user needs to set `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY` as repo secrets.
6. **v6 baseline run + writeup** — 4 cells Sonnet-only first, then filled in 4 Gemini cells. Surfaced an important finding: v6 judge score shifted 2.95 → 3.17 just by re-running the judge (LLM non-determinism at temp=0 is ~±0.2 on 4-cell means).
7. **v7 full run** + parallel v6 Gemini fill-in. Both completed; v6 judge done; **v7 judge still running** (7/8 transcripts scored as of last check).
8. **User shared BEA-2025 Shared Task page** and said "the judge rubric should follow this kind of tutor evaluation". I drafted the rubric spec and presented for review. User signed off on the design decisions ("Replace 10-principle with BEA" + "re-judge all baselines") but **the actual rubric draft is what they're now reviewing**. They have NOT yet said "build it".

## Current state — verify before acting

| Thing | Location | Status |
|---|---|---|
| Local branch | `post-deploy-eval-workflow` | clean working tree (verify with `git status`) |
| PRs | #7 merged · #8 open | check `gh pr list` |
| Prod | revision `aitutor-pixel-app--0000491`-ish, image `2788b68...` | check `az containerapp revision list` |
| `eval-reports` branch on origin | does NOT exist yet | will be created by first CI run after #8 merges |
| Background tasks | none active — v7 judge finished as the handoff was being written | all 24 transcripts now have 10-principle judge scores (full old-rubric snapshot before the BEA switch) |
| Loaded curriculum locally | 354 lessons in SQLite | L1137 (10 steps) + L1425 (5 steps) confirmed |

Verify with:

```bash
git branch --show-current
git status
gh pr view 8
ls ab-test-reports-baseline-2026-05-23/judge_scores/  # should be 8 files
ls ab-test-reports-v6-2026-05-23/judge_scores/        # should be 8 files
ls ab-test-reports-v7-2026-05-23/judge_scores/        # 7 or 8 by the time you check
```

## The pending BEA judge build

User said yes to:
1. **Replace** the 10-principle judge with BEA-aligned
2. **Re-judge all three** baselines (v3, v6, v7) under the new rubric for proper comparison

Rubric I presented for review (full text in conversation history; key points):

- **Unit of analysis**: per tutor turn, when preceding student turn contained a mistake or confusion
- **4 dimensions** (BEA-2025 verbatim): Mistake Identification · Mistake Location · Providing Guidance · Actionability
- **3-class labels**: `Yes` / `To some extent` / `No`
- **Aggregates**: per-dim exact and lenient (Yes + Somewhat) pass rates; overall strict and lenient pass rates
- **Skip-criteria**: first tutor turn, or tutor turns following a non-substantive student reply ("ok", "yes")

Decisions deferred to user (asked but no answer yet):
1. Strict vs lenient as headline metric (BEA leaderboard uses strict F1; recommend strict for parity)
2. Threshold for "confusion" — only clear mistakes, or also vague confusion?
3. Add a complementary tutor-turn shape classifier (`question_only`, `teach_then_question`, `silent_advance`)?
4. Token budget — re-judging 24 transcripts ≈ $15-25 in Opus calls; confirm OK

## What to build (when user greenlights)

1. **`scripts/judge_transcripts_bea.py`** — replaces `judge_transcripts.py`. Per-transcript Opus call returning per-tutor-turn BEA evaluations as structured JSON.
2. **Update `scripts/generate_reports.py`** for the new score schema. Replace 10-principle tables with BEA per-dim and strict/lenient pass rates.
3. **Update `.github/workflows/deploy.yml::post_deploy_eval`** to invoke the new judge script.
4. **Update `eval-reports/README.md`** to describe BEA methodology, cite Maurya et al. 2025 + BEA-2025 Shared Task.
5. **Re-judge all 24 transcripts** under BEA: `ab-test-reports-baseline-2026-05-23/`, `ab-test-reports-v6-2026-05-23/`, `ab-test-reports-v7-2026-05-23/`. ~12 min wall, ~$15-25.
6. **Write `eval-reports/bea-comparison-2026-05-23.md`** — 3-way v3/v6/v7 comparison under BEA.
7. **Delete `scripts/judge_transcripts.py`** (the 10-principle version) per user's "replace" choice.
8. **Keep `eval-reports/baseline-2026-05-23.md` and `v6-baseline-2026-05-23.md`** but add a note at the top: "Methodology superseded by BEA-2025 rubric — see `bea-comparison-2026-05-23.md`".

## Important findings to surface

- **Judge non-determinism is large** even at temp=0. v6 Sonnet 4-cell mean shifted 2.95 → 3.17 on a re-judge. This means small-sample comparisons (4-cell deploy-mode) have noise ~±0.2. Be honest about this in any writeup.
- **The PR's reported v6=3.27 was measured against an engine WITHOUT the filler-reply guard**; our re-measurement was with all 5 guards present. The "v6 regression" I initially flagged was mostly noise + engine drift, not a genuine prompt failure.
- **v7's prompt was tuned against the same final engine we have now**, so v7 is the cleanest apples-to-apples comparison candidate.
- **`turns=7` is uniform across all 24 cells.** Suspect undercount — the transcripts visibly contain more student-tutor exchanges. Spot-check confirms: at least 10 visible exchanges in `sonnet-4_L1137_capable.md`. The `turns` field needs a one-line fix in `apps/tutoring/student_sim/driver.py` (haven't done it; noted in `baseline-2026-05-23.md` as a suspect signal).

## Key files (don't re-derive these)

| File | Purpose |
|---|---|
| `apps/curriculum/management/commands/load_prod_dump.py` | Generic pg_dump → SQLite loader via Django ORM |
| `scripts/extract_eval_fixture.py` | Extracts L1137 + L1425 + deps as a Django fixture |
| `scripts/seed_eval_modelconfig.py` | Seeds judge / regen / tutoring ModelConfigs on fresh CI DB |
| `scripts/run_ab_test.py` | Simulator runner (added `EVAL_MATRIX_MODE=deploy|full` env var) |
| `scripts/judge_transcripts.py` | **Will be replaced** — current 10-principle judge |
| `scripts/generate_reports.py` | **Will be modified** — aggregator |
| `eval-fixtures/baseline.json` | 94-row fixture committed to repo |
| `eval-reports/README.md` | Split-store explanation (main vs `eval-reports` branch) |
| `eval-reports/baseline-2026-05-23.md` | v3 baseline review (10-principle rubric) |
| `eval-reports/v6-baseline-2026-05-23.md` | v6 review (10-principle rubric) |
| `.github/workflows/deploy.yml::post_deploy_eval` | Tail-job that runs the eval |
| `memory/tutor_engine_redesign_plan.md` | Bigger structural redesign plan (separate work track) |

## Cross-references

- `memory/tutor_engine_redesign_plan.md` — engine decomposition + intent classifier + tool-using grader + council judges + single-shot regen. **Separate work track from this session's eval work.** The eval pipeline measures progress on that plan when phases ship.
- `memory/tutor_responsiveness_plan.md` — small responsiveness fixes; older plan.
- BEA 2025 Shared Task: <https://sig-edu.org/sharedtask/2025>
- Dataset paper: Maurya et al. 2025 — "Unifying AI Tutor Evaluation: An Evaluation Taxonomy for Pedagogical Ability Assessment of LLM-Powered AI Tutors" (NAACL 2025)
- Paper draft (in user's research): cites Maurya et al. for the auditor dimensions; reports 84.3% pass rate (math 71.4%, geo 97.1%) — that pass rate is per-turn, BEA-style, which is why we're switching the judge.

## Posture for next session

- The user has been decisive and moves fast. Don't over-ask; pick reasonable defaults and confirm in line.
- They redlined Docker → use SQLite for everything local. Honour that.
- They want eval results that are publication-comparable to the paper. The 10-principle judge wasn't that; BEA is.
- The PR #8 workflow is ready to merge; pre-req is the repo secrets. Don't merge it yourself.
- The redesign-plan branch (`tutor-engine-redesign`) is parked; future work, not active.

Refs:
  - auto-memory/feedback_dev_collaboration.md (don't auto-commit, confirm direction shifts)
  - 2788b68 (the merged PR #7)
  - c3164c5 (the open PR #8 commit)
