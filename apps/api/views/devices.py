"""Device enrolment: turning a one-time code into a lasting token.

The device is unauthenticated when it calls this — that is the point of the
code. What makes it safe is that the code is short-lived in practice (a teacher
generates it, types it, and it is consumed), single-use, and scoped to one
institution.

Rate limited: an 8-character code from a 31-character alphabet is not something
to leave open to unlimited guessing.
"""
from __future__ import annotations

import secrets

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.accounts.models import Device


class EnrolThrottle(AnonRateThrottle):
    """Slower than the global anon rate — this endpoint guesses codes."""
    scope = 'device_enrol'
    rate = '10/hour'


@api_view(['POST'])
@authentication_classes([])          # the code IS the credential here
@permission_classes([])
@throttle_classes([EnrolThrottle])
def enrol(request):
    """Exchange a one-time enrolment code for a device token.

    POST {"code": "ABCD-2345", "device_id": "<uuid>", "name": "Lab laptop 3"}
    ->  {"token": "...", "institution_id": 4, "institution_name": "..."}

    The token is returned exactly once and stored hashed. If a device loses it,
    a teacher issues a new code — which is also how a reinstalled laptop is
    handled, and why losing it is not a crisis.
    """
    code = (request.data.get('code') or '').strip().upper()
    device_id = (request.data.get('device_id') or '').strip()
    name = (request.data.get('name') or '').strip()[:120]

    if not code:
        return Response({'detail': 'Enrolment code is required.'},
                        status=status.HTTP_400_BAD_REQUEST)

    device = Device.objects.filter(
        enrolment_code=code, status=Device.Status.PENDING,
    ).select_related('institution').first()

    # One message for both "no such code" and "already used". Distinguishing
    # them tells someone guessing which codes exist.
    if device is None:
        return Response({'detail': 'That enrolment code is not valid.'},
                        status=status.HTTP_400_BAD_REQUEST)

    raw_token = secrets.token_urlsafe(32)
    device.token_hash = Device.hash_token(raw_token)
    device.device_id = device_id or None
    device.name = name or device.name
    device.status = Device.Status.ACTIVE
    device.enrolled_at = timezone.now()
    device.save(update_fields=['token_hash', 'device_id', 'name', 'status', 'enrolled_at'])

    return Response({
        'token': raw_token,
        'institution_id': device.institution_id,
        'institution_name': device.institution.name,
        'device_name': device.name,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def device_check(request):
    """Whether the presented device token still works.

    Lets a device tell "revoked" apart from "no internet" without attempting a
    sync — the difference between showing a teacher "contact your admin" and
    "we'll try again later".
    """
    from apps.api.device_auth import DeviceTokenAuthentication

    try:
        result = DeviceTokenAuthentication().authenticate(request)
    except Exception as exc:                       # noqa: BLE001
        return Response({'ok': False, 'detail': str(exc)},
                        status=status.HTTP_401_UNAUTHORIZED)

    if result is None:
        return Response({'ok': False, 'detail': 'No device token presented.'},
                        status=status.HTTP_401_UNAUTHORIZED)

    _, device = result
    return Response({
        'ok': True,
        'institution_id': device.institution_id,
        'device_name': device.name,
    })
