"""Session lifecycle endpoints. Most of these wrap the existing tutor
engine logic in apps/tutoring/views.py so we don't duplicate the
ConversationalTutor wiring.
"""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import IsInstitutionMember
from apps.api.serializers.tutoring import TutorSessionSerializer
from apps.curriculum.models import Lesson
from apps.tutoring.models import TutorSession


def _serialize_tutor_message(msg):
    """ConversationalTutor.respond() returns a dataclass — flatten it."""
    return {
        'message': msg.content,
        'phase': msg.phase,
        'media': msg.media or [],
        'show_exit_ticket': bool(msg.show_exit_ticket),
        'exit_ticket': msg.exit_ticket_data,
        'is_complete': bool(msg.is_complete),
        'step_number': msg.step_number,
        'total_steps': msg.total_steps,
        'is_correct': getattr(msg, 'is_correct', None),
        'streak_count': getattr(msg, 'streak_count', None),
        'practice_score': getattr(msg, 'practice_score', None),
        'milestone': getattr(msg, 'milestone', None),
        'artifact_html': getattr(msg, 'artifact_html', None),
    }


def _client_form_factor(request) -> str:
    """Read X-Client-Form-Factor header. Mobile clients ship 'mobile';
    everything else is treated as 'web'. Used by the tutor to keep
    responses concise on small screens."""
    raw = (request.META.get('HTTP_X_CLIENT_FORM_FACTOR') or '').strip().lower()
    return 'mobile' if raw == 'mobile' else 'web'


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsInstitutionMember])
def start_session(request):
    """POST /api/v1/sessions/  body={lesson_id, initial_participants?}"""
    from apps.accounts.models import Institution
    from apps.tutoring.conversational_tutor import ConversationalTutor
    from apps.tutoring.models import StudentLessonProgress, SessionParticipant
    from apps.tutoring.views import _try_add_participant
    from django.db.models import Q

    lesson_id = request.data.get('lesson_id')
    if not lesson_id:
        return Response({'detail': 'lesson_id required'}, status=400)

    user = request.user
    institution_ids = list(
        user.memberships.filter(is_active=True).values_list('institution_id', flat=True)
    )
    if not user.is_staff and not institution_ids:
        return Response({'detail': 'no institution membership'}, status=403)
    lesson_qs = Lesson.objects.filter(is_published=True)
    if not user.is_staff:
        lesson_qs = lesson_qs.filter(
            Q(unit__course__institution_id__in=institution_ids)
            | Q(unit__course__institution__isnull=True),
        )
    lesson = get_object_or_404(lesson_qs, id=lesson_id)

    # Resume an existing active session if one exists.
    existing = TutorSession.objects.filter(
        student=user, lesson=lesson, status=TutorSession.Status.ACTIVE,
    ).first()
    if existing:
        tutor = ConversationalTutor(existing)
        tutor.client_form_factor = _client_form_factor(request)
        msg = tutor.resume()
        return Response({
            'session_id': existing.id,
            **_serialize_tutor_message(msg),
            'resumed': True,
        })

    session_institution = lesson.unit.course.institution or Institution.get_global()
    session = TutorSession.objects.create(
        student=user, lesson=lesson, institution=session_institution,
        status=TutorSession.Status.ACTIVE,
    )
    SessionParticipant.objects.get_or_create(
        session=session, student=user,
        defaults={'is_primary': True, 'is_active': True},
    )

    # Optional: initial_participants for one-shot group session start.
    initial = request.data.get('initial_participants') or []
    primary_inst = lesson.unit.course.institution
    if initial and lesson.allow_group_mode:
        for entry in initial[: max(lesson.max_group_size - 1, 0)]:
            _try_add_participant(session, entry, primary_inst)

    StudentLessonProgress.objects.get_or_create(
        student=user, lesson=lesson,
        defaults={'institution': session_institution, 'mastery_level': 'in_progress'},
    )
    tutor = ConversationalTutor(session)
    tutor.client_form_factor = _client_form_factor(request)
    msg = tutor.start()
    return Response({'session_id': session.id, **_serialize_tutor_message(msg)},
                    status=status.HTTP_201_CREATED)


def _user_owns_or_participates(session, user):
    if session.student_id == user.id:
        return True
    return session.participants.filter(student=user, is_active=True).exists()


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsInstitutionMember])
def respond(request, session_id):
    """POST /api/v1/sessions/<id>/respond/  body={message: str}"""
    from apps.tutoring.conversational_tutor import ConversationalTutor

    session = get_object_or_404(TutorSession, id=session_id)
    if not _user_owns_or_participates(session, request.user):
        return Response({'detail': 'forbidden'}, status=403)
    message = (request.data.get('message') or '').strip()
    if not message:
        return Response({'detail': 'message required'}, status=400)

    tutor = ConversationalTutor(session)
    tutor.client_form_factor = _client_form_factor(request)
    msg = tutor.respond(message)
    return Response(_serialize_tutor_message(msg))


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsInstitutionMember])
def submit_exit_ticket(request, session_id):
    """POST /api/v1/sessions/<id>/exit-ticket/  body={answers: [...]}."""
    from apps.tutoring.competency import attempt_response_block
    from apps.tutoring.conversational_tutor import ConversationalTutor
    from apps.tutoring.models import ExitTicket, StudentLessonProgress

    session = get_object_or_404(TutorSession, id=session_id)
    if not _user_owns_or_participates(session, request.user):
        return Response({'detail': 'forbidden'}, status=403)
    answers = request.data.get('answers') or []

    tutor = ConversationalTutor(session)
    msg = tutor.submit_exit_ticket(answers)

    exit_ticket = ExitTicket.objects.filter(lesson=session.lesson).first()
    progress = StudentLessonProgress.objects.filter(
        student=request.user, lesson=session.lesson,
    ).first()
    results = (msg.exit_ticket_data or {}).get('results', [])
    score = (msg.exit_ticket_data or {}).get('score', 0)
    competency = attempt_response_block(score, results, exit_ticket, progress)
    enriched = dict(msg.exit_ticket_data or {})
    enriched['competency'] = competency

    return Response({
        'message': msg.content,
        'phase': msg.phase,
        'exit_ticket': enriched,
        'is_complete': msg.is_complete,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsInstitutionMember])
def start_review(request, session_id):
    """POST /api/v1/sessions/<id>/review/ — re-enter review mode for a
    completed session."""
    from apps.tutoring.conversational_tutor import ConversationalTutor

    session = get_object_or_404(TutorSession, id=session_id, student=request.user)
    tutor = ConversationalTutor(session)
    msg = tutor.start_review()
    return Response(_serialize_tutor_message(msg))


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsInstitutionMember])
def lesson_competency(request, lesson_id):
    """GET /api/v1/lessons/<id>/competency/."""
    from django.db.models import Q

    from apps.api.mixins import get_user_institution_ids
    from apps.tutoring.competency import competency_snapshot

    institution_ids = get_user_institution_ids(request.user)
    lesson_qs = Lesson.objects.all()
    if not request.user.is_staff:
        lesson_qs = lesson_qs.filter(
            Q(unit__course__institution_id__in=institution_ids)
            | Q(unit__course__institution__isnull=True),
        )
    lesson = get_object_or_404(lesson_qs, id=lesson_id)
    return Response(competency_snapshot(request.user, lesson))
