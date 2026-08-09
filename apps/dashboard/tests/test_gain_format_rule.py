"""The format-alignment rule for learning gain.

A before/after pair only measures learning if both sides asked the same KIND of
question. A 4-option MCQ carries a 25% guessing floor; fill-in-the-blank carries
roughly none. Pairing one against the other measures the change of instrument
and reports it as learning — which is exactly what happened before 2026-08-09,
when the pre-test drew from the whole bank and the exit ticket served MCQ only.

These tests pin the rule so the contamination cannot come back quietly.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson, Unit
from apps.dashboard.views import _format_signature, _progression_stats
from apps.tutoring.models import ExitTicket, ExitTicketAttempt, TutorSession


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Test School', slug='fmt-school')


@pytest.fixture
def ticket(school):
    course = Course.objects.create(title='Geo', institution=school)
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    lesson = Lesson.objects.create(unit=unit, title='Maps', objective='o',
                                   order_index=1, is_published=True)
    return ExitTicket.objects.create(lesson=lesson, passing_score=8,
                                     questions_per_attempt=10)


@pytest.fixture
def student(school):
    u = User.objects.create_user(username='pupil')
    Membership.objects.create(user=u, institution=school, role='student',
                              is_active=True)
    return u


def _answers(formats):
    """An answers payload whose per_question carries the given formats."""
    return {'per_question': [{'question_type': f, 'correct': True} for f in formats]}


def _attempt(ticket, student, purpose, score, days_ago, formats):
    # A session is required: _progression_stats scopes attempts by
    # session__institution, so a session-less attempt is invisible to it.
    session = TutorSession.objects.create(
        student=student, lesson=ticket.lesson,
        institution=ticket.lesson.unit.course.institution,
    )
    return ExitTicketAttempt.objects.create(
        exit_ticket=ticket, student=student, session=session, purpose=purpose,
        score=score, passed=score >= 8,
        answers=_answers(formats) if formats else {},
        completed_at=timezone.now() - timedelta(days=days_ago),
    )


def _gain(school):
    return _progression_stats(school, date.today() - timedelta(days=30),
                              date.today(), weekly=False)['gain']


@pytest.mark.django_db
class TestFormatSignature:
    def test_reads_the_formats_served(self):
        assert _format_signature(_answers(['mcq', 'mcq'])) == frozenset({'mcq'})
        assert _format_signature(_answers(['mcq', 'short_answer'])) == \
            frozenset({'mcq', 'short_answer'})

    def test_unrecorded_formats_are_unverifiable_not_assumed(self):
        """Every diagnostic before 2026-08-09 is in this state, and those are
        precisely the mixed-format ones."""
        assert _format_signature({}) is None
        assert _format_signature({'per_question': []}) is None
        assert _format_signature({'per_question': [{'correct': True}]}) is None
        assert _format_signature(None) is None


@pytest.mark.django_db
class TestPairingRule:
    def test_matching_formats_are_counted(self, school, ticket, student):
        _attempt(ticket, student, 'practice', 4, 10, ['mcq'] * 10)
        _attempt(ticket, student, 'practice', 9, 2, ['mcq'] * 10)
        g = _gain(school)
        assert g['pairs'] == 1
        assert g['excluded_format_mismatch'] == 0

    def test_mismatched_formats_are_excluded_and_counted(
            self, school, ticket, student):
        """The original bug: a mixed-format pre-test against an MCQ exit ticket."""
        _attempt(ticket, student, 'diagnostic', 3, 10,
                 ['mcq', 'fill_in_blank', 'short_answer'])
        _attempt(ticket, student, 'practice', 9, 2, ['mcq'] * 10)
        g = _gain(school)
        assert g['pairs'] == 0
        assert g['excluded_format_mismatch'] == 1

    def test_unverifiable_formats_are_excluded(self, school, ticket, student):
        _attempt(ticket, student, 'diagnostic', 3, 10, None)   # records nothing
        _attempt(ticket, student, 'practice', 9, 2, ['mcq'] * 10)
        g = _gain(school)
        assert g['pairs'] == 0
        assert g['excluded_unverifiable'] == 1

    def test_an_aligned_diagnostic_is_allowed_back_in(
            self, school, ticket, student):
        """The rule is about formats, not about the word 'diagnostic'. Once
        pre-tests are MCQ-only they should count again with no code change."""
        _attempt(ticket, student, 'diagnostic', 3, 10, ['mcq'] * 10)
        _attempt(ticket, student, 'practice', 9, 2, ['mcq'] * 10)
        g = _gain(school)
        assert g['pairs'] == 1
        assert g['from_diagnostic'] == 1

    def test_a_mixed_format_exit_ticket_is_also_caught(
            self, school, ticket, student):
        """The rule is symmetric — the old gate only ever suspected the
        diagnostic side."""
        _attempt(ticket, student, 'practice', 4, 10, ['mcq'] * 10)
        _attempt(ticket, student, 'practice', 9, 2,
                 ['mcq', 'mcq', 'short_numeric'])
        g = _gain(school)
        assert g['pairs'] == 0
        assert g['excluded_format_mismatch'] == 1

    def test_it_falls_back_to_a_valid_pair_when_one_source_is_invalid(
            self, school, ticket, student):
        """A mismatched diagnostic must not block an otherwise-valid
        first-vs-latest comparison."""
        _attempt(ticket, student, 'diagnostic', 2, 20, ['fill_in_blank'] * 10)
        _attempt(ticket, student, 'practice', 4, 10, ['mcq'] * 10)
        _attempt(ticket, student, 'practice', 9, 2, ['mcq'] * 10)
        g = _gain(school)
        assert g['pairs'] == 1
        assert g['from_first_attempt'] == 1
        assert g['excluded_format_mismatch'] == 1
