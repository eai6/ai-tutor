# Developer Guide — AI Tutor Platform

`[STAFF]` Architecture + how-to reference for engineers working on
the codebase. Indexed into the help-assistant KB so the assistant
can answer "how was this built" / "where does X live" questions.

For per-app deep dives, see the README under each app
(`apps/<name>/README.md`). This doc is the high-level glue.

---

## Stack at a glance

- **Backend** — Django 5, Python 3.11, PostgreSQL (prod) /
  SQLite (dev), Gunicorn 4 workers × 4 threads
- **LLM layer** — `apps/llm/client.py::BaseLLMClient` wraps
  Anthropic / OpenAI / Google / Ollama with a uniform interface.
  `apps/llm/models.ModelConfig` per-purpose row picks which model
  serves which feature.
- **Vector DB** — ChromaDB + sentence-transformers
  (`all-MiniLM-L6-v2`, offline). Two collections:
  curriculum (`curriculum_<institution_id>`) and help docs
  (`help_docs`, platform-wide). Lives at `VECTORDB_ROOT`
  (`/tmp/vectordb` on Azure).
- **Image generation** — OpenAI `gpt-image-2` (primary) +
  Gemini fallback. The 35 SVG figure templates that used to
  exist are dead code.
- **Deploy** — Azure Container Apps (Pixel Design Labs LLC
  subscription), Pulumi IaC (`infra/__main__.py`, stack `pixel`),
  GitHub Actions on push to `main`. Mac builds need
  `--platform linux/amd64` or use CI.

---

## App boundaries

```
apps/
  accounts/      auth, Institution, StudentProfile, Membership
  curriculum/    Course → Unit → Lesson → LessonStep, KnowledgeBase,
                 ContentGenerator, parser pipeline
  tutoring/      TutorSession + ConversationalTutor engine,
                 judges/, regen/, validator, exit ticket attempts,
                 competency tracker
  dashboard/     teacher / super-admin views + templates
  llm/           BaseLLMClient + ModelConfig per-purpose router
  media_library/ MediaAsset (images, attached figures)
  safety/        ContentSafetyFilter (regex PII), audit log,
                 rate limiting, GDPR consent + data export
  support/       help assistant (KB, services, tools, models)
  exam/          summative exam + bank generation
  bug_reports/   feedback ticket model + email pipeline
```

Boundaries are fluid; the canonical separation is:
- **curriculum** owns content authoring + parsing.
- **tutoring** owns the live student session.
- **dashboard** is read-only(ish) over both.

---

## ConversationalTutor — the engine

`apps/tutoring/conversational_tutor.py` is the heart. ~8000 lines
spanning prompt building, math grading, judge orchestration, regen,
session state, group sessions, audio mode, gamification.

### Lifecycle of one student turn

```
1. tutor.respond(student_input)
2. Deterministic math check (apps/tutoring/grader.py)
   → MathCheckResult is_correct True/False/None
3. Build system prompt: lesson context + media catalog +
   bank stems + recent conversation + step directives
   + evaluation_signal (when math_check fired)
4. LLM call to llm_client (Sonnet by default)
5. Parse |||MEDIA:N||| signal → clean_response, parsed_media
6. run_combined_judge (= run_all_judges concurrent orchestrator)
   → CombinedJudgeResult with arithmetic / factual / rule /
     step_eval / coherence / figure_ref / figure_vision / safety
7. validate_tutor_response → ValidationResult
   (issues + needs_regeneration)
8. If needs_regeneration:
     run_regen_ensemble → focused prompt × N concurrent models
     × cycle loop with temperature decay → best clean candidate
     (or stock fallback)
9. Save SessionTurn + update engine_state
10. Return TutorResult with content + media + step_number
```

### Key state on `ConversationalTutor`

- `self.session` — `TutorSession` row (DB-backed)
- `self.engine_state` — JSONField on the session that survives
  reloads. Holds `exchange_count`, `cognitive_load`,
  `display_phase`, `current_step_index`, etc.
- `self.steps` — list of `LessonStep` for this lesson
- `self._media_id_map` — `{int: media_dict}` for `|||MEDIA:N|||`
  resolution
- `self._pending_math_check` — set by `_deterministic_math_check`
  before the LLM call so the system prompt can inject
  `<evaluation_signal>` with the verdict

### Lazy LLM clients

Properties that resolve at access time and cache:
- `tutor.llm_client` — `Purpose.TUTORING`
- `tutor.judge_client` — `Purpose.JUDGE` (falls back to tutoring
  when no JUDGE config exists)
- `tutor.regen_clients` — list of `Purpose.REGEN` clients (falls
  back to `[tutor.llm_client]` if no REGEN configs)

---

## Judges (post-response review)

`apps/tutoring/judges/` package. Each judge is a single-task LLM
call with a focused ~2KB prompt. They run concurrently via
ThreadPoolExecutor (`apps/tutoring/judges/__init__.py::run_all_judges`)
and return typed dataclasses; the orchestrator merges them into
`CombinedJudgeResult`.

| Judge | What it checks | LLM call? |
|---|---|---|
| arithmetic | wrong arithmetic in tutor prose | Deterministic + LLM fallback |
| factual | numeric / named claims vs curriculum KB | LLM (Sonnet) |
| rule | NO_AUTHORING + RULE_1 violations | LLM (Sonnet) |
| step_eval | answer_correct + step_complete | Deterministic short-circuit + LLM |
| coherence | within-response self-contradiction | LLM |
| figure_ref | "the diagram" with no figure attached | Pure regex, no LLM |
| figure_vision | attached figure matches the question | LLM vision |
| safety | harmful / inappropriate / manipulation | LLM |

Why split: the previous monolithic combined_judge had a 9KB prompt
that asked one model to do all 4 checks; Sonnet drifted (rule
violations flipped answer_correct). Per-domain focused prompts
are more reliable. See `apps/tutoring/tests/test_*_judge.py` for
the contracts each judge guarantees.

### Adding a new judge

1. Create `apps/tutoring/judges/<name>.py` with a `<Name>Result`
   dataclass + `run_<name>_judge(...)` function. Skip on cheap
   pre-gates (empty input, no LLM client, etc.).
2. Add an `ex.submit(run_<name>_judge, …)` line to
   `run_all_judges()` and a `_safe_result(...)` collector.
3. Surface the findings on `CombinedJudgeResult`
   (`apps/tutoring/combined_judge.py`).
4. In `apps/tutoring/validator.py`, add an `ISSUE_*` code, append
   to `_REGEN_ISSUES` if it should trigger regen, and map findings
   into `extra_meta` so the regen prompt can reference them.
5. In `apps/tutoring/conversational_tutor.py::_build_regen_constraint_block`,
   add a clause for the new issue.
6. In `apps/tutoring/regen/prompt.py::_violation_line`, add the
   per-issue repair instruction.
7. In `apps/tutoring/regen/score.py`, add a penalty to the score
   function so the ensemble prefers candidates without this
   violation.
8. Tests: `apps/tutoring/tests/test_<name>_judge.py`.

---

## Validator — the decision layer

`apps/tutoring/validator.py::validate_tutor_response`. Takes the
tutor's response + the judges' findings and produces a
`ValidationResult` with:

- `issues: list[str]` — `ISSUE_*` codes
- `metadata: dict` — extra context (regen_reason details,
  factual claims, safety categories, etc.)
- `needs_regeneration: bool` — when any `_REGEN_ISSUES` member
  is present

Soft issues (e.g. `info_dump_warning`) are recorded but don't
trigger regen. Hard issues do.

### Issue catalog

```
ISSUE_NO_QUESTION                 — soft (practice/quiz only)
ISSUE_INFO_DUMP_WARNING           — soft
ISSUE_FIGURE_REF_WITHOUT_SIGNAL   — hard
ISSUE_NUMERIC_CLAIM_CONTRADICTED  — hard
ISSUE_AUTHORING_VIOLATION         — hard (NO_AUTHORING)
ISSUE_ARITHMETIC_VIOLATION        — hard
ISSUE_RULE1_VIOLATION             — hard
ISSUE_TUTOR_INCOHERENT            — hard
ISSUE_FIGURE_MISMATCH             — hard
ISSUE_VERDICT_MISMATCH            — hard (deterministic verdict
                                    contradicts the tutor's text)
ISSUE_TUTOR_UNSAFE                — hard (safety judge)
```

---

## Regen ensemble

`apps/tutoring/regen/` package. When the validator says
`needs_regeneration=True`, the engine calls `run_regen_ensemble`:

1. Build a **focused ~1KB rewrite prompt** (`regen/prompt.py`)
   with ONLY: previous response, violations to fix, bank stems,
   media catalog. NO tutoring system framing — that's why the
   previous single-call regen kept producing text with the same
   violations.
2. Fan out to N concurrent `Purpose.REGEN` ModelConfigs via
   ThreadPoolExecutor. Each gets the same prompt at the cycle's
   current temperature.
3. Run the full judge orchestrator on every candidate.
4. Score candidates with `regen/score.py::score_candidate`
   (lower violations = higher score; safety has a 50pt
   penalty so an unsafe candidate can never beat a safe one).
5. Pick the best clean candidate. If none clean, drop temperature
   by 0.05 and retry. Hard cap = 3 cycles.
6. On exhaustion: send the highest-scoring candidate (best-effort)
   or `STOCK_FALLBACK` if no model produced usable text.

Telemetry: `[Regen] cycle=N temp=X model=… score=… clean=…
issues=…` per cycle, `[Regen] cycles exhausted — sending …` on
fallback.

---

## ModelConfig — per-purpose model selection

`apps/llm/models.ModelConfig`. One row per (institution, purpose)
selects which model serves that purpose for that institution.
Purposes:

```
generation       — content gen (lesson steps, exit-ticket bank)
tutoring         — the live tutor LLM
exit_tickets     — exit-ticket bank generation specifically
skill_extraction — DOK / objective extraction
image_generation — gpt-image-2 / gemini for figures
help_assistant   — the support chatbot
judge            — post-response judge LLM
regen            — rewrite-LLM(s) for the regen ensemble
```

`ModelConfig.get_for(purpose)` returns the active row for the
caller's institution (or platform-wide). Multi-active rows for
`regen` is the supported way to ensemble across providers.

Migrations seed default configs:
- `0019_seed_regen_config_and_lower_tutor_temp` — auto-seeds a
  Sonnet REGEN config + drops tutor temperature to 0.2.

---

## Curriculum pipeline

`apps/curriculum/curriculum_parser.py` → `content_generator.py` →
`knowledge_base.py`.

### Parsing
1. Teacher uploads syllabus PDF via `/dashboard/curriculum/upload/`.
2. Parser extracts course, units, and one lesson per teaching
   objective.
3. Lesson rows created with `objective` field populated; other
   fields empty.

### Content generation
4. Super-admin clicks ⚡ Generate. `content_generator.py` LLMs
   each lesson into N steps (5E phase, teacher_script,
   expected_answer, media references).
5. Exit-ticket bank generated per lesson (~32 questions).
6. Course summative bank sampled from per-lesson exit tickets.

### Indexing
7. ChromaDB collection per institution. Curriculum sections +
   uploaded teaching materials are chunked + embedded
   (sentence-transformers offline) and queryable at session time
   for RAG context.

---

## Help assistant

`apps/support/`. RAG-backed chatbot.

- `kb.py::HelpKB` — ChromaDB collection `help_docs`. Audience-
  filtered (`all` vs `staff`).
- `services.py::answer` — orchestration. Retrieve docs by
  audience → compose system prompt → LLM call (Anthropic Haiku
  by default per `Purpose.HELP_ASSISTANT`) → tool dispatch.
- `tools.py` — tool catalog (navigation, escalate to support).
- `views.py` — `/help/` chat endpoints.
- `management/commands/build_help_index.py` — re-indexes
  `templates/help/index.html` + `docs/*.md` + `CLAUDE.md` +
  `README.md` + `memory/*.md` + source tree on every container
  boot.
- `management/commands/generate_recent_updates.py` — pulls
  the last 30 days of git commits, writes `docs/recent_updates.md`.
  Runs before `build_help_index` in the Dockerfile CMD so
  the assistant always has the latest changelog.

---

## Deploy flow

```
git push origin main
  → GitHub Actions (.github/workflows/deploy.yml)
      → docker buildx build linux/amd64
      → docker push aitutorpixelacr.azurecr.io/aitutor:<sha>
      → az containerapp update --image …
  → Container App pulls image, runs CMD:
      migrate
      → seed_gamification → backfill_progress
      → classify_unit_grades
      → seed_help_assistant_model
      → cp /app/media/vectordb /tmp/vectordb
      → generate_recent_updates
      → build_help_index --with-source
      → gunicorn ai_tutor.config.wsgi:application
```

Pulumi infra changes (`infra/__main__.py`) are **not** auto-deployed
— run `pulumi up --stack pixel` manually after committing infra
changes.

---

## Common gotchas

- **Mac build → Azure deploy fails**: Mac docker builds default to
  arm64. Azure needs amd64. Use `--platform linux/amd64` locally
  or rely on GitHub Actions (which builds amd64).
- **CSRF_TRUSTED_ORIGINS**: uses `env.default_domain` (env-hash
  aware) not a hardcoded URL. Don't replace.
- **ChromaDB SQLite over SMB**: hangs the worker. That's why
  `VECTORDB_ROOT=/tmp/vectordb` exists and the Dockerfile copies
  on startup.
- **Dev-server logging**: background threads' `logger.info()`
  may not show. Use `print(..., flush=True)` for visibility.
- **No SSE / streaming in production**: Azure Container Apps
  buffer StreamingHttpResponse. `respond_stream()` exists but
  unused in prod; production uses buffered JSON.
- **`{# multi-line comment #}` Django bug**: multi-line `{# … #}`
  renders as visible text. Always use `{% comment %}…{% endcomment %}`
  for multi-line.
- **Background content-gen stuck**: lessons with
  `content_status='generating'` from a failed run must be reset
  to `pending` manually. Auto-recovery exists (10 min timeout)
  on the course detail page.

---

## Where to look for…

- LLM call site for the tutor → `conversational_tutor.py:_generate_response`
- Step prompt building → `conversational_tutor.py:_build_system_prompt`
- Math grading → `apps/tutoring/grader.py`
- Bank picking → `conversational_tutor.py:_current_bank_stems` +
  `apps/tutoring/question_picker.py`
- Exit ticket attempt scoring → `apps/tutoring/exit_ticket.py`
- Competency map data → `apps/tutoring/competency_tracker.py::class_competency_matrix`
- Per-student competency → same module, `student_competency_rows`
- Image gen → `apps/tutoring/image_service.py::ImageGenerationService`
- Audio (TTS) → `apps/tutoring/audio_service.py` (Piper, on-device)

For "how does X work" questions outside this list, the help
assistant + indexed source tree (`build_help_index --with-source`)
should answer.
