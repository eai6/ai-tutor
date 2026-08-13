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

from ai_tutor.apps.accounts.models import Device
from ai_tutor.apps.api.device_auth import DeviceTokenAuthentication


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
    from ai_tutor.apps.api.device_auth import DeviceTokenAuthentication

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


@api_view(['POST'])
@authentication_classes([DeviceTokenAuthentication])
@permission_classes([])
def device_sync(request):
    """Accept one outbox item from an enrolled device.

    One item per request rather than a batch: a classroom uplink drops
    mid-request often enough that an all-or-nothing batch would retry work that
    already landed. Per-item, a dropped connection costs one row's retry, and
    the client_uuid makes that retry free.

    Authenticated as a DEVICE (request.auth), never as a user. The device says
    which student the work belongs to via server_user_id, and the server checks
    that student is actually in the device's institution — a compromised device
    must not be able to write into another school.
    """
    from django.db import IntegrityError, transaction
    from ai_tutor.apps.accounts.models import Membership
    from ai_tutor.apps.tutoring.models import TutorSession, SessionTurn

    device = request.auth
    if device is None:
        return Response({'detail': 'Device authentication required.'},
                        status=status.HTTP_401_UNAUTHORIZED)

    payload = request.data or {}
    client_uuid = (payload.get('client_uuid') or '').strip()
    kind = (payload.get('kind') or '').strip()
    server_user_id = payload.get('server_user_id')
    body = payload.get('payload') or {}

    if not client_uuid or not kind:
        return Response({'detail': 'client_uuid and kind are required.'},
                        status=status.HTTP_400_BAD_REQUEST)

    # Resolve the student, and refuse anyone outside this device's institution.
    student = None
    if server_user_id:
        membership = (
            Membership.objects
            .filter(user_id=server_user_id, institution_id=device.institution_id,
                    is_active=True)
            .select_related('user').first()
        )
        if membership is None:
            # Not "not found" — the device asked to write for someone who is
            # not its school's student. Worth refusing loudly.
            return Response(
                {'detail': 'That student does not belong to this device\'s institution.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        student = membership.user

    if kind == 'session':
        if student is None:
            return Response({'detail': 'server_user_id is required for a session.'},
                            status=status.HTTP_400_BAD_REQUEST)

        lesson_id = body.get('lesson_id')
        if not lesson_id:
            return Response({'detail': 'lesson_id is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # The session itself is deduped on the outbox's client_uuid, carried
        # onto its first turn. A device that retries after a lost response must
        # not create a second session for the same lesson sitting.
        existing = SessionTurn.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return Response({'detail': 'already recorded',
                             'session_id': existing.session_id},
                            status=status.HTTP_409_CONFLICT)

        try:
            with transaction.atomic():
                session = TutorSession.objects.create(
                    student=student,
                    lesson_id=lesson_id,
                    institution_id=device.institution_id,
                    status=body.get('status') or TutorSession.Status.ACTIVE,
                )
                for i, turn in enumerate(body.get('turns') or []):
                    SessionTurn.objects.create(
                        session=session,
                        role=turn.get('role') or 'student',
                        content=turn.get('content') or '',
                        # Only the first turn carries the outbox uuid; the rest
                        # are unique by their own client ids if supplied.
                        client_uuid=client_uuid if i == 0 else (turn.get('client_uuid') or None),
                        generated_offline=True,
                        metadata={'source': 'desktop_offline',
                                  'device_id': str(device.device_id or '')},
                    )
        except IntegrityError:
            return Response({'detail': 'already recorded'},
                            status=status.HTTP_409_CONFLICT)

        return Response({'session_id': session.id}, status=status.HTTP_201_CREATED)

    return Response({'detail': f'Unsupported kind: {kind}'},
                    status=status.HTTP_400_BAD_REQUEST)
