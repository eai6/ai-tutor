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

# Vector DB root: defaults to MEDIA_ROOT/vectordb, but can be overridden
# to use fast local storage (e.g., /tmp/vectordb) in production where
# MEDIA_ROOT is on a slow Azure File Share (SMB) mount.
VECTORDB_ROOT = os.getenv('VECTORDB_ROOT', os.path.join(MEDIA_ROOT, 'vectordb'))

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
