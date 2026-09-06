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
from ai_tutor.apps.accounts.tenancy import visible_q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ai_tutor.apps.api.mixins import get_user_institution_ids
from ai_tutor.apps.api.permissions import IsInstitutionMember
from ai_tutor.apps.api.serializers.curriculum import (
    LessonSerializer, LessonStepSerializer,
)
from ai_tutor.apps.api.serializers.tutoring import (
    ExitTicketSerializer, StudentLessonProgressSerializer,
)
from ai_tutor.apps.curriculum.models import Lesson
from ai_tutor.apps.tutoring.models import (
    ExitTicket, LessonPackVersion, StudentLessonProgress,
)


def _evaluator_kind_for(step) -> str:
    """Pick the evaluator the mobile runner should use for this step.

    Three modes:
    - 'deterministic': numeric / mcq / true-false / short-text-with-expected.
      Mobile grader handles entirely; no LLM call needed for correctness.
    - 'llm': free-text / explanation answers. On-device LLM judges, with
      mobile-side safety valves (max_attempts, min_exchanges).
    - 'none': step doesn't expect an answer (teach / summary). Just
      advance after the configured exchange count.
    """
    answer_type = (step.answer_type or '').lower()
    if answer_type in ('none', ''):
        return 'none'
    if answer_type in ('multiple_choice', 'true_false', 'short_numeric'):
        return 'deterministic'
    if answer_type == 'free_text':
        return 'deterministic' if (step.expected_answer or '').strip() else 'llm'
    return 'llm'


def _build_baked_system_prompt(lesson) -> str:
    """Slim system prompt for the on-device tutor.

    The full Python TUTOR_SYSTEM_PROMPT_TEMPLATE is ~16 KB / ~4K
    tokens — way too much for a small on-device model's context
    window, and a 0.5–3B model can't reliably follow that much
    instruction anyway. The mobile runner appends per-turn
    `<mobile_response_format>` + wrong-answer reminders + step
    blocks on top of this baked prefix (see prompt-builder.ts).

    Goal: identity + lesson context, ~600 chars / ~150 tokens.
    """
    institution = lesson.unit.course.institution
    institution_name = institution.name if institution else 'our school'
    grade_level = lesson.unit.course.grade_level or 'secondary school'
    return (
        f"You are a tutor for {grade_level} students in Seychelles. "
        f"You teach by asking questions, not by giving answers.\n\n"
        f"RULES — follow EVERY reply:\n"
        f"1. End your reply with exactly ONE question. Never finish with a "
        f"period. Always finish with a question mark.\n"
        f"2. Never give the answer. Give a hint or ask a question instead.\n"
        f"3. If the student is wrong, do not say 'correct', 'exactly', "
        f"'great job', or 'right'. Just say what's missing and ask again.\n"
        f"4. Keep replies short: 1–3 sentences plus the question. Under 60 words.\n\n"
        f"EXAMPLE — what to do:\n"
        f"Student: \"I think volcanoes are hot rocks\"\n"
        f"You: \"Hot rocks is part of it. Where inside the Earth do those hot rocks come from?\"\n\n"
        f"EXAMPLE — what NOT to do:\n"
        f"Student: \"I think volcanoes are hot rocks\"\n"
        f"You: \"That's right! Volcanoes are formed by magma rising from the mantle.\"\n"
        f"(Wrong because you praised + revealed the answer + did not ask a question.)\n\n"
        f"LESSON\n"
        f"Title: {lesson.title}\n"
        f"Goal: {lesson.objective or ''}\n"
        f"Unit: {lesson.unit.title}\n"
        f"Course: {lesson.unit.course.title}"
    )


def _collect_context_chunks(lesson, n: int = 6) -> list:
    """Precomputed RAG snippets shipped with the pack.

    Replaces live ChromaDB queries the server-side prompt builder
    runs. Mobile runner concatenates these into the system prompt
    instead of doing vector search on-device. The query is built
    against the lesson's title + objective + step concept tags, so
    the chunks are biased toward what THIS lesson covers.

    Returns a list of {text, source} dicts, capped at `n`. Empty list
    is fine — the mobile runner just gets a smaller system prompt.
    """
    try:
        from ai_tutor.apps.curriculum.knowledge_base import KnowledgeBase
        institution_id = (
            lesson.unit.course.institution_id if lesson.unit.course.institution_id else None
        )
        kb = KnowledgeBase(institution_id=institution_id)
        result = kb.query_for_tutoring(lesson=lesson, n_results=n)
    except Exception:
        # KB unavailable (no ChromaDB, no vectors indexed for this
        # institution, etc) — ship an empty list rather than failing
        # the whole pack build.
        return []

    chunks: list = []
    for chunk in (result.chunks or [])[:n]:
        text = (chunk.get('text') or chunk.get('content') or '').strip()
        if not text:
            continue
        meta = chunk.get('metadata') or {}
        source_bits = [
            meta.get('source_title') or meta.get('document_title') or '',
            meta.get('section') or '',
        ]
        chunks.append({
            'text': text[:800],
            'source': ' / '.join(b for b in source_bits if b) or 'curriculum',
        })
    return chunks


def _build_state_machine_policy(lesson) -> dict:
    """Emit a self-contained state-machine policy the on-device runner consumes.

    See memory/offline_mobile_architecture.md ("policy-as-data state
    machine"). The mobile runner walks the steps in order, applies
    deterministic answer evaluation per answer_type via the grader,
    falls back to the on-device LLM only for free-text / explanation
    steps, and never calls the server while running offline.
    """
    steps = list(lesson.steps.order_by('order_index'))
    return {
        'version': 2,
        'lesson_id': lesson.id,
        'session_states': ['tutoring', 'exit_ticket', 'completed'],
        'initial_state': 'tutoring',
        'system_prompt_template': _build_baked_system_prompt(lesson),
        'context_chunks': _collect_context_chunks(lesson),
        'steps': [
            {
                'index': i,
                'step_id': s.id,
                'step_type': s.step_type,
                'phase': s.phase,
                'concept_tag': s.concept_tag,
                'answer_type': s.answer_type,
                'evaluator_kind': _evaluator_kind_for(s),
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
            visible_q(institution_ids, 'unit__course__institution'),
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
