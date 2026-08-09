"""Authenticating a desktop device to the cloud.

Devices are not users. A laptop in a classroom pushes work on behalf of many
students, so it cannot hold any student's credentials — and the students it
syncs for may never have logged in online at all.

The token is presented as ``Authorization: Device <token>``. A distinct scheme
from ``Bearer`` on purpose: it makes the two credential types impossible to
confuse in a log or a middleware, and a device token accidentally sent to a
JWT-only endpoint fails closed rather than being parsed.

The token is stored hashed. A leaked table should not let anyone push as a
school's device, which is the same reasoning applied to passwords.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import authentication, exceptions

HEADER_PREFIX = 'Device '


class DeviceTokenAuthentication(authentication.BaseAuthentication):
    """DRF authentication for enrolled devices.

    Returns ``(None, device)`` — there is no user. Views must therefore check
    ``request.auth`` rather than ``request.user``, which is deliberate: a view
    written for logged-in students will not silently accept a device.
    """

    keyword = 'Device'

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode('utf-8', 'ignore')
        if not header.startswith(HEADER_PREFIX):
            return None                      # not ours; let other classes try

        raw = header[len(HEADER_PREFIX):].strip()
        if not raw:
            raise exceptions.AuthenticationFailed('Device token missing.')

        from apps.accounts.models import Device

        device = Device.objects.filter(
            token_hash=Device.hash_token(raw),
        ).select_related('institution').first()

        if device is None:
            raise exceptions.AuthenticationFailed('Unknown device token.')
        if not device.is_usable:
            # Distinguished from "unknown" on purpose: a revoked laptop should
            # tell its operator that it was revoked, not that it is broken.
            raise exceptions.AuthenticationFailed('This device has been revoked.')

        # Cheap liveness signal for the dashboard. update() rather than save()
        # to avoid a full-row write on every sync.
        Device.objects.filter(pk=device.pk).update(last_seen_at=timezone.now())
        return (None, device)

    def authenticate_header(self, request):
        return self.keyword
