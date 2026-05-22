# A/B Testing Plan: Validating Science-of-Learning Improvements

> Companion to `SCIENCE_LEARNING_AUDIT_v3.md`. Defines what we need to run
> empirically-grounded `(prompt × model)` experiments locally, and where
> deployed-system access is and isn't required.

---

## Why this document exists

The v3 audit recommends several changes whose impact is *qualitative*
(prompt rewrites, model swaps, scaffolding fades). Unit tests verify
they don't break the engine; they don't verify the *student-facing
quality* improves. For that we need a transcript-level A/B harness.

The infrastructure for this already exists in the repo
(`apps/tutoring/management/commands/simulate_session.py` and
`run_model_experiment.py`). What's missing is the **data** and the
**scoring layer** — neither of which require deployed-system access in
the conventional sense.

---

## What we need for a valid A/B test

Three independent requirements. Only one is a real bottleneck.

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
read query against the production DB scoped to a few tables (Section 2
below).

### Requirement 2 — API keys for the models under test

Whatever models we want to compare must have credentials in the local
environment:

| Model | Env var | Status |
|-------|---------|--------|
| Anthropic Claude (Opus 4.7, Sonnet 4) | `ANTHROPIC_API_KEY` | ✅ in `.env` |
| Google Gemini (Flash, Flash Lite, Pro) | `GOOGLE_API_KEY` | ❌ missing |
| OpenAI (optional, if expanding scope) | `OPENAI_API_KEY` | ❌ missing |

**Important**: the most informative *first* comparison is **current
prompt vs v3 prompt on the same model**. That isolates the prompt
effect from the model effect, which is the question audit v3 H2
actually asks. This comparison needs only `ANTHROPIC_API_KEY`.

Adding `GOOGLE_API_KEY` (free tier at https://aistudio.google.com,
~5 minutes) unlocks the cross-model comparison the audit's H1
discussed. Now that H1 is invalidated (regression predates the
2026-05-20 model swap), the cross-model comparison is lower priority
than the cross-prompt one.

### Requirement 3 — A scorer for the transcripts

The harness produces transcripts; converting them to a comparable
score requires a rubric:

| Option | What it is | Best for |
|--------|-----------|----------|
| **`evaluate-tutor` skill** in `.claude/skills/evaluate-tutor/` | Designed exactly for this. Scores transcripts against science-of-learning principles. | The qualitative principle compliance |
| **LLM-as-judge inline script** (~60 lines of Python) | Asks Opus/Sonnet to score 0-5 per principle on a transcript, returns JSON | Custom rubrics, batch scoring |
| **Programmatic counters** | Math-error rate (via `audit_math_false_positives`), turn-length distribution, % turns ending with a question | The hard metrics |

The right answer is a mix: programmatic for the hard metrics that have
deterministic ground truth (math accuracy, turn lengths, tool-use
compliance), and LLM-as-judge for the soft ones (active-learning
ratio, scaffolding quality, layering frequency).

---

## Do we need access to the deployed main backend?

**For running the experiment itself: no.** `run_model_experiment.py`
runs entirely locally — it talks to the LLM APIs directly, writes
transcripts to `memory/`, and never touches production.

**For getting representative lesson content: yes — but only DB *read*
access, not *deploy* access.** This is the single ask of the deployed
system. A `pg_dump` (or Django `dumpdata` equivalent) of 5-10 lessons,
scoped to a handful of tables, is enough. No code is being shipped,
no traffic is routed, no service is restarted.

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
~10-50 MB. Drop it into the local SQLite via the equivalent
`loaddata` command after light SQL massaging (pg→sqlite type
differences are minor for content tables).

**For grounding the scorer against a real-world baseline: optional.**
Exporting 20-50 recent `SessionTurn` + `TutorSession` rows gives the
scorer a reference for "what does today's tutor actually produce on
real student inputs?" Useful for calibration but not blocking.

---

## The simple, quick path

Two days. One external dependency (a teammate running one `pg_dump`).

| Day | Step | Output |
|-----|------|--------|
| 1 AM | Request the content dump (Section 2). Verify locally that `manage.py loaddata` or psql import works. | `db.sqlite3` with 5-10 real lessons |
| 1 PM | Run `manage.py simulate_session --lesson <id> --persona struggler` once with the current prompt to confirm the pipeline works end-to-end against real content. Fix any environment issues. | One working baseline transcript |
| 2 AM | Add v3 prompt as a feature-flagged alternate (env var or DB flag). Run `manage.py run_model_experiment --models opus-current-prompt,opus-v3-prompt --lessons <5-10 IDs> --personas struggler,bright,careless`. | ~30 transcripts saved under `memory/` |
| 2 PM | Score every transcript with the `evaluate-tutor` skill + programmatic counters. Aggregate: `prompt × persona × principle → mean score`. Decide on R2 based on the table. | A before/after comparison committed to `design/AB_RESULTS_<DATE>.md` |

**Compute cost estimate**: ~30 transcripts × ~30 turns × ~$0.05/turn
(Opus) ≈ ~$45. Scoring: ~30 × $0.10 ≈ $3. **Total under $50.**

**What this gives**: a defensible answer to *"does the v3 prompt
improve science-of-learning compliance on real lesson content?"*, in
two days, with one external dependency.

**What this does NOT give**:

- *Long-horizon effects* — SM-2 spaced repetition only pays off across
  multi-day sessions. Local synthetic runs verify the schedule is
  written correctly but not that retention improves.
- *Real-student distribution of misconceptions* — synthetic personas
  cover the broad strokes (struggler, bright, careless) but real
  students fail in long-tail ways. Cloud canary remains the final
  check before broad rollout.
- *Mobile UX regressions* — runs through the engine, not the rendered
  chat. Browser-load testing per `LOCAL_TESTING_GUIDE.md §5` is still
  needed for any UI-touching change.

---

## Which v3 recommendations need this A/B before shipping

| Recommendation | Needs A/B? | Why |
|----------------|-----------|-----|
| R1 — Roll back tutoring to Opus 4.7 | No | Code change is a data migration with a clear reverse. Risk is *production traffic cost / latency*, not quality. |
| R2 — Slim v3 system prompt | **YES** | Quality is the whole point. Cannot be verified by unit tests alone. |
| R3 — LLM concept coverage on per-turn path | No | Unit tests verify the gate logic. Cost is bounded (rare event). |
| R4 — `Course.subject_code` backfill | No | Pure data-correctness change. |
| R5 — Tighter algebra filter | No | Unit tests cover the regex change. |
| R6+ — Tier 2/3 items (automaticity, daily review, etc.) | Per item | Most are new features, not behavior changes — ship with feature tests, validate quality post-rollout. |

**Decision rule**: behavior changes that affect what the tutor *says*
to the student need this A/B. Plumbing changes that affect *what data
the engine sees* don't.

---

## Next step

Identify someone with production DB read access and request the dump
described in Section 2. While waiting, ship `feature/science-learning-tier1-v3`
(R1+R3+R4+R5) since none of those need this A/B. Open the R2 PR as a
follow-up after the 2-day experiment lands.

If a Google API key is added at any point, re-run the experiment with
`--models opus-current-prompt,opus-v3-prompt,gemini-flash-v3-prompt`
to revisit H1 with empirical data rather than the speculation that
just got walked back.
