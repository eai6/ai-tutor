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

**Grader-driven correctness + LLM-routed moves + structural conformance is the default (Phase 3 cutover).** The v2 engine in `apps/tutoring/v2/` is the production tutoring pipeline for new sessions. Kill switch: `NEW_TUTOR=off` routes new sessions back to the legacy `ConversationalTutor` — reserved for student-facing safety incidents, NOT benchmark drift or dashboard noise. The per-turn flow: `StudentGrader` (math-DSL or grounded adjudication; `unverified` is a first-class verdict) → `TutorEngine.pick_move` (LLM `MoveRouter` picks one of 8 moves — `confirm_and_advance`, `confirm_and_extend`, `scaffold_hint`, `name_misconception`, `worked_example`, `explain`, `pivot`, `close_topic` — plus 1-3 principle-emphasis tags and a per-turn focus_note; 5 deterministic safety floors then run in order: turn caps, objective-evidence saturation, name_misconception repeat-without-progress, pose-tool saturation, help-request regex backstop — each can override the router's move and emits a `router.floor_override` span) → `StudentTutor` (one focused per-move prompt, grounded in `design/science-principles.md`, specialized to the turn by the router's focus_note + principle emphasis) → structural conformance (one fast-LLM 9-binary-label classifier + deterministic gates: state-coherence, answer-leak, safety, figure-ref, rule). One retry on rejection → move escalation pass → verdict-keyed safe terminal template as the floor. Question posing is tool-only with two-phase commit (Phase A validates: signed `pre_pose_token` + derivability + in-session/cross-session repeat guards; Phase B commits the ledger + `open_question` only after conformance approves) — every non-terminal move (all 7 except `close_topic`) is pose-capable and uses `tool_choice="auto"`; the legacy `pose_question` move is gone, and its force-tool semantics are enforced structurally by the conformance gate `all__no_assessment_in_prose`. **Phase A feedback loop**: when Phase A rejects a slot the LLM gets a `tool_result(is_error=True, content=<reason>)` block listing the attempted slots and picks a different slot within the same turn (`MAX_POSE_ATTEMPTS_PER_TURN=2` in `apps/tutoring/v2/services/student_tutor.py`; the retry uses `tool_choice="any"`). **Sticky retry**: on a conformance failure `classify_conformance_failure` (in `apps/tutoring/v2/services/conformance/check.py`) splits violations into `prose_only` (state-coherence, safety, figure-ref, rule-check, praise-filter, answer-leak, verdict-matrix shape rules, tutor-claim adjudication) vs `pose_related` (open-question stickiness, extractor `one_question_per_turn` / `active_end_required`, `all__no_assessment_in_prose` when no PendingPose). Prose-only retries HOLD the first attempt's `PendingPose`, skip the tool path, and reattach the held pose on conformance accept — Phase A does not re-run. Pose-related / mixed retries run the full pipeline. Observability: `pose_question.phase_a_rejection`, `pose_question.tool_loop_attempts`, and `v2_trace.retry_classification` spans/fields surfaced on the v2 dashboard. Sessions are sticky per engine version via `TutorSession.engine_version`; in-flight legacy sessions keep using `ConversationalTutor`. Legacy modules under `apps/tutoring/{conversational_tutor.py,combined_judge.py,judges/{unified,factual,arithmetic,coherence,figure_vision,history,step_eval,handoff}.py,regen/,prompts/}` are marked DEPRECATED — deletion gated on 4 weeks of clean production traffic per `design/refactor/refactor-implementation-plan.md` §3.5. Observability dashboard at `/dashboard/v2-observability/` (Phase 3 §3.3) — read this before debugging suspected v2 misbehavior. Authoritative references: `design/refactor/refactor-analysis.md` + `design/refactor/refactor-implementation-plan.md` + `design/tasks/move-router-implementation-plan.md`.

**Media signal format.** The tutor appends `|||MEDIA:N|||` as the LAST line of its response (N is 1-based index into the catalog in the system prompt). Parse and strip BEFORE saving to DB; the frontend also sanitizes defensively. Never use the legacy `[SHOW_MEDIA:title]` format — the fuzzy matcher was deleted.

**Azure Container Apps constraints.**
- No SSE / chunked streaming in production → `respond_stream()` exists but unused; production uses buffered JSON.
- ChromaDB SQLite hangs over the SMB mount → `VECTORDB_ROOT=/tmp/vectordb`; Dockerfile CMD copies vectordb from the mount at startup.
- `CSRF_TRUSTED_ORIGINS` uses `env.default_domain` (not a hardcoded URL) so the env hash is correct.

**LLM JSON robustness.** Content-generation models sometimes return single-quoted Python dicts, not JSON. `apps/curriculum/content_generator.py::_try_fix_json` handles this; the 3-attempt retry loop with correction prompt catches the rest. Don't bypass either.

**Math tutoring — bare-answer handling.** For math lessons: a **correct bare answer** gets confirmed with a one-line "because…" and we advance — no probing. A **wrong bare answer** triggers a single ask-for-working as diagnosis. The blanket "never evaluate a bare numeric answer" rule was reversed 2026-05-17 (created friction without learning signal on items the student clearly had). Working-request is a diagnostic for wrong answers, not a default gate. Still applies: teach via named subskills + named tips, use a rung-based complexity ladder. If touching `conversational_tutor.py` math paths, read `~/.claude/projects/-Users-edwardamoah-Documents-GitHub-ai-tutor/memory/feedback_math_tutoring.md` first.

**Temperature controls (runtime invariants).** Enforced by `ModelConfig.effective_temperature` and the resolved `temperature` parameter on `BaseLLMClient.generate()`. Do not bypass.
- **JUDGE** purpose: always **0** for evaluation consistency. The stored DB value is ignored at runtime.
- **TUTORING** purpose: clamped to **[0.1, 0.3]**. Stored values outside this range are clamped at the API call site.
- **REGEN** ensemble: starts at 0.20, decays 0.05 per cycle. Hard cap **2 cycles** (`DEFAULT_MAX_CYCLES`, dropped 4→2 on 2026-05-12 — prod logs showed cycles 3/4 converging identically with cycle 2, marginal value near zero). Cycle temps: 0.20 → 0.15. Early-exit on any judge-clean candidate.
- **All other purposes** (GENERATION, EXIT_TICKETS, REGEN per-call override, etc.) use the raw stored value or the explicit `temperature` kwarg.

**Exit ticket = lesson competency (in progress).** `ExitTicket.passing_score` is the mastery threshold. Dead fields — do not add new references to: `Lesson.mastery_rule`, `StudentLessonProgress.{correct_streak, total_attempts, total_correct}`. See `memory/lesson_competency_plan.md` for the migration in flight.

**Stuck content generation.** Lessons with `content_status='generating'` from failed runs must be manually reset to `pending`. Background threads may not log via `logger.info` in dev server — use `print("[ContentGen] ...", flush=True)` for visibility.

## Architecture — apply when designing or extending

This codebase has good bones to mirror: pluggable `BaseLLMClient` factory (`apps/llm/client.py:38`), fail-soft concurrent `judges/` orchestration (`apps/tutoring/judges/`), purpose-based `ModelConfig` dispatch (`apps/llm/models.py:225`), priority-ordered curriculum hierarchy, `Lesson.content_status` state machine. **Mirror these patterns; don't reinvent.**

**Conservative bias — applied to every architectural proposal:**
- **Rule of Three.** Don't abstract until duplication appears three times. The multi-tenancy `Q(institution=inst) | Q(institution__isnull=True)` pattern is past threshold — extract a manager method when next touched.
- **Simplest first.** The validated decomposition is **grader / tutor / profiler + tools**, shipped Phase 3. That split is justified by the cancelled-pilot bottleneck measurement (`design/refactor/refactor-analysis.md` §1). **Do not introduce additional agents** — e.g., a separate "explainer agent", "hint agent", "praise agent", or per-move LLM router — unless the same kind of evidence (a measured benchmark bottleneck the existing split cannot close) justifies it. Cemri et al. 2025 found 17× error amplification in unstructured multi-agent setups; the closed move table + structural conformance is the bounded counter-pattern.
- **Modular monolith.** No service extraction. Everything in this Django app; use in-process boundaries.
- **Mirror existing patterns.** New pluggable component → `BaseLLMClient` shape (ABC + factory). New evaluator → `judges/` shape (one file, fail-soft, structured result with `skipped` + `skip_reason`).
- **Instrument before splitting.** Trace logging before any agent decomposition.

**Don't propagate these inconsistencies:**
- Untyped JSON state — document the schema near the writer, or use Pydantic.
- `metadata` / `judge_outputs` overlap on `SessionTurn` — new judge fields go to `judge_outputs` only.
- Silent-skip on missing dependencies — log a warning, never silently no-op.
- Backward-compat heuristics (e.g., `Course.is_math` MATH_KEYWORDS fallback) — backfill old data instead.

**Consult the architecture skills:**
- `codebase-architecture-expert` — adding/refactoring abstractions in THIS codebase.
- `architecture-patterns-expert` — debating module boundaries, when to extract, schema modeling, modular monolith vs services.
- `agent-orchestration-expert` — before any multi-agent or evaluator-loop work; covers when NOT to use multi-agent.

**Always consult the prompting skills before writing or tuning ANY LLM prompt** (system / user / judge / regen / image-gen / content-gen). Non-negotiable — see `~/.claude/projects/-Users-edwardamoah-Documents-GitHub-ai-tutor/memory/feedback_consult_prompting_skills.md`. Order: (1) `prompting-fundamentals-expert` first — universals (CoT-or-not, few-shot, output format, long-context query-last, injection defense). (2) Provider-specific to match the target model: `claude-prompting-expert` (XML tags, prompt caching, extended thinking), `openai-prompting-expert` (developer role, Structured Outputs, strip CoT on o-series, auto caching), `gemini-prompting-expert` (`system_instruction`, multimodal ordering, 1M context query-last, grounding, Gemini 3 anti-flowery rule). Skipping this step has cost a debug cycle every time.

**Active architecture plan**: `memory/agentic_platform_architecture_plan.md` — read before proposing structural changes. Phased path: trace logging → benchmark infra → typed state + dedup → lesson graph → risk routing; multi-agent decomposition deferred until benchmark evidence demands it.

## Project-local planning

`memory/` in the repo root contains active multi-phase plans. Read the relevant plan BEFORE starting work in that area:

- `memory/eval_benchmark_v2_simplified.md` — tutor evaluation benchmark (30 labels, 19 failure categories); pairs with `SessionTurn.judge_outputs` persistence shipped 2026-05-11.
- `memory/agentic_platform_architecture_plan.md` — architecture evolution plan; read before proposing structural changes.

Archived plans live in `memory/archives/` (gitignored) — historical reference for completed or paused work (mobile RN, group lessons, lesson competency, Tanzania pilot brief, offline mobile architecture, Martin session fix, judge disagreements audit, etc.).

Auto-memory at `~/.claude/projects/-Users-edwardamoah-Documents-GitHub-ai-tutor/memory/` covers deployment history, resolved incidents, and cross-session learnings — loaded automatically; don't duplicate here.

## Conventions

- Code references in chat: `apps/tutoring/conversational_tutor.py:1182` format.
- Migrations: one logical change per file; descriptive names (`0014_add_session_participant.py`).
- Tests: pytest, `tests/test_<feature>.py` per feature. Factory fixtures in `apps/<app>/tests/factories.py` when they exist.
- Commits: descriptive subject, body for the "why". Don't reference tasks/issues in code comments — they rot.
- **Memory ↔ commit cross-citations.** Git history and memory files are two persistence layers that work better when bidirectionally linked. Convention:
  - **Commit body → memory**: end the body with `Refs:` listing every memory file the commit implements, responds to, or invalidates. Repo memory is `memory/<name>.md`; auto-memory is `auto-memory/<name>.md` (use that prefix even though the path resolves elsewhere — it's a stable label). Multiple files comma-separated.
  - **Memory body → commit**: when writing/updating a memory file that ties to a single concrete change, end the body with `Commit: <sha>` so `git show <sha>` rehydrates the work.
  - **Why**: git history is immutable, ordered, queryable; memory is semantic and cross-cutting. With bidirectional links, `git log --grep=feedback_probe_strip` finds every change tied to a learning, and any memory entry can jump to the introducing commit. Future Claude bootstraps faster.
  - **Skip when**: trivial commits (typos, whitespace) need no `Refs`. Architectural plans and project briefs that span many commits skip `Commit:` — those are living docs, not change-bound.
  - **Bootstrap recipe** (session start, when memory feels stale): `git log --oneline -20` for recent activity; `git log --grep=memory/` to surface commits that touched the memory layer; `git show <sha>` on any cited commit.
- `README.md` is 780 lines and maintained deliberately — don't edit as part of routine feature work.
- **UI testing**: `chrome-devtools-mcp` is installed for this project (`~/.claude.json` local scope). Provides `mcp__chrome-devtools__*` tools (navigate, click, screenshot, evaluate JS, inspect DOM). Use it to verify dashboard / annotation UI changes — drive the running dev server, don't speculate about rendered output. Requires Claude Code restart after install for tools to appear.

## Before risky actions

- **Destructive git** (reset --hard, force push, branch delete): confirm with user.
- **Pulumi destroy / Azure resource deletion**: confirm with user. Pulumi stack is `pixel`.
- **Migrations against prod**: dry-run against a copy of the prod DB dump first. Backfill migrations especially.
- **Editing `config/settings.py`, `Dockerfile`, `.github/workflows/`, `infra/__main__.py`**: confirm approach before changing. These are load-bearing for production.

## Bug-fix workflow — test locally before deploy

When fixing a user-reported production bug or shipping any new feature, **always** reproduce + verify locally before `git commit` / `git push`. Each deploy round-trip is ~10 min via GitHub Actions; shipping an unverified change that then fails in prod costs the user a 20-min wait before the next attempt.

Required sequence:

1. **Reproduce locally**. For UI/upload bugs, drive the local dev server with `mcp__chrome-devtools__*` tools using the same input file/path the user reported. For backend logic, write a one-off Django shell test (`python manage.py shell <<'PY' ... PY`) that exercises the exact code path with realistic inputs.
2. **Confirm the fix works end-to-end** before committing. "Active revision shows the right image tag" is necessary but NOT sufficient — Python module caches, deferred startup work, or different code paths can mean the fix isn't actually firing in prod.
3. **Visual check via screenshot for ANY UI change** — `mcp__chrome-devtools__take_screenshot` and actually look at the rendered output. DOM inspection via `evaluate_script` is necessary but NOT sufficient: styling bugs, broken layouts, mis-rendered flash messages, off-screen elements, and contrast issues all slip past programmatic checks. For interactive flows (forms, modals, multi-step wizards), capture screenshots at each step (panel-open, form-filled, post-submit success message, error state). See `auto-memory/feedback_visual_check_required.md`.
4. **Surface the local test result to the user** ("I uploaded the file via chrome-devtools and it succeeded" / "Django shell confirms add_log scrubs NUL" / "screenshots show the panel renders + flash message displays") before pushing.
5. If the user-reported file is sandbox-blocked (e.g. in `~/Downloads`), ask them to copy it into the project tree (`media/test_uploads/` or similar) so the bash sandbox can read it. Don't skip the local test just because the file is awkward to access.

Compounds with: don't auto-commit during active iteration (`auto-memory/feedback_dev_collaboration.md`). Code → local test → visual check (if UI) → user confirmation → commit + push, in that order. Never skip a step.
