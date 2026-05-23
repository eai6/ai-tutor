# A/B Testing Plan: Generating Recommendations to Improve the Tutoring System Prompt

> Companion to `SCIENCE_LEARNING_AUDIT_v3.md`. Defines an empirical loop
> for **improving the tutoring system prompt** (and adjacent flow /
> experience choices) by running `(prompt × model × lesson × persona)`
> cells locally and harvesting prescriptive recommendations from a
> structured LLM-as-judge.

---

## What this exercise is — and is **not**

**This A/B harness exists to produce prescriptive recommendations for
improving the tutoring system prompt.** Read this twice; it's the
single most-violated rule in past runs.

- ✅ The **unit of variation** is the *system prompt* (current vs. v3
  vs. proposed future variants). Running multiple models is a
  *robustness check* — does a prompt improvement hold across
  providers? — not a model bake-off.
- ✅ The **primary artefact** is a ranked, actionable list of changes
  to the system prompt, plus secondary recommendations on flow,
  scaffolding, and student experience. The science-of-learning rubric
  scores are inputs to that synthesis, not the output.
- ❌ This is **not** a model evaluation. We are not picking a "winner"
  among Anthropic / Google / etc. Model selection is governed by
  `apps/llm/models.py::ModelConfig` for production reasons (cost,
  latency, capability per purpose) that have nothing to do with this
  harness.
- ❌ Per-model rankings ("model X scored 2.98, model Y scored 2.93") in
  the final report are **not** the headline result and should not be
  framed as such. They exist only to detect whether a prompt change
  is model-robust or model-specific.

If a future run produces a "FINAL_REPORT.md" whose headline is "Winner
by judge mean: model X", it has misread this document.

---

## Why this matters now

`SCIENCE_LEARNING_AUDIT_v3.md` recommends a slim v3 system prompt (R2)
plus several adjacent flow changes (R6+ — interleaving, prereq
routing, daily review). These recommendations are *informed* by
manual transcript review but not *validated* at scale. The harness
closes the loop: produce transcripts → judge them against the
science-of-learning rubric **and** extract prescriptive edits → feed
those back into the next prompt revision.

---

## What we need for a valid A/B run

Three independent requirements.

### Requirement 1 — Realistic lesson content in the local DB

The experiment harness drives a real tutoring session through the
engine. For the resulting transcript to be representative of production
behavior, the local DB must hold lessons with the structured content
the engine reads:

- `Lesson` rows with `is_published=True`, real titles + objectives
- `LessonStep` rows with populated `educational_content` (especially
  `worked_example` payloads with labelled subgoals)
- `ExitTicket` + `ExitTicketQuestion` rows
- `LessonPrerequisite` edges
- Optional: media catalog entries

The default `seed_seychelles` command produces **skeletal** lessons (1
empty `teach` step per lesson, no `educational_content`, no question
banks, no exit tickets). Running an experiment against that content
tests the LLM's *default behavior on a thin prompt*, not the production
tutor pipeline.

Three ways to get rich content locally, ranked by cost / realism:

| Path | Effort | Cost | Realism |
|------|--------|------|---------|
| **Production DB dump → import locally** | 30 min (read-only DB query) | $0 | ★★★★★ |
| **Run `manage.py run_full_pipeline` locally** on 3-5 chosen lessons | 1-2h | $5-15 in Anthropic API | ★★★★ |
| **Hand-curate** 2 lessons with full content | days | $0 | ★★★ |

**Recommended**: production DB dump. It needs no deploy access — just a
read query against the production DB scoped to a few tables (Section
below).

### Requirement 2 — API keys for the models under test

The supported model providers for this harness are **Anthropic** and
**Google**. OpenAI is explicitly out of scope (see "Removed: OpenAI"
below).

| Model | Env var | Status |
|-------|---------|--------|
| Anthropic Claude (Opus 4.7, Sonnet 4) | `ANTHROPIC_API_KEY` | ✅ in `.env` |
| Google Gemini (Flash, Flash Lite, Pro) | `GOOGLE_API_KEY` | required if running Gemini cells |

The most informative comparison is **current prompt vs v3 prompt on
the same model** — it isolates the prompt effect, which is the only
variable we can actually act on. Cross-model runs come second, as a
robustness check on prompt changes.

#### Removed: OpenAI

OpenAI / GPT models are not part of this testing plan. Reasons:

1. The recent GPT-4o-mini cells failed with 0% tool-use rate
   (structural incompatibility between this engine's tool-calling
   shape and the OpenAI path) — the cells produced no usable signal.
2. Production tutoring is dispatched through `ModelConfig` to
   Anthropic and Google providers; GPT is not on the deployment path
   for this purpose.
3. Including GPT cells dilutes the budget for the comparison that
   actually matters (cross-prompt within the deployed providers).

If OpenAI re-enters the tutoring path in future, treat that as a
separate, scoped exercise — debug `apps/llm/client.py` OpenAI tool
schema first, then add it back here.

### Requirement 3 — A structured-recommendation judge

The harness produces transcripts; converting them to *recommendations*
requires a judge that does two things in one pass:

1. **Scores** the transcript 0–5 on each of the 10 science-of-learning
   principles (rubric from `.claude/skills/evaluate-tutor/SKILL.md`).
2. **Prescribes** changes — concrete, actionable recommendations for
   improving the system prompt, flow, and experience, each grounded
   in a quoted transcript turn.

The judge output schema is **non-negotiable** — downstream aggregation
depends on it. See `scripts/judge_transcripts.py::build_prompt` for
the authoritative template; high-level shape:

```jsonc
{
  "scores": { "<principle>": { "score": 0-5, "evidence": "…" }, … },
  "strongest_behaviors": [str, …],
  "weakest_behaviors": [str, …],
  "prompt_recommendations": [
    {
      "title": "Short imperative ('Forbid two consecutive teach blocks')",
      "rationale": "Why — tied to which principle/failure mode",
      "evidence_quote": "Verbatim excerpt from the transcript",
      "evidence_turn": "TUTOR turn id or section reference",
      "suggested_prompt_edit": "Concrete language to add/change in the system prompt",
      "expected_effect": "What measurable behavior should improve",
      "severity": "high|medium|low"
    }
  ],
  "flow_recommendations":      [ /* same schema, fixes target engine/flow, not the prompt */ ],
  "experience_recommendations":[ /* same schema, fixes target student UX/scaffolding */ ],
  "overall_summary": "2-3 sentences"
}
```

The aggregator (`scripts/generate_reports.py`) clusters
recommendations across cells, deduplicates near-duplicates, and ranks
by (frequency × severity) so the final report leads with the most
load-bearing prompt edits.

Programmatic counters (math-error rate, turn-length distribution,
% turns ending with a question, regen-cycles-exhausted) remain
captured as supplementary inputs but are **not** the report headline.

---

## Do we need access to the deployed main backend?

**For running the experiment itself: no.** The runner (`scripts/run_ab_test.py`)
runs entirely locally — it talks to the LLM APIs directly, writes
transcripts to `ab-test-reports/`, and never touches production.

**For getting representative lesson content: yes — but only DB *read*
access, not *deploy* access.** A `pg_dump` (or Django `dumpdata`
equivalent) of 5-10 lessons, scoped to a handful of tables, is enough.

The minimum scope:

```bash
pg_dump --data-only \
  --table=curriculum_course \
  --table=curriculum_unit \
  --table=curriculum_lesson \
  --table=curriculum_lessonstep \
  --table=tutoring_exitticket \
  --table=tutoring_exitticketquestion \
  --table=curriculum_lessonprerequisite \
  > prod_content_dump.sql
```

…then filter to ~5-10 representative lesson IDs by hand (a mix of
S1-S5, math + geography, varied step types). Resulting dump is
~10-50 MB.

---

## The run loop (one cycle = one prompt iteration)

Each cycle of the loop revises the tutoring system prompt once.

| Phase | Step | Output |
|-------|------|--------|
| 1. **Baseline** | Run matrix with the *current* system prompt across both supported models, the chosen lessons, both personas. | `ab-test-reports/cell_results.jsonl` + transcripts |
| 2. **Score & prescribe** | Run `scripts/judge_transcripts.py` to score every transcript and extract structured recommendations. | `judge_scores/<key>.json` per cell |
| 3. **Synthesise** | Run `scripts/generate_reports.py` to cluster recommendations across cells, rank by (frequency × severity), and emit `FINAL_REPORT.md`. | Ranked recommendation list |
| 4. **Triage** | Human reviewer picks the top 3–5 recommendations to apply. Document the rationale; note which were deferred and why. | A prompt patch + a deferred-items list |
| 5. **Apply** | Edit the tutoring system prompt with the chosen recommendations. Commit the new prompt as a named variant (e.g., `v4`). | Updated prompt in source |
| 6. **Re-run** | Re-run the matrix with the new prompt variant. Compare aggregate principle scores and recommendation counts to baseline. | Did the targeted principles improve? Did new failure modes emerge? |

Two passes through the loop is the typical floor; three is realistic
for a prompt revision that materially moves the rubric.

**Compute cost estimate per cycle**: ~8 cells × ~30 turns × ~$0.05/turn
≈ ~$12. Judging: ~8 cells × ~$0.10 ≈ $1. **Under $15 per cycle.**

---

## The matrix

After removing OpenAI:

- **Models** (robustness axis, not evaluation): Anthropic Claude
  Sonnet 4, Google Gemini 3 Flash.
- **Lessons** (subject coverage): L1137 (Math — Angles around a
  point), L1425 (Geography — Map Scale and Map Types). Expand as
  needed.
- **Personas** (student variation): `struggler`, `capable`.

Default cell count: 2 × 2 × 2 = **8 cells per prompt variant**. Two
prompt variants per cycle (current vs. proposed) → 16 cells per
cycle.

---

## What this loop gives us — and what it doesn't

**Gives**:

- A ranked, actionable list of system-prompt edits each cycle,
  grounded in transcript evidence.
- A measurable signal on whether a prompt change moves the
  science-of-learning rubric in the targeted direction.
- A cross-model robustness check on each prompt edit.

**Doesn't give**:

- *Long-horizon effects* — SM-2 spaced repetition only pays off
  across multi-day sessions. Local synthetic runs verify the schedule
  is written correctly but not that retention improves.
- *Real-student distribution of misconceptions* — synthetic personas
  cover the broad strokes (struggler, capable) but real students fail
  in long-tail ways. Cloud canary remains the final check before
  broad rollout.
- *Mobile UX regressions* — runs through the engine, not the rendered
  chat. Browser-load testing per `LOCAL_TESTING_GUIDE.md §5` is still
  needed for any UI-touching change.

---

## Which v3 audit items run through this harness

| Recommendation | Use this harness? | Why |
|----------------|------------------|-----|
| R1 — Roll back tutoring to Opus 4.7 | No | Code change is a data migration with a clear reverse. Risk is *production traffic cost / latency*, not quality. |
| R2 — Slim v3 system prompt | **YES — this is the canonical case** | Quality is the whole point. Recommendations from the judge feed directly into the next prompt revision. |
| R3 — LLM concept coverage on per-turn path | No | Unit tests verify the gate logic. Cost is bounded (rare event). |
| R4 — `Course.subject_code` backfill | No | Pure data-correctness change. |
| R5 — Tighter algebra filter | No | Unit tests cover the regex change. |
| R6+ — Tier 2/3 items (automaticity, interleaving, daily review, prereq routing) | **YES** for the prompt-side pieces; **partly** for the flow pieces (the judge's `flow_recommendations` block is the input) | Anything that changes what the tutor *says* to the student needs the harness. |

**Decision rule**: behavior changes that affect what the tutor *says*
to the student need this A/B. Plumbing changes that affect *what data
the engine sees* don't.

---

## Next step

Run one full cycle against the current prompt to populate baseline
recommendations. Apply the top-ranked subset, ship as v4, re-run.
Track cycle-over-cycle delta in `design/AB_RESULTS_<DATE>.md`.
