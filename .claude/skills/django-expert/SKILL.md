---
name: django-expert
description: Expert-level Django 5 patterns for the AI Tutor backend. Auto-loads when working on Python files under apps/, config/, or infra/. Covers Django models, ORM, views, DRF (when added), migrations, multi-tenancy, auth, and this project's specific constraints (Azure Container Apps, institution scoping, LLM integration patterns). Use when writing or modifying Django code.
paths:
  - "apps/**/*.py"
  - "config/**/*.py"
  - "infra/**/*.py"
  - "manage.py"
  - "tests/**/*.py"
---

# Django Expert — AI Tutor Backend

Expert guidance for Django 5 work on this specific codebase. For general Django docs, don't replicate — assume the reader knows Django. This focuses on patterns that are PROJECT-SPECIFIC or commonly-missed.

## The stack

- Django 5.x, Python 3.11
- PostgreSQL prod (Azure), SQLite dev
- Gunicorn 4 workers × 4 threads, 120s timeout
- WhiteNoise for static files
- ChromaDB (via `apps/curriculum/knowledge_base.py`) with sentence-transformers embeddings
- LLM abstraction: `apps/llm/client.py::BaseLLMClient` (Anthropic / OpenAI / Google / Ollama)
- **No DRF yet** (planned in `memory/mobile_rn_plan.md` Phase A — Django REST Framework + JWT + CORS + `/api/v1/*`)
- Azure Container Apps deploy via Pulumi (`infra/`) + GitHub Actions

## Apps and what each owns

| App | Domain | Key files |
|---|---|---|
| `accounts` | Auth, User, Institution, Membership, StudentProfile, TutorPersonality | `models.py`, `views.py` |
| `curriculum` | Course, Unit, Lesson, LessonStep, ContentGenerator, KnowledgeBase | `models.py`, `content_generator.py`, `knowledge_base.py` |
| `tutoring` | TutorSession, SessionTurn, ExitTicket, ConversationalTutor engine | `tutoring_models.py`, `conversational_tutor.py`, `views.py` |
| `dashboard` | Teacher UI, curriculum upload + review, progress reports | `views.py` (large), `background_tasks.py` |
| `llm` | ModelConfig, PromptPack, LLM client abstraction | `client.py`, `models.py` |
| `media_library` | MediaAsset storage (institution-scoped) | `models.py` |
| `safety` | ContentSafetyFilter, RateLimiter, AuditLog, consent/GDPR | `filters.py`, `rate_limiter.py` |

## Critical patterns for this project

### 1. Multi-tenancy (institution scoping)

**Every query touching user data must scope by institution.** Missing this is a cross-school data leak.

Three institution states:
- `institution=<inst>` — scoped to one school
- `institution=None` — platform-wide / "All Schools" super-admin content
- In ChromaDB: `None` normalized to `GLOBAL_INSTITUTION_ID = 0` (see `apps/curriculum/knowledge_base.py:106`)

Standard query pattern for student-facing views (include platform-wide content):

```python
from django.db.models import Q

def get_visible_lessons(student):
    inst = student.memberships.filter(is_active=True).first().institution
    return Lesson.objects.filter(
        Q(unit__course__institution=inst) | Q(unit__course__institution__isnull=True),
        is_published=True,
    )
```

For admin/teacher queries, institution is usually narrower — scope to their institution only unless they're super-admin (`user.is_staff`).

### 2. The LLM client abstraction (`apps/llm/client.py`)

Never call Anthropic/OpenAI SDK directly. Always go through the abstraction:

```python
from apps.llm.client import get_llm_client
from apps.llm.models import ModelConfig

config = ModelConfig.get_for(institution=inst, purpose='tutoring')
client = get_llm_client(config)
response = client.generate(
    messages=[{"role": "user", "content": "..."}],
    system_prompt="...",
    max_tokens=1024,
)
# response.content, response.tokens_in, response.tokens_out, response.model, response.stop_reason
```

`ModelConfig.get_for(purpose=X)` picks the right model per purpose: `generation`, `tutoring`, `exit_tickets`, `skill_extraction`, `image_generation`.

### 3. The ConversationalTutor engine

Core file: `apps/tutoring/conversational_tutor.py`. This is large (~4000+ lines) — don't try to read it all at once.

Key rules (also in `CLAUDE.md`):
- **`SessionState` enum, not `ConversationPhase`**. Values: TUTORING, EXIT_TICKET, COMPLETED. Display-level 5E phase comes from each `LessonStep.phase`.
- **Media signal**: tutor appends `|||MEDIA:N|||` as the LAST line (N = 1-based index into `_media_id_map`). Parse + strip BEFORE `_save_turn()`.
- **State persistence**: engine state lives in `TutorSession.engine_state` (JSONField). Loaded via `_load_state()`, saved via `_save_state()`.
- **Public API**: `respond(student_input: str) -> TutorMessage`, `respond_stream(...)` (exists but unused in prod — Azure Container Apps buffers).
- **Step evaluation**: `_evaluate_step()` uses `instructor` (structured LLM) returning `StepEvaluationResult`. `_should_advance_step()` applies safety valves (exchange cap, practice fast-path).
- **Exit ticket trigger**: `current_topic_index >= len(self.steps)` (no more steps). NOT by phase transition.

### 4. Content generator JSON robustness

LLMs sometimes return single-quoted Python dicts, not JSON. `apps/curriculum/content_generator.py` has defenses:
- `_try_fix_json()` — handles single-quote → double-quote, Python bool/None → JSON literals
- `_generate_steps()` — 3-attempt retry loop with correction prompt

Don't bypass these. If you see JSON-parsing errors, extend these defenses.

### 5. Logging in background threads

Background generation (`dashboard/background_tasks.py`, `material_tasks.py`) runs in threads, not Celery. `logger.info()` may not show in dev server. Use:

```python
print(f"[ContentGen] Starting lesson {lesson_id}", flush=True)
```

The `[ContentGen]` prefix is the convention — grep-friendly.

### 6. Stuck content generation

Content generation can hang with `content_status='generating'`. Manual reset:

```python
from apps.curriculum.models import Lesson
Lesson.objects.filter(content_status='generating').update(content_status='pending')
```

If you're writing new content-gen logic, always wrap in try/except + set `failed` or `pending` on exception.

### 7. Media files and MediaAsset

- `MediaAsset` FK to `Institution` (scoped)
- Files go to `media/{institution.slug}/{filename}`
- Served at `/media/<path>` — no auth gate (direct file serve via `serve()` in `config/urls.py`). OK for images/audio. Don't put sensitive data there.
- `LessonStep.media` (JSONField) references these via URL strings: `{"images": [{"url": ..., "alt": ...}], ...}`

### 8. Azure Container Apps gotchas

See `CLAUDE.md` for the full list. Key ones when writing code:

- **No SSE** — use `JsonResponse`, not `StreamingHttpResponse`
- **ChromaDB on /tmp** — `VECTORDB_ROOT=/tmp/vectordb`, Dockerfile copies from SMB mount at startup
- **Images must be amd64** — your Mac builds arm64 by default. Use GitHub Actions for deploys, or `--platform linux/amd64` locally.
- **CSRF_TRUSTED_ORIGINS** uses `env.default_domain` in Pulumi (not hardcoded)

## Migration patterns

### Safe migrations

- One logical change per migration file
- Descriptive names: `0014_add_session_participant.py`
- Data migrations: use `RunPython` with forward + reverse function
- Backfill migrations: dry-run against a prod DB dump FIRST
- Column removal: split across two releases — stop writing to column (release N), drop column (release N+1)

### Dry-run checklist

```bash
# Before running in prod:
python manage.py makemigrations --dry-run
python manage.py migrate --plan  # see SQL to be run
python manage.py sqlmigrate <app_name> <migration_number>  # inspect SQL
```

## DRF patterns (once installed)

The RN plan (`memory/mobile_rn_plan.md`) adds DRF. When you're building those endpoints, default pattern:

```python
# apps/api/views/lessons.py
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from apps.api.permissions import IsInstitutionMember
from apps.api.mixins import InstitutionScopedMixin

class LessonListView(InstitutionScopedMixin, APIView):
    permission_classes = [IsAuthenticated, IsInstitutionMember]

    def get(self, request):
        qs = self.scope_to_institution(Lesson.objects.all())  # enforces scoping
        serializer = LessonSerializer(qs, many=True)
        return Response(serializer.data)
```

The `InstitutionScopedMixin` should be the default — every mobile view uses it.

**Auth**: JWT via `simplejwt`, `SessionAuthentication` kept for web parity. Dual-auth in `DEFAULT_AUTHENTICATION_CLASSES`.

**OpenAPI**: use `drf-spectacular` — auto-generates schema for mobile TS type generation.

## Testing patterns

```python
# apps/tutoring/tests/test_foo.py
import pytest
from django.contrib.auth.models import User
from apps.tutoring.tutoring_models import TutorSession

@pytest.mark.django_db
def test_something(student_user, published_lesson):
    # Use factories where they exist (apps/*/tests/factories.py)
    session = TutorSession.objects.create(
        student=student_user,
        lesson=published_lesson,
        institution=student_user.memberships.first().institution,
    )
    assert session.status == 'active'
```

Run scoped tests: `pytest apps/tutoring/tests/ -k mastery -v`.

## Before making risky changes

**Before editing `config/settings.py`**: check if the variable is referenced in `infra/__main__.py` or `Dockerfile`. Settings changes can silently break production.

**Before a migration**: run `python manage.py makemigrations --dry-run` first. Unnecessary migrations (from model docstring changes, etc.) bloat history.

**Before deleting a field**: grep the whole project for the field name. Dead fields often have template references that break silently.

**Before changing `conversational_tutor.py`**: read the relevant method + its callers. State is shared via `self.` attributes persisted in `engine_state`. Adding a new attribute requires updating `_save_state()` and `_load_state()`.

## Resources to read when needed

- `CLAUDE.md` — always-apply project rules
- `memory/lesson_competency_plan.md` — the mastery/exit-ticket work in progress
- `memory/mobile_rn_plan.md` — upcoming DRF + JWT + mobile API layer
- `README.md` — architecture overview (780 lines, deliberate — don't edit casually)
- Auto-memory at `~/.claude/projects/-Users-edwardamoah-Documents-GitHub-ai-tutor/memory/` — deployment history, past incidents
