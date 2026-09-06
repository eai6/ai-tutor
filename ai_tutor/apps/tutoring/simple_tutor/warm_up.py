"""Pick the warm-up question that opens a lesson.

Every lesson starts with a ``LessonStep`` of type ``warm_up`` (order_index 0).
That row is a container: it holds no question, because a LessonStep is shared
curriculum while a warm-up has to come from what *this* student has already
done. This module fills it.

Selection is deterministic and server-side. The tutor LLM never chooses which
prior lesson to revisit — it receives one question in ``<question_pool>`` and
poses it by index, exactly as it does for the lesson's own questions.

Order of preference:

  1. **Prerequisites** the student has mastered, strongest first. This is the
     point of the feature: open the lesson on the thing it builds upon.
  2. **Recency** — the last few lessons they mastered, most recent first, when
     no prerequisite qualifies (or the course has no prerequisite graph).

Returns None when there is nothing sensible to ask — a student's first-ever
lesson, or a prior lesson whose questions are all figure-dependent. The caller
starts the session on step 1 instead, so nobody lands on an empty warm-up.
"""
from __future__ import annotations

import logging
import random
import re
from typing import TYPE_CHECKING

from django.db.models import Q
from ai_tutor.apps.accounts.tenancy import visible_q

if TYPE_CHECKING:
    from ai_tutor.apps.tutoring.models import ExitTicketQuestion, TutorSession

logger = logging.getLogger(__name__)


# How far back "recently" reaches. Small on purpose: a warm-up from six
# lessons ago is a different feature (spaced review) and wants its own
# scheduling, not a wider window here.
WARM_UP_LESSON_WINDOW = 5

# Difficulty preference. A warm-up opens the session, so it should be
# answerable — the point is to reactivate prior knowledge, not to gate entry on
# the hardest item in an old bank. 'hard' is excluded entirely.
_DIFFICULTY_ORDER = ('easy', 'medium')

# Stems that only make sense with the figure they refer to. The warm-up comes
# from a DIFFERENT lesson, so that lesson's figure catalog is not loaded and
# the tutor cannot show it — the student would be asked about a diagram that
# is not on screen. Ported from the v1 engine's prerequisite recap puller.
_FIGURE_REFERENCE_RE = re.compile(
    r'\b(figure|diagram|graph|chart|image|map|table|photograph|picture)\b'
    r'\s*(\d|[a-z]\b|above|below|shown|opposite)',
    re.IGNORECASE,
)


def _references_a_figure(text: str) -> bool:
    return bool(_FIGURE_REFERENCE_RE.search(text or ''))


def _institution_scope(institution_id):
    """Curriculum visible to this institution, including platform-wide content.

    Scoped through the course rather than a row's own institution column so
    that ``institution=None`` lessons — the platform-wide ones — stay visible.
    Same shape as tutoring/views.py::get_student_progress.
    """
    return (
        visible_q(institution_id, 'lesson__unit__course__institution')
    )


def _mastered_lesson_ids(session) -> set:
    """Lesson ids this student has mastered, within their institution's scope."""
    from ai_tutor.apps.tutoring.models import StudentLessonProgress

    return set(
        StudentLessonProgress.objects
        .filter(
            _institution_scope(session.institution_id),
            student=session.student,
            mastery_level=StudentLessonProgress.MasteryLevel.MASTERED,
        )
        .exclude(lesson=session.lesson)
        .values_list('lesson_id', flat=True)
    )


def _prerequisite_lessons(session, mastered: set) -> list:
    """Mastered prerequisites of this lesson, strongest first.

    ``Course.prerequisites_enabled`` is deliberately ignored. That flag decides
    whether prerequisites GATE access to a lesson; it says nothing about
    whether revisiting one is useful, and a teacher who turned off gating did
    not ask us to stop connecting lessons together.
    """
    from ai_tutor.apps.tutoring.skills_models import LessonPrerequisite

    rows = (
        LessonPrerequisite.objects
        .filter(lesson=session.lesson, prerequisite_id__in=mastered)
        .select_related('prerequisite')
        .order_by('-strength', '-is_direct', 'prerequisite_id')
    )
    return [row.prerequisite for row in rows]


def _recent_lessons(session, mastered: set) -> list:
    """The most recently mastered lessons, newest first.

    ``last_attempt_at`` is the recency signal. ``last_session_at`` looks like
    the right field and is not — nothing in the codebase ever writes it.
    """
    from ai_tutor.apps.curriculum.models import Lesson
    from ai_tutor.apps.tutoring.models import StudentLessonProgress

    rows = (
        StudentLessonProgress.objects
        .filter(
            student=session.student,
            lesson_id__in=mastered,
            last_attempt_at__isnull=False,
        )
        .select_related('lesson')
        .order_by('-last_attempt_at')[:WARM_UP_LESSON_WINDOW]
    )
    lessons = [row.lesson for row in rows]
    # Legacy rows: _complete_session() marked lessons mastered without ever
    # setting last_attempt_at, so a student can have mastery with no timestamp.
    # Those are invisible to the query above; fall back to them rather than
    # returning nothing.
    if not lessons:
        lessons = list(Lesson.objects.filter(id__in=mastered).order_by('-id')[:WARM_UP_LESSON_WINDOW])
    return lessons


def _questions_for(lesson, allowed_types: tuple) -> list:
    """Answerable warm-up candidates from a lesson's exit-ticket bank.

    Filtered on ``assessment_type``, never ``is_published`` — lesson banks are
    not gated by that flag (see question_bank.py's note); only summatives are.
    """
    from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketQuestion

    candidates = (
        ExitTicketQuestion.objects
        .filter(
            exit_ticket__lesson=lesson,
            exit_ticket__assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
            question_type__in=allowed_types,
        )
        .order_by('order_index', 'id')
    )
    return [q for q in candidates if not _references_a_figure(q.question_text)]


def _pick(questions: list, rng: random.Random):
    """Prefer an easy item, then medium; never hard."""
    for level in _DIFFICULTY_ORDER:
        tier = [q for q in questions if (q.difficulty or '').strip().lower() == level]
        if tier:
            return rng.choice(tier)
    untagged = [q for q in questions if not (q.difficulty or '').strip()]
    return rng.choice(untagged) if untagged else None


def select_warm_up_question(session: 'TutorSession') -> 'ExitTicketQuestion | None':
    """The warm-up question for this session, or None if there isn't one.

    Deterministic: seeded on ``session.pk``, so every turn of a session resolves
    the same question and a retake — a new session, new pk — gets a different
    one. Same convention as build_question_pool's pool ordering.

    Never raises. A warm-up is an opening flourish; a failure here must not be
    able to stop a lesson.
    """
    try:
        from ai_tutor.apps.tutoring.simple_tutor.tools import _allowed_tutoring_types

        lesson = getattr(session, 'lesson', None)
        if lesson is None:
            return None

        mastered = _mastered_lesson_ids(session)
        if not mastered:
            return None

        rng = random.Random(getattr(session, 'pk', None) or 0)
        allowed_types = _allowed_tutoring_types()

        # Tier 1 then tier 2, in order. Both are lists of candidate lessons;
        # the first one that yields an answerable question wins, so a
        # prerequisite whose bank is entirely figure-based falls through to the
        # next candidate rather than killing the warm-up.
        for lessons in (
            _prerequisite_lessons(session, mastered),
            _recent_lessons(session, mastered),
        ):
            for prior in lessons:
                questions = _questions_for(prior, allowed_types)
                if not questions:
                    continue
                chosen = _pick(questions, rng)
                if chosen is not None:
                    logger.info(
                        "[warm_up] session=%s question=%s from lesson=%s (%s)",
                        session.pk, chosen.pk, prior.pk, prior.title,
                    )
                    return chosen
        return None
    except Exception as exc:                       # noqa: BLE001
        logger.warning("select_warm_up_question failed (session=%s): %s",
                       getattr(session, 'pk', None), exc)
        return None


def source_lesson_for(question) -> 'object | None':
    """The lesson a warm-up question came from — for naming it in the prompt."""
    try:
        return question.exit_ticket.lesson
    except Exception:                              # noqa: BLE001
        return None
