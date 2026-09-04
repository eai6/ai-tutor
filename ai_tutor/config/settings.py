"""
Django settings for AI Tutor project.

Key architecture decisions:
- Apps live in 'apps/' directory for clean organization
- Using SQLite for development (easy swap to Postgres for production)
- Media files stored locally by default
"""

from pathlib import Path
import os
import sys
from dotenv import load_dotenv
import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse_lazy

load_dotenv()

# Two roots, because they answer different questions and move independently.
#
# PACKAGE_DIR is where the code and the assets that ship WITH it live —
# templates, static, locale, the seed curriculum. Inside the installed wheel,
# read-only, replaced wholesale on upgrade.
#
# BASE_DIR is where this deployment's mutable state lives — the database, uploads,
# collected static. It must be writable and must SURVIVE an upgrade, so it can
# never be inside the package.
#
# From a checkout the two differ by one level and everything works as it always
# has. From an installed wheel, site-packages/ai_tutor is not writable and its
# parent is meaningless, so AI_TUTOR_DATA_DIR overrides BASE_DIR. Phase 3 gives
# that a proper default per platform; until then the checkout layout is the
# default and the env var is the escape hatch.
PACKAGE_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.getenv('AI_TUTOR_DATA_DIR') or PACKAGE_DIR.parent)

_DEV_SECRET_KEY = 'dev-secret-key-change-in-production'
SECRET_KEY = os.getenv('SECRET_KEY', _DEV_SECRET_KEY)

# DEBUG defaults to FALSE, so a deployment that forgets to set it gets the safe
# value rather than tracebacks and ALLOWED_HOSTS=['*'] on a public address.
#
# LOCAL DEVELOPMENT: put `DEBUG=True` in your .env. Every real deployment
# already sets this explicitly — AWS at infra/aws/__main__.py:127, Azure at
# infra/__main__.py:647, the kiosk via its systemd unit, the desktop build in
# config/settings_desktop.py — so flipping the default changes none of them.
DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'

# Refuse to boot with the publicly-known development signing key on anything
# that is not in DEBUG. With that key an attacker forges session cookies and
# password-reset tokens, and nothing in the logs would ever show it. Failing
# loudly at startup is far better than serving.
#
# Three exemptions, each deliberate:
#   * ALLOW_DEV_SECRET_KEY=1 — the offline builds set this before importing
#     this module. They serve loopback (desktop) or an isolated access point
#     (kiosk), ship no secret store, and are not reachable from a network an
#     attacker is on. See config/settings_desktop.py and settings_kiosk.py.
#   * running under pytest — Django forces DEBUG=False for tests and the suite
#     supplies no key, so without this every test would fail at import.
#   * `manage.py` housekeeping that never serves traffic, so that a fresh clone
#     can still run migrations or collectstatic before secrets are configured.
_NON_SERVING_COMMANDS = {'collectstatic', 'makemigrations', 'migrate', 'shell',
                         'test', 'check', 'createsuperuser'}
_RUNNING_TESTS = 'pytest' in sys.modules
_ALLOW_DEV_KEY = (
    os.getenv('ALLOW_DEV_SECRET_KEY') == '1'
    or _RUNNING_TESTS
    or bool(set(sys.argv) & _NON_SERVING_COMMANDS)
)
if not DEBUG and SECRET_KEY == _DEV_SECRET_KEY and not _ALLOW_DEV_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY is still the development default while DEBUG is False. "
        "Set a real one before serving traffic:\n\n"
        "    python -c \"import secrets; print(secrets.token_urlsafe(64))\"\n\n"
        "then put it in SECRET_KEY (see deploy/compose/.env.example). "
        "For local development set DEBUG=True instead."
    )

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
    # Brute-force lockout on every login path. Assessment finding F-04: the
    # admin console answers on the public internet and no account lockout,
    # CAPTCHA or progressive delay was observed at the application layer. The
    # WAF's 2000-per-5-minutes-per-IP rule is a flood control, not a per-account
    # one, and a school NAT means it has to stay loose — a password-spray at one
    # attempt per account per minute passes under it all day.
    'axes',
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'drf_spectacular',
    # Our apps
    'ai_tutor.apps.accounts',
    'ai_tutor.apps.curriculum',
    'ai_tutor.apps.media_library',
    'ai_tutor.apps.tutoring',
    'ai_tutor.apps.llm',
    'ai_tutor.apps.safety',
    'ai_tutor.apps.dashboard',
    'ai_tutor.apps.api',
    'ai_tutor.apps.support',
    'ai_tutor.apps.benchmark',
    # Offline desktop build (memory/desktop_offline_app_plan.md). Inert on the
    # server: it only adds a device-local state table nothing else reads.
    'ai_tutor.apps.desktop',
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
    # LocaleMiddleware sits between SessionMiddleware and
    # CommonMiddleware per Django docs. Our custom LocaleResolverMiddleware
    # (below, after AuthenticationMiddleware) overrides the default
    # cookie/header resolution chain with our per-course / per-student /
    # per-institution chain — see apps/accounts/locale_middleware.py and
    # memory/multi_locale_architecture_research.md.
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    # Rejects NUL bytes and strips other C0 control characters from query
    # string and form input. Sits here — after host/URL normalisation, before
    # any view or the CSRF token comparison — because the crash it fixes
    # (2026-08-13 assessment F-01) happened in the database driver, one layer
    # below every view. See apps/safety/request_validation.py.
    'ai_tutor.apps.safety.request_validation.ControlCharacterMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # LocaleResolverMiddleware runs after AuthenticationMiddleware
    # (needs request.user) and overrides the language activated by
    # Django's LocaleMiddleware above. Resolution chain:
    #   course.locale (in-session, applied by the view itself)
    #     > StudentProfile.preferred_locale
    #       > Institution.default_locale
    #         > settings.LANGUAGE_CODE
    # See apps/accounts/locale_middleware.py and
    # memory/multi_locale_architecture_research.md.
    'ai_tutor.apps.accounts.locale_middleware.LocaleResolverMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'ai_tutor.apps.safety.SafetyMiddleware',
    # Content-Security-Policy. Report-only unless CSP_ENFORCE=1 — the templates
    # still carry inline handlers, so a blocking policy would break the
    # dashboard. See apps/safety/csp.py for the counts and the path to
    # enforcing it.
    'ai_tutor.apps.safety.csp.ContentSecurityPolicyMiddleware',
    # Permissions-Policy + Cross-Origin-Resource-Policy on every response, and
    # no-store on HTML/JSON rendered for a signed-in user. After
    # AuthenticationMiddleware because it reads request.user. See
    # apps/safety/response_headers.py (assessment findings F-09, F-12).
    'ai_tutor.apps.safety.response_headers.SecurityHeadersMiddleware',
    # Forces a password change after an admin reset (sets
    # Membership.password_reset_required=True). Must come AFTER
    # AuthenticationMiddleware (request.user must be set).
    'ai_tutor.apps.accounts.password_reset_middleware.PasswordResetRequiredMiddleware',
    # django-axes. Its own documentation requires this to be LAST: it wraps the
    # request so that authenticate() calls made inside a view can be attributed
    # to a client, which cannot work if something below it short-circuits first.
    'axes.middleware.AxesMiddleware',
]

ROOT_URLCONF = 'ai_tutor.config.urls'

# Where the Django admin is mounted. Default unchanged, so no existing
# deployment, bookmark or runbook breaks by upgrading.
#
# Assessment finding F-04 recommended moving it off /admin/. That is obscurity,
# not security, and it is listed here as the SECOND of two measures — the first
# is the network restriction at the load balancer (infra/aws/components/edge.py,
# admin-allowed-cidrs), which is the one that actually decides who may reach the
# console. What this buys on top is that every credential-stuffing toolkit
# probes /admin/ by default and stops there, so the noise floor drops.
#
# Set ADMIN_URL to a path with a random component — 'internal-console-7f3a/' —
# and mind the trailing slash; Django's path() wants it.
ADMIN_URL = os.getenv('ADMIN_URL', 'admin/').lstrip('/')

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [PACKAGE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.template.context_processors.i18n',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'ai_tutor.apps.accounts.context_processors.institution_theme',
                'ai_tutor.apps.accounts.context_processors.email_verification_status',
                'ai_tutor.apps.accounts.context_processors.baseline_recommendations',
                'ai_tutor.apps.accounts.context_processors.staff_flag',
                'ai_tutor.apps.accounts.context_processors.desktop_build',
            ],
        },
    },
]

WSGI_APPLICATION = 'ai_tutor.config.wsgi.application'


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


# ── Brute-force lockout (assessment finding F-04) ────────────────────────────
#
# AxesStandaloneBackend must come FIRST. It authenticates nothing; it raises
# PermissionDenied for a locked-out (ip, username) pair before ModelBackend gets
# a chance to check the password, which is what makes the lockout a lockout
# rather than a log entry. "Standalone" rather than AxesBackend because the
# project has a second authentication path — SimpleJWT for the mobile client —
# and the standalone variant does not assume it owns the chain.
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# Five is the number the report suggested and it is the right shape for this
# audience: a student mistyping a password three times is normal, six attempts
# in an hour is not.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # hours

# Lock the (IP, username) PAIR, not either alone. Locking by username alone
# hands anyone a denial-of-service against a named student — five wrong
# passwords and that child cannot sit their lesson. Locking by IP alone locks
# out an entire school, because a classroom shares one NAT address. The pair
# stops a spray against one account from one source while leaving the rest of
# the class working, which is the only version of this that survives contact
# with a Seychelles computer lab.
AXES_LOCKOUT_PARAMETERS = [['ip_address', 'username']]

# A successful login clears the counter, so yesterday's typos never accumulate
# into today's lockout.
AXES_RESET_ON_SUCCESS = True

# Read the client address the same way everything else does, through the
# gateway-appended last X-Forwarded-For hop. Without these two, axes keys on
# REMOTE_ADDR, which behind the ALB is the load balancer — every failure in the
# estate would land on one bucket and lock out the world. See
# apps/safety/client_ip.py for why the LAST hop is the trustworthy one.
AXES_IPWARE_PROXY_COUNT = 1
AXES_IPWARE_META_PRECEDENCE_ORDER = ['HTTP_X_FORWARDED_FOR', 'REMOTE_ADDR']

# Lockout answers 429 rather than axes' default 403, so the ALB and CloudWatch
# can tell a throttled login apart from a CSRF rejection.
AXES_HTTP_RESPONSE_CODE = 429

# Failures are worth a log line each; the lockout itself is the event to alert
# on. axes writes both to the 'axes' logger, which the root handler picks up.
AXES_VERBOSE = True

# The sign-up form states four rules beside the password box — eight
# characters, a letter, a number, a symbol — and static/js/password-field.js
# ticks them off live. CharacterVarietyValidator is what makes the last three
# true on the server; without it the checklist would be advice rather than
# the rule. The other four run as well and are stated under the list as
# checks made on submit: they cannot be decided in the browser (the common
# list is 20,000 entries) or are not worth a red cross while someone types.
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
    {'NAME': 'ai_tutor.apps.accounts.password_validators.CharacterVarietyValidator'},
]


# Default UI locale for anonymous visitors (pre-login) and the global
# fallback in LocaleResolverMiddleware. Env-driven so a single codebase
# can default differently per deployment: Seychelles prod leaves it unset
# → 'en-us'; the Mozambique pilot sets DEFAULT_LANGUAGE=pt-mz (wired via
# Pulumi.staging.yaml) so students see Portuguese without toggling.
LANGUAGE_CODE = os.getenv('DEFAULT_LANGUAGE', 'en-us')
# Supported locales for the unified multi-locale platform. Per-course
# locale is the primary signal at runtime (see memory/multi_locale_
# architecture_research.md). The .po directories live under locale/ as
# `<lang>_<COUNTRY>` per gettext convention (e.g. pt_MZ); the language
# tag used in settings + Course.locale is hyphenated-lowercase
# (e.g. 'pt-mz').
#
# Labels are country-forward ("🇸🇨 Seychelles — English") because
# raw BCP 47 codes mean nothing to teachers and students. The same
# tuples are used as `choices=settings.LANGUAGES` on the three locale
# fields (Course.locale, Institution.default_locale,
# StudentProfile.preferred_locale) so picker dropdowns auto-render
# with these labels. See auto-memory/feedback_locale_picker_country_forward.md.
#
# Storage stays `en-us` for Seychelles (technically inaccurate vs
# `en-sc`; Edward chose to defer the rename to avoid churn — flag if
# revisiting).
LANGUAGES = [
    ('en-us', '🇸🇨 Seychelles — English'),
    ('pt-mz', '🇲🇿 Moçambique — Português'),
    # 'fr' is language-level, not deployment-level, which is why it carries a
    # language flag where the other two carry a country. The first two exist
    # because a Seychelles course and a Mozambique course are different things
    # a teacher picks between; French was added for the interface, and no
    # francophone deployment has claimed it yet. Give it a country the day one
    # does — that is a label change plus a data migration, not a new locale.
    ('fr', '🇫🇷 Français'),
]

# Language-only labels for the public language switcher.
#
# LANGUAGES above stays country-forward because it is wired to `choices` on
# three locale fields (Course.locale, Institution.default_locale,
# StudentProfile locale) — a teacher choosing a course locale needs to know
# whether it is the Seychelles or the Mozambique one, and editing those labels
# would also generate a migration on every one of those fields.
#
# A visitor picking the interface language does not need the country: they are
# choosing what language to read, not which deployment they belong to.
LANGUAGE_SHORT_LABELS = {
    'en-us': 'English',
    'pt-mz': 'Português',
    'fr': 'Français',
}
LOCALE_PATHS = [PACKAGE_DIR / 'locale']
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# Static files
STATIC_URL = 'static/'
STATICFILES_DIRS = [PACKAGE_DIR / 'static']
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

# The `Access-Control-Allow-Origin: *` the 2026-08-13 assessment found on five
# responses (F-03) came from here, not from django-cors-headers — that one is
# already scoped to ^/api/.*$ by CORS_URLS_REGEX and never sees /static/.
# WhiteNoise adds the wildcard to everything it serves by default, so that
# webfonts still load when the assets live on a separate CDN host.
#
# Ours do not. Static is served from the same origin as the pages that use it,
# the templates reference no external host, and a same-origin font needs no CORS
# header at all — so the wildcard granted every site on the internet read access
# in exchange for nothing. The files are public either way; what the finding was
# really about is that the policy was permissive by default rather than by
# decision, and would have silently covered any future asset placed under
# /static/.
WHITENOISE_ALLOW_ALL_ORIGINS = False

# ── Media storage — Azure Blob and S3 side by side ───────────────────────────
# Azure Container Apps and AWS ECS run the SAME image; each deployment supplies
# only its own env vars, and whichever is configured wins. Azure is live for
# real users, so its path must keep working untouched while AWS is stood up.
# S3 takes precedence if both are somehow set.
#
# Either way media is stored PRIVATELY and served from our own domain via the
# matching serve_media (school-network-friendly, behind the WAF, range-capable)
# — never a blob/bucket URL and never a presigned link, because schools
# allowlist our domain and nothing else. With neither set (local/dev) media
# stays on the filesystem. See memory/blob_media_hosting_plan.md.
AZURE_BLOB_MEDIA_ACCOUNT = os.getenv('AZURE_BLOB_MEDIA_ACCOUNT', '')
AZURE_BLOB_MEDIA_KEY = os.getenv('AZURE_BLOB_MEDIA_KEY', '')
AZURE_BLOB_MEDIA_CONTAINER = os.getenv('AZURE_BLOB_MEDIA_CONTAINER', 'media')
USE_BLOB_MEDIA = bool(AZURE_BLOB_MEDIA_ACCOUNT and AZURE_BLOB_MEDIA_KEY)

# Public installer downloads. Separate bucket from media on purpose — this
# one is world-readable; media and ops are not. See infra/aws/components/storage.py.
# Where an enrolled desktop device pushes its work. Empty on the server
# itself and on any build that should never phone home.
SYNC_SERVER_URL = os.getenv('SYNC_SERVER_URL', '')

# True only in the packaged offline builds (config/settings_desktop.py,
# config/settings_kiosk.py). `apps.desktop` is installed everywhere — the
# /desktop/ URLs are inert on a server — so templates cannot tell which build
# they are rendering in without this. Used to show device-only settings, which
# would be meaningless and confusing on the hosted web app.
DESKTOP_BUILD = False

# Where post-install model assets live. Both default to BASE_DIR/models, which
# on the desktop build is the application-data directory — writable, survives
# an upgrade, and per-user on a shared classroom machine. Weights are NOT
# shipped inside the app: the installer stays small, an app update does not
# re-download them, and they change far less often than the code does. See
# apps/desktop/provisioning.py, which acquires them from a file or the network.
MINILM_ONNX_DIR = os.getenv('MINILM_ONNX_DIR', '')
PIPER_VOICE_DIR = os.getenv('PIPER_VOICE_DIR', '')

AWS_DOWNLOADS_BUCKET = os.getenv('AWS_DOWNLOADS_BUCKET', '')
DESKTOP_APP_VERSION = os.getenv('DESKTOP_APP_VERSION', '')

# Version of the server wheel published to the downloads bucket. The filename
# has to carry the real version — pip parses it out of the name and refuses
# anything else — so the download page cannot link to a stable "latest" name
# without knowing this. Unset, the page offers Docker and the release list
# instead of a direct link, which is right: better no link than a broken one.
SERVER_WHEEL_VERSION = os.getenv('SERVER_WHEEL_VERSION', '')

AWS_MEDIA_BUCKET = os.getenv('AWS_MEDIA_BUCKET', '')
AWS_MEDIA_REGION = os.getenv('AWS_MEDIA_REGION', 'us-east-1')
USE_S3_MEDIA = bool(AWS_MEDIA_BUCKET)

if USE_S3_MEDIA:
    STORAGES['default'] = {
        'BACKEND': 'ai_tutor.apps.media_library.s3_media.S3MediaStorage',
    }
elif USE_BLOB_MEDIA:
    STORAGES['default'] = {
        'BACKEND': 'ai_tutor.apps.media_library.blob_media.AzureMediaStorage',
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

# Auth settings.
#
# LOGIN_URL is where every @login_required view sends a logged-out user, with
# ?next= appended. reverse_lazy rather than a hardcoded path: this was
# '/accounts/login/' for a long time, but ai_tutor.apps.accounts.urls is mounted at the
# ROOT (config/urls.py) so the real route is '/login/'. Django never validates
# LOGIN_URL, so the mismatch silently 404'd every protected view for anyone
# arriving with an expired session or a bookmarked URL. Resolving by name means
# a future route rename fails loudly instead of quietly.
#
# accounts:login is a dispatcher that reads ?next= and routes dashboard traffic
# to the staff login form — logic that was unreachable while LOGIN_URL pointed
# at a non-existent path.
LOGIN_URL = reverse_lazy('accounts:login')
LOGIN_REDIRECT_URL = '/tutor/'
# Unused in practice — ai_tutor.apps.accounts.views.logout_view redirects to the landing
# page explicitly, so Django never consults this. Kept correct so that anything
# switching to Django's built-in LogoutView doesn't inherit the old 404.
LOGOUT_REDIRECT_URL = '/'


# Email — defaults to console (prints to terminal). Azure Container Apps and
# AWS ECS run the SAME image and each supplies only its own vars, so both
# transactional backends stay wired up and the configured one wins.
#
# Azure (live) — Pulumi wires:
#   AZURE_COMMUNICATION_CONNECTION_STRING (secret)
#   AZURE_COMMUNICATION_SENDER_ADDRESS    (e.g. noreply@mail.example.com)
# AWS — the ECS task definition wires:
#   AWS_SES_SENDER                        (e.g. noreply@mail.example.com)
#   AWS_SES_REGION                        (defaults to us-east-1)
# Both — DEFAULT_FROM_EMAIL               (e.g. "AI Tutor <noreply@...>")
AZURE_COMMUNICATION_CONNECTION_STRING = os.getenv(
    'AZURE_COMMUNICATION_CONNECTION_STRING', ''
)
AZURE_COMMUNICATION_SENDER_ADDRESS = os.getenv(
    'AZURE_COMMUNICATION_SENDER_ADDRESS', ''
)

AWS_SES_SENDER = os.getenv('AWS_SES_SENDER', '')
AWS_SES_REGION = os.getenv('AWS_SES_REGION', 'us-east-1')

# An explicit EMAIL_BACKEND always wins — it is the SMTP escape hatch
# (Mailgun/Resend/etc.) for local testing without either cloud:
#   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
#   EMAIL_HOST=smtp.resend.com
#   EMAIL_HOST_USER=resend
#   EMAIL_HOST_PASSWORD=re_xxx
if os.getenv('EMAIL_BACKEND'):
    EMAIL_BACKEND = os.getenv('EMAIL_BACKEND')
elif AWS_SES_SENDER:
    # AWS path. Real delivery via the SESv2 API.
    EMAIL_BACKEND = 'ai_tutor.apps.safety.email_backends.SESEmailBackend'
elif AZURE_COMMUNICATION_CONNECTION_STRING:
    # Azure path. Real delivery via the ACS REST API.
    EMAIL_BACKEND = 'ai_tutor.apps.safety.email_backends.AzureCommunicationEmailBackend'
else:
    # Dev path — console prints emails to stdout.
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.mailgun.org')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'AI Tutor <noreply@example.com>')

# Production security settings.
#
# HTTPS_EDGE says whether the load balancer in front of us terminates TLS.
# It defaults to true, so Azure (App Gateway, TLS-terminating) keeps hardening
# exactly as before without setting anything.
#
# The AWS deployment sets HTTPS_EDGE=false while it runs HTTP-only on the ALB's
# own hostname — it serves no users and has no domain, and a public ACM cert
# cannot be issued for an AWS-owned ELB domain. Leaving the cookies Secure there
# breaks login in the most confusing way possible: browsers refuse to send
# Secure cookies over plain HTTP, so every login silently bounces back to the
# login page with no error anywhere. config/settings_kiosk.py and
# settings_desktop.py exist to undo the same block for plain-HTTP builds.
#
# Drop HTTPS_EDGE (or set it true) the moment a certificate is attached.
HTTPS_EDGE = os.getenv('HTTPS_EDGE', 'true').lower() not in ('0', 'false', 'no', 'off')

# Cookie attributes, stated for every cookie rather than per cookie.
#
# The 2026-08-13 assessment (F-06) found django_language carrying none of
# Secure, HttpOnly or SameSite while csrftoken on the same responses carried
# Secure and SameSite — the signature of hardening applied one cookie at a time.
# These four blocks are the policy: every cookie this application sets declares
# all three, and the only one that may be readable by JavaScript is one that
# JavaScript demonstrably needs.
#
# Secure follows HTTPS_EDGE for the same reason the block below does: a browser
# will not send a Secure cookie over plain HTTP, so setting it unconditionally
# breaks login on the plain-HTTP kiosk and desktop builds in the most confusing
# way available — a silent bounce back to the login page with no error anywhere.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

# HttpOnly on the CSRF cookie is Django's non-default. It is safe here only
# because every JavaScript caller now reads the token out of the DOM instead of
# document.cookie — see static/js/csrf.js and the note in that file. Adding an
# AJAX call that reads document.cookie for 'csrftoken' will silently 403.
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
# Drop Django's 1-year default (2026-08 assessment QA-09 / QAS-08 / F-07): a
# token that lives for 364 days on a shared classroom device is a long theft
# window. 12h covers a school day; when it lapses the next GET mints a fresh
# token and forms re-render with it. Not made session-scoped (None) so the
# offline PWA still has a usable token across a browser restart.
CSRF_COOKIE_AGE = 60 * 60 * 12

# The other half of that decision. Sessions run for two weeks (Django's
# SESSION_COOKIE_AGE default) while the token above lasts twelve hours, so a
# signed-in user outlives several token generations and a page left open across
# one submits a token the cookie no longer matches. Django's stock answer is a
# bare "Forbidden (403)" that names a mechanism the reader has never heard of
# and offers no way back. This one says what happened, that their input was not
# saved, and links to the form. The request is still rejected.
CSRF_FAILURE_VIEW = 'ai_tutor.apps.safety.csrf_failure.csrf_failure'

# The language cookie is set by django.views.i18n.set_language, which reads
# these settings rather than taking arguments. It carries a locale code, not a
# credential, so the direct impact was minimal — but no JavaScript reads it, so
# there is no reason for it to be readable.
LANGUAGE_COOKIE_HTTPONLY = True
LANGUAGE_COOKIE_SAMESITE = 'Lax'

if not DEBUG:
    # Safe to keep unconditionally: both App Gateway and the ALB overwrite
    # X-Forwarded-Proto, so it cannot be spoofed by a client.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = HTTPS_EDGE
    CSRF_COOKIE_SECURE = HTTPS_EDGE
    # Same gate, same reason. Missing before: the language cookie was the one
    # cookie left at framework defaults (F-06).
    LANGUAGE_COOKIE_SECURE = HTTPS_EDGE

    # Gated on HTTPS_EDGE, not on DEBUG alone. Both of these are actively
    # harmful without TLS in front:
    #
    #   SECURE_SSL_REDIRECT on a plain-HTTP deployment redirects every request
    #   to an https:// URL that nothing is listening on — the kiosk and the
    #   desktop build would answer nothing at all.
    #
    #   HSTS is worse, because a browser REMEMBERS it. Sending it from a
    #   plain-HTTP host tells every browser that reached it to refuse http://
    #   for a year, and there is no way to reach those users to undo it. That is
    #   why Django's own warning calls it "serious, irreversible".
    #
    # With TLS in front, both are correct and close security.W004 / W008.
    if HTTPS_EDGE:
        # ...except under pytest. Django forces DEBUG=False for tests, and the
        # test client speaks http://testserver, so a redirect here turns every
        # request in the suite into a 301 that never reaches a view. Observed:
        # 155 of 541 tests failed on the first version of this block.
        SECURE_SSL_REDIRECT = not _RUNNING_TESTS
        # ...except the health check, which MUST keep answering 200 on plain
        # HTTP. Load balancers health-check the container directly and do not
        # send X-Forwarded-Proto, so without this exemption Django answers 301,
        # the target group (matcher: 200) marks every task unhealthy, and the
        # service is taken out of rotation. Verified against the ALB in front of
        # the AWS deployment: HTTP GET /health/ on port 8000, matcher 200.
        SECURE_REDIRECT_EXEMPT = [r'^health/$']
        # One year, the value browsers expect before honouring a preload.
        SECURE_HSTS_SECONDS = 31536000
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        # NOT preloaded. Submitting to the preload list is a one-way door for
        # the whole domain — including subdomains nobody has built yet — and it
        # is not this file's decision to make for a ministry's domain.
        SECURE_HSTS_PRELOAD = False


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
            'device_enrol': '10/hour',
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
