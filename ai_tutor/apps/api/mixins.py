"""Shared mixins for mobile API views."""

from django.db.models import Q


def get_user_institution_ids(user):
    """Return list of institution IDs the user is a member of.

    Superadmins (request.user.is_staff) get an empty list; callers
    should treat that as "no scoping" (visible everywhere).
    """
    if not user or not user.is_authenticated:
        return []
    if user.is_staff:
        return []
    return list(
        user.memberships.filter(is_active=True).values_list('institution_id', flat=True)
    )


class InstitutionScopedMixin:
    """Apply the standard institution scope to a queryset.

    `institution_field` is the dotted path to the Institution FK on the
    model (e.g. 'institution' or 'unit__course__institution'). Subclasses
    must set this attribute.

    Behavior:
      - Authenticated user with memberships → restrict to those
        institutions, plus null (platform-wide) content.
      - Superadmin (is_staff) → no scoping (sees everything).
      - Unauthenticated → empty queryset (defense in depth; permission
        classes already block this case).
    """

    institution_field = 'institution'

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user or not user.is_authenticated:
            return qs.none()
        if user.is_staff:
            return qs
        ids = get_user_institution_ids(user)
        if not ids:
            return qs.none()
        field = self.institution_field
        return qs.filter(
            Q(**{f'{field}__in': ids}) | Q(**{f'{field}__isnull': True})
        )
