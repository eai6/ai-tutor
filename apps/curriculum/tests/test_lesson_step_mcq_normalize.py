"""Tests for LessonStep._normalize_mcq_choices and the data migration
0029_normalize_mcq_choices.

The save hook + migration normalize bare-choice MCQ rows to
letter-prefixed form so the downstream pose_question renderer +
``mcq_options_missing`` safety floor stay in lockstep. See
design/tasks/pose-question-two-phase-commit-fixes-plan.md Fix 1.
"""

import pytest

from apps.accounts.models import Institution
from apps.curriculum.models import Course, Lesson, LessonStep, Unit


@pytest.fixture
def lesson(db):
    institution = Institution.objects.create(name="Test Inst")
    course = Course.objects.create(
        title="Test Course", institution=institution,
    )
    unit = Unit.objects.create(course=course, title="U1", order_index=0)
    return Lesson.objects.create(unit=unit, title="L1", order_index=0)


@pytest.mark.django_db
def test_save_hook_synthesizes_letters_on_bare_choices(lesson):
    step = LessonStep.objects.create(
        lesson=lesson,
        order_index=0,
        teacher_script="",
        question="Which describes condensation?",
        answer_type=LessonStep.AnswerType.MULTIPLE_CHOICE,
        choices=["evaporates", "condenses", "precipitates"],
    )
    step.refresh_from_db()
    assert step.choices == [
        "A) evaporates", "B) condenses", "C) precipitates",
    ]


@pytest.mark.django_db
def test_save_hook_idempotent_on_prefixed_choices(lesson):
    step = LessonStep.objects.create(
        lesson=lesson,
        order_index=0,
        teacher_script="",
        question="Pick one.",
        answer_type=LessonStep.AnswerType.MULTIPLE_CHOICE,
        choices=["A) foo", "B. bar", "C: baz"],
    )
    step.refresh_from_db()
    assert step.choices == ["A) foo", "B. bar", "C: baz"]


@pytest.mark.django_db
def test_save_hook_skips_non_mcq(lesson):
    step = LessonStep.objects.create(
        lesson=lesson,
        order_index=0,
        teacher_script="",
        question="Type a word.",
        answer_type=LessonStep.AnswerType.FREE_TEXT,
        choices=["foo", "bar"],
    )
    step.refresh_from_db()
    assert step.choices == ["foo", "bar"]


@pytest.mark.django_db
def test_save_hook_handles_empty_and_non_strings(lesson):
    step = LessonStep.objects.create(
        lesson=lesson,
        order_index=0,
        teacher_script="",
        question="Pick one.",
        answer_type=LessonStep.AnswerType.MULTIPLE_CHOICE,
        choices=["foo", "", 42, "bar"],
    )
    step.refresh_from_db()
    # Empty string and non-string pass through unchanged at their
    # positions; string entries get sequential letters based on their
    # list index (A for foo, D for bar — positional with the original
    # list).
    assert step.choices == ["A) foo", "", 42, "D) bar"]


@pytest.mark.django_db
def test_save_hook_noop_on_empty_choices(lesson):
    step = LessonStep.objects.create(
        lesson=lesson,
        order_index=0,
        teacher_script="",
        question="Pick one.",
        answer_type=LessonStep.AnswerType.MULTIPLE_CHOICE,
        choices=[],
    )
    step.refresh_from_db()
    assert step.choices == []


@pytest.mark.django_db
def test_save_hook_handles_more_than_six_choices(lesson):
    step = LessonStep.objects.create(
        lesson=lesson,
        order_index=0,
        teacher_script="",
        question="Pick one.",
        answer_type=LessonStep.AnswerType.MULTIPLE_CHOICE,
        choices=["a", "b", "c", "d", "e", "f", "g"],
    )
    step.refresh_from_db()
    # First six get A-F; overflow falls back to "OptionN".
    assert step.choices == [
        "A) a", "B) b", "C) c", "D) d", "E) e", "F) f", "Option7) g",
    ]
