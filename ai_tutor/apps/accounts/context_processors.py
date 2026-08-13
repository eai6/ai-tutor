"""
Theme context processor — injects platform branding into every template.

Branding is platform-wide (stored in PlatformConfig), so all users see the
same theme regardless of which school they belong to.
"""

from ai_tutor.apps.accounts.models import PlatformConfig


def _darken_hex(hex_color, factor=0.15):
    """Darken a hex color by a factor (0-1)."""
    hex_color = hex_color.lstrip('#')
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r = max(0, int(r * (1 - factor)))
    g = max(0, int(g * (1 - factor)))
    b = max(0, int(b * (1 - factor)))
    return f'#{r:02x}{g:02x}{b:02x}'


def _lighten_hex(hex_color, factor=0.85):
    """Lighten a hex color towards white by a factor (0-1)."""
    hex_color = hex_color.lstrip('#')
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r = min(255, int(r + (255 - r) * factor))
    g = min(255, int(g + (255 - g) * factor))
    b = min(255, int(b + (255 - b) * factor))
    return f'#{r:02x}{g:02x}{b:02x}'


def _build_theme_dict():
    """Build theme context dict from PlatformConfig singleton."""
    config = PlatformConfig.load()
    primary = config.primary_color or '#E8590C'
    return {
        'theme_primary': primary,
        'theme_secondary': config.secondary_color or '#4ECDC4',
        'theme_accent': config.accent_color or '#FFE66D',
        'theme_primary_dark': _darken_hex(primary),
        'theme_primary_light': _lighten_hex(primary),
        'theme_logo_url': config.logo.url if config.logo else '',
        'theme_institution_name': config.platform_name or 'AI Tutor',
    }


def institution_theme(request):
    """Return platform-wide theme variables for every template."""
    return _build_theme_dict()


def staff_flag(request):
    """Expose ``is_staff_user`` to every template — True when the user
    is a Django super-admin OR has an active Membership with role='staff'
    (a teacher). Lets templates render staff-only chrome (back-to-dashboard
    links, edit buttons, etc.) without each view computing it.

    Defensive: false on unauthenticated / anon / errored requests."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {'is_staff_user': False}
    if user.is_staff or user.is_superuser:
        return {'is_staff_user': True}
    try:
        from ai_tutor.apps.accounts.models import Membership
        return {
            'is_staff_user': Membership.objects.filter(
                user=user, role='staff', is_active=True,
            ).exists(),
        }
    except Exception:
        return {'is_staff_user': False}


def email_verification_status(request):
    """Expose `user_email_verified` to every template so the soft-gate
    banner can decide whether to show. Defensive — handles users who
    pre-date the UserEmailStatus migration."""
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {'user_email_verified': True}  # treat unauthenticated as no banner
    if not request.user.email:
        return {'user_email_verified': True}  # nothing to verify
    try:
        from ai_tutor.apps.accounts.models import UserEmailStatus
        status = UserEmailStatus.objects.filter(user=request.user).first()
        return {'user_email_verified': bool(status and status.verified_at)}
    except Exception:
        return {'user_email_verified': True}  # fail-open: don't show banner on error


def baseline_recommendations(request):
    """Expose `baseline_recommended_courses` (list of {id, title, url})
    to every template so the soft baseline-recommend banner can render.

    Returns courses where:
      - the course has a published summative
      - the student has NO completed baseline attempt on it
    Skips staff and superusers. Empty for unauthenticated users.

    Replaces the old hard gate (baseline_required_for that
    short-circuited lesson access). Per the 2026-04-29 pilot decision:
    students can start lessons immediately; the banner keeps nudging.
    """
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated or user.is_staff or user.is_superuser:
        return {'baseline_recommended_courses': []}
    try:
        from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketAttempt
        from ai_tutor.apps.accounts.models import Membership

        # Limit to courses the student is actually enrolled in via institution.
        memberships = Membership.objects.filter(
            user=user, role='student', is_active=True,
        ).select_related('institution')
        if not memberships:
            return {'baseline_recommended_courses': []}

        institutions = [m.institution for m in memberships if m.institution]
        if not institutions:
            return {'baseline_recommended_courses': []}

        # Published summatives in the student's institution(s) OR platform-wide.
        from django.db.models import Q
        summatives = ExitTicket.objects.filter(
            assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
            is_published=True,
        ).filter(
            Q(course__institution__in=institutions) | Q(course__institution__isnull=True),
        ).select_related('course')

        recommended = []
        for sm in summatives:
            has_baseline = ExitTicketAttempt.objects.filter(
                exit_ticket=sm,
                student=user,
                purpose=ExitTicketAttempt.Purpose.BASELINE,
                completed_at__isnull=False,
            ).exists()
            if not has_baseline and sm.course:
                recommended.append({
                    'id': sm.course.id,
                    'title': sm.course.title,
                    'url': f"/tutor/summative/{sm.course.id}/",
                })
        return {'baseline_recommended_courses': recommended}
    except Exception:
        return {'baseline_recommended_courses': []}  # fail-open


def desktop_build(request):
    """Expose ``is_desktop_build`` so templates can show device-only settings.

    The school-server address belongs to one installed machine. On the hosted
    web app there is no such thing, so the panel must not appear there — and
    `apps.desktop` being installed everywhere means the template cannot tell
    the difference by itself.
    """
    from django.conf import settings
    return {'is_desktop_build': getattr(settings, 'DESKTOP_BUILD', False)}
