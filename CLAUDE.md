# AI Tutor — Claude instructions

Django LLM-tutoring platform for secondary school students. Conversational sessions follow the 5E model; teachers manage curriculum via dashboard. Seychelles pilot in production; Tanzania pilot in planning. See `README.md` for the full architecture walkthrough.

## Stack

- **Backend**: Django 5, Python 3.11, PostgreSQL (prod) / SQLite (dev), Gunicorn
- **LLM layer**: Anthropic / OpenAI / Google / Ollama, picked per purpose via `llm.ModelConfig`. Abstraction lives in `apps/llm/client.py::BaseLLMClient`. Mirror this pattern when adding providers.
- **Vector DB**: ChromaDB + `sentence-transformers` (all-MiniLM-L6-v2, offline)
- **Deploy**: Azure Container Apps (Pixel Design Labs subscription), Pulumi IaC in `infra/`, GitHub Actions on push to `main`. Mac must build `--platform linux/amd64` or use CI.
- **Apps** under `apps/`: `accounts` (auth, Institution, StudentProfile), `curriculum` (Course → Unit → Lesson → LessonStep, KnowledgeBase, ContentGenerator), `tutoring` (TutorSession + `conversational_tutor.py` engine), `dashboard` (teacher UI), `llm`, `media_library`, `safety`

## Commands

```bash
python manage.py runserver                  # dev server
python manage.py migrate                    # apply migrations
python manage.py makemigrations             # create migrations
pytest                                      # run tests (pytest-django)
pytest apps/tutoring/tests/ -k mastery      # scoped test run
git push origin main                        # triggers Azure deploy via GitHub Actions
```

Azure commands require `az account set --subscription "Pixel Design Labs LLC"` first.

## Critical rules — always apply

**Multi-tenancy / institution scoping.** Every query touching user data must filter by institution. Use `Q(institution=inst) | Q(institution__isnull=True)` to include platform-wide content. `institution=None` means "all schools"; normalize to `GLOBAL_INSTITUTION_ID = 0` when indexing ChromaDB. Missing this = cross-school data leak.

**Session state, not phase.** The `ConversationPhase` enum was removed. Use `SessionState` (TUTORING, EXIT_TICKET, COMPLETED). Display-level 5E phase comes from each step's `phase` field. Don't reintroduce phase-based flow control in `apps/tutoring/conversational_tutor.py`.

**Media signal format.** The tutor appends `|||MEDIA:N|||` as the LAST line of its response (N is 1-based index into the catalog in the system prompt). Parse and strip BEFORE saving to DB; the frontend also sanitizes defensively. Never use the legacy `[SHOW_MEDIA:title]` format — the fuzzy matcher was deleted.

**Azure Container Apps constraints.**
- No SSE / chunked streaming in production → `respond_stream()` exists but unused; production uses buffered JSON.
- ChromaDB SQLite hangs over the SMB mount → `VECTORDB_ROOT=/tmp/vectordb`; Dockerfile CMD copies vectordb from the mount at startup.
- `CSRF_TRUSTED_ORIGINS` uses `env.default_domain` (not a hardcoded URL) so the env hash is correct.

**LLM JSON robustness.** Content-generation models sometimes return single-quoted Python dicts, not JSON. `apps/curriculum/content_generator.py::_try_fix_json` handles this; the 3-attempt retry loop with correction prompt catches the rest. Don't bypass either.

**Math tutoring.** For math lessons, the tutor must NOT evaluate a bare numeric answer. Teach via named subskills + named tips; use a rung-based complexity ladder. If touching `conversational_tutor.py` math paths, read `~/.claude/projects/-Users-edwardamoah-Documents-GitHub-ai-tutor/memory/feedback_math_tutoring.md` first.

**Exit ticket = lesson competency (in progress).** `ExitTicket.passing_score` is the mastery threshold. Dead fields — do not add new references to: `Lesson.mastery_rule`, `StudentLessonProgress.{correct_streak, total_attempts, total_correct}`. See `memory/lesson_competency_plan.md` for the migration in flight.

**Stuck content generation.** Lessons with `content_status='generating'` from failed runs must be manually reset to `pending`. Background threads may not log via `logger.info` in dev server — use `print("[ContentGen] ...", flush=True)` for visibility.

## Project-local planning

`memory/` in the repo root contains multi-phase plans currently being implemented. Read the relevant plan BEFORE starting work in that area:

- `memory/mobile_rn_plan.md` — React Native mobile app (Expo + TS + llama.rn on-device LLM)
- `memory/offline_mobile_architecture.md` — mobile architecture decisions (framework-agnostic)
- `memory/group_lessons_plan.md` — multi-student same-device sessions
- `memory/lesson_competency_plan.md` — switch competency to exit-ticket-driven
- `memory/platform_brief_tanzania.md` — Tanzania pilot context

Auto-memory at `~/.claude/projects/-Users-edwardamoah-Documents-GitHub-ai-tutor/memory/` covers deployment history, resolved incidents, and cross-session learnings — loaded automatically; don't duplicate here.

## Conventions

- Code references in chat: `apps/tutoring/conversational_tutor.py:1182` format.
- Migrations: one logical change per file; descriptive names (`0014_add_session_participant.py`).
- Tests: pytest, `tests/test_<feature>.py` per feature. Factory fixtures in `apps/<app>/tests/factories.py` when they exist.
- Commits: descriptive subject, body for the "why". Don't reference tasks/issues in code comments — they rot.
- `README.md` is 780 lines and maintained deliberately — don't edit as part of routine feature work.

## Before risky actions

- **Destructive git** (reset --hard, force push, branch delete): confirm with user.
- **Pulumi destroy / Azure resource deletion**: confirm with user. Pulumi stack is `pixel`.
- **Migrations against prod**: dry-run against a copy of the prod DB dump first. Backfill migrations especially.
- **Editing `config/settings.py`, `Dockerfile`, `.github/workflows/`, `infra/__main__.py`**: confirm approach before changing. These are load-bearing for production.
