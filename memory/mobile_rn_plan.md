---
name: React Native Mobile App - Detailed Plan
description: Execution plan for building the AI Tutor mobile app in React Native (Expo) with offline tutoring via on-device LLMs. Solo developer, companion to project_offline_mobile.md architecture decisions.
type: project
originSessionId: 09ee43ad-944b-4bd2-a344-262985284940
---
# React Native Mobile App — Detailed Plan (2026-04-23)

Companion to `project_offline_mobile.md` (which captures framework-agnostic architecture decisions). This doc is the concrete RN execution plan grounded in a full audit of the current Django project.

## Context

- **Who's building**: Solo (Edward). Backend strength (Django/Python), JS familiar, new to native mobile.
- **Why RN over Flutter**: Solo + JS familiarity + larger community + Expo's solo-dev ergonomics + `llama.rn` provides a reasonable on-device LLM path.
- **What exists today**: Django monolith with 7 apps (accounts, curriculum, tutoring, llm, media_library, safety, dashboard), session-cookie auth, vanilla-JS templates, many existing `JsonResponse`-returning views under `/tutor/api/*`. **No DRF, no token auth, no CORS headers, no channels/SSE in production.**
- **Driver**: Seychelles pilot has intermittent connectivity. Native app with offline tutoring is the eventual goal; content-review-only mode ships earlier as value-delivery.

## Tech stack (final decisions)

| Layer | Choice | Rationale |
|---|---|---|
| Framework | **Expo SDK 55 with dev client** (RN 0.83, React 19.2) | Avoids bare RN config pain; dev client lets us load native LLM modules. New Architecture (Fabric + Turbo Modules) is mandatory in SDK 55 — good fit for `llama.rn` performance. Minimums: iOS 15.1+, Android 7+, Xcode 26.2+. |
| Language | **TypeScript** | Non-negotiable — safety for a solo dev across a big codebase |
| Navigation | **Expo Router** (file-based) | Next.js-like paradigm, less boilerplate than React Navigation |
| State (client) | **Zustand** | Lighter than Redux; no providers; simple API. Plenty for this scope. |
| State (server) | **TanStack Query (React Query) v5** | Caching, retries, background refetch for API calls |
| Storage | **expo-sqlite + Drizzle ORM** | Type-safe SQLite via Drizzle; expo-sqlite is first-party & maintained |
| HTTP | **Axios** with interceptors | Auth token injection, retry logic; fetch is fine too but interceptors are simpler in Axios |
| Secure storage | **expo-secure-store** | Keychain (iOS) / Keystore (Android) for auth tokens |
| Connectivity | **expo-network** + **@react-native-community/netinfo** | Online/offline detection; netinfo has better change listeners |
| On-device LLM | **llama.rn** (primary) | GGUF ecosystem → download any Qwen 3.5 / Gemma variant from HuggingFace |
| LLM fallback (later) | `react-native-executorch` or custom MediaPipe Turbo Module | For audio input or better optimization when pilot demands |
| Build | **EAS Build** | Cloud builds for iOS & Android; handles code signing |
| Distribution | **EAS Submit** → TestFlight + Google Play Internal | Solo-friendly; skip app store review during pilot |
| Icons | **@expo/vector-icons** | Bundled with Expo, no config |
| Forms | **react-hook-form** + **zod** | Forms + validation for login, settings; zod schemas shared with API types |
| Testing | **Jest** + **@testing-library/react-native** | Enough for critical paths; skip E2E for v1 |
| Error tracking | **Sentry** (via `@sentry/react-native`) | Free tier plenty for pilot; crash reports matter on mobile |

**Explicit non-choices**: No Redux, no GraphQL (REST is fine), no Nativewind/Tailwind (StyleSheet.create is simpler for solo — don't chase web patterns).

---

## Phase A — Backend API work (Django side, 2 weeks, before any RN code)

This is pure Django work that plays to existing strengths. It unblocks all mobile development and is reusable for a future PWA.

### A1 — Install DRF + CORS + token auth

```
pip install djangorestframework djangorestframework-simplejwt django-cors-headers drf-spectacular
```

Add to `config/settings.py`:
- `'rest_framework'`, `'corsheaders'`, `'drf_spectacular'` to INSTALLED_APPS
- `'corsheaders.middleware.CorsMiddleware'` to MIDDLEWARE (before CommonMiddleware)
- `REST_FRAMEWORK` config with JWT default auth + PageNumberPagination
- `CORS_ALLOWED_ORIGINS` env-configured for mobile dev (localhost:8081 for Expo) + production app bundle ID check (use `CORS_ALLOW_ALL_ORIGINS=False` in prod, whitelist origins)
- `SIMPLE_JWT` config with 1-day access + 30-day refresh

### A2 — API app and versioning

Create `apps/api/` — new Django app that houses all mobile-facing endpoints under `/api/v1/*`. This isolates mobile API from the existing template-rendered views; existing URLs under `/tutor/*` keep working for web.

Structure:
```
apps/api/
  __init__.py
  apps.py
  urls.py              # router + all /api/v1/ paths
  serializers/
    __init__.py
    auth.py            # LoginSerializer, RegisterSerializer, TokenObtainPair
    curriculum.py      # CourseSerializer, UnitSerializer, LessonSerializer, LessonStepSerializer
    tutoring.py        # TutorSessionSerializer, SessionTurnSerializer, TutorMessageSerializer
    exit_ticket.py
    models_catalog.py  # MobileInferenceModelSerializer
    progress.py        # StudentLessonProgressSerializer
  views/
    __init__.py
    auth.py
    lessons.py
    sessions.py
    offline_packs.py   # GET /api/v1/lessons/<id>/offline-pack
    mobile_models.py   # GET /api/v1/mobile/models
    sync.py
  permissions.py       # IsStudent, IsInstitutionMember
  pagination.py
  throttling.py        # Reuse apps.safety.RateLimiter logic
```

**Key design decisions**:
- **Reuse existing view logic**, don't rewrite. `chat_respond()` already returns JSON — wrap it in a DRF APIView that accepts JWT + returns the same dict as a serialized response.
- **JWT not session**: mobile uses `Authorization: Bearer <token>`; leave session auth in place for web. Dual-auth via `DEFAULT_AUTHENTICATION_CLASSES = [JWTAuthentication, SessionAuthentication]` is fine.
- **Institution scoping middleware/mixin**: add `InstitutionScopedMixin` that filters querysets by `request.user.memberships.filter(is_active=True)` — every view uses it.
- **OpenAPI schema**: drf-spectacular generates `/api/v1/schema/` and Swagger UI at `/api/v1/docs/`. Feeds the mobile type generator (see A5).

### A3 — Endpoints (mobile-first set)

| Method | Path | Purpose | Body / Response |
|---|---|---|---|
| POST | `/api/v1/auth/login/` | Student login | `{username, password}` → `{access, refresh, user: {...}}` |
| POST | `/api/v1/auth/register/` | Student self-register | `{username, password, email, school, grade_level}` → tokens + user |
| POST | `/api/v1/auth/refresh/` | Refresh access token | `{refresh}` → `{access}` |
| POST | `/api/v1/auth/logout/` | Blacklist refresh token | `{refresh}` → 204 |
| GET | `/api/v1/me/` | Current user + profile | `{user, student_profile, memberships}` |
| GET | `/api/v1/courses/` | Courses for student's institution | paginated |
| GET | `/api/v1/lessons/` | Lessons (filterable by course/unit) | paginated |
| GET | `/api/v1/lessons/<id>/` | Single lesson with unit + course | |
| GET | `/api/v1/lessons/<id>/steps/` | All steps for a lesson | array |
| **GET** | **`/api/v1/lessons/<id>/offline-pack/`** | **Full offline bundle (JSON + media manifest)** | `{lesson, steps, exit_ticket, policy, remediation_variants, media_manifest}` |
| POST | `/api/v1/sessions/` | Start tutor session for lesson | `{lesson_id}` → session |
| GET | `/api/v1/sessions/<id>/` | Session state + turns | |
| POST | `/api/v1/sessions/<id>/respond/` | Student message → tutor reply | existing `chat_respond` shape |
| POST | `/api/v1/sessions/<id>/exit-ticket/` | Submit exit ticket answers | |
| POST | `/api/v1/sessions/<id>/review/` | Start review after failed ticket | |
| **POST** | **`/api/v1/sessions/<id>/sync/`** | **Bulk-upload offline turns + state** | `{turns: [...], engine_state: {...}, exit_ticket_attempt: {...}}` → conflict result |
| GET | `/api/v1/progress/` | Student's lesson progress across all | |
| **GET** | **`/api/v1/mobile/models/`** | **Available on-device models for device** | query param `device_tier` → filtered catalog |
| POST | `/api/v1/transcribe/` | Audio → text (existing logic) | |
| POST | `/api/v1/speak/` | Text → audio URL | |
| GET | `/api/v1/gamification/` | Streak, points, milestones | |

**Not migrating initially** (stays web-only): curriculum upload, dashboard analytics, settings, flagged sessions. Teachers use the web dashboard.

### A4 — New Django models

Two new models to create in Phase A:

**`apps/llm/models.py` — `MobileInferenceModel`**
```python
class MobileInferenceModel(models.Model):
    id = models.CharField(max_length=100, primary_key=True)  # e.g. "qwen-3-5-2b-q4"
    display_name = models.CharField(max_length=200)
    family = models.CharField(choices=[('gemma_3n','...'),('qwen_3_5','...'),('llama','...')])
    size_mb = models.PositiveIntegerField()
    ram_required_mb = models.PositiveIntegerField()
    download_url = models.URLField()
    checksum_sha256 = models.CharField(max_length=64)
    license = models.CharField(max_length=50)  # "Apache 2.0", "Gemma"
    runtime = models.CharField(choices=[('llama_cpp',...),('mediapipe',...),('mlx',...),('executorch',...)])
    capabilities = models.JSONField(default=dict)  # {text, image, audio, video}
    chat_template = models.TextField()  # Jinja-style, includes {role} {content} etc.
    system_prompt_style = models.CharField(choices=[('system_role',...),('prepend_user',...),('none',...)])
    recommended_tier = models.CharField(choices=[('flagship',...),('mid',...),('low',...)])
    quality_scores = models.JSONField(default=dict)  # {math: 0.8, science: 0.85, ...} from M0
    is_active = models.BooleanField(default=True)
    min_app_version = models.CharField(max_length=20, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

**`apps/tutoring/tutoring_models.py` — add to `SessionTurn`**
```python
# New field on existing model, migration needed:
generated_offline = models.BooleanField(default=False)
offline_model_id = models.CharField(max_length=100, null=True, blank=True)  # FK-string to MobileInferenceModel
client_generated_at = models.DateTimeField(null=True, blank=True)  # separate from server `created_at`
```

**`apps/tutoring/tutoring_models.py` — new `LessonPackVersion`**
```python
class LessonPackVersion(models.Model):
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='pack_versions')
    version = models.PositiveIntegerField()
    policy_json = models.JSONField()  # state machine policy
    content_snapshot = models.JSONField()  # frozen steps + exit ticket at time of version
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        unique_together = [('lesson', 'version')]
```

Pack version auto-increments when teacher edits a published lesson. Sessions record `pack_version` they used so stale sync can be detected.

### A5 — Type generation

Use `drf-spectacular` to emit OpenAPI, then run `openapi-typescript` in the mobile repo to generate TS types. One command = types always in sync with server. Saves many bugs.

```bash
# in mobile/:
npx openapi-typescript http://localhost:8000/api/v1/schema/ -o src/api/types.ts
```

### A6 — Auth token migration notes

Existing student users have no passwords they use on mobile — they use the same username/password they already have for web. JWT login uses same credentials. No data migration needed. The `/api/v1/auth/login/` view just wraps `authenticate(username, password)` and issues tokens if valid.

**Production deploy**: existing Azure Container Apps deploy stays. The new `/api/v1/*` routes are served by the same gunicorn process. No infra changes.

---

## Phase B — Mobile feasibility (3 days, gated)

Before committing to months of RN work, validate the on-device model can actually do the job. Detail in `project_offline_mobile.md` under M0.

**Minimal harness for Phase B**:
1. Use the `llama.rn` example app as-is, or an off-the-shelf model-test app like Pocket Pal.
2. Download Qwen 3.5 2B (GGUF Q4_K_M), Qwen 3.5 4B, Gemma 3n E2B, Gemma 3n E4B onto a test device.
3. Hand-transcribe a real Grade 8 Physics lesson (hardest case per `feedback_math_tutoring.md`) into prompt form: system prompt + 10-turn conversation covering practice + remediation.
4. Score each model on: math-rule adherence, answer classification accuracy, per-turn latency, memory pressure.
5. Decision gate:
   - If best model scores acceptable (≥80% rule adherence + ≤3s/turn) → greenlight M3+ with that model as default
   - If not → kill on-device tutoring. Ship content-review-only app. On-device as v2 when models improve.

---

## Mobile project structure

```
mobile/                                  # Sibling directory to the Django project; separate repo OR monorepo
├── app/                                 # Expo Router — file-based routes
│   ├── _layout.tsx                      # Root layout: auth gate, theme, providers
│   ├── (auth)/
│   │   ├── login.tsx
│   │   └── register.tsx
│   ├── (app)/
│   │   ├── _layout.tsx                  # Tab bar
│   │   ├── index.tsx                    # Home: my courses
│   │   ├── lessons/
│   │   │   ├── [id].tsx                 # Lesson detail + "Start session"
│   │   │   └── [id]/download.tsx        # Pack download progress
│   │   ├── tutor/
│   │   │   └── [sessionId].tsx          # Chat screen (core)
│   │   ├── progress.tsx
│   │   └── settings/
│   │       ├── index.tsx
│   │       ├── models.tsx               # Model store (v2 — hidden behind flag in v1)
│   │       └── storage.tsx
│   └── +not-found.tsx
├── src/
│   ├── api/
│   │   ├── client.ts                    # Axios instance, interceptors (auth, refresh)
│   │   ├── auth.ts
│   │   ├── lessons.ts
│   │   ├── sessions.ts
│   │   ├── models.ts
│   │   ├── types.ts                     # generated from OpenAPI
│   │   └── errors.ts
│   ├── db/
│   │   ├── schema.ts                    # Drizzle schema (SQLite tables)
│   │   ├── client.ts                    # Drizzle + expo-sqlite setup
│   │   ├── migrations/                  # Drizzle migrations
│   │   └── queries/
│   │       ├── lessons.ts
│   │       ├── sessions.ts
│   │       └── sync_queue.ts
│   ├── inference/
│   │   ├── types.ts                     # InferenceClient interface
│   │   ├── llama-rn-client.ts           # llama.rn implementation
│   │   ├── registry.ts                  # resolve active model → client instance
│   │   ├── prompt-templates.ts          # render chat_template from catalog
│   │   └── mock-client.ts               # for dev without model
│   ├── model-manager/
│   │   ├── catalog.ts                   # fetch + cache mobile model catalog
│   │   ├── download.ts                  # resumable downloads, checksum
│   │   ├── storage.ts                   # disk usage, delete, active model selection
│   │   └── device-tier.ts               # detect RAM + storage → tier
│   ├── state-machine/
│   │   ├── runner.ts                    # consume JSON policy, drive tutor flow
│   │   ├── evaluators.ts                # deterministic answer-classifier adapters
│   │   └── types.ts                     # StateMachineState, TutorTurn, etc.
│   ├── sync/
│   │   ├── queue.ts                     # pending writes table
│   │   ├── worker.ts                    # background sync orchestration
│   │   └── conflict.ts                  # pack-version / engine_state resolution
│   ├── components/
│   │   ├── ChatMessage.tsx
│   │   ├── ChatInput.tsx
│   │   ├── LessonCard.tsx
│   │   ├── OfflineBanner.tsx
│   │   ├── ModelPicker.tsx
│   │   ├── ProgressBar.tsx
│   │   └── ui/                          # Button, Card, etc.
│   ├── hooks/
│   │   ├── useAuth.ts
│   │   ├── useOffline.ts
│   │   ├── useLesson.ts
│   │   ├── useSession.ts
│   │   └── useModel.ts
│   ├── stores/                          # Zustand
│   │   ├── auth-store.ts
│   │   ├── active-model-store.ts
│   │   └── ui-store.ts
│   ├── lib/
│   │   ├── audio.ts                     # expo-av wrapper for STT playback
│   │   ├── crypto.ts                    # sha256 for checksum verify
│   │   └── constants.ts
│   └── theme/
│       ├── colors.ts
│       ├── typography.ts
│       └── index.ts
├── plugins/                             # Expo config plugins for native modules
│   └── llama-rn-plugin.js
├── assets/
│   ├── icons/
│   └── splash/
├── e2e/                                  # Skip for v1
├── app.config.ts                         # Expo config (dynamic — env-aware)
├── eas.json                              # Build profiles: development, preview, production
├── babel.config.js
├── metro.config.js
├── tsconfig.json
├── package.json
└── README.md
```

---

## Data layer — SQLite schema (Drizzle)

Mirror-subset of Django tables. All writes go local first, sync worker pushes later.

```ts
// src/db/schema.ts (abridged)

export const courses = sqliteTable('courses', {
  id: integer('id').primaryKey(),
  title: text('title').notNull(),
  subject: text('subject').notNull(),
  grade_level: text('grade_level').notNull(),
  institution_id: integer('institution_id'),
  synced_at: integer('synced_at', { mode: 'timestamp' }),
});

export const lessons = sqliteTable('lessons', {
  id: integer('id').primaryKey(),
  unit_id: integer('unit_id').notNull(),
  title: text('title').notNull(),
  objective: text('objective'),
  order_index: integer('order_index'),
  pack_downloaded: integer('pack_downloaded', { mode: 'boolean' }).default(false),
  pack_version: integer('pack_version'),
  pack_downloaded_at: integer('pack_downloaded_at', { mode: 'timestamp' }),
  synced_at: integer('synced_at', { mode: 'timestamp' }),
});

export const lesson_packs = sqliteTable('lesson_packs', {
  lesson_id: integer('lesson_id').primaryKey(),
  version: integer('version').notNull(),
  policy_json: text('policy_json', { mode: 'json' }).notNull(),
  steps_json: text('steps_json', { mode: 'json' }).notNull(),
  exit_ticket_json: text('exit_ticket_json', { mode: 'json' }).notNull(),
  remediation_variants_json: text('remediation_variants_json', { mode: 'json' }),
  media_manifest_json: text('media_manifest_json', { mode: 'json' }),
  downloaded_at: integer('downloaded_at', { mode: 'timestamp' }).notNull(),
});

export const sessions = sqliteTable('sessions', {
  id: text('id').primaryKey(),              // client-generated UUID until synced; server ID merges on sync
  server_id: integer('server_id'),          // nullable until sync confirms
  lesson_id: integer('lesson_id').notNull(),
  student_id: integer('student_id').notNull(),
  pack_version: integer('pack_version').notNull(),
  status: text('status').notNull(),         // 'active', 'completed', 'abandoned'
  engine_state_json: text('engine_state_json', { mode: 'json' }),
  started_at: integer('started_at', { mode: 'timestamp' }).notNull(),
  ended_at: integer('ended_at', { mode: 'timestamp' }),
  last_synced_at: integer('last_synced_at', { mode: 'timestamp' }),
});

export const session_turns = sqliteTable('session_turns', {
  id: text('id').primaryKey(),              // UUID
  session_id: text('session_id').notNull(),
  server_id: integer('server_id'),          // nullable until synced
  role: text('role').notNull(),             // 'tutor', 'student', 'system'
  content: text('content').notNull(),
  step_id: integer('step_id'),
  metadata_json: text('metadata_json', { mode: 'json' }),
  generated_offline: integer('generated_offline', { mode: 'boolean' }).default(false),
  offline_model_id: text('offline_model_id'),
  client_generated_at: integer('client_generated_at', { mode: 'timestamp' }).notNull(),
  synced: integer('synced', { mode: 'boolean' }).default(false),
});

export const media_cache = sqliteTable('media_cache', {
  url: text('url').primaryKey(),
  local_path: text('local_path').notNull(),
  size_bytes: integer('size_bytes'),
  downloaded_at: integer('downloaded_at', { mode: 'timestamp' }).notNull(),
});

export const sync_queue = sqliteTable('sync_queue', {
  id: text('id').primaryKey(),              // UUID
  kind: text('kind').notNull(),             // 'turn', 'engine_state', 'exit_ticket'
  payload_json: text('payload_json', { mode: 'json' }).notNull(),
  session_id: text('session_id'),
  created_at: integer('created_at', { mode: 'timestamp' }).notNull(),
  attempt_count: integer('attempt_count').default(0),
  last_attempt_at: integer('last_attempt_at', { mode: 'timestamp' }),
  last_error: text('last_error'),
});

export const progress = sqliteTable('progress', {
  lesson_id: integer('lesson_id').primaryKey(),
  mastery_level: text('mastery_level').notNull(),  // 'not_started', 'in_progress', 'mastered'
  best_score: real('best_score'),
  last_session_at: integer('last_session_at', { mode: 'timestamp' }),
});

export const active_model = sqliteTable('active_model', {
  id: integer('id').primaryKey(),           // always 1 (singleton row)
  model_id: text('model_id').notNull(),     // FK-string to server's MobileInferenceModel.id
  set_at: integer('set_at', { mode: 'timestamp' }).notNull(),
});
```

**Sync strategy**:
- **Append-only for turns**: client always wins for `session_turns` (student said what they said; no conflict possible)
- **Last-write-wins for engine_state**: client-generated state is authoritative for that session (mobile is the writer)
- **Pack version check for policy/content**: server rejects sync if the session's `pack_version` was superseded and a teacher re-generated content. Client handles: shows "This lesson was updated — here's what's new"
- **Client-generated IDs**: UUIDs until server confirms, then server_id populated. Turns keep their client UUID as stable identifier.

---

## Inference abstraction — mirrors Django's `apps/llm/client.py`

**Design principle**: direct structural analog of the backend's `BaseLLMClient` abstraction. Same interface shape, same factory pattern, same config-driven selection. The engine/state-machine code calls `client.generate(messages, systemPrompt)` without knowing whether the request goes on-device or across the network.

### Mapping to backend

| Backend (`apps/llm/client.py`) | Mobile (`src/inference/`) |
|---|---|
| `BaseLLMClient` (abstract) | `InferenceClient` (TS interface) |
| `LLMResponse` dataclass | `InferenceResponse` interface |
| `get_llm_client(config: ModelConfig)` | `resolveClient(): InferenceClient` (reads active_model) |
| `AnthropicClient`, `OpenAIClient`, `OllamaClient` | `CloudApiClient`, `LlamaRnClient`, `ExecuTorchClient` |
| `ModelConfig` DB row | `MobileInferenceModel` (server catalog) + `active_model` (local SQLite) |
| `ModelConfig.get_api_key()` | `CloudApiClient` uses JWT; on-device clients need no keys |
| Django admin edits `ModelConfig` | App settings UI ("Model Store") edits `active_model` |

### Client hierarchy (three concrete implementations, one interface)

```
InferenceClient (abstract)
├── CloudApiClient         → POST /api/v1/sessions/<id>/respond/   [ONLINE only]
│                            Django backend proxies to Claude/Gemini (configurable via existing ModelConfig)
│                            Benefits: safety middleware, rate limiting, single school key
├── LlamaRnClient          → llama.rn + GGUF model on device       [OFFLINE; primary]
│                            Works for: Qwen 3.5 0.8B/2B/4B/9B, Gemma 3n E2B/E4B, Llama 3.2 1B/3B
└── ExecuTorchClient       → react-native-executorch                [OFFLINE; future/fallback]
                             Meta-backed, narrower model zoo
```

### Two axes of configurability

**Online LLM choice** — NOT user-facing. Managed via Django admin editing `ModelConfig` (Claude / Gemini / GPT-4). Mobile inherits transparently through `CloudApiClient`. This keeps safety filtering, rate limits, and school-level API keys server-enforced.

**Offline LLM choice** — user-facing. App has a Model Store screen:
- Fetches catalog from `GET /api/v1/mobile/models/` (admin-curated list of `MobileInferenceModel` entries)
- User sees per-model cards: size, RAM required, capabilities, quality score, "Recommended for your device"
- Download → checksum verify → activate. `active_model` SQLite row stores the selection.
- Admin can add new models (Qwen 4, Gemma 4, etc.) without an app update — catalog is server-driven.

**Online vs offline routing** — automatic based on `expo-network` + `@react-native-community/netinfo` connectivity state, with a manual "force offline" toggle in settings for testing.

### TypeScript interface

```ts
// src/inference/types.ts

export interface Message {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface InferenceResponse {
  content: string;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  model_id: string;
  generated_offline: boolean;   // true for LlamaRn/ExecuTorch, false for CloudApi
}

export interface ModelCapabilities {
  text: boolean;
  image: boolean;
  audio: boolean;
  video: boolean;
}

export interface InferenceClient {
  readonly modelId: string;
  readonly capabilities: ModelCapabilities;
  readonly isOffline: boolean;
  generate(messages: Message[], systemPrompt: string, opts?: GenerateOpts): Promise<InferenceResponse>;
  generateStream(messages: Message[], systemPrompt: string, opts?: GenerateOpts): AsyncIterable<string>;
  classify(text: string, labels: string[]): Promise<{ label: string; confidence: number }>;
  unload(): Promise<void>;        // release memory on model switch
}

export interface GenerateOpts {
  maxTokens?: number;
  temperature?: number;
  stop?: string[];
  timeoutMs?: number;
}
```

### Registry (factory pattern — matches backend's `get_llm_client`)

```ts
// src/inference/registry.ts

import { useNetworkState } from '@/hooks/useNetworkState';
import { useActiveModel } from '@/stores/active-model-store';
import { CloudApiClient } from './cloud-api-client';
import { LlamaRnClient } from './llama-rn-client';
import { ExecuTorchClient } from './executorch-client';

export async function resolveClient(opts?: { forceOffline?: boolean }): Promise<InferenceClient> {
  const { isConnected } = useNetworkState.getState();
  const { activeModel } = useActiveModel.getState();
  const forceOffline = opts?.forceOffline ?? false;

  // Online path: delegate to Django backend
  if (isConnected && !forceOffline) {
    return new CloudApiClient();   // uses JWT, hits /api/v1/sessions/<id>/respond/
  }

  // Offline path: instantiate client for active on-device model
  if (!activeModel) {
    throw new OfflineButNoModelError();  // caller handles: prompt user to download
  }

  switch (activeModel.runtime) {
    case 'llama_cpp':
      return LlamaRnClient.load(activeModel);
    case 'executorch':
      return ExecuTorchClient.load(activeModel);
    default:
      throw new UnsupportedRuntimeError(activeModel.runtime);
  }
}
```

### `LlamaRnClient` sketch (concrete)

```ts
// src/inference/llama-rn-client.ts
import { initLlama, LlamaContext } from 'llama.rn';
import { renderChatTemplate } from './prompt-templates';

export class LlamaRnClient implements InferenceClient {
  readonly isOffline = true;
  private ctx: LlamaContext | null = null;

  private constructor(
    public readonly modelId: string,
    private readonly modelPath: string,
    private readonly chatTemplate: string,
    public readonly capabilities: ModelCapabilities,
  ) {}

  static async load(model: MobileInferenceModel): Promise<LlamaRnClient> {
    const path = await getLocalModelPath(model.id);
    return new LlamaRnClient(model.id, path, model.chat_template, model.capabilities);
  }

  private async ensureLoaded() {
    if (!this.ctx) {
      this.ctx = await initLlama({
        model: this.modelPath,
        n_ctx: 8192,
        n_threads: 4,
        n_gpu_layers: 99,  // use Metal on iOS, Vulkan on Android when available
      });
    }
  }

  async generate(messages, systemPrompt, opts) {
    await this.ensureLoaded();
    const prompt = renderChatTemplate(this.chatTemplate, systemPrompt, messages);
    const t0 = Date.now();
    const result = await this.ctx!.completion({
      prompt,
      n_predict: opts?.maxTokens ?? 512,
      temperature: opts?.temperature ?? 0.7,
      stop: opts?.stop ?? [],
    });
    return {
      content: result.text,
      tokens_in: result.tokens_evaluated,
      tokens_out: result.tokens_predicted,
      latency_ms: Date.now() - t0,
      model_id: this.modelId,
      generated_offline: true,
    };
  }

  async unload() {
    if (this.ctx) {
      await this.ctx.release();
      this.ctx = null;
    }
  }

  // generateStream + classify similar
}
```

### `CloudApiClient` sketch

```ts
// src/inference/cloud-api-client.ts
import { apiClient } from '@/api/client';

export class CloudApiClient implements InferenceClient {
  readonly modelId = 'cloud';       // backend decides actual model
  readonly isOffline = false;
  readonly capabilities = { text: true, image: true, audio: true, video: false };

  async generate(messages, systemPrompt, opts) {
    // Note: online tutor call uses session-level /respond/ endpoint, not a raw generate.
    // This client is the transport; the engine wraps it with session context.
    const t0 = Date.now();
    const res = await apiClient.post('/sessions/respond/', { messages, system_prompt: systemPrompt });
    return {
      content: res.data.content,
      tokens_in: res.data.tokens_in,
      tokens_out: res.data.tokens_out,
      latency_ms: Date.now() - t0,
      model_id: res.data.model,
      generated_offline: false,
    };
  }
}
```

**Chat template rendering**: the catalog's `chat_template` field is a Jinja-like string. Use a minimal client-side template renderer (custom, ~30 lines) rather than pulling in a full Jinja library.

### What this buys us

1. **Engine is runtime-agnostic** — state-machine runner calls `client.generate()`; adding Qwen 4 or swapping Claude for Gemini changes zero engine code.
2. **Server-controlled catalog** — admin adds a new on-device model via Django admin; users see it in the Model Store on next app open. No app update.
3. **Transparent online/offline transition** — mid-session, if connectivity drops, next turn resolves to `LlamaRnClient` with same message history. Engine doesn't know.
4. **Parallel to backend mental model** — anyone who understands `apps/llm/client.py` understands the mobile abstraction immediately.

---

## Screen inventory + MVP sequencing

### Tier 1 — Ship first (weeks 1–4 of RN work):

1. **Login** (`app/(auth)/login.tsx`) — username + password, JWT flow
2. **Register** (`app/(auth)/register.tsx`) — self-register student
3. **Home / My Courses** (`app/(app)/index.tsx`) — list from `/api/v1/courses/`
4. **Lesson detail** (`app/(app)/lessons/[id].tsx`) — description + "Download for offline" + "Start"
5. **Pack download** (`app/(app)/lessons/[id]/download.tsx`) — progress bar, media cache
6. **Content review mode** — step browser for offline lessons without chat (works on low-tier devices, no LLM needed)

### Tier 2 — Core tutoring (weeks 5–7):

7. **Tutor chat online** (`app/(app)/tutor/[sessionId].tsx`) — talk to cloud LLM via `/api/v1/sessions/<id>/respond/`
8. **Exit ticket** — render from pack, submit to `/api/v1/sessions/<id>/exit-ticket/`
9. **Progress** (`app/(app)/progress.tsx`) — from `/api/v1/progress/`
10. **Settings** (`app/(app)/settings/index.tsx`) — logout, about, storage usage

### Tier 3 — Offline tutoring (weeks 8–10):

11. **Tutor chat offline** — swap online API call for local `InferenceClient`. State machine runner drives the flow.
12. **Sync worker** — background upload of queued turns
13. **Offline indicator banner** — persistent when disconnected
14. **Model picker** (dev flag only for v1) — `app/(app)/settings/models.tsx`

### Tier 4 — Post-pilot polish:

- Gamification UI (streaks, milestones — already have API)
- Voice input (if Gemma 3n audio integrated)
- Image snap-and-ask (capture worksheet, send to tutor)
- Model store UX (expose model picker publicly)

---

## Phased delivery

| Phase | Weeks | Deliverable | Gate |
|---|---|---|---|
| **A — Backend API** | 1–2 | DRF + JWT + CORS, all `/api/v1/*` endpoints, OpenAPI schema, `MobileInferenceModel` + `LessonPackVersion` models | Server tests green; OpenAPI renders |
| **B — M0 feasibility** | 0.5 (parallel) | On-device model bake-off on test device | Greenlight or kill |
| **C — RN scaffold** | 3 | Expo + TS + Drizzle + auth + home + lesson list | App installs, login works, lessons render |
| **D — Pack download + review mode** | 2 | Lesson pack download, offline content review | Lesson works fully offline (read + MCQ exit ticket) |
| **E — Online tutor chat** | 2 | Chat screen calling `/api/v1/sessions/<id>/respond/` | Same UX as web tutor, mobile-optimized |
| **F — Policy-as-data runner** | 2 | Server emits JSON policy; RN runs it (still with cloud LLM) | State machine matches web behavior in smoke tests |
| **G — On-device inference** | 3 | `llama.rn` integration, model manager, active model selection, offline chat | Offline conversation on test device works for one lesson |
| **H — Sync layer** | 2 | Background sync worker, conflict handling, teacher dashboard "offline session" badge | Offline session syncs cleanly, teacher can review |
| **I — Pilot hardening** | 2 | Error tracking (Sentry), crash-free tuning, TestFlight + Play internal | Pilot-ready build |

**Solo + running-in-parallel-with-Django-work timeline**: ~4–5 months calendar time to Phase I. Full-time focused: ~10 weeks. Phase A alone unlocks a PWA fallback in parallel if the RN work slips.

---

## Dev environment setup

```bash
# One-time:
brew install watchman cocoapods
npm install -g eas-cli

# Create project:
npx create-expo-app mobile --template expo-template-blank-typescript
cd mobile
npx expo install expo-router expo-sqlite expo-secure-store expo-network \
    @react-native-community/netinfo expo-av expo-file-system \
    @expo/vector-icons
npm install zustand @tanstack/react-query axios drizzle-orm drizzle-kit \
    react-hook-form zod @sentry/react-native
npm install llama.rn   # Check version — needs Expo dev client

# iOS native deps (after installing native modules):
cd ios && pod install && cd ..

# Running:
npx expo start --dev-client
# On first run with native modules:
eas build --profile development --platform ios --local
```

**Xcode version**: latest stable; matches current Expo SDK.
**Test devices**: need at least one real iPhone + one real Android phone. Simulators can't run GGUF models in reasonable time.

---

## Distribution strategy

**Pilot (now through ~3 months post-launch)**:
- iOS: **TestFlight internal testing** (up to 100 testers, no App Store review)
- Android: **Google Play internal testing track** (private distribution) OR direct APK via link on school's intranet
- CI: GitHub Actions runs `eas build --profile preview` on push to `mobile/main` branch, uploads to TestFlight + Play internal automatically

**Post-pilot**:
- iOS: App Store review. Prepare: content safety docs, age rating (probably 12+), data collection disclosure (text + audio if used, no advertising)
- Android: Production Play Store track
- Education-specific: Apple School Manager / Google Workspace for Education for bulk deployment if schools want it

---

## Risks + open questions

### High risk

1. **M0 result** — if Gemma 3n / Qwen 3.5 can't hold math rules, offline tutoring is dead. 3-day gate before investing further. Mitigation: PWA fallback plan exists.
2. **llama.rn stability on iOS** — community-maintained, bleeding edge. Mitigation: ExecuTorch as backup, or custom MLX Turbo Module if llama.rn doesn't hold up. Budget 1 week of integration buffer.
3. **Solo scope** — 10 weeks full-time, 4–5 months in parallel. Non-trivial. Mitigation: Phase A delivers web value regardless of mobile. Content-review mode ships usable app even without on-device LLM.
4. **App Store rejection risk** — AI chat with students under 13 may get scrutinized. Mitigation: age gate (13+), clear content safety docs, parental consent flow for <13.

### Medium risk

5. **llama.rn iOS audio** — doesn't natively expose Gemma 3n's audio input. Means no voice answers on day 1 for iOS. Android via MediaPipe Turbo Module later.
6. **Chat template drift** — if model families change chat format, catalog entries go stale. Mitigation: version the chat_template field; have server validate against known families during admin entry.
7. **Media caching cost** — lessons with many images = multi-MB downloads. Mitigation: WiFi-only default, low-quality JPEG transcoding server-side for mobile consumers.
8. **Expo managed workflow limits** — if we need deep native changes later, may need to eject. Mitigation: start with config plugins only; eject only if absolutely needed.

### Open questions to resolve during Phase A

- **Username vs email login** — current Django uses Django's default User with username. Mobile registration should probably use email too. Decision: accept both.
- **Multi-device same student** — same account on phone + tablet. Sessions should sync. Use server-authoritative session IDs; client creates provisional IDs, merges on sync.
- **Password reset on mobile** — link to web? Or mobile-native flow? Recommend web link initially (less UI work).
- **Parent / family accounts** — out of scope for v1, but structure the User model changes so it's not locked out later.

---

## What NOT to do

- **Don't** port `conversational_tutor.py` Python logic to TS — use policy-as-data
- **Don't** ship Redux — Zustand is plenty
- **Don't** ship pure Expo Go — you'll hit native module walls immediately; use dev client from day 1
- **Don't** build an admin panel in the mobile app — teachers stay on web
- **Don't** skip Phase A — building RN against unstable JSON endpoints with no auth tokens is chaos
- **Don't** bundle model weights in the app binary — they're 1–5GB each, will fail App Store review on size, and make updates painful

---

## Next step

**Begin Phase A.1 — install DRF + CORS + simplejwt, scaffold `apps/api/` app, wire up `/api/v1/auth/login/`.** This is pure Django, 1–2 days, zero mobile dependency, unblocks everything else.

Separately (in parallel): order/identify test devices for M0 if not already available. Minimum: one recent-ish Android (Pixel 7+ ideal) and one iPhone (12+ ideal).
