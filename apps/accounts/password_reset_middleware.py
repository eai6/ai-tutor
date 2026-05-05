"""Force-password-change middleware.

When an admin resets a staff user's password (see
``dashboard.staff_reset_password_show`` /
``dashboard.staff_reset_password_email``), every Membership row for
that user gets ``password_reset_required=True``. This middleware
checks the flag on every authenticated request and redirects the
user to the forced-change view until they set a new password.

Skipped paths so the user can actually GET to the change form:
  - the change-password URL itself
  - logout (so they can opt out)
  - static / media (no point gating these)
  - login flow (already unauthenticated)
"""

from __future__ import annotations

import logging
from django.shortcuts import redirect
from django.urls import reverse, NoReverseMatch
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


# Paths that must NOT redirect to the change-password page (otherwise
# the user can never reach the change form). Compared as prefixes.
_ALLOWLISTED_PREFIXES = (
    '/password/change-required/',  # the change form itself
    '/logout/',                    # let the user log out
    '/static/',
    '/media/',
    '/api/v1/auth/',               # mobile app auth; handles its own flow
)


class PasswordResetRequiredMiddleware(MiddlewareMixin):
    def process_request(self, request):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return None
        path = request.path or ''
        for prefix in _ALLOWLISTED_PREFIXES:
            if path.startswith(prefix):
                return None
        # Cheap O(1) cached check on the request — avoids hitting the
        # DB twice if multiple middlewares / views all want to know.
        cached = getattr(request, '_password_reset_required', None)
        if cached is None:
            try:
                cached = user.memberships.filter(
                    password_reset_required=True,
                ).exists()
            except Exception as e:
                # Don't gate the request on a transient DB error.
                logger.warning(
                    "[PasswordReset] check failed for user=%s: %s",
                    getattr(user, 'username', '?'), e,
                )
                cached = False
            request._password_reset_required = cached
        if cached:
            try:
                target = reverse('accounts:password_change_required')
            except NoReverseMatch:
                target = '/accounts/password/change-required/'
            return redirect(target)
        return None
