"""Session bootstrap for the terminal tutor client.

Kept out of the management command so it is importable and testable without
spawning a subprocess — the command itself stays a thin arg-parse + I/O loop.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tutoring.models import TutorSession

logger = logging.getLogger(__name__)

# The eval fixture identities, reused deliberately rather than inventing a
# second synthetic student. They are already institution-scoped correctly and
# keep CLI traffic out of real student records. Same constants as
# evals/runner.py:61 — imported there rather than redefined so the two cannot
# drift apart.
DEFAULT_INSTITUTION_PK = 999001
DEFAULT_STUDENT_PK = 999001

# Preferred default lesson: a 5-step S3 geography lesson (Mount Fleuri course).
# Geography rather than maths on purpose — the maths paths get exercised heavily
# by the eval harness and by the math-specific rules in the family prompts, so a
# non-maths lesson surfaces the general tutoring voice, free-text grading and
# figure handling that the maths ladder can hide.
#
# Falls back to the first lesson with steps when this id is absent, so the
# default still works against a database loaded from different fixtures.
#
# The resolved lesson is ALWAYS announced by the caller. A default is fine; a
# *silent* default is not — that is how you end up unsure which lesson a
# behaviour was observed on.
PREFERRED_LESSON_ID = 1463


class BootstrapError(RuntimeError):
    """Raised when a session cannot be built — message is user-facing."""


def list_lessons(limit: int = 40) -> list[tuple[int, str, str]]:
    """(id, course_title, lesson_title) for lessons that have steps.

    Lessons without steps cannot be tutored — the engine has nothing to walk —
    so they are filtered out rather than offered and then failing at turn 1.
    """
    from django.db.models import Count
    from apps.curriculum.models import Lesson

    rows = (
        Lesson.objects
        .annotate(n_steps=Count('steps'))
        .filter(n_steps__gt=0)
        .select_related('unit__course')
        .order_by('unit__course__title', 'order_index')[:limit]
    )
    out = []
    for lesson in rows:
        course = getattr(getattr(lesson.unit, 'course', None), 'title', '') or '—'
        out.append((lesson.pk, course, lesson.title or ''))
    return out


def resolve_lesson_id(explicit: int | None = None) -> tuple[int, bool]:
    """Return ``(lesson_id, was_defaulted)``.

    An explicit id is returned untouched — validation belongs to
    ``bootstrap_session`` so the error message is the same either way.
    """
    from django.db.models import Count
    from apps.curriculum.models import Lesson

    if explicit is not None:
        return explicit, False

    with_steps = Lesson.objects.annotate(n=Count('steps')).filter(n__gt=0)
    if with_steps.filter(pk=PREFERRED_LESSON_ID).exists():
        return PREFERRED_LESSON_ID, True

    first = with_steps.order_by('pk').values_list('pk', flat=True).first()
    if first is None:
        raise BootstrapError(
            "No lesson in this database has any steps, so there is nothing to "
            "tutor. Load the eval fixtures:\n"
            "  python manage.py loaddata evals/fixtures/institution.json "
            "evals/fixtures/lessons.json"
        )
    return int(first), True


def bootstrap_session(
    lesson_id: int,
    *,
    student_username: str | None = None,
) -> 'TutorSession':
    """Create a fresh TutorSession for a CLI chat.

    Marked ``is_synthetic=True`` with a ``cli_session`` marker in engine_state.
    Without that, every development chat would land in session analytics and any
    future benchmark draw alongside real student sessions. The eval runner marks
    its sessions the same way (evals/runner.py:320).

    Always creates a new session — resuming introduces state-reconciliation
    questions that are not the point of a development tool.
    """
    from django.contrib.auth.models import User
    from apps.accounts.models import Institution
    from apps.curriculum.models import Lesson
    from apps.tutoring.models import TutorSession

    try:
        lesson = Lesson.objects.select_related('unit__course').get(pk=lesson_id)
    except Lesson.DoesNotExist:
        raise BootstrapError(
            f"Lesson {lesson_id} not found. Run with --list-lessons to see "
            f"what is available in this database."
        )

    if not lesson.steps.exists():
        raise BootstrapError(
            f"Lesson {lesson_id} ({lesson.title!r}) has no steps, so there is "
            f"nothing for the tutor to walk through. Pick another with "
            f"--list-lessons."
        )

    if student_username:
        try:
            student = User.objects.get(username=student_username)
        except User.DoesNotExist:
            raise BootstrapError(f"No user named {student_username!r}.")
    else:
        try:
            student = User.objects.get(pk=DEFAULT_STUDENT_PK)
        except User.DoesNotExist:
            raise BootstrapError(
                f"Default CLI student (pk={DEFAULT_STUDENT_PK}) not found. Load "
                f"the eval fixtures first:\n"
                f"  python manage.py loaddata evals/fixtures/institution.json "
                f"evals/fixtures/lessons.json\n"
                f"or pass --student <username>."
            )

    # Prefer the lesson's own institution so multi-tenancy scoping matches what
    # a real student on this lesson would get; fall back to the eval fixture
    # institution. See CLAUDE.md on institution scoping.
    institution = getattr(lesson, 'institution', None)
    if institution is None:
        institution = Institution.objects.filter(pk=DEFAULT_INSTITUTION_PK).first()
    if institution is None:
        raise BootstrapError(
            "No institution available for the session. Load the eval fixtures."
        )

    session = TutorSession.objects.create(
        student=student,
        lesson=lesson,
        institution=institution,
        status=TutorSession.Status.ACTIVE,
        is_synthetic=True,
        engine_state={'cli_session': True},
    )
    logger.info(
        "[tutor_chat] session=%s lesson=%s student=%s institution=%s",
        session.pk, lesson.pk, student.pk, institution.pk,
    )
    return session


def last_tutor_turn(session: 'TutorSession'):
    """The most recent persisted tutor turn, or None.

    ``respond_for_view`` projects the browser's JSON shape and does not carry
    tool calls or judge output, so the diagnostics have to be read back off the
    persisted turn. Same approach as evals/runner.py:355.
    """
    from apps.tutoring.models import SessionTurn

    return (
        SessionTurn.objects
        .filter(session=session, role='tutor')
        .order_by('-id')
        .first()
    )
