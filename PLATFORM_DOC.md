# AI Tutor — Platform Onboarding

> A practical orientation to the codebase: what the project is, how it's laid out, and what every folder does. For a deeper architecture walkthrough see [README.md](README.md); for working rules and gotchas see [CLAUDE.md](CLAUDE.md).

---

## 1. What this project is

**AI Tutor** is a Django web platform that gives secondary-school students one-on-one
conversational tutoring driven by large language models (Claude, GPT, Gemini, or local
Ollama). Teachers upload a curriculum document; the platform generates the lessons,
diagrams, and assessments; students then learn through chat sessions that follow a
research-based teaching model.

- **Who uses it:** students (chat tutoring) and teachers (dashboard for curriculum + monitoring).
- **Where it runs:** Seychelles secondary schools (live pilot — Geography & Mathematics).
  A Tanzania pilot is in planning.
- **Pedagogy:** the **5E model** — Engage → Explore → Explain → Practice → Evaluate —
  with Socratic questioning, scaffolded hints, exit-ticket assessments, and prerequisite gating.
- **Stack:** Django 5/6, Python 3.11 (repo target), PostgreSQL in prod / SQLite in dev,
  ChromaDB for vector search, deployed to Azure Container Apps via Pulumi + GitHub Actions.

### The core loop (how a tutoring session works)

```
Teacher uploads curriculum PDF/DOCX
        │
        ▼
curriculum_parser → Course › Unit › Lesson hierarchy
        │
        ▼
content_generator (LLM) → 8–12 LessonSteps per lesson  ┐
image_service (Gemini) → educational diagrams           │  content
exit-ticket generator (LLM) → ~35 MCQ question bank     │  pipeline
skill_extraction (LLM) → skill graph + prerequisites    ┘
        │
        ▼
Student picks a lesson  →  ConversationalTutor engine runs the chat
        │                  (step-by-step, LLM responses, answer grading)
        ▼
Every tutor reply is checked by the unified judge (factual / safety / rule / …)
        │
        ▼
Exit ticket (10 MCQs, 80% to pass)  →  mastery recorded per student per lesson
```

The single most important file is **[apps/tutoring/conversational_tutor.py](apps/tutoring/conversational_tutor.py)** —
the `ConversationalTutor` engine that runs the live session.

---

## 2. Top-level directory map

| Folder / file | What it is |
|---|---|
| **`apps/`** | All Django application code — 10 apps. The heart of the project. See §3. |
| **`config/`** | Django project config: `settings.py`, `urls.py` (root routing), `wsgi.py`/`asgi.py` entry points. |
| **`templates/`** | Server-rendered HTML (Django templates) — `base.html` plus a folder per app area. |
| **`static/`** | Front-end assets: `js/` helpers and `pwa/` (service worker, manifest, offline page, icons). |
| **`media/`** | Runtime user uploads — curriculum docs, institution logos — and the ChromaDB vector store. Not source code. |
| **`infra/`** | **Pulumi IaC** for Azure (`__main__.py`). Defines Container App, Postgres, file share, registry. Stack = `pixel`. |
| **`.github/`** | CI/CD — GitHub Actions workflow that builds the Docker image and deploys to Azure on push to `main`. |
| **`ops/`** | Operational tooling. `annotator_agent/` is a containerized agent + CI scripts for automated transcript annotation. |
| **`scripts/`** | One-off / research scripts: model benchmarking, judge experiments, transcript replays. Not part of the running app. |
| **`memory/`** | **Active multi-phase plans** (markdown). Read the relevant plan before working in that area. Archived plans live in `memory/archives/` (gitignored). |
| **`docs/`** | Human-facing docs: developer/teacher/student guides, curriculum source documents, pedagogy reference PDFs. |
| **`design/`** | Architecture & analysis write-ups (gap analysis, root-cause analyses, system architecture). |
| **`tests/`** | Repo-level test plans (manual test plans) and `run_all_checks.py`. Per-app unit tests live inside each app. |
| **`mobile/`** | A separate **React Native / Expo** app (offline-capable mobile client). Has its own `src/`, `app/`, on-device inference. Not Python. |
| **`.claude/`** | Claude Code project config — `skills/` holds project-specific expert skills. |
| **`Dockerfile`** | Production image build. CPU-only PyTorch installed first, then `requirements.txt`. |
| **`requirements.txt`** | Python dependencies. **Note:** `torch` is intentionally commented out — installed separately, CPU-only. |
| **`manage.py`** | Django's CLI entry point (`python manage.py <command>`). |
| **`CLAUDE.md`** | Working rules, critical invariants, and gotchas. Read before changing anything load-bearing. |
| **`datadump.json`** | A database fixture/dump (large). Sample/seed data — not authoritative source. |

---

## 3. The Django apps (`apps/`)

Ten apps, all registered in `config/settings.py` `INSTALLED_APPS`. They form a
**modular monolith** — in-process boundaries, no microservices. Roughly grouped:

**Domain core:** `accounts` · `curriculum` · `tutoring` · `media_library`
**Supporting services:** `llm` · `safety`
**Interfaces:** `dashboard` (teacher web UI) · `api` (mobile REST)
**Quality & help:** `benchmark` · `support`

### `accounts` — identity & multi-tenancy
Every record in the system is scoped to an **`Institution`** (a school). This is the
multi-tenancy root. Models: `Institution`, `Membership` (user↔institution + role),
`StudentProfile`, `PlatformConfig` (singleton — branding, school/grade lists),
`StaffInvitation` (staff can't self-register; they're invited). Also hosts the landing
page and login/registration. `context_processors.py` injects institution branding into
every template.

> **Critical rule:** every query touching user data must filter by institution.
> `institution=None` means "platform-wide / all schools."

### `curriculum` — course content & the knowledge base
The largest content app. Hierarchy: **`Course` › `Unit` › `Lesson` › `LessonStep`**.
A `LessonStep` is the atomic unit of instruction (types: teach, worked_example,
practice, quiz, summary) with rich JSON fields for media, educational content, and
teaching context.

Key pieces:
- `curriculum_parser.py` — extracts the Course/Unit/Lesson tree from PDF/DOCX uploads.
- `content_generator.py` + `content_gen_providers.py` / `content_gen_schemas.py` — LLM-driven lesson-step generation (structured output).
- `content_verifier.py`, `content_judges/`, `content_regen/` — quality checking and regeneration of generated content.
- `knowledge_base.py` — the **RAG layer**: ChromaDB + `sentence-transformers` (offline embeddings). Indexes textbooks/materials and serves semantic search to content generation, tutoring, and exit-ticket grounding.
- `figure_*` / `vision_ocr.py` / `parametric_renderer.py` — diagram/figure extraction, fact-checking, and rendering.
- `pipeline.py` — orchestrates upload → parse → generate → media → exit tickets.
- `quality_*` — content quality metrics and capture.

### `tutoring` — the conversational engine (the heart of the app)
Runs live student sessions and tracks mastery. Key models: `TutorSession`,
`SessionTurn` (one message), `StudentLessonProgress`, `ExitTicket` /
`ExitTicketQuestion` / `ExitTicketAttempt`, plus the skill graph (`Skill`,
`LessonPrerequisite`, `StudentSkillMastery`).

Important sub-areas:

| Path | Purpose |
|---|---|
| `conversational_tutor.py` | **The engine.** Builds the system prompt, runs step-by-step progression, evaluates answers, advances steps, triggers exit tickets. |
| `judges/` | Post-hoc evaluators that audit each tutor reply. `unified.py` is the **current default** — one multi-axis LLM call (10 dimensions). The older specialists (`factual.py`, `rule.py`, `safety.py`, …) are deprecated. |
| `combined_judge.py` | Dispatches to the unified judge; kill-switch `UNIFIED_JUDGE=off` falls back to the old fan-out. |
| `prompts/` | Provider-specific tutor-prompt builders (`base.py`, `anthropic.py`). |
| `regen/` | Regeneration loop — when a judge flags a reply, re-generate a better one (`prompt.py`, `score.py`, `self_retry.py`). |
| `student_sim/` | LLM "student simulator" — synthetic personas that drive sessions for testing. |
| `grader.py`, `competency*.py`, `exit_ticket_grader.py`, `bank_grader.py` | Answer evaluation, competency tracking, exit-ticket scoring. |
| `skill_extraction.py`, `personalization.py` | Skill-graph construction; adaptive difficulty + prerequisite gating. |
| `tracing.py` | Orchestration trace logging. |

> Session flow uses `SessionState` (**TUTORING → EXIT_TICKET → COMPLETED**) — *not* the
> old removed `ConversationPhase` enum. The 5E phase is display-only, read off each step.

### `media_library` — reusable media assets
`MediaAsset` (institution-scoped images/audio/video/PDF) and `StepMedia` (attaches an
asset to a `LessonStep` with placement/order).

### `llm` — provider abstraction & prompt management
Decouples the rest of the system from any one LLM vendor.
- `client.py` — `BaseLLMClient`: an abstract base + **factory** that dispatches to
  Anthropic / OpenAI / Google / Azure OpenAI / Ollama. Mirror this pattern for new providers.
- `models.py` — `ModelConfig` (per-institution, **per-purpose** model selection with
  encrypted API keys) and `PromptPack` (customizable prompt components).
- Purposes route to different models: `tutoring`, `generation`, `judge`, `exit_tickets`,
  `image_generation`, etc. See `ModelConfig.get_for(purpose)`.
- `json_utils.py` — robust parsing of messy LLM JSON (markdown fences, single quotes).

### `safety` — compliance, flagging & GDPR
`SafetyAuditLog` (safety-event log) and `ConsentRecord` (GDPR consent tracking).
Provides privacy pages, data export/deletion, audit logging, and email backends
(`email_backends.py` — Azure Communication Services) and an image-safety pipeline.

### `dashboard` — the teacher web UI
The teacher-facing admin panel (`/dashboard/`): curriculum upload & review, content
generation monitoring, student/class progress, flagged-session review, platform
settings. `background_tasks.py` runs the parallel generation pipeline (threading).
`job_dispatch.py` / `material_*` handle large textbook uploads via Azure Container
Apps Jobs. `views_health.py` is the container health probe.

### `api` — REST API for the mobile app
DRF-based JSON API mounted at `/api/v1/`, JWT auth (simplejwt). `views/` is split by
concern: `auth`, `resources`, `sessions`, `offline_pack`, `mobile_models`, `sync`.
Backs the React Native client in `mobile/`. OpenAPI schema via drf-spectacular.

### `benchmark` — tutor-quality evaluation
The evaluation harness. `BenchmarkItem` / `BenchmarkAnnotation` / `BenchmarkRun`
models, plus sampling, scoring, an LLM judge, and label taxonomy. Pairs with the plan
in `memory/eval_benchmark_v2_simplified.md`. Has a web UI under `templates/benchmark/`.

### `support` — in-app help assistant
A chatbot that answers user questions about the platform itself. `HelpAssistantConversation` /
`Message` / `ToolCall` models, with `kb.py` (help knowledge base), `tools.py`, and
`services.py`. Mounted at `/support/`.

---

## 4. Routing — where URLs go

Root routing is [config/urls.py](config/urls.py):

| Prefix | App | Audience |
|---|---|---|
| `/` | `accounts` | Landing page, login, registration |
| `/tutor/` | `tutoring` | Student lesson catalog + chat sessions |
| `/dashboard/` | `dashboard` | Teacher admin panel |
| `/support/` | `support` | In-app help assistant |
| `/api/v1/` | `api` | Mobile REST API (JWT) |
| `/admin/` | Django admin | Superuser DB admin |
| `/health/` | `dashboard.views_health` | Container liveness/readiness probe |
| `/media/<path>` | `serve` | Uploaded files (explicit view — works in prod) |
| `/sw.js` | — | PWA service worker (served from root for full scope) |

---

## 5. Getting started locally

```bash
# 1. Environment (venv already set up — see the torch note below)
source venv/bin/activate

# 2. Configure — create a .env in the repo root
#    Minimum: ANTHROPIC_API_KEY, SECRET_KEY, DEBUG=True
#    (SQLite is the default DB; no DATABASE_URL needed for dev)

# 3. Database
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_seychelles      # seeds the Seychelles S1 curriculum

# 4. Run
python manage.py runserver
#   Students:  http://localhost:8000/tutor/
#   Teachers:  http://localhost:8000/dashboard/
#   Admin:     http://localhost:8000/admin/

# Tests
pytest                                # all tests (pytest-django)
pytest apps/tutoring/tests/ -k mastery # scoped run
```

**Dependency note (this machine):** `torch` is installed **CPU-only** and pinned to
`2.10.0+cpu`. It is *not* in `requirements.txt` (line 133 is a comment) — install it
separately from `https://download.pytorch.org/whl/cpu` *before* `requirements.txt`,
or pip will pull the multi-GB CUDA build. `requirements.txt`'s `setuptools==82.0.0`
pin is what forces torch to `2.10.0` (newer torch caps `setuptools<82`).

---

## 6. Recommended reading path for a new developer

1. **[README.md](README.md)** — full architecture walkthrough, data flow, deployment.
2. **[CLAUDE.md](CLAUDE.md)** — the non-negotiable rules: multi-tenancy scoping,
   `SessionState` (not phase), the unified judge, media-signal format, temperature
   invariants, math-tutoring handling.
3. **[apps/tutoring/conversational_tutor.py](apps/tutoring/conversational_tutor.py)** —
   the engine; read `respond()` and follow the call chain.
4. **[apps/llm/client.py](apps/llm/client.py)** + **[apps/llm/models.py](apps/llm/models.py)** —
   how LLM calls are dispatched per purpose.
5. **[apps/curriculum/models.py](apps/curriculum/models.py)** — the Course/Unit/Lesson/Step data model.
6. **[apps/tutoring/judges/unified.py](apps/tutoring/judges/unified.py)** — how tutor replies are audited.
7. **`memory/`** — skim `agentic_platform_architecture_plan.md` and any plan touching
   your area before making structural changes.

---

## 7. Key concepts cheat-sheet

| Term | Meaning |
|---|---|
| **5E model** | Engage → Explore → Explain → Practice → Evaluate — the pedagogy each lesson follows. |
| **Institution scoping** | Every query filters by school; `institution=None` = platform-wide. Missing it leaks data across schools. |
| **`SessionState`** | TUTORING → EXIT_TICKET → COMPLETED. Drives session flow. The old `ConversationPhase` enum was removed. |
| **`ModelConfig` purpose dispatch** | One config picks a different model per task (tutoring vs generation vs judge…). |
| **Unified judge** | A single multi-axis LLM call that audits every tutor reply (default since 2026-05-18). |
| **Exit ticket** | The lesson's summative assessment — 10 MCQs drawn from a ~35-question bank, 80% to pass. Equals lesson competency. |
| **Knowledge base / RAG** | ChromaDB vector store of curriculum materials; grounds generation and tutoring. |
| **Media signal** | The tutor appends `|||MEDIA:N|||` as its last line to show catalog media; parsed & stripped before DB save. |
| **`engine_state`** | JSON blob on `TutorSession` holding live engine state between turns. |

---

*This document is a high-level orientation. When details here and the code disagree,
the code wins — and please update this file.*
