"""
Django settings for AI Tutor project.

Key architecture decisions:
- Apps live in 'apps/' directory for clean organization
- Using SQLite for development (easy swap to Postgres for production)
- Media files stored locally by default
"""

from pathlib import Path
import os
from dotenv import load_dotenv
import dj_database_url

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'

ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
# In DEBUG, accept any host so LAN dev (mobile phone hitting Mac's IP) works.
if DEBUG:
    ALLOWED_HOSTS = ['*']

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party (mobile API — memory/mobile_rn_plan.md Phase A)
    'corsheaders',
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'drf_spectacular',
    # Our apps
    'apps.accounts',
    'apps.curriculum',
    'apps.media_library',
    'apps.tutoring',
    'apps.llm',
    'apps.safety',
    'apps.dashboard',
    'apps.api',
    'apps.support',
    'apps.benchmark',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # GZip dynamic responses (chat HTML, JSON tutor replies). WhiteNoise
    # already compresses static assets; this catches everything else.
    # Must come before WhiteNoise / CommonMiddleware per Django docs.
    'django.middleware.gzip.GZipMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    # CORS must run BEFORE CommonMiddleware so preflight responses are
    # not stripped of CORS headers. See django-cors-headers docs.
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.safety.SafetyMiddleware',
    # Forces a password change after an admin reset (sets
    # Membership.password_reset_required=True). Must come AFTER
    # AuthenticationMiddleware (request.user must be set).
    'apps.accounts.password_reset_middleware.PasswordResetRequiredMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.accounts.context_processors.institution_theme',
                'apps.accounts.context_processors.email_verification_status',
                'apps.accounts.context_processors.baseline_recommendations',
                'apps.accounts.context_processors.staff_flag',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database - uses DATABASE_URL env var when set (Postgres in production),
# falls back to SQLite for local development.
DATABASES = {
    'default': dj_database_url.config(
        default='sqlite:///' + str(BASE_DIR / 'db.sqlite3')
    )
}

# SQLite-only tuning for local dev: WAL mode + 30s busy timeout. Without
# these, the dev server's background content-gen tasks (run as threads
# inside the same process) lock the DB and concurrent requests
# ("POST /tutor/api/chat/start/...") fail with "database is locked".
# Production (PostgreSQL) ignores both keys, so this is a no-op there.
if DATABASES['default'].get('ENGINE', '').endswith('sqlite3'):
    DATABASES['default'].setdefault('OPTIONS', {})
    DATABASES['default']['OPTIONS']['timeout'] = 30
    DATABASES['default']['OPTIONS']['init_command'] = (
        "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"
    )


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static files
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (user uploads)
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# NOTE (2026-05-24): VECTORDB_ROOT removed. Vectors live in Postgres
# via the pgvector extension (apps/curriculum/models.py::CurriculumChunk
# + apps/curriculum/kb_storage.py). The previous setup wrote ChromaDB
# SQLite to /tmp/vectordb on container startup because the Azure Files
# SMB mount hung under SQLite, but writes never synced back —
# every TeachingMaterialUpload's vectors were silently lost on the
# next container restart. See memory/pgvector_migration_plan.md.

# Content quality judges (apps/curriculum/content_judges/). Kill-switch
# in case a judge is misbehaving and we need to bypass without a deploy.
# When False, image_service skips the PRE-gen prompt review and sends
# the prompt straight to the gen model. Default True.
CONTENT_JUDGE_IMAGE_PROMPT_ENABLED = (
    os.getenv('CONTENT_JUDGE_IMAGE_PROMPT_ENABLED', '1').lower()
    not in ('0', 'false', 'no', 'off', '')
)

# POST-gen factual review of every newly-generated LessonStep. Runs
# concurrently across all steps in a lesson. Default True; flip off
# via env to bypass without a deploy if a judge regression appears.
CONTENT_JUDGE_FACTUAL_STEP_ENABLED = (
    os.getenv('CONTENT_JUDGE_FACTUAL_STEP_ENABLED', '1').lower()
    not in ('0', 'false', 'no', 'off', '')
)

# POST-gen vision review of every newly-generated figure. Runs on the
# bytes returned from image_service after a successful gen call.
# Default True; flip off via env to bypass without a deploy if a vision
# regression appears.
CONTENT_JUDGE_FIGURE_ALIGNMENT_ENABLED = (
    os.getenv('CONTENT_JUDGE_FIGURE_ALIGNMENT_ENABLED', '1').lower()
    not in ('0', 'false', 'no', 'off', '')
)

# Q2 bounded content regen — when factual_step judge rejects a
# step, fire run_step_regen up to DEFAULT_MAX_CYCLES (=2) attempts.
# Default True; flip off via env to disable rewriting and only flag.
CONTENT_REGEN_ENABLED = (
    os.getenv('CONTENT_REGEN_ENABLED', '1').lower()
    not in ('0', 'false', 'no', 'off', '')
)

# Tutor self-retry — replace the text-only regen ensemble with a
# tool-aware retry that re-invokes the tutor's own generate path with
# judge feedback prepended. Fixes bank_question_ref/render drift
# caused by regen producing prose that doesn't match the original
# tool call (task #176, memory/tutor_self_retry_plan.md). Default OFF
# in production until E2E validates; ON in dev for testing.
# Flip via env: TUTOR_SELF_RETRY_ENABLED=1 to enable.
TUTOR_SELF_RETRY_ENABLED = (
    os.getenv('TUTOR_SELF_RETRY_ENABLED', '1' if DEBUG else '0').lower()
    not in ('0', 'false', 'no', 'off', '')
)

# Q4 exit_question content judge — runs POST-gen on every newly-generated
# MCQ exit-ticket question. Verdict persists to
# ExitTicketQuestion.judge_outputs.exit_question and surfaces as a
# badge in the lesson detail UI.
CONTENT_JUDGE_EXIT_QUESTION_ENABLED = (
    os.getenv('CONTENT_JUDGE_EXIT_QUESTION_ENABLED', '1').lower()
    not in ('0', 'false', 'no', 'off', '')
)

# Q4 pedagogy_step content judge — runs POST-gen alongside factual_step
# in the same per-step concurrent fan-out. Verdict on
# step.judge_outputs.pedagogy_step.
CONTENT_JUDGE_PEDAGOGY_STEP_ENABLED = (
    os.getenv('CONTENT_JUDGE_PEDAGOGY_STEP_ENABLED', '1').lower()
    not in ('0', 'false', 'no', 'off', '')
)

# Q4 safety_content content judge — runs POST-gen alongside factual_step.
# Verdict on step.judge_outputs.safety_content.
CONTENT_JUDGE_SAFETY_CONTENT_ENABLED = (
    os.getenv('CONTENT_JUDGE_SAFETY_CONTENT_ENABLED', '1').lower()
    not in ('0', 'false', 'no', 'off', '')
)

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Logging — output to stdout for container logs
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'apps': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

# LLM Provider settings (from environment)
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')

# Embedding backend: 'openai' (API, fast, no PyTorch) or 'local' (sentence-transformers, offline)
EMBEDDING_BACKEND = os.getenv('EMBEDDING_BACKEND', 'local')

# Speech backends: configurable TTS/STT with local defaults
TTS_BACKEND = os.getenv('TTS_BACKEND', 'piper')        # 'piper' or 'elevenlabs'
STT_BACKEND = os.getenv('STT_BACKEND', 'whisper')       # 'whisper' or 'elevenlabs'
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY', '')
ELEVENLABS_VOICE_ID = os.getenv('ELEVENLABS_VOICE_ID', '2vbhUP8zyKg4dEZaTWGn')
ELEVENLABS_MODEL_ID = os.getenv('ELEVENLABS_MODEL_ID', 'eleven_multilingual_v2')

# Judge history window. The number of trailing conversation turns
# passed to history-sensitive judges (coherence / factual / rule).
# 0 disables (legacy behaviour, pre-Phase-2.2.5).
#
# Default bumped from 4 → 12 on 2026-05-12 per pilot feedback: a
# 4-turn window missed cross-turn coherence violations where the
# tutor contradicted something said 5-8 turns ago. 12 turns ≈
# ~6 student + 6 tutor exchanges before the current pair, capped
# at 400 chars per turn → ~4800 chars total. Predictable budget,
# meaningful coverage. Going wider (whole session) would blow up
# token cost on long sessions and recalibrate all judge prompts
# at once; we'll only widen further if the benchmark shows the
# 12-turn window still misses real failures.
#
# Recorded on SessionTurn.metadata['judge_history_turns'] so the
# benchmark can slice agreement by history-aware vs not.
JUDGE_HISTORY_TURNS = int(os.getenv('JUDGE_HISTORY_TURNS', '12'))

# Auth settings
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/tutor/'
LOGOUT_REDIRECT_URL = '/accounts/login/'


# Email — defaults to console (prints to terminal). Production uses
# Azure Communication Services Email (transactional only) when the
# connection string is provided. Pulumi wires this via:
#   AZURE_COMMUNICATION_CONNECTION_STRING (secret)
#   AZURE_COMMUNICATION_SENDER_ADDRESS    (e.g. noreply@mail.example.com)
#   DEFAULT_FROM_EMAIL                    (e.g. "AI Tutor <noreply@...>")
AZURE_COMMUNICATION_CONNECTION_STRING = os.getenv(
    'AZURE_COMMUNICATION_CONNECTION_STRING', ''
)
AZURE_COMMUNICATION_SENDER_ADDRESS = os.getenv(
    'AZURE_COMMUNICATION_SENDER_ADDRESS', ''
)

if AZURE_COMMUNICATION_CONNECTION_STRING:
    # Production / live test path. Real delivery via ACS REST API.
    EMAIL_BACKEND = os.getenv(
        'EMAIL_BACKEND',
        'apps.safety.email_backends.AzureCommunicationEmailBackend',
    )
else:
    # Dev path — console prints emails to stdout. SMTP is still
    # supported as an explicit override (Mailgun/Resend/etc.) for
    # local testing without ACS:
    #   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
    #   EMAIL_HOST=smtp.resend.com
    #   EMAIL_HOST_USER=resend
    #   EMAIL_HOST_PASSWORD=re_xxx
    EMAIL_BACKEND = os.getenv(
        'EMAIL_BACKEND',
        'django.core.mail.backends.console.EmailBackend',
    )

EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.mailgun.org')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'AI Tutor <noreply@example.com>')

# Production security settings
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


# =============================================================================
# REST API for the React Native mobile app — Phase A of memory/mobile_rn_plan.md
# =============================================================================

from datetime import timedelta  # noqa: E402

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        # Mobile uses JWT bearer tokens; web uses the existing session
        # cookie. Both work transparently for any DRF endpoint.
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.AnonRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/min',
        'user': '300/min',
    },
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'TOKEN_TYPE_CLAIM': 'token_type',
    'JTI_CLAIM': 'jti',
}

# CORS — locked down by default. Override CORS_ALLOWED_ORIGINS in .env to add
# the mobile dev server (Expo defaults to http://localhost:8081 + the device
# IP). For the production build, the mobile app sends Authorization headers
# from a non-browser context, so CORS doesn't apply — but Expo dev does run
# in a browser-ish environment.
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()
]
# In DEBUG, default-allow the Expo web dev server origins so local browser
# testing works without extra env vars. Production must set
# CORS_ALLOWED_ORIGINS explicitly.
if DEBUG and not CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS = [
        'http://localhost:8081',
        'http://127.0.0.1:8081',
        'http://localhost:19006',
        'http://127.0.0.1:19006',
        'http://localhost:8082',
        'http://127.0.0.1:8082',
    ]
CORS_ALLOW_CREDENTIALS = True
# Only allow CORS on the API surface — never on /admin/, /tutor/, /dashboard/.
CORS_URLS_REGEX = r'^/api/.*$'
# Whitelist mobile-client custom headers so preflight passes.
from corsheaders.defaults import default_headers as _default_cors_headers  # noqa: E402
CORS_ALLOW_HEADERS = list(_default_cors_headers) + [
    'x-client-form-factor',
]

# OpenAPI schema (drf-spectacular). Used by the RN repo to generate
# TypeScript types via `openapi-typescript`.
SPECTACULAR_SETTINGS = {
    'TITLE': 'AI Tutor Mobile API',
    'DESCRIPTION': 'REST API consumed by the React Native mobile client.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    'SCHEMA_PATH_PREFIX': r'/api/v1/',
}
