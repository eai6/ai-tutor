# Changelog

This file records release-level changes — what's in each tagged
version, why, and the commit range it spans. Detail beyond the
summary lines below lives in the individual commit messages.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with the platform's API surface being the Django views + Container App
runtime contract (env vars, /health/, /tutor/, /api/v1/).

## [0.1.0] — 2026-05-29

Seychelles pilot release candidate. First proper versioned release of
the platform — establishes the "we can re-deploy this exact build"
contract via the `aitutor:v0.1.0` ACR image tag and the `v0.1.0` git
tag pointing at this changelog's commit.

### Added — simple_tutor engine

A second tutoring engine sitting alongside the legacy
`ConversationalTutor`, designed around a deterministic two-call
tool-use loop (Call 1 emits text + tool_use; Call 2 composes the
student-facing reply with tool_results in hand). Selected at request
time via the `SIMPLE_TUTOR_ENGINE` env var. **Both prod and staging
run simple_tutor at v0.1.0** (env vars `SIMPLE_TUTOR_ENGINE=on`,
`TUTORING_QUESTION_TYPES=mcq`); the legacy engine is still in the
codebase as a fallback (`SIMPLE_TUTOR_ENGINE=off`) but not active.

Concrete pieces:

- `InFlightQuestion` model + migration — persists the question state
  the LLM posed, so the next student turn is graded against an
  authoritative slot rather than chat-history inference.
- `pose_question` / `record_answer` tool pair — the LLM authors a
  question with its own reference; the platform handles the grade
  cycle.
- Two-call agentic loop (`apps/tutoring/simple_tutor/engine.py`) —
  eliminates the empty-tool-call-bubble failure mode the prior
  single-call loop produced.
- Deterministic intent classifier (`apps/tutoring/simple_tutor/intent.py`)
  — pure-regex pre-call labels every student message as
  `answer / clarification / pushback / off_topic / non_engagement /
  answer_or_other`. Flows into the prompt as `<message_intent>`
  guidance so the LLM routes conversationally on non-answer turns
  instead of force-grading.
- MCQ-only tutoring filter — `TUTORING_QUESTION_TYPES` env var
  (default `mcq`) narrows the question pool + the `pose_question`
  tool's `question_type` enum so the engine doesn't hit the
  short_answer "partial verdict" step-advance starvation observed
  on staging.
- Lightweight `submit_exit_ticket` path — bypasses ConversationalTutor
  cold-init for the submit flow; measured 0.51 s vs the prior 5–8 s.

### Added — systematic eval harness

- 81-scenario dataset under `evals/dataset/` covering math, geography,
  multi-turn, persona, cross-cutting, format, and pedagogy axes.
- Deterministic scorer (`evals/scorers/deterministic.py`) with new
  `meta_reasoning_leak` and `passive_ending` regex checks (universal
  voice rules; replaced the dropped `max_paragraphs` length check).
- LLM rubric scorer (`evals/scorers/llm_rubric.py`) — 8-dimension
  pedagogical judge plus per-scenario rubric items.
- Rule registry (`evals/rule_registry.py`) — every prompt rule maps
  to at least one eval check; lock-in test catches drift.
- Multi-dimensional judge with structured JSON output; mid-stream
  JSON-truncation repair so a max_tokens cap doesn't lose the score.
- `seed_inflight_question` block in scenario YAMLs — 48 of 60
  single-turn scenarios migrated so the engine starts in GRADE mode
  rather than spuriously re-posing.

Final eval result: **78 / 80 (97.5%) on the full suite** (60 single-turn
+ 20 multi-turn), with `actionability` / `tutor_tone` / `mistake_location`
all at 100%. Reports in `evals/reports/`.

### Added — staging environment

- `infra/Pulumi.staging.yaml` + `Pulumi.preview.yaml` cheap-er stack
  configs (B1ms Postgres + Consumption Container App).
- `.github/workflows/deploy-staging.yml` + `deploy-preview.yml` — push
  to `dev` deploys staging; push to `refactor/conversational-tutor-redesign`
  deploys preview (used by Roy's v2 engine work).
- Parameterized `infra/__main__.py` so a single Python file drives any
  stack (`pixel` prod, `staging`, `preview`).
- Staging Postgres seeded from prod; preview Postgres seeded from
  staging (table-subset via pg_dump --column-inserts, FK-safe).

### Added — content generation

- MCQ correct-answer distribution fix in
  `apps/tutoring/management/commands/generate_exit_tickets.py` — the
  format example previously used `"correct": "B"` literally, anchoring
  ~60.6% of all 7,073 existing MCQs on B (Lu et al. 2022 few-shot
  positional bias). New prompt uses an enum placeholder and asks the
  model to balance A/B/C/D distribution with a self-audit step.
  *Existing skewed content is NOT remediated here* — see the
  follow-up plan in `memory/`.

### Added — release infrastructure

- `VERSION` file at repo root; `/health/` returns `{version}` field.
- `CHANGELOG.md` (this file).
- `RELEASING.md` with the rollback recipe.
- Deploy workflow now also tags the ACR image with the git tag
  (`aitutor:v0.1.0` in addition to `aitutor:<sha>` and `aitutor:latest`)
  when triggered by a tag push.

### Added — UX polish for Seychelles government review

- Email-verify banner removed from `templates/base.html` +
  `templates/dashboard/base.html` — the soft nag was a normal-flow
  element above the chat tutor's `100dvh` container, pushing the
  "Type your answer…" input below the viewport fold. Banner template
  file kept on disk so it can be re-enabled later.

### Planning docs (memory/)

- `memory/portuguese_mozambique_pilot_plan.md` — five-PR plan for
  Mozambique pilot (next-week deliverable for Paschal's visit).
- `memory/simple_tutor_systematic_eval_plan.md` + audit / iteration
  reports under `evals/reports/`.

### Infra (not user-visible)

- Parameterized Pulumi infra accepts `aitutor:new-tutor` config key
  → `NEW_TUTOR` env var (used by Roy's preview stack only, not prod
  or staging).
- Preview Container App provisioned for the
  `refactor/conversational-tutor-redesign` branch (~$20–25/mo while
  live; `pulumi destroy --stack preview` to tear down).

### Commit-range reference

This release spans the 83 commits in `git log --oneline v0.1.0
^363267f` (i.e. commits on `main` between the prior reference point
and the v0.1.0 tag). For the auditable list:

```
git log --oneline 363267f..v0.1.0
```

---

## Earlier history

No prior versioned release. The `v1.0.0` git tag from 2026-02-01
points at a Flask-era prototype with committed `__pycache__/` and
`*.db` files — it was exploratory tagging, not a release, and was
removed when v0.1.0 was cut.
