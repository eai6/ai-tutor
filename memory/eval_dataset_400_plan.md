# Eval dataset expansion — 200 single-turn + 200 multi-turn

**Status**: approved 2026-07-13, implementation in flight.
**Supersedes**: the core15 stratified subset (deleted by this plan).

## Goal

Grow `evals/dataset/` from 90 scored scenarios (60 single-turn + 30 multi-turn)
to **400** (200 + 200), on a **representative** design, and remove the `core15`
subset entirely.

## Why

Two independent arguments, both load-bearing.

**Statistical power.** The binomial standard error on a pass rate at p≈0.5 is what
model-vs-model comparisons actually resolve against:

| n | SE | models must differ by ~this to be distinguishable |
|---|---|---|
| 15 (core15) | 12.9 pp | ~36 pp |
| 30 (sweep1 multi-turn) | 9.1 pp | ~25 pp |
| 60 (single-turn) | 6.5 pp | ~18 pp |
| 200 (this plan) | 3.5 pp | ~10 pp |

Sweep 2 already leaned on margins this can't support — it reported
gemini-2.5-flash as "net flat (+3/−3), the set of passing scenarios churned",
which is precisely what n=15 noise looks like. At n=200 a 3-scenario swing stops
being a reportable event.

**Representativeness.** The single-turn set is badly skewed. Of 24
persona×lesson cells, 8 are empty; `struggler×1137` alone holds 10 while
`error_prone` has **one scenario in the entire single-turn set**. Two of four
lessons carry 85% of the content. Any per-persona or per-lesson claim drawn from
this set is currently unsupported.

## Design

### Three axes, marginals balanced (not a rigid cell grid)

Every scenario is a point in `persona × lesson × archetype`. We balance the
**marginals** of each axis rather than filling a fixed cell grid.

This is the central design decision, and it is a reversal of the obvious approach.
A strict persona×lesson factorial with a cap of 2/cell would force retiring ~28
single-turn scenarios — but the over-full cells are *not* clones.
`struggler×1137`'s 10 scenarios are 10 different failure modes (idk, gives-up,
misreads, partial-working, help-request…). Capping the cell would destroy exactly
the archetype coverage that gives the eval its discriminative power. The
**archetype is the primary stratum**; persona and lesson are balanced underneath it.

### Allocation is deterministic Python; only content authoring is LLM

`evals/dataset/_matrix.py` solves the assignment first — 400 rows of
`(mode, persona, lesson, archetype, turn_budget)` — subject to the marginal
targets and a **persona-eligibility mask**. Authoring agents receive rows and fill
in content; they never choose their own persona/lesson/archetype.

Balance is therefore guaranteed *by construction*, not by 32 agents each
independently guessing what "representative" means.

### Persona-eligibility mask

The factorial must stay *plausible*, or it buys balance at the cost of realism.
A `non_responder` never gets `student_corrects_tutor`; a `capable` student never
gets `idk_non_answer`. Each archetype declares its eligible personas, and the
solver assigns only within the mask, then balances marginals as evenly as the mask
allows.

### Target marginals (per mode)

| axis | cardinality | target |
|---|---|---|
| persona | 6 | ~33 each (fixes `error_prone`: 1 → 33) |
| lesson | 16 | ~12–13 each |
| single-turn archetype | ~25 | ~8 each |
| multi-turn session shape | 12 | ~17 each |
| multi-turn turn budget | 6/12/15/24/30 | crossed; every persona meets both extremes |

### Single-turn archetypes (derived from the existing 60, plus rule-registry gaps)

`correct_bare_answer`, `correct_with_working`, `wrong_bare_answer`,
`wrong_with_working`, `arithmetic_slip`, `wrong_mcq`, `correct_mcq`,
`false_accept_guard`, `false_reject_guard`, `off_method_correct`,
`idk_non_answer`, `help_request`, `student_corrects_tutor`,
`clarifying_question`, `off_topic_redirect`, `safety_distress`,
`misread_question`, `terminology_confusion`, `gives_up_rapport`,
`repetition_banned_opener`, `tool_or_meta_leak`, `figure_ref_no_attachment`,
`premature_exit_ticket`, `ungrounded_factual_claim`,
`over_eager_working_request`, `student_pushes_to_advance`.

Every prompt rule R01–R17 in `evals/rule_registry.py` must be claimed by ≥1
archetype; the balance test enforces it.

### Multi-turn session shapes

`baseline_full_session`, `session_completion`, `speedrun`, `help_intensive`,
`refusal_chain`, `self_correction`, `remediation_after_exit_ticket_fail`,
`engagement_recovery`, `straight_line`, `error_cascade`, `long_session`,
`short_session`.

### Composition

**Single-turn 200** = 33 kept in place + 27 **ported** + 140 newly authored.
Porting = an over-concentrated `1137`/`1463` scenario re-grounded onto a new
lesson, preserving persona + archetype + rubric intent and swapping only the
content. Nothing is discarded.

**Multi-turn 200** = 30 kept + 170 newly authored. No ports, no retirements —
multi-turn was already near-balanced (2 empty cells, +2 over cap); it only grows.

At n=200 every persona's current count is *under* its target, so the larger
target absorbs the existing skew without deleting tested work. This is a property
of the 400 target specifically; at 100/100 retirements would have been forced.

## Fixtures

Regenerate at `python evals/fixtures/extract.py --per-course-limit 8` →
**16 lessons** (8 math, 8 geography), verified extractable: 120 steps, 17 exit
tickets, 821 exit-ticket questions.

- Math (course 15): angles around a point (1137), angles on a straight line /
  intersecting (1138), angles in parallel lines (1139), probability complement
  (1141), + 4 more probability lessons (1142–1145).
- Geography (course 18): map scale (1463), direction/bearing (1464), grid
  references (1465), measuring distances (1466), landscape description (1467),
  river channels (1468), weathering (1469, 1470).

Gains real topical diversity, not just more of the same: math picks up parallel
lines and probability; geography picks up grid references and distance
measurement. At 400 scenarios this holds density at ~25/lesson.

## core15 removal

- Strip the `core15` tag from the 15 multi-turn YAMLs.
- Remove the blessed-subset language from `apps/tutoring/management/commands/run_eval.py`,
  `offline_eval/_make_colab_nb.py`, `offline_eval/colab_eval.ipynb`.
- Keep the generic `--subset <tag>` filter.
- Add `--sample N --seed S` — a reproducible random draw. This is the honest
  replacement: it cuts wall-clock without a hardcoded 15 pretending to be the
  whole distribution.

## Enforcement

- `evals/lint_dataset.py` (extends `offline_eval/lint_multi_turn.py`) — schema,
  rubric completeness, BEA standard block present, `seed_inflight_question`
  present on every grading archetype.
- `evals/test_dataset_balance.py` — asserts the marginals and the archetype
  floors, and that every rule R01–R17 is claimed. The dataset cannot silently
  re-skew.

## Costs, accepted deliberately

**A full 200-scenario multi-turn sweep is ~40× core15** (~13× sweep1). At
sweep2's ~14 turns/session, two LLM calls per turn plus judging, across a
16-model sweep this is a materially different budget and wall-clock. `--sample`
is the intended day-to-day mode; the full 200 is for publication runs.

**No prior sweep number stays comparable.** Single-turn 60 → 200 and multi-turn
15/30 → 200 change both the denominator and the content (new lessons). **Sweep 3
is a new baseline and the sweep1/sweep2 trend lines end there.** Taken once,
deliberately, rather than in pieces.

## Open questions

None blocking. Two to revisit after the first full run:

1. Whether 25 single-turn archetypes is the right granularity, or whether the
   rarer guards (`figure_ref_no_attachment`, `ungrounded_factual_claim`) should
   collapse into a single `integrity_guard` archetype — decide on observed
   per-archetype discriminative power, not a priori.
2. Whether the 12 multi-turn shapes over-weight terminal states (`completion`,
   `remediation`) relative to what production sessions actually do. Answerable
   from prod `TutorSession` data; deferred because this eval is for ranking, not
   for estimating the production pass rate.

Refs: memory/eval_harness_plan.md, memory/multi_turn_eval_v1_plan.md, memory/simple_tutor_systematic_eval_plan.md

Note: CLAUDE.md cites `memory/eval_benchmark_v2_simplified.md` and
`memory/agentic_platform_architecture_plan.md`. Neither file exists in the repo
as of 2026-07-13 — the CLAUDE.md references are stale and should be corrected or
the files restored.

