"""Who a signed-in staff user is, and what that lets them do.

`get_staff_context` answers both questions at once, and every dashboard view
reads its answer off `request.staff_ctx`. It used to live in
`dashboard/views.py`; it moved here when a third and fourth role appeared,
because the question "what may this person do" is now asked of four kinds of
account and the answer belongs next to the models that define them, not
inside a 9,000-line view module.

FOUR ROLES, MOST PRIVILEGED FIRST. A user may hold several at once — a
ministry officer who also administers one school — so the dispatch order in
`get_staff_context` IS the precedence rule:

  superadmin    `User.is_staff`. Every school on the platform.
  country       a `CountryMembership`. Every school in one country.
  school_admin  a `Membership` with role SCHOOL_ADMIN. One school's people
                and settings, not its content.
  staff         a `Membership` with role STAFF. A teacher.

`can_edit_school_settings` is separate from `can_manage_people` because a
school admin holds both while the two answer different questions — a school's
name and timezone versus its teachers' accounts — and a later role may well
hold one without the other.

CAPABILITIES ARE FLAGS, NOT ROLE CHECKS. Views ask
`request.staff_ctx['can_manage_people']`, never `role == 'school_admin'`.
Adding a fifth role then means adding a row here rather than hunting for
every `is_superuser` in the codebase — which is exactly the hunt this module
was extracted to end.
"""

import logging

from django.utils.translation import gettext as _

from ai_tutor.apps.accounts.models import (
    CountryMembership, Institution, Membership)

logger = logging.getLogger(__name__)


def get_staff_context(request):
    """Common context for staff views, or None when the user is not staff.

    Supports multi-school via session-stored ``selected_school_id``.
    When no school is selected (or value is ``'all'``), ``institution``
    is ``None`` which means aggregated / all-schools mode.
    """
    selected = request.session.get('selected_school_id')

    if request.user.is_staff:
        return _superadmin_context(request, selected)

    country_membership = CountryMembership.objects.filter(
        user=request.user, is_active=True).select_related('country').first()
    if country_membership:
        return _country_context(request, country_membership, selected)

    admin = Membership.objects.filter(
        user=request.user, role=Membership.Role.SCHOOL_ADMIN, is_active=True,
    ).select_related('institution', 'institution__country').first()
    if admin:
        return _school_admin_context(request, admin)

    return _staff_context(request, selected)


def _superadmin_context(request, selected):
    """Platform-wide access."""
    all_schools = list(Institution.objects.filter(is_active=True).order_by('name'))

    if selected and selected != 'all':
        institution = Institution.objects.filter(id=selected, is_active=True).first()
    else:
        institution = None  # aggregated mode

    # Validator flags removed from the safety badge per Edward
    # (2026-05-07) — flagged dashboard is safety-only.
    return {
        'membership': None,
        'country': None,
        'institution': institution,
        'role': 'superadmin',
        # What to print where a person's role is shown. Two templates used
        # to each carry their own `{% if user.is_staff %}…{% else %}
        # membership.get_role_display{% endif %}`, and had already drifted
        # apart ("Super admin" in the topbar, "Super Admin" in settings).
        # The membership label is no help either: Role.STAFF reads "Staff
        # (Teacher/Admin)" because one value covers both, so it names two
        # roles and commits to neither. The distinction the product
        # actually makes is this one — platform-wide vs a school — so it
        # is resolved once, here, next to the flag it depends on.
        'role_label': _('Super Admin'),
        'all_schools': all_schools,
        'is_aggregated': institution is None,
        'unreviewed_flag_count': _safety_flag_count(institution),
        'can_edit_content': True,  # Superadmin always has full access
        'can_upload_curriculum': True,
        'can_regenerate_courses': True,
        'can_add_schools': True,
        'can_manage_people': True,
        'can_edit_school_settings': True,
    }


def _country_context(request, membership, selected):
    """A super admin bounded by one country.

    `selected` arrives from the session, where the school switcher put it, so
    it is user input. Resolving it against `schools` rather than against
    `Institution.objects` is what stops a ministry reading another country's
    school by posting its id.
    """
    schools = list(
        Institution.objects.filter(
            country=membership.country, is_active=True,
        ).exclude(country__is_hidden=True).order_by('name'))

    institution = None
    if selected and selected != 'all':
        institution = next((s for s in schools if str(s.pk) == str(selected)), None)

    return {
        'membership': None,
        'country': membership.country,
        'institution': institution,
        'role': 'country',
        'role_label': _('Country'),
        'all_schools': schools,
        'is_aggregated': institution is None,
        'unreviewed_flag_count': _safety_flag_count(institution, membership.country),
        'can_edit_content': True,
        'can_upload_curriculum': True,
        'can_regenerate_courses': True,
        'can_add_schools': True,
        'can_manage_people': True,
        'can_edit_school_settings': True,
    }


def _school_admin_context(request, membership):
    """Runs one school: its people and its settings, not its content.

    No school switcher — a school admin administers the one school, so
    `all_schools` is empty and `selected` is not consulted.
    """
    return {
        'membership': membership,
        'country': membership.institution.country,
        'institution': membership.institution,
        'role': 'school_admin',
        'role_label': _('School Admin'),
        'all_schools': [],
        'is_aggregated': False,
        'unreviewed_flag_count': _safety_flag_count(membership.institution),
        'can_edit_content': False,
        'can_upload_curriculum': False,
        'can_regenerate_courses': False,
        'can_add_schools': False,
        'can_manage_people': True,
        'can_edit_school_settings': True,
    }


def _staff_context(request, selected):
    """A teacher, who may belong to more than one school."""
    memberships = list(
        Membership.objects.filter(
            user=request.user,
            role=Membership.Role.STAFF,
            is_active=True
        ).select_related('institution', 'institution__country')
    )
    if not memberships:
        return None

    staff_schools = [m.institution for m in memberships if m.institution.is_active]

    if selected and selected != 'all':
        institution = next((s for s in staff_schools if str(s.id) == str(selected)), None)
        if not institution:
            institution = staff_schools[0] if staff_schools else memberships[0].institution
    else:
        institution = staff_schools[0] if staff_schools else memberships[0].institution

    membership = next((m for m in memberships if m.institution == institution), memberships[0])

    # Edward (2026-05-07): teachers should NOT be able to edit
    # lessons, regenerate, or upload curriculum — those are platform-
    # admin operations only. The PlatformConfig flags
    # (teachers_can_*) are now ignored for staff; only superusers
    # see editing affordances. Hard removal vs config flag was
    # explicitly requested to remove the operational risk.
    return {
        'membership': membership,
        'country': institution.country,
        'institution': institution,
        'role': 'staff',
        # See the superadmin branch above. Membership.Role now carries a
        # SCHOOL_ADMIN value, and a school admin is dispatched before this
        # function is reached — so a STAFF membership is a teacher, and
        # saying so is more accurate than "Staff (Teacher/Admin)", not less.
        'role_label': _('Teacher'),
        'all_schools': staff_schools if len(staff_schools) > 1 else [],
        'is_aggregated': False,
        'unreviewed_flag_count': _safety_flag_count(institution),
        'can_edit_content': False,
        'can_upload_curriculum': False,
        'can_regenerate_courses': False,
        'can_add_schools': False,
        'can_manage_people': False,
        'can_edit_school_settings': False,
    }


def _safety_flag_count(institution, country=None) -> int:
    """Count of unreviewed sessions with at least one harmful /
    inappropriate / manipulation flag from the safety judge.

    Per Edward (2026-05-07), the nav badge is safety-only — validator
    flags (curriculum-contradicted etc.) do NOT contribute. The legacy
    helper `_validator_flagged_count` was removed; if a future caller
    needs that count, query SessionTurn.metadata directly.

    *country* narrows the aggregated count, for a caller whose "all schools"
    is one country's rather than the platform's. Without it a ministry's
    badge would count flags raised in every other country — a small leak,
    but the number is the one thing on the page that is always visible.
    """
    from ai_tutor.apps.tutoring.models import SessionTurn, TutorSession

    SAFETY_FLAG_TYPES = ('harmful', 'inappropriate', 'manipulation')
    session_ids = set(
        SessionTurn.objects
        .filter(is_flagged=True, flag_type__in=SAFETY_FLAG_TYPES)
        .values_list('session_id', flat=True)
        .distinct()
    )
    qs = TutorSession.objects.filter(
        id__in=session_ids, is_flagged=True, flag_reviewed=False,
    )
    if institution is not None:
        qs = qs.filter(institution=institution)
    elif country is not None:
        qs = qs.filter(institution__country=country)
    return qs.count()


def has_staff_access(user):
    """Whether *user* may reach the dashboard at all.

    The same four roles `get_staff_context` dispatches over, asked without a
    request. The login gate used to spell out its own, narrower version —
    `is_staff` or a Membership with role STAFF — which meant the two roles
    added for country adoption could be created but never signed in with. A
    door and a room disagreeing about who may enter is the kind of bug that
    only shows up as "it just says invalid password".
    """
    if user.is_staff:
        return True
    if CountryMembership.objects.filter(user=user, is_active=True).exists():
        return True
    return Membership.objects.filter(
        user=user,
        role__in=(Membership.Role.STAFF, Membership.Role.SCHOOL_ADMIN),
        is_active=True,
    ).exists()


# ---------------------------------------------------------------------------
# Reach: which schools and which people an account may act on
# ---------------------------------------------------------------------------

def manageable_school_ids(staff_ctx):
    """Ids of the schools whose people this account may manage.

    ``None`` means every school — the platform super admin, and the same
    meaning ``institution=None`` carries everywhere else in the dashboard.
    An empty list means none, which is what a teacher gets; a teacher never
    reaches a view that asks, and returning `[]` rather than raising keeps
    that true if one ever does.

    Country accounts get a list rather than a country filter because the
    callers hold ids: every school, user and invitation on the staff page
    arrives as a POST id that has to be resolved against something.
    """
    role = staff_ctx.get('role')
    if role == 'superadmin':
        return None
    if role == 'country':
        return list(Institution.objects.filter(
            country=staff_ctx['country'], is_active=True,
        ).values_list('pk', flat=True))
    if role == 'school_admin':
        return [staff_ctx['institution'].pk]
    return []


def may_manage(staff_ctx, target):
    """Whether this account may act on *target*'s user record.

    A platform super admin is out of everyone else's reach. Promoting,
    deactivating or deleting one is the escalation the rest of these rules
    exist to prevent, and `is_staff` is the flag that grants platform-wide
    access, so it is checked before the school lookup rather than after.

    Someone with no membership at any school in reach is not reachable
    either. That is deliberate: it means the staff page's find-by-email box
    cannot be used to enumerate the platform's users, and it is why adding
    someone genuinely new goes through an invitation instead.
    """
    if target is None:
        return False
    ids = manageable_school_ids(staff_ctx)
    if ids is None:
        return True
    if target.is_staff or target.is_superuser:
        return False
    if not ids:
        return False
    return Membership.objects.filter(
        user=target, institution_id__in=ids).exists()
