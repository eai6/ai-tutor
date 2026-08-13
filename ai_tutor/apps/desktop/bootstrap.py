"""Readiness checks the desktop shell renders as splash-screen progress.

First launch is slow — the measured cold path is ~30 s of Ollama model load
before a single token, plus migrations and a content pack import. A spinner
with no explanation reads as a hang, and the student's instinct is to force
quit halfway through writing the database. So every slow step reports what it
is doing and whether it succeeded.

Each check returns the same shape so the shell can render them uniformly
without knowing what any of them mean:

    {"key": ..., "ok": bool, "label": str, "detail": str, "blocking": bool}

``blocking`` separates "the app cannot work" from "something is degraded".
A missing content pack blocks; an empty knowledge base does not — the tutor
still runs, just ungrounded, which is exactly the failure this project spent
Phase 0 making visible rather than silent.

Plan: memory/desktop_offline_app_plan.md
"""
from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get('OLLAMA_HOST') or 'http://localhost:11434'

# The tag the tutor resolves to. Must match DEFAULTS in desktop_server.py and
# the exact-match profile at apps/llm/model_profiles.py:209 — a bare qwen3 tag
# falls through to a cloud profile that sets num_ctx 24192, which evicts and
# reloads the runner on every graded turn.
REQUIRED_MODEL_TAG = 'qwen3-4b-jetson'


def _check(key, ok, label, detail='', blocking=True):
    return {'key': key, 'ok': bool(ok), 'label': label,
            'detail': detail, 'blocking': blocking}


def check_ollama_running() -> dict:
    from ai_tutor.apps.desktop import ollama_runtime
    info = ollama_runtime.describe()
    try:
        with urllib.request.urlopen(f'{OLLAMA_HOST}/api/version', timeout=5) as resp:
            import json
            version = json.loads(resp.read()).get('version', '?')
        origin = 'bundled' if info['bundled'] else 'system'
        return _check('ollama', True, 'Tutor engine', f'Ollama {version} ({origin})')
    except Exception as exc:
        # Distinguish "no binary to run" from "binary exists but is not
        # serving" — on an offline install those need completely different
        # fixes, and both otherwise present as "not reachable".
        if not info['available']:
            return _check('ollama', False, 'Tutor engine',
                          f'No Ollama binary for {info["platform"]}. This build '
                          f'is missing its bundled engine.')
        return _check('ollama', False, 'Tutor engine',
                      f'Not reachable at {OLLAMA_HOST} ({exc.__class__.__name__})')


def check_model_present() -> dict:
    """The model tag must exist locally — we never download at runtime."""
    try:
        import json
        with urllib.request.urlopen(f'{OLLAMA_HOST}/api/tags', timeout=5) as resp:
            tags = json.loads(resp.read()).get('models') or []
        names = {(m.get('name') or '').split(':')[0] for m in tags}
        if REQUIRED_MODEL_TAG in names:
            return _check('model', True, 'Language model', REQUIRED_MODEL_TAG)
        return _check('model', False, 'Language model',
                      f'{REQUIRED_MODEL_TAG} not installed. Run setup from the '
                      f'installation drive.')
    except Exception as exc:
        return _check('model', False, 'Language model',
                      f'Could not list models ({exc.__class__.__name__})')


def check_migrations() -> dict:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    try:
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        pending = executor.migration_plan(targets)
        if pending:
            return _check('migrations', False, 'Database',
                          f'{len(pending)} migration(s) pending')
        return _check('migrations', True, 'Database', 'up to date')
    except Exception as exc:
        return _check('migrations', False, 'Database', str(exc))


def check_static() -> dict:
    """WhiteNoise's manifest storage raises on any {% static %} without it.

    Blocking, because the failure mode is a 500 on the very first page with
    DEBUG=False and no clue in the UI as to why.
    """
    from pathlib import Path
    from django.conf import settings
    manifest = Path(settings.STATIC_ROOT) / 'staticfiles.json'
    if manifest.exists():
        return _check('static', True, 'Assets', 'manifest present')
    return _check('static', False, 'Assets',
                  'not collected — run collectstatic')


def check_content_pack() -> dict:
    from ai_tutor.apps.desktop.models import DeviceState
    try:
        state = DeviceState.load()
    except Exception as exc:
        return _check('content', False, 'Lessons', f'unreadable ({exc})')
    if not state.is_provisioned:
        return _check('content', False, 'Lessons',
                      'No lessons installed. Import a content pack from the '
                      'installation drive.')
    from ai_tutor.apps.curriculum.models import Lesson
    n = Lesson.objects.count()
    return _check('content', True, 'Lessons',
                  f'pack v{state.pack_version} — {n} lesson(s)')


def check_knowledge_base() -> dict:
    """Non-blocking: an empty KB degrades answers, it does not stop them.

    Reported anyway. Between the pgvector migration and 2026-07-30 this was
    empty on every SQLite install and nothing said so; the whole point of
    surfacing it here is that a silent version of this bug cannot recur.
    """
    from ai_tutor.apps.curriculum import kb_storage
    from ai_tutor.apps.desktop.models import DeviceState
    try:
        state = DeviceState.load()
        if state.institution_id is None:
            return _check('kb', False, 'Knowledge base', 'device not bound',
                          blocking=False)

        # Count the institution's own chunks AND the platform-wide bucket.
        # The tutor retrieves through query_with_global_fallback, which unions
        # both, and shared curriculum is normally indexed at
        # GLOBAL_INSTITUTION_ID = 0 — so counting only the bound institution
        # reported "no chunks" against a fully populated KB.
        own = kb_storage.collection_stats(state.institution_id)
        glob = (kb_storage.collection_stats(0)
                if state.institution_id != 0 else {'total_chunks': 0})
        n_own = own.get('total_chunks') or 0
        n_glob = glob.get('total_chunks') or 0
        n = n_own + n_glob
        if n == 0:
            return _check('kb', False, 'Knowledge base',
                          'no chunks — tutoring will be ungrounded',
                          blocking=False)
        detail = f"{n} chunks ({own.get('backend')})"
        if n_glob:
            detail += f" — {n_own} school, {n_glob} shared"
        return _check('kb', True, 'Knowledge base', detail, blocking=False)
    except Exception as exc:
        return _check('kb', False, 'Knowledge base', str(exc), blocking=False)


def check_embedding_backend() -> dict:
    """Non-blocking, but a wrong answer here means a much larger install."""
    from django.conf import settings
    from ai_tutor.apps.curriculum import kb_storage
    backend = getattr(settings, 'EMBEDDING_BACKEND', 'local')
    if backend != 'onnx':
        return _check('embeddings', False, 'Embeddings',
                      f'backend={backend} (desktop expects onnx)', blocking=False)
    directory = kb_storage._onnx_dir()
    if not (directory / 'model.onnx').exists():
        return _check('embeddings', False, 'Embeddings',
                      f'ONNX encoder missing from {directory}', blocking=False)
    return _check('embeddings', True, 'Embeddings', 'ONNX MiniLM', blocking=False)


CHECKS = (
    check_migrations,
    check_static,
    check_content_pack,
    check_ollama_running,
    check_model_present,
    check_embedding_backend,
    check_knowledge_base,
)


def status() -> dict:
    """Run every check. Never raises — the splash must render regardless."""
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:              # noqa: BLE001 - splash must render
            logger.exception('bootstrap check failed')
            results.append(_check(getattr(check, '__name__', 'check'), False,
                                  'Check failed', str(exc)))
    blocking_failures = [r for r in results if r['blocking'] and not r['ok']]
    return {
        'ready': not blocking_failures,
        'checks': results,
        'blocking_failures': [r['key'] for r in blocking_failures],
    }
