"""Endpoints the desktop shell and setup screen use. Not part of the web app.

All of these are unauthenticated on purpose: they run before anyone can log
in — a freshly installed app has no model, no lessons, and no accounts — and
the server binds to loopback only, so the sole caller is the shell in the same
process tree. Nothing here exposes student data.
"""
import json
from pathlib import Path

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
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


# ─── Roster claim ───────────────────────────────────────────────────────
#
# On a provisioned device the student does NOT self-register. They pick their
# name from the roster the content pack shipped, and that binds their local
# login to the server user id their work will sync under. Self-registration
# still exists as the fallback for someone missing from the roster, but their
# rows sync as unmatched for a teacher to reconcile.
#
# Loopback-only, like every view in this module: the desktop server binds to
# 127.0.0.1, so anyone who can reach this is already sitting at the machine.
# Picking a name off a list is exactly as strong as the paper register it
# replaces — the password set here is what protects the account afterwards.

def claim_page(request):
    """Show the unclaimed roster so a student can pick their name."""
    from apps.desktop.models import DeviceState, RosterEntry

    state = DeviceState.load()
    if not state.is_provisioned:
        return redirect('desktop:setup')

    query = (request.GET.get('q') or '').strip()
    entries = RosterEntry.objects.filter(local_user__isnull=True)
    if query:
        entries = entries.filter(
            Q(display_name__icontains=query) | Q(username__icontains=query)
        )

    return render(request, 'desktop/claim.html', {
        'entries': entries[:200],
        'query': query,
        'unclaimed_total': RosterEntry.objects.filter(local_user__isnull=True).count(),
        'claimed_total': RosterEntry.objects.filter(local_user__isnull=False).count(),
    })


@require_POST
def claim_submit(request):
    """Bind a roster entry to a new local account and sign the student in."""
    from django.contrib.auth import login
    from django.contrib.auth.models import User
    from apps.accounts.models import Institution, Membership, StudentProfile
    from apps.desktop.models import DeviceState, RosterEntry

    state = DeviceState.load()
    server_user_id = (request.POST.get('server_user_id') or '').strip()
    password = request.POST.get('password') or ''

    def fail(message):
        return render(request, 'desktop/claim.html', {
            'entries': RosterEntry.objects.filter(local_user__isnull=True)[:200],
            'error': message,
        }, status=400)

    if len(password) < 4:
        return fail(_('Choose a password of at least 4 characters.'))

    # select_for_update so two people cannot claim the same name at once. The
    # OneToOne on local_user would raise anyway, but a 500 is a poor way to
    # tell a student their classmate got there first.
    with transaction.atomic():
        entry = (
            RosterEntry.objects.select_for_update()
            .filter(server_user_id=server_user_id, local_user__isnull=True)
            .first()
        )
        if entry is None:
            return fail(_('That name has already been set up on this device. '
                          'Sign in instead, or ask your teacher.'))

        # The local username is derived, not the server's, so it cannot collide
        # with a self-registered account that happens to share a name.
        base = f'{entry.username}'.strip() or f'student{entry.server_user_id}'
        username = base
        suffix = 1
        while User.objects.filter(username=username).exists():
            suffix += 1
            username = f'{base}{suffix}'

        parts = entry.display_name.split(' ', 1)
        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else '',
        )
        StudentProfile.objects.get_or_create(
            user=user, defaults={'grade_level': entry.grade_level or ''},
        )
        institution = Institution.objects.filter(pk=state.institution_id).first()
        if institution:
            Membership.objects.get_or_create(
                user=user, institution=institution,
                defaults={'role': Membership.Role.STUDENT, 'is_active': True},
            )

        entry.local_user = user
        entry.claimed_at = timezone.now()
        entry.save(update_fields=['local_user', 'claimed_at'])

    login(request, user)
    return redirect('tutoring:catalog')
