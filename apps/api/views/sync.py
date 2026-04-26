"""POST /api/v1/sessions/<id>/sync/

Bulk-upload offline-generated turns + an engine_state snapshot from the
React Native app's local SQLite store. Conflict policy (per
memory/mobile_rn_plan.md):

  - SessionTurn rows: append-only. The server rejects duplicates by
    (session_id, client_uuid) pairing — clients send a UUID for each
    turn so they can be deduped if a sync is retried after a partial
    success.
  - engine_state: last-write-wins, keyed on client_generated_at — the
    server stores the snapshot if it's newer than the current one.
  - ExitTicketAttempt: append-only (one row per submission); server
    iterates active participants and writes one per student.

The endpoint is intentionally tolerant of partial payloads (only state,
only turns, etc.) so the client can sync incrementally.
"""

from datetime import datetime

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import IsInstitutionMember
from apps.tutoring.models import (
    ExitTicket, ExitTicketAttempt, SessionTurn, TutorSession,
)


def _user_authorized(session, user):
    if session.student_id == user.id:
        return True
    return session.participants.filter(student=user, is_active=True).exists()


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsInstitutionMember])
def sync(request, session_id):
    session = get_object_or_404(TutorSession, id=session_id)
    if not _user_authorized(session, request.user):
        return Response({'detail': 'forbidden'}, status=403)

    payload = request.data or {}
    turns_in = payload.get('turns') or []
    state_in = payload.get('engine_state')
    state_at = _parse_iso(payload.get('engine_state_client_at'))
    exit_attempt = payload.get('exit_ticket_attempt')

    summary = {
        'turns_created': 0,
        'turns_skipped_duplicate': 0,
        'state_updated': False,
        'exit_attempt_created': False,
    }

    # ── Turns (append-only, dedupe on metadata.client_uuid) ──
    for turn in turns_in:
        if not isinstance(turn, dict):
            continue
        role = turn.get('role')
        content = (turn.get('content') or '').strip()
        if role not in {'student', 'tutor', 'system'} or not content:
            continue
        client_uuid = turn.get('client_uuid') or ''
        if client_uuid:
            existing = SessionTurn.objects.filter(
                session=session, metadata__client_uuid=client_uuid,
            ).exists()
            if existing:
                summary['turns_skipped_duplicate'] += 1
                continue
        meta = turn.get('metadata') or {}
        if not isinstance(meta, dict):
            meta = {}
        if client_uuid:
            meta['client_uuid'] = client_uuid
        meta.setdefault('source', 'mobile_offline')
        SessionTurn.objects.create(
            session=session,
            role=role,
            content=content,
            metadata=meta,
            generated_offline=bool(turn.get('generated_offline', True)),
            offline_model_id=turn.get('offline_model_id', '') or '',
            client_generated_at=_parse_iso(turn.get('client_generated_at')),
        )
        summary['turns_created'] += 1

    # ── engine_state (last-write-wins, gated on client timestamp) ──
    if isinstance(state_in, dict) and state_at is not None:
        last_at_raw = (session.engine_state or {}).get('_synced_client_at')
        last_at = _parse_iso(last_at_raw)
        if last_at is None or state_at > last_at:
            new_state = dict(session.engine_state or {})
            new_state.update(state_in)
            new_state['_synced_client_at'] = state_at.isoformat()
            session.engine_state = new_state
            session.save(update_fields=['engine_state'])
            summary['state_updated'] = True

    # ── Exit ticket attempt (append; iterate active participants) ──
    if isinstance(exit_attempt, dict):
        exit_ticket = ExitTicket.objects.filter(lesson=session.lesson).first()
        if exit_ticket:
            answers = exit_attempt.get('answers') or []
            score = int(exit_attempt.get('score') or 0)
            passed = bool(exit_attempt.get('passed'))
            completed_at = _parse_iso(exit_attempt.get('completed_at')) or timezone.now()
            try:
                participants = list(session.active_students)
            except Exception:
                participants = []
            if not participants:
                participants = [session.student]
            for participant in participants:
                ExitTicketAttempt.objects.create(
                    exit_ticket=exit_ticket,
                    student=participant,
                    session=session,
                    score=score,
                    passed=passed,
                    answers=answers,
                    completed_at=completed_at,
                )
            summary['exit_attempt_created'] = True

    return Response(summary, status=status.HTTP_200_OK)
