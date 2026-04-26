"""Offline pack endpoint.

GET /api/v1/lessons/<lesson_id>/offline-pack/

Returns a self-contained JSON bundle the React Native app can save
locally and use to run a tutor session without network access. Pack
contents:

  - lesson + steps (full LessonStep payload)
  - exit_ticket + questions
  - student profile snapshot (grade, prior mastery)
  - simple state-machine policy describing flow
  - media manifest (URLs the client should pre-cache)

A LessonPackVersion row is created the first time a pack is generated
so we can track which version a session was using when it goes
offline. Subsequent calls reuse the latest version unless ?refresh=1
is passed.

See memory/mobile_rn_plan.md and memory/offline_mobile_architecture.md.
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.mixins import get_user_institution_ids
from apps.api.permissions import IsInstitutionMember
from apps.api.serializers.curriculum import (
    LessonSerializer, LessonStepSerializer,
)
from apps.api.serializers.tutoring import (
    ExitTicketSerializer, StudentLessonProgressSerializer,
)
from apps.curriculum.models import Lesson
from apps.tutoring.models import (
    ExitTicket, LessonPackVersion, StudentLessonProgress,
)


def _build_state_machine_policy(lesson) -> dict:
    """Emit a simple state-machine policy the on-device runner consumes.

    See memory/offline_mobile_architecture.md ("policy-as-data state
    machine"). The mobile runner doesn't reproduce the Python
    ConversationalTutor logic — it walks the steps in order, applies
    deterministic answer evaluation per answer_type, and uses the
    on-device LLM only for the natural-language layer.
    """
    steps = list(lesson.steps.order_by('order_index'))
    return {
        'version': 1,
        'lesson_id': lesson.id,
        'session_states': ['tutoring', 'exit_ticket', 'completed'],
        'initial_state': 'tutoring',
        'steps': [
            {
                'index': i,
                'step_type': s.step_type,
                'phase': s.phase,
                'concept_tag': s.concept_tag,
                'answer_type': s.answer_type,
                'expected_answer': s.expected_answer,
                'max_attempts': s.max_attempts,
                'min_exchanges_before_advance': 1,
            }
            for i, s in enumerate(steps)
        ],
        'advance_rules': {
            'teach': {'min_exchanges': 1, 'auto_advance_after': 3},
            'worked_example': {'min_exchanges': 1, 'auto_advance_after': 3},
            'practice': {'min_exchanges': 1, 'on_correct': 'advance', 'cap': 4},
            'quiz': {'min_exchanges': 1, 'on_correct': 'advance', 'cap': 4},
            'summary': {'min_exchanges': 1, 'auto_advance_after': 1},
        },
        'transition_to_exit_ticket_when': 'all_steps_complete',
        'remediation_safety_valve_exchanges': 15,
    }


def _collect_media_manifest(lesson) -> list:
    """Walk steps + exit ticket and return a flat list of media URLs the
    client should pre-cache before going offline."""
    urls = set()
    for step in lesson.steps.all():
        media = step.media or {}
        for cat in ('images', 'videos', 'audio'):
            for entry in media.get(cat, []) or []:
                url = entry.get('url') if isinstance(entry, dict) else None
                if url:
                    urls.add(url)
    exit_ticket = ExitTicket.objects.filter(lesson=lesson).first()
    if exit_ticket:
        for q in exit_ticket.questions.all():
            ad = q.answer_data or {}
            if isinstance(ad, dict) and ad.get('figure_url'):
                urls.add(ad['figure_url'])
    return sorted(urls)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsInstitutionMember])
def offline_pack(request, lesson_id):
    user = request.user
    institution_ids = get_user_institution_ids(user)
    lesson_qs = Lesson.objects.filter(is_published=True)
    if not user.is_staff:
        lesson_qs = lesson_qs.filter(
            Q(unit__course__institution_id__in=institution_ids)
            | Q(unit__course__institution__isnull=True),
        )
    lesson = get_object_or_404(
        lesson_qs.select_related('unit', 'unit__course'),
        id=lesson_id,
    )

    refresh = request.query_params.get('refresh') in ('1', 'true', 'yes')
    pack = None if refresh else LessonPackVersion.latest_for(lesson)
    if pack is None:
        steps_payload = LessonStepSerializer(
            lesson.steps.order_by('order_index'), many=True,
        ).data
        exit_ticket = ExitTicket.objects.filter(lesson=lesson).first()
        exit_payload = ExitTicketSerializer(exit_ticket).data if exit_ticket else None
        snapshot = {
            'lesson': LessonSerializer(lesson).data,
            'steps': steps_payload,
            'exit_ticket': exit_payload,
        }
        policy = _build_state_machine_policy(lesson)
        manifest = _collect_media_manifest(lesson)
        next_version = (
            LessonPackVersion.objects.filter(lesson=lesson).count() + 1
        )
        pack = LessonPackVersion.objects.create(
            lesson=lesson,
            version=next_version,
            policy_json=policy,
            content_snapshot=snapshot,
            media_manifest=manifest,
            created_by=user if user.is_authenticated else None,
        )

    progress = StudentLessonProgress.objects.filter(
        student=user, lesson=lesson,
    ).first()

    return Response({
        'lesson_id': lesson.id,
        'pack_version': pack.version,
        'created_at': pack.created_at.isoformat(),
        'policy': pack.policy_json,
        'content': pack.content_snapshot,
        'media_manifest': pack.media_manifest,
        'student_progress': (
            StudentLessonProgressSerializer(progress).data if progress else None
        ),
    })
