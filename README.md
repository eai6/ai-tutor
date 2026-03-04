# AI Tutor

An AI-powered tutoring platform for secondary school students, built with Django and powered by LLMs (Claude, GPT, Gemini, or local Ollama). Originally designed for Seychelles secondary schools covering Geography and Mathematics, but adaptable to any curriculum.

Students learn through structured conversational sessions that follow research-based pedagogy (5E model), with automatic content generation, exit-ticket assessments, prerequisite gating, and real-time progress tracking.

---

## Table of Contents

- [Features](#features)
- [Architecture Overview](#architecture-overview)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Django Apps](#django-apps)
  - [accounts](#accounts)
  - [curriculum](#curriculum)
  - [tutoring](#tutoring)
  - [dashboard](#dashboard)
  - [llm](#llm)
  - [media_library](#media_library)
  - [safety](#safety)
- [Configuration](#configuration)
- [LLM Providers](#llm-providers)
- [Content Generation Pipeline](#content-generation-pipeline)
- [Management Commands](#management-commands)
- [Deployment](#deployment)
- [URL Routes](#url-routes)
- [Testing](#testing)
- [Contributing](#contributing)
- [License](#license)

---

## Features

- **Conversational AI Tutoring** -- LLM-driven sessions following the 5E instructional model (Engage, Explore, Explain, Practice, Evaluate) with Socratic questioning
- **Automatic Content Generation** -- Upload a curriculum PDF/DOCX and the system generates lesson steps, media, exit tickets, and skill maps
- **RAG Knowledge Base** -- ChromaDB vector store indexes textbooks, teaching materials, and exam papers for context-aware tutoring and content generation
- **Multi-Provider LLM Support** -- Anthropic Claude, OpenAI GPT, Google Gemini, Azure OpenAI, or local Ollama with per-purpose model configuration
- **Exit Ticket Assessments** -- 35-question banks per lesson, 10 randomly selected per session, 80% pass threshold
- **Skill Extraction & Prerequisites** -- Automatic skill graph construction with lesson prerequisite detection and student mastery gating
- **Multi-Tenancy** -- Institution-scoped content, users, and configuration with platform-wide curriculum support
- **Teacher Dashboard** -- Curriculum management, content review/editing, student progress tracking, safety flagging review
- **Adaptive Personalization** -- Difficulty adjustment, interleaved practice, remediation before exit tickets
- **Image Generation** -- Educational diagrams via Gemini with anti-hallucination prompt engineering
- **Safety & Compliance** -- Content flagging, GDPR consent tracking, audit logging, data export/deletion
- **Cloud-Ready** -- Dockerized, Azure Container Apps deployment via Pulumi IaC, GitHub Actions CI/CD

---

## Architecture Overview

```
                                    +------------------+
                                    |   Student/Teacher |
                                    |     Browser       |
                                    +--------+---------+
                                             |
                                    +--------v---------+
                                    |  Django (Gunicorn)|
                                    |  config/urls.py   |
                                    +--------+---------+
                                             |
                +----------------------------+----------------------------+
                |                            |                            |
    +-----------v----------+    +------------v-----------+   +------------v-----------+
    |   accounts app       |    |    tutoring app         |   |    dashboard app        |
    | Auth, Institutions,  |    | ConversationalTutor,    |   | Admin panel, Content    |
    | StudentProfile       |    | Sessions, Grading,      |   | management, Uploads,    |
    |                      |    | Skills, Personalization  |   | Progress reports        |
    +----------+-----------+    +-----+------+------+-----+   +------------+------------+
               |                      |      |      |                      |
    +----------v-----------+    +-----v------v------v-----+   +------------v------------+
    |   curriculum app     |    |      llm app            |   |    safety app           |
    | Course/Unit/Lesson/  |    | PromptPack, ModelConfig, |   | AuditLog, Consent,     |
    | Step, KnowledgeBase, |    | Provider abstraction     |   | Flagging, GDPR         |
    | ContentGenerator     |    +-------------------------+   +-------------------------+
    +----------+-----------+
               |
    +----------v-----------+
    |  media_library app   |
    | MediaAsset, StepMedia|
    +----------------------+
               |
    +----------v-----------+
    | ChromaDB VectorStore |   <-- sentence-transformers (all-MiniLM-L6-v2)
    +----------------------+
```

### Data Flow

1. **Teacher uploads** a curriculum document (PDF/DOCX) via the dashboard
2. **Parser** extracts course > unit > lesson hierarchy
3. **Content generator** (LLM) creates lesson steps with media descriptions, worked examples, and vocabulary
4. **Image service** generates educational diagrams via Gemini
5. **Exit ticket generator** creates 35 MCQ questions per lesson
6. **Skill extractor** builds a knowledge graph with prerequisites
7. **Student** selects a lesson from the catalog, starts a tutoring session
8. **ConversationalTutor** engine manages the step-by-step session via LLM
9. **Exit ticket** is presented after all steps; 8/10 required to pass
10. **Progress** is tracked per-student, per-lesson with mastery levels

---

## Quick Start

### 1. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file in the project root:

```bash
# Required: at least one LLM provider key
ANTHROPIC_API_KEY=sk-ant-xxxxx

# Optional providers
OPENAI_API_KEY=sk-xxxxx
GOOGLE_API_KEY=xxxxx

# Django settings
DEBUG=True
SECRET_KEY=your-secret-key-here

# Database (defaults to SQLite if not set)
# DATABASE_URL=postgres://user:pass@host:5432/dbname

# Embedding backend: 'local' (default, uses sentence-transformers) or 'openai'
EMBEDDING_BACKEND=local
```

### 3. Initialize Database

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_seychelles  # Seed Seychelles curriculum (7 geo + 8 math units)
```

### 4. Configure LLM Models

Visit Django Admin (`/admin/`) and create `ModelConfig` entries for each purpose:
- **Tutoring** -- the model students interact with (e.g., Claude Haiku for speed)
- **Generation** -- for content generation (e.g., Claude Sonnet for quality)
- **Exit Tickets** -- for generating assessment questions
- **Skill Extraction** -- for analyzing lesson content
- **Image Generation** -- for creating diagrams (Google Gemini recommended)

### 5. Run the Server

```bash
python manage.py runserver
```

- **Student interface**: http://localhost:8000/tutor/
- **Teacher dashboard**: http://localhost:8000/dashboard/
- **Django admin**: http://localhost:8000/admin/

### Test Accounts (after `seed_seychelles`)

- **Student**: `student1` / `student123`
- **Teacher**: `teacher1` / `teacher123`

---

## Project Structure

```
ai-tutor/
├── apps/
│   ├── accounts/          # Multi-tenancy, auth, user profiles
│   ├── curriculum/        # Course hierarchy, knowledge base, content generation
│   ├── dashboard/         # Teacher admin panel, upload processing
│   ├── llm/               # LLM provider abstraction, prompt packs
│   ├── media_library/     # Reusable media assets
│   ├── safety/            # Compliance, flagging, GDPR
│   └── tutoring/          # Conversational engine, sessions, skills, grading
├── config/
│   ├── settings.py        # Django configuration
│   ├── urls.py            # Root URL routing
│   ├── wsgi.py            # WSGI entry point
│   └── asgi.py            # ASGI entry point
├── templates/
│   ├── base.html          # Master layout with theming
│   ├── accounts/          # Login, registration
│   ├── tutoring/          # Lesson catalog, chat interface
│   ├── dashboard/         # Admin panel templates
│   └── safety/            # Privacy pages
├── static/                # CSS/JS assets
├── media/                 # User uploads, vectordb
├── infra/                 # Pulumi IaC for Azure
├── .github/workflows/     # CI/CD pipeline
├── Dockerfile             # Multi-stage production build
├── requirements.txt       # Python dependencies
└── manage.py
```

---

## Django Apps

### accounts

**Purpose**: Multi-tenancy, authentication, and user profile management.

Every record in the system is scoped to an `Institution` (school). Users belong to institutions via `Membership` records with role-based access (staff or student).

#### Models

| Model | Description |
|-------|-------------|
| `Institution` | A school or organization. The multi-tenancy root entity. Has a `get_global()` classmethod for platform-wide content. |
| `Membership` | Links a `User` to an `Institution` with a role (`STAFF` or `STUDENT`). A user can have different roles at different institutions. |
| `StudentProfile` | Extended profile for students: school name, grade level (S1-S5). OneToOne with Django `User`. |
| `PlatformConfig` | Singleton for platform-wide settings: branding colors, logo, school list, grade list. Editable from the dashboard settings page. |
| `StaffInvitation` | Token-based invitation system for staff members. Staff cannot self-register -- they must be invited by an existing staff member. |

#### Key Files

| File | Description |
|------|-------------|
| `models.py` | 5 models defining the multi-tenant user system |
| `views.py` | Registration (students), login, profile management |
| `urls.py` | Auth routes (`/accounts/login/`, `/accounts/register/`, etc.) |
| `context_processors.py` | Injects institution theme (colors, logo) into all templates |
| `admin.py` | Django admin configuration for all models |

#### How It Works

- Students self-register by choosing a school and grade level
- Staff are invited via token links generated from the dashboard
- The `institution_theme` context processor makes branding available in every template
- `PlatformConfig` is a singleton (pk=1) that controls platform-wide settings
- When `Institution` records exist, school choices come from the DB; otherwise falls back to `PlatformConfig.schools` JSON, then to hardcoded Seychelles defaults

---

### curriculum

**Purpose**: Defines the course hierarchy and handles automated content generation powered by LLMs and a RAG knowledge base.

This is the largest app -- it manages everything from curriculum document parsing to lesson step generation with media, educational materials, and Seychelles-localized context.

#### Models

| Model | Description |
|-------|-------------|
| `Course` | Top-level container (e.g., "S1 Geography"). Scoped to an institution or `null` for platform-wide. |
| `Unit` | Groups related lessons within a course. Ordered by `order_index`. |
| `Lesson` | A single teaching unit with an objective, mastery rule, estimated time, and content status (`empty` > `generating` > `ready` / `failed`). |
| `LessonStep` | The atomic unit of instruction. Five types: `teach`, `worked_example`, `practice`, `quiz`, `summary`. Contains teacher script, questions, hints, media JSON, educational content JSON, curriculum context JSON, and 5E phase tags. |

#### Key Files

| File | Description |
|------|-------------|
| `models.py` | 4 models with rich JSON fields for media, educational content, and curriculum context |
| `knowledge_base.py` | RAG system using ChromaDB + sentence-transformers. Indexes curriculum documents and teaching materials into vector collections. Provides semantic search for content generation, tutoring context, and exit ticket grounding. |
| `content_generator.py` | LLM-powered lesson step generator. Uses Instructor library for structured output. Generates 8-12 steps per lesson following the 5E model, with media descriptions, vocabulary, worked examples, and Seychelles context. |
| `curriculum_parser.py` | Extracts text from PDF/DOCX curriculum documents and parses the course > unit > lesson hierarchy. Supports pdfplumber, PyMuPDF, and python-docx. |
| `pipeline.py` | Orchestrates the full upload-to-content pipeline: parse document, create DB records, generate steps, create media, build exit tickets. |
| `signals.py` | Auto-creates exit tickets and seeds prerequisites when lessons are created. |

#### LessonStep JSON Fields

Each `LessonStep` has three rich JSON fields:

**`media`** -- Images, videos, and audio for the step:
```json
{
  "images": [
    {"url": "/media/...", "alt": "...", "caption": "...", "type": "diagram", "source": "generated"}
  ]
}
```

**`educational_content`** -- Vocabulary, worked examples, formulas, key points:
```json
{
  "key_vocabulary": [{"term": "...", "definition": "...", "example": "..."}],
  "worked_example": {"problem": "...", "steps": [...], "final_answer": "..."},
  "key_points": ["point 1", "point 2"],
  "seychelles_context": "Local example..."
}
```

**`curriculum_context`** -- Teaching strategies and differentiation from the knowledge base:
```json
{
  "teaching_strategies": ["..."],
  "differentiation": {"support": "...", "extension": "..."}
}
```

#### Knowledge Base (RAG)

The `CurriculumKnowledgeBase` class manages ChromaDB vector collections:

- **Embedding**: `sentence-transformers/all-MiniLM-L6-v2` (local, offline, no API cost)
- **Storage**: `VECTORDB_ROOT` setting (defaults to `media/vectordb`, overridable to `/tmp/vectordb` for fast local disk in production)
- **Collections**: Separate per institution (with institution_id prefix)
- **Query methods**:
  - `query_for_content_generation()` -- lesson context for step generation
  - `query_for_tutoring()` -- teaching strategies during live sessions
  - `query_for_exit_ticket_generation()` -- exam questions for grounding
  - `query_for_figure_descriptions()` -- textbook diagrams for reference

#### Management Commands

| Command | Description |
|---------|-------------|
| `seed_seychelles` | Seeds 7 geography units (20 lessons) + 8 math units (27 lessons) for Seychelles S1 curriculum |
| `seed_sample_data` | Creates sample curriculum data |
| `generate_content` | Generates lesson content for a course |
| `generate_lesson_content` | Generates content for a single lesson |
| `generate_media` | Generates images via Gemini for lesson steps |
| `index_openstax` | Indexes OpenStax open-source resources into the knowledge base |

---

### tutoring

**Purpose**: The core conversational tutoring engine, session management, student progress tracking, skill extraction, and exit ticket assessments.

This app contains the AI tutoring logic that students interact with. It manages the full lifecycle of a tutoring session: from starting a lesson, through step-by-step instruction with LLM-generated responses, to exit ticket assessment and mastery tracking.

#### Models

| Model | Description |
|-------|-------------|
| `TutorSession` | A single student-lesson interaction. Tracks status (`active`/`completed`/`abandoned`), current step, engine state, and safety flags. |
| `SessionTurn` | Individual messages in the conversation (system/tutor/student). Stores content, token counts, and metadata (hints, grading results). |
| `StudentLessonProgress` | Cross-session mastery tracking per student per lesson. Tracks mastery level (`not_started`/`in_progress`/`mastered`), correct streaks, and best scores. |
| `ExitTicket` | Summative assessment for a lesson. One per lesson, 80% pass threshold, 10 questions randomly selected per session from a bank of 30-35. |
| `ExitTicketQuestion` | Individual MCQ with 4 options, correct answer, explanation, concept tag, and difficulty level (easy/medium/hard). Optional image field. |
| `ExitTicketAttempt` | Records each student attempt with score, pass/fail, and detailed answer breakdown. |
| `Skill` | An atomic, measurable learning outcome extracted from lessons. Has difficulty score, Bloom's level, and prerequisite relationships. |
| `LessonPrerequisite` | Prerequisite relationship between lessons (direct or skill-inferred) with strength score. Powers the lock UI in the catalog. |
| `StudentSkillMastery` | Per-student skill mastery tracking with performance history. |
| `SkillPracticeLog` | Logs individual skill practice events for spaced repetition scheduling. |
| `StudentKnowledgeProfile` | Aggregated knowledge profile per student for personalization. |

#### Key Files

| File | Description |
|------|-------------|
| `conversational_tutor.py` | **Core engine** (~3,250 lines). `ConversationalTutor` class manages the full session lifecycle: system prompt construction, media catalog building, step-by-step progression, LLM response generation, answer evaluation, step advancement, exit ticket triggering, and remediation. |
| `views.py` | Student-facing endpoints: lesson catalog, session start/resume, chat respond, exit ticket submission, review lesson. |
| `grader.py` | Answer evaluation: grades open-ended responses, scores MCQs, LLM-based rubric evaluation. |
| `image_service.py` | Image generation via Gemini with enhanced prompts (lesson context, "SCHEMATIC map" style, anti-hallucination rules). |
| `skill_extraction.py` | LLM-powered skill extraction from lesson content. Builds skill prerequisite graph and detects cross-lesson prerequisites. |
| `skills_models.py` | Models for the skill knowledge graph (Skill, LessonPrerequisite, StudentSkillMastery, etc.). |
| `personalization.py` | Adaptive difficulty, interleaved practice scheduling, prerequisite gating logic. |

#### ConversationalTutor Engine

The engine uses a `SessionState` enum with three states:
- **`TUTORING`** -- Working through lesson steps (display phase comes from step's 5E `phase` field)
- **`EXIT_TICKET`** -- Taking the summative assessment
- **`COMPLETED`** -- Session finished

Key methods:
- `respond(student_input)` -- Main entry point. Evaluates answers, advances steps, generates tutor response via LLM.
- `_evaluate_step()` -- LLM evaluator: determines answer correctness + step completion in one call.
- `_should_advance_step()` -- Logic for when to move to the next step (safety valve at 8 exchanges).
- `_build_media_catalog()` -- Creates numbered `[N] title` entries for deterministic media lookup.
- `_parse_media_signal()` -- Extracts `|||MEDIA:N|||` tail-line signals from LLM responses (clean DB storage).

#### Media Signal Pipeline

The tutor can show media during sessions using a deterministic signal system:
1. System prompt includes a `<media_catalog>` with numbered entries
2. LLM appends `|||MEDIA:N|||` as the last line when media should be shown
3. `_parse_media_signal()` parses and strips the signal before saving to DB
4. Frontend `sanitizeContent()` strips any leaked signals

#### Management Commands

| Command | Description |
|---------|-------------|
| `generate_exit_tickets` | Generates exit ticket question banks for lessons |
| `detect_prerequisites` | Detects and backfills lesson prerequisites from skill relationships (no LLM calls). Supports `--course N` and `--clear` flags. |

#### Test Suite

16 test files covering requirements R1-R14:

| Test File | Coverage |
|-----------|----------|
| `test_r1_skill_extraction_pipeline.py` | Skill extraction from lessons |
| `test_r2_skill_assessment_wiring.py` | Skill assessment integration |
| `test_r3_r4_session_personalization.py` | Adaptive personalization |
| `test_r5_remediation_wiring.py` | Remediation before exit |
| `test_r6_interleaved_practice.py` | Spaced practice scheduling |
| `test_r7_prerequisite_gating.py` | Prerequisite lock/unlock logic |
| `test_r8_safety_wiring.py` | Content safety flagging |
| `test_r9_system_prompt.py` | System prompt construction |
| `test_r10_mastery_transitions.py` | SessionState transitions |
| `test_r11_student_profile.py` | Profile-based adaptation |
| `test_r12_concept_coverage.py` | Concept coverage tracking |
| `test_r13_gamification.py` | Gamification elements |
| `test_r14_worked_examples.py` | Worked example presentation |

---

### dashboard

**Purpose**: The teacher-facing admin panel for curriculum management, content generation monitoring, student progress tracking, and safety review.

Teachers use the dashboard to upload curriculum documents, review/edit generated content, monitor student progress, manage classes, and review flagged conversations.

#### Models

| Model | Description |
|-------|-------------|
| `CurriculumUpload` | Tracks curriculum document uploads through the processing pipeline: `pending` > `processing` > `review` > `media_processing` > `completed`/`failed`. Stores parsed data, processing logs, and results. |
| `TeachingMaterialUpload` | Tracks teaching material uploads (textbooks, references, worksheets, question banks). Materials are indexed into the knowledge base for RAG. Nullable institution for platform-wide uploads. |
| `TeacherClass` | Groups students into classes for easier management. M2M with students and courses. |

#### Key Files

| File | Description |
|------|-------------|
| `views.py` | Main admin interface (~2,500 lines). Curriculum CRUD, upload processing, content generation, student/class management, safety review, settings. Protected by `@teacher_required` decorator. |
| `background_tasks.py` | Background task runner using threading. Manages parallel content generation (ThreadPoolExecutor, 2 workers), media generation, exit ticket creation, and skill extraction. |
| `views_health.py` | Health check endpoint for container orchestration (liveness/readiness probes). |
| `urls.py` | Dashboard routes under `/dashboard/` namespace. |
| `templatetags/dashboard_extras.py` | Custom template filters for the dashboard. |

#### Dashboard Pages

| Page | URL | Description |
|------|-----|-------------|
| Home | `/dashboard/` | Key metrics overview |
| Curriculum List | `/dashboard/curriculum/` | All courses with status |
| Course Detail | `/dashboard/curriculum/course/<id>/` | Units, lessons, generation controls |
| Lesson Detail | `/dashboard/curriculum/lesson/<id>/` | Steps, media, exit questions, prerequisites with inline editing |
| Upload | `/dashboard/curriculum/upload/` | Upload curriculum PDF/DOCX |
| Students | `/dashboard/students/` | Student list with progress |
| Student Detail | `/dashboard/students/<id>/` | Individual progress report |
| Classes | `/dashboard/classes/` | Class management |
| Flagged Sessions | `/dashboard/flagged/` | Safety review queue |
| Settings | `/dashboard/settings/` | Platform branding, schools, grades |

#### Content Generation Pipeline (Background)

The `generate_all_content_async()` function runs the full pipeline for a course:

1. Identifies lessons needing generation (skips those with 5+ steps)
2. Processes lessons in parallel (2 workers via ThreadPoolExecutor)
3. Each lesson runs: **steps** > **media** > **exit tickets** > **skills**
4. After all lessons complete, runs **course-level prerequisite detection**
5. Logs progress to the `CurriculumUpload` record for real-time UI updates

---

### llm

**Purpose**: LLM provider abstraction layer and customizable prompt management.

This app decouples the tutoring engine from specific LLM providers, allowing institutions to configure different models for different purposes and customize the tutor's behavior through prompt packs.

#### Models

| Model | Description |
|-------|-------------|
| `PromptPack` | Collection of prompts defining tutor behavior. Components: system prompt, teaching style, safety rules, format rules. Extended prompts for tutoring, content generation, exit tickets, grading, and image generation. Supports `{institution_name}`, `{grade_level}`, etc. placeholders. |
| `ModelConfig` | LLM provider configuration per institution. Supports 5 providers, 5 purposes, encrypted API key storage (Fernet via Django SECRET_KEY), custom API endpoints, and per-model temperature/max_tokens. |

#### Supported Providers

| Provider | Models | Notes |
|----------|--------|-------|
| Anthropic | Claude Haiku, Sonnet, Opus | Recommended for tutoring |
| OpenAI | GPT-4o, GPT-4o-mini | Alternative provider |
| Google | Gemini Flash, Gemini Pro | Recommended for image generation |
| Azure OpenAI | Any Azure-hosted model | Enterprise deployments |
| Local Ollama | Llama, Mistral, etc. | Offline/development use |

#### Purpose-Based Model Selection

Different tasks can use different models via `ModelConfig.get_for(purpose)`:

| Purpose | Typical Model | Why |
|---------|---------------|-----|
| `tutoring` | Claude Haiku | Fast, low-cost for conversational responses |
| `generation` | Claude Sonnet | Higher quality for curriculum content |
| `exit_tickets` | Claude Haiku | Structured MCQ generation |
| `skill_extraction` | Claude Haiku | Skill analysis from lesson content |
| `image_generation` | Gemini Flash | Native image generation |

#### Key Files

| File | Description |
|------|-------------|
| `models.py` | PromptPack + ModelConfig with encrypted API key storage |
| `client.py` | Provider-agnostic LLM client abstraction |
| `prompts.py` | Built-in default system prompts |
| `json_utils.py` | Utilities for parsing LLM JSON responses (handles markdown fences, single quotes, Python booleans) |

---

### media_library

**Purpose**: Manages reusable media assets (images, audio, video, PDFs) that can be attached to lesson steps.

#### Models

| Model | Description |
|-------|-------------|
| `MediaAsset` | A reusable media file scoped to an institution. Types: image, audio, video, PDF. Includes alt text, caption, and tags for accessibility and search. Files organized by institution slug. |
| `StepMedia` | Attachment of a `MediaAsset` to a `LessonStep` with placement info (top, inline, side panel) and ordering. |

#### Key Files

| File | Description |
|------|-------------|
| `models.py` | MediaAsset + StepMedia with upload path organization |

---

### safety

**Purpose**: Content safety, compliance monitoring, and GDPR data protection.

Provides audit logging for safety events, consent tracking for GDPR compliance, and tools for data export/deletion.

#### Models

| Model | Description |
|-------|-------------|
| `SafetyAuditLog` | Event log for safety incidents: content flagging, rate limiting, age verification, data access, consent changes. Indexed on event type, user hash, and severity for efficient querying. |
| `ConsentRecord` | GDPR consent tracking per user per consent type (data processing, AI tutoring, analytics, parental consent). Tracks given/withdrawn timestamps and parental contact info. |

#### Key Files

| File | Description |
|------|-------------|
| `models.py` | SafetyAuditLog + ConsentRecord |
| `views.py` | Privacy dashboard, privacy policy, terms of service, data export/deletion endpoints |
| `urls.py` | Safety routes |
| `SafetyMiddleware` | Middleware for IP logging and session tracking |

#### Management Commands

| Command | Description |
|---------|-------------|
| `delete_user_data` | GDPR-compliant user data deletion |
| `export_user_data` | Export all user data as JSON |
| `safety_cleanup` | Clean up old audit log entries |

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | `dev-secret-key...` | Django secret key (change in production) |
| `DEBUG` | `True` | Debug mode |
| `DATABASE_URL` | SQLite | Database connection string (Postgres in production) |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts |
| `CSRF_TRUSTED_ORIGINS` | (empty) | Comma-separated trusted origins for CSRF |
| `ANTHROPIC_API_KEY` | (empty) | Anthropic API key |
| `OPENAI_API_KEY` | (empty) | OpenAI API key |
| `GOOGLE_API_KEY` | (empty) | Google AI API key |
| `EMBEDDING_BACKEND` | `local` | Embedding backend: `local` (sentence-transformers) or `openai` |
| `VECTORDB_ROOT` | `media/vectordb` | ChromaDB storage path (use `/tmp/vectordb` in production for fast local disk) |
| `EMAIL_BACKEND` | console | Email backend for notifications |

### Django Settings (`config/settings.py`)

- **Database**: `dj_database_url` -- SQLite in dev, Postgres in production
- **Static files**: WhiteNoise with `CompressedManifestStaticFilesStorage`
- **Media**: `FileSystemStorage` with explicit serve view (works in production without DEBUG)
- **Logging**: Console output to stdout for container visibility
- **Security**: SSL redirect, secure cookies, and CSRF protection in production

---

## LLM Providers

### Anthropic Claude (Recommended)

Set `ANTHROPIC_API_KEY` in `.env`. Create a `ModelConfig` in Django admin:
- Provider: `anthropic`
- Model: `claude-haiku-4-5-20251001` (tutoring) or `claude-sonnet-4-6` (generation)
- API key env var: `ANTHROPIC_API_KEY`

### OpenAI GPT

Set `OPENAI_API_KEY` in `.env`. Create a `ModelConfig`:
- Provider: `openai`
- Model: `gpt-4o` or `gpt-4o-mini`
- API key env var: `OPENAI_API_KEY`

### Google Gemini

Set `GOOGLE_API_KEY` in `.env`. Create a `ModelConfig`:
- Provider: `google`
- Model: `gemini-2.0-flash` or `gemini-2.5-pro`
- API key env var: `GOOGLE_API_KEY`

### Local Ollama

1. Install Ollama: https://ollama.ai
2. Pull a model: `ollama pull llama3`
3. Start Ollama: `ollama serve`
4. Create a `ModelConfig`:
   - Provider: `local_ollama`
   - Model: `llama3`
   - API base: `http://localhost:11434`

---

## Content Generation Pipeline

The system supports two content workflows:

### Automatic Pipeline (via Dashboard)

1. Teacher uploads curriculum PDF/DOCX at `/dashboard/curriculum/upload/`
2. Parser extracts course/unit/lesson hierarchy
3. Teacher reviews and approves the parsed structure
4. "Generate All" triggers parallel content generation:
   - **Step 1**: LLM generates 8-12 lesson steps per lesson (5E model)
   - **Step 2**: Gemini generates educational diagrams for steps with media descriptions
   - **Step 3**: LLM generates 35 exit ticket MCQs per lesson
   - **Step 4**: LLM extracts skills and builds prerequisite graph
   - **Step 5**: Course-level prerequisite detection across all lessons

### Manual Editing

Teachers can edit any generated content via the dashboard:
- **Lesson steps**: Edit teacher scripts, questions, expected answers via step editor
- **Exit questions**: Inline edit/delete questions, with difficulty and concept tagging
- **Prerequisites**: Add/remove prerequisite lessons via pill UI
- **Publish/Unpublish**: Control which lessons are visible to students

---

## Management Commands

| Command | Description |
|---------|-------------|
| `python manage.py seed_seychelles` | Seed Seychelles S1 curriculum (Geography + Mathematics) |
| `python manage.py generate_content --course <id>` | Generate lesson content for a course |
| `python manage.py generate_media --course <id>` | Generate images for lesson steps |
| `python manage.py generate_exit_tickets --course <id>` | Generate exit ticket questions |
| `python manage.py detect_prerequisites` | Detect lesson prerequisites from skill graph |
| `python manage.py detect_prerequisites --course <id>` | Detect prerequisites for a specific course |
| `python manage.py detect_prerequisites --clear` | Clear and rebuild all prerequisites |
| `python manage.py index_openstax` | Index OpenStax resources into knowledge base |
| `python manage.py export_user_data --user <id>` | Export user data (GDPR) |
| `python manage.py delete_user_data --user <id>` | Delete user data (GDPR) |
| `python manage.py safety_cleanup --days <n>` | Clean up old audit logs |

---

## Deployment

### Docker

```bash
# Build image (use --platform for Azure)
docker build --platform linux/amd64 -t aitutor .

# Run locally
docker run -p 8000:8000 --env-file .env aitutor
```

The Dockerfile uses a multi-stage build:
1. **Builder stage**: Installs Python dependencies + CPU-only PyTorch
2. **Runtime stage**: Copies dependencies, collects static files
3. **CMD**: Runs migrations, copies vectordb to fast local disk, starts Gunicorn (4 workers, 4 threads, 120s timeout)

### Azure Container Apps (Production)

Infrastructure is managed with Pulumi IaC (`infra/__main__.py`):

- **Container App**: 4 vCPU, 8 GiB RAM (Dedicated D4 workload profile)
- **Database**: Azure PostgreSQL Flexible Server
- **Storage**: Azure File Share for media + vectordb
- **Registry**: Azure Container Registry (ACR)
- **CI/CD**: GitHub Actions on push to `main`

```bash
# Deploy infrastructure
cd infra
pulumi up --stack pixel

# Manual deploy
az acr build --registry aitutorpixelacr --image aitutor:latest --platform linux/amd64 .
az containerapp update --name aitutor-pixel-app --resource-group aitutor-pixel-rg --image aitutorpixelacr.azurecr.io/aitutor:latest
```

### CI/CD Pipeline (`.github/workflows/deploy.yml`)

Triggered on push to `main`:
1. Builds Docker image with `--platform linux/amd64`
2. Pushes to ACR with `latest` + commit SHA tags
3. Updates Container App with new revision

### Known Production Considerations

- **VectorDB on SMB**: Azure File Share (SMB) is too slow for SQLite-backed ChromaDB. The Dockerfile CMD copies vectordb from the mount to `/tmp/vectordb` on startup.
- **arm64 vs amd64**: Local Mac builds produce arm64 images. Azure requires amd64. Always use `--platform linux/amd64` or build via GitHub Actions.
- **Media serving**: Django's `static()` helper only works with `DEBUG=True`. The project uses an explicit `serve` view in `config/urls.py` for production media serving.

---

## URL Routes

| URL Pattern | View | Description |
|-------------|------|-------------|
| `/` | redirect | Redirects to `/tutor/` |
| `/accounts/login/` | accounts | Student/staff login |
| `/accounts/register/` | accounts | Student self-registration |
| `/tutor/` | tutoring | Subject and lesson catalog |
| `/tutor/lesson/<id>/` | tutoring | Tutoring session (chat interface) |
| `/dashboard/` | dashboard | Teacher dashboard home |
| `/dashboard/curriculum/` | dashboard | Curriculum management |
| `/dashboard/curriculum/lesson/<id>/` | dashboard | Lesson detail with step/question editing |
| `/dashboard/curriculum/lesson/<id>/prerequisites/` | dashboard | Prerequisite add/remove (AJAX) |
| `/dashboard/students/` | dashboard | Student progress list |
| `/dashboard/flagged/` | dashboard | Flagged session review |
| `/dashboard/settings/` | dashboard | Platform configuration |
| `/admin/` | Django admin | Django admin interface |
| `/health/` | health check | Container liveness/readiness probe |

---

## Testing

```bash
# Run all tests
python manage.py test

# Run tests for a specific app
python manage.py test apps.tutoring.tests

# Run a specific test file
python manage.py test apps.tutoring.tests.test_r7_prerequisite_gating
```

The test suite covers 14 requirements (R1-R14) across skill extraction, assessment, personalization, remediation, prerequisites, safety, system prompts, mastery transitions, student profiles, concept coverage, gamification, and worked examples.

---

## Pedagogy

The AI tutor follows research-based pedagogy:

1. **5E Instructional Model** -- Lessons follow Engage > Explore > Explain > Practice > Evaluate phases
2. **Socratic Questioning** -- The tutor never gives direct answers; instead guides students through questions
3. **Scaffolded Hints** -- Three-level hint ladder before revealing answers
4. **Retrieval Practice** -- Interleaved review of previously learned concepts
5. **Exit Ticket Assessment** -- Standardized 10-MCQ summative assessment with 80% pass threshold
6. **Remediation** -- Students who answer incorrectly are guided through relevant steps before retaking the exit ticket
7. **Prerequisite Gating** -- Lessons are locked until prerequisite lessons are mastered
8. **Local Context** -- Examples and scenarios adapted for Seychelles geography and culture

---

## License

MIT License

## Contributors

- World Bank Education Team
- Roy & Edward (Development Lead)
