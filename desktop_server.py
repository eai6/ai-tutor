#!/usr/bin/env python3
"""Serve the tutor to a desktop app on loopback only.

    ./desktop_server.py                 # pick a free port, print it, serve
    ./desktop_server.py --port 8321
    ./desktop_server.py --probe         # print the handshake and exit

The third sibling of ``chat.py`` (terminal) and ``serve.py`` (hotspot/LAN).
It exists because neither of those is right for a single-user desktop app:

  - ``serve.py`` binds ``0.0.0.0``. On a laptop in a staffroom that publishes
    a student's whole session to the network. Desktop binds ``127.0.0.1``.
  - ``serve.py`` hardcodes port 8000, which collides with a dev server or a
    second copy of the app. Desktop asks the OS for a free port and tells the
    shell which one it got.
  - ``runserver`` is a development server and single-threaded by default.
    Desktop uses waitress: production-grade, pure Python, and — unlike
    gunicorn — actually works on Windows.

The port handshake is one line of JSON on stdout, flushed before serving:

    {"event": "listening", "port": 8321, "url": "http://127.0.0.1:8321/..."}

The shell reads that line rather than guessing or scanning ports.

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Same contract as serve.py:33-41 and chat.py — see the comments in chat.py for
# why each of these matters. OLLAMA_KV_CACHE_TYPE in particular must match how
# `ollama serve` was launched or the client's fit preflight sizes the KV cache
# at double its real value and refuses a model that fits.
DEFAULTS = {
    # NOTE: TUTOR_MODEL_OVERRIDE is deliberately NOT set here.
    #
    # It used to be, pinning the tutor to the local model — which silently
    # defeated the design documented at
    # apps/tutoring/simple_tutor/engine.py:2022: "the kiosk deliberately does
    # NOT set TUTOR_MODEL_OVERRIDE so that an admin can pick the model from the
    # browser". With the env var set, the model choice in
    # /dashboard/settings/ is read, saved, and then ignored at runtime.
    #
    # Instead, _ensure_tutor_model_config() seeds an active local ModelConfig
    # on first run, and from then on the database is the source of truth. That
    # is what makes "use the local model offline, switch to a cloud model when
    # the school has internet" a setting rather than a rebuild.
    #
    # Safe because model_profiles is keyed on the full spec: with no override,
    # the engine falls back to f'{provider}/{model_name}', which for
    # local_ollama/qwen3-4b-jetson matches the exact profile entry at
    # apps/llm/model_profiles.py:209 and keeps num_ctx pinned at 16384.
    'OLLAMA_FLASH_ATTENTION': '1',
    'OLLAMA_KV_CACHE_TYPE': 'q8_0',
    'OLLAMA_NUM_PARALLEL': '1',
    'OLLAMA_MAX_LOADED_MODELS': '1',
    'HF_HUB_OFFLINE': '1',
    'TRANSFORMERS_OFFLINE': '1',
    # The desktop build ships the ONNX encoder, not torch. Explicit rather
    # than inferred: a silent fall back to sentence-transformers would mean
    # shipping ~270 MB we deliberately removed.
    'EMBEDDING_BACKEND': 'onnx',
}

# Desktop-specific Django settings. The important divergence is cookies:
# config/settings.py marks session + CSRF cookies Secure whenever DEBUG is
# False, and a browser will not store a Secure cookie over http://127.0.0.1 —
# so every POST, login included, fails CSRF verification. See
# config/settings_desktop.py for the full reasoning.
SETTINGS_MODULE = 'config.settings_desktop'

LANDING_PATH = '/student/login/'


def data_dir() -> Path:
    """Where this installation keeps its database and media.

    A desktop app's data belongs in the platform's application-data location,
    not beside the source tree and not in a temp directory. Three reasons that
    matter here: it survives reinstalls and upgrades, it is per-user on a
    shared classroom machine, and it is somewhere a person can actually find
    and back up.

    Overridable with AI_TUTOR_DATA_DIR, which is how a test run points at a
    throwaway device without disturbing the real one.
    """
    override = os.environ.get('AI_TUTOR_DATA_DIR')
    if override:
        return Path(override).expanduser()

    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    elif os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or (Path.home() / 'AppData' / 'Roaming'))
    else:
        # XDG default; honours a customised XDG_DATA_HOME when set.
        base = Path(os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share'))
    return base / 'AI Tutor'


def _free_port() -> int:
    """Ask the OS for an unused port.

    Bind-then-close rather than a fixed default, so two copies of the app (or
    a dev server on 8000) cannot collide. The small race between closing here
    and waitress binding is acceptable on a single-user desktop.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def emit(event: str, **fields) -> None:
    """One JSON object per line on stdout, flushed.

    Flushing matters: the shell blocks reading this line, and Python buffers
    stdout when it is a pipe — which it always is when launched by the shell.
    """
    print(json.dumps({'event': event, **fields}), flush=True)


def _first_run_setup() -> None:
    """Apply migrations and collect static files if they are not current.

    Both are cheap no-ops once done, so this runs on every launch rather than
    behind a "have I run before" flag — a flag can disagree with reality after
    an app upgrade ships new migrations or new static assets.

    Static matters more than it looks: the project uses WhiteNoise's
    CompressedManifestStaticFilesStorage, which raises
    ``ValueError: Missing staticfiles manifest entry`` on the FIRST template
    that calls {% static %} when the manifest is absent. With DEBUG=False that
    surfaces as a bare 500 on the login page with nothing in the UI to explain
    it — which is exactly how this was found.
    """
    from django.core.management import call_command

    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if pending:
        emit('setup', step='migrate', detail=f'{len(pending)} pending')
        call_command('migrate', interactive=False, verbosity=0)

    from django.conf import settings
    manifest = Path(settings.STATIC_ROOT) / 'staticfiles.json'
    if not manifest.exists():
        emit('setup', step='collectstatic', detail='building asset manifest')
        call_command('collectstatic', interactive=False, verbosity=0)

    _ensure_tutor_model_config()


def _ensure_tutor_model_config() -> None:
    """Seed an active local tutoring ModelConfig if none exists.

    The desktop app resolves its tutor model from the database rather than an
    env var, so that a teacher can switch between the offline model and a cloud
    model from /dashboard/settings/ without a reinstall. That only works if
    there is a row to begin with: on a fresh device the table is empty and
    ``ModelConfig.get_for('tutoring')`` finds nothing.

    Seeds once and then leaves the row alone. If an admin later switches the
    active tutoring model to a cloud provider, this must not quietly switch it
    back on the next launch — which is why the guard is "is there an active
    tutoring config at all", not "is the local one active".
    """
    from apps.accounts.models import Institution
    from apps.llm.models import ModelConfig

    if ModelConfig.objects.filter(purpose='tutoring', is_active=True).exists():
        return

    ModelConfig.objects.update_or_create(
        provider='local_ollama', model_name='qwen3-4b-jetson', purpose='tutoring',
        defaults=dict(
            # institution is a required FK; the platform-global institution is
            # the codebase's idiom for "not school-specific" (it is
            # get-or-created, so this works on a completely fresh device DB).
            # Without it the seed raises IntegrityError and the server dies
            # before ever listening — found driving desktop.py --url-only
            # against the dev DB, 2026-08-03.
            institution=Institution.get_global(),
            name='Offline tutor (qwen3-4b)',
            api_key_env_var='',      # keyless: talks to the local Ollama server
            api_base='',             # client defaults to http://localhost:11434
            temperature=0.2,         # inside the TUTORING clamp [0.1, 0.3]
            max_tokens=1024,
            is_active=True,
        ),
    )
    emit('setup', step='model_config', detail='seeded offline tutor model')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--port', type=int, default=None,
                        help='Fixed port. Default: ask the OS for a free one.')
    parser.add_argument('--probe', action='store_true',
                        help='Print bootstrap status as JSON and exit.')
    parser.add_argument('--no-setup', action='store_true',
                        help='Skip migrate/collectstatic on startup.')
    args = parser.parse_args()

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', SETTINGS_MODULE)

    # Point Django at this installation's own data before it configures. Both
    # are setdefault, so an explicit DATABASE_URL / MEDIA_ROOT still wins —
    # that is how a developer runs the desktop build against the repo's dev DB.
    root = data_dir()
    (root / 'media').mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('DATABASE_URL', f'sqlite:///{root / "tutor.db"}')
    os.environ.setdefault('MEDIA_ROOT', str(root / 'media'))
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)

    import django
    django.setup()

    if args.probe:
        from apps.desktop.bootstrap import status
        emit('status', **status())
        return 0

    if not args.no_setup:
        _first_run_setup()

    port = args.port or _free_port()
    url = f'http://127.0.0.1:{port}{LANDING_PATH}'

    from django.core.wsgi import get_wsgi_application
    from waitress import create_server

    application = get_wsgi_application()

    # create_server() binds the socket; run() starts accepting. Splitting them
    # is the point: `waitress.serve()` blocks forever, so the only place to
    # announce the port would be *before* the bind — which is a lie the shell
    # then acts on, polling a socket that is still refusing connections
    # ("startup failed: [Errno 61] Connection refused"). Binding first makes
    # the announcement true when it is printed.
    #
    # threads=4: a tutor turn is one long blocking LLM call, and the page still
    # needs to serve static assets alongside it. 1 thread would deadlock the UI
    # behind a 15 s generation.
    server = create_server(application, host='127.0.0.1', port=port, threads=4,
                           clear_untrusted_proxy_headers=True,
                           ident='ai-tutor-desktop')

    emit('listening', port=port, url=url)
    server.run()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
