"""Permission classes for the mobile REST API.

`IsInstitutionMember` is the default — students see only the courses /
lessons / sessions for institutions they're a member of, plus
platform-wide content (institution=None). Mirrors the existing
filter_by_institution helper used by the dashboard.
"""

from rest_framework.permissions import BasePermission


class IsInstitutionMember(BasePermission):
    """Authenticated user must have at least one active Membership."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_staff:
            return True
        return request.user.memberships.filter(is_active=True).exists()
