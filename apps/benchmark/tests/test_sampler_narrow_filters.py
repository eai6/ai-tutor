"""Tests for the narrow filters added to candidate_tutor_turns:
``lesson_id``, ``since``, ``until``.

The existing subject filter already had coverage in
test_sampling_filter.py — these tests pin the new narrowing knobs that
let an annotator focus on a specific lesson, a specific time window,
or both.
"""
from datetime import datetime, timedelta, timezone

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone as _tz

from apps.accounts.models import Institution
from apps.benchmark.sampling import candidate_tutor_turns
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import SessionTurn, TutorSession


_USER_SEQ = [0]


def _build_session(*, lesson, when=None) -> TutorSession:
    """Make a session + one student/tutor turn pair on the given
    lesson, with the tutor turn's created_at set to `when` (default:
    now). The tutor turn carries full Phase-2.2.5 tracking so the
    require_full_tracking filter doesn't shadow our test."""
    inst = lesson.unit.course.institution or Institution.objects.create(
        name='T', slug=f't-{lesson.id}',
    )
    _USER_SEQ[0] += 1
    user = User.objects.create_user(
        username=f'u-{lesson.id}-{_USER_SEQ[0]}', password='x',
    )
    session = TutorSession.objects.create(
        student=user, lesson=lesson, institution=inst,
    )
    SessionTurn.objects.create(session=session, role='student', content='hi')
    tutor = SessionTurn.objects.create(
        session=session, role='tutor', content='hello',
        metadata={'is_correct': True, 'judge_history_turns': 0},
        judge_outputs={'rule': {'violations': []}},
    )
    if when is not None:
        # auto_now_add overrides our value; force it.
        SessionTurn.objects.filter(pk=tutor.pk).update(created_at=when)
    return session


def _make_lesson(title: str, subject: str = 'mathematics') -> Lesson:
    inst, _ = Institution.objects.get_or_create(
        slug=f'i-{title.lower()}',
        defaults={'name': title},
    )
    course = Course.objects.create(
        institution=inst, title=title, subject_type=subject,
    )
    unit = Unit.objects.create(course=course, title='U1', order_index=1)
    return Lesson.objects.create(
        unit=unit, title='L1', objective='x', order_index=1,
    )


class LessonIdFilterTest(TestCase):
    def test_lesson_id_filter_isolates_one_lesson(self):
        l_a = _make_lesson('A')
        l_b = _make_lesson('B')
        _build_session(lesson=l_a)
        _build_session(lesson=l_a)
        _build_session(lesson=l_b)
        # No filter — all 3 tutor turns
        self.assertEqual(candidate_tutor_turns().count(), 3)
        # lesson_id=A → 2
        self.assertEqual(candidate_tutor_turns(lesson_id=l_a.id).count(), 2)
        # lesson_id=B → 1
        self.assertEqual(candidate_tutor_turns(lesson_id=l_b.id).count(), 1)


class TimeWindowFilterTest(TestCase):
    def test_since_includes_and_until_excludes(self):
        l = _make_lesson('Z')
        anchor = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
        _build_session(lesson=l, when=anchor - timedelta(hours=2))   # T-2h
        _build_session(lesson=l, when=anchor)                         # T
        _build_session(lesson=l, when=anchor + timedelta(hours=2))   # T+2h
        # since = T-1h → excludes T-2h, includes T and T+2h
        n_since = candidate_tutor_turns(since=anchor - timedelta(hours=1)).count()
        self.assertEqual(n_since, 2)
        # until = T+1h (exclusive) → includes T-2h and T, excludes T+2h
        n_until = candidate_tutor_turns(until=anchor + timedelta(hours=1)).count()
        self.assertEqual(n_until, 2)
        # window: T-1h .. T+1h → just the T turn
        n_window = candidate_tutor_turns(
            since=anchor - timedelta(hours=1),
            until=anchor + timedelta(hours=1),
        ).count()
        self.assertEqual(n_window, 1)

    def test_filters_compose_with_lesson_id(self):
        l_a = _make_lesson('A')
        l_b = _make_lesson('B')
        anchor = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
        _build_session(lesson=l_a, when=anchor)
        _build_session(lesson=l_a, when=anchor + timedelta(hours=3))
        _build_session(lesson=l_b, when=anchor + timedelta(hours=3))
        # lesson_id=A AND since=T+1h → only the T+3h A turn
        n = candidate_tutor_turns(
            lesson_id=l_a.id,
            since=anchor + timedelta(hours=1),
        ).count()
        self.assertEqual(n, 1)


class SubjectFilterTest(TestCase):
    """Pin the subject mapping. The earlier `subject='math'` filter
    included an empty-string subject_type catchall that turned the
    filter into a no-op for any course missing an explicit
    classification. Now the math filter requires either
    subject_type='math' OR a title-keyword match on uncategorised
    courses; geography requires humanities OR a title match."""

    def test_math_subject_filter_excludes_unrelated_uncategorised(self):
        # Two uncategorised courses: one has 'math' in the title, one
        # is a pure geography course with empty subject_type.
        math_l = _make_lesson('Algebra Basics', subject='')
        geog_l = _make_lesson('Maps of Africa', subject='')
        _build_session(lesson=math_l)
        _build_session(lesson=geog_l)
        eligible = candidate_tutor_turns()
        self.assertEqual(eligible.count(), 2)
        # Math filter pulls the algebra course (title-keyword fallback)
        # but NOT the maps course.
        n = candidate_tutor_turns(subject='math').count()
        self.assertEqual(n, 1)

    def test_geography_subject_filter_matches_humanities_or_title(self):
        humanities_l = _make_lesson('Intro to Civics', subject='humanities')
        geography_l = _make_lesson('Geography of East Africa', subject='')
        unrelated_l = _make_lesson('Algebra', subject='math')
        _build_session(lesson=humanities_l)
        _build_session(lesson=geography_l)
        _build_session(lesson=unrelated_l)
        n = candidate_tutor_turns(subject='geography').count()
        # humanities (canonical) + title-match (fallback) = 2
        self.assertEqual(n, 2)
