"""Endpoints the desktop shell and setup screen use. Not part of the web app.

All of these are unauthenticated on purpose: they run before anyone can log
in — a freshly installed app has no model, no lessons, and no accounts — and
the server binds to loopback only, so the sole caller is the shell in the same
process tree. Nothing here exposes student data.
"""
import json
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.desktop import provisioning
from apps.desktop.bootstrap import status


@never_cache
def health(request):
    """Bootstrap status for the splash screen."""
    return JsonResponse(status())


@never_cache
def setup(request):
    """First-run screen: install the model, import lessons."""
    return render(request, 'desktop/setup.html', {
        'status': status(),
        'provision': provisioning.STATE.snapshot(),
        'model_tag': provisioning.MODEL_TAG,
    })


@never_cache
@require_POST
def install_model(request):
    """Start a model install. Returns immediately; poll install_progress."""
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = {}
    source = payload.get('source')
    path = payload.get('path')

    if source not in ('file', 'download'):
        return JsonResponse({'error': "source must be 'file' or 'download'"}, status=400)
    if source == 'file' and not path:
        return JsonResponse({'error': 'No file selected.'}, status=400)

    return JsonResponse(provisioning.start_install(source, path))


@never_cache
def install_progress(request):
    state = provisioning.STATE.snapshot()
    state['installed'] = provisioning.model_installed()
    return JsonResponse(state)


@never_cache
@require_POST
def import_pack(request):
    """Import a content pack from a path on disk (USB stick or downloads)."""
    from apps.desktop.packs import PackError, import_pack as do_import

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = {}
    path = payload.get('path')
    if not path:
        return JsonResponse({'error': 'No file selected.'}, status=400)

    target = Path(path).expanduser()
    # A folder is what a teacher will actually pick on a USB stick; find the
    # pack inside it rather than making them drill down to the .tar.gz.
    if target.is_dir():
        candidates = sorted(target.rglob('content-pack-*.tar.gz'))
        if not candidates:
            return JsonResponse(
                {'error': f'No content pack found in {target.name}.'}, status=400)
        target = candidates[0]

    try:
        manifest = do_import(target)
    except PackError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except Exception as exc:                              # noqa: BLE001
        return JsonResponse({'error': f'{type(exc).__name__}: {exc}'}, status=500)

    return JsonResponse({
        'ok': True,
        'version': manifest.get('version'),
        'skipped': manifest.get('skipped'),
        'lessons': (manifest.get('counts') or {}).get('lessons'),
        'chunks': (manifest.get('counts') or {}).get('chunks'),
    })
