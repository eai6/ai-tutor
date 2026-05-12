"""Tests for the Phase-2.2.5 full-tracking sampling filter.

Pins:
  - candidate_tutor_turns(require_full_tracking=True) excludes turns
    that are missing either judge_outputs OR judge_history_turns key.
  - require_full_tracking=False restores the legacy behaviour.
  - The dashboard sampling POST view creates BenchmarkItem rows with
    created_by set, redirects, and flashes a status message.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Institution
from apps.benchmark.models import BenchmarkItem
from apps.benchmark.sampling import candidate_tutor_turns
from apps.curriculum.models import Course, Lesson, LessonStep, Unit
from apps.tutoring.models import SessionTurn, TutorSession


def _build_session_with_turns(*, n_turns: int, judge_outputs_on,
                              history_key_on, subject_type='mathematics',
                              user=None, institution=None) -> TutorSession:
    """Construct a TutorSession with N tutor turns, each carrying or
    omitting the post-2.2.5 instrumentation per the flags."""
    institution = institution or Institution.objects.create(
        name=f"Inst-{timezone.now().timestamp()}",
        slug=f"inst-{int(timezone.now().timestamp() * 1000)}",
    )
    user = user or User.objects.create_user(
        username=f'stud-{timezone.now().timestamp()}', password='x',
    )
    course = Course.objects.create(
        institution=institution, title='Angles', subject_type=subject_type,
    )
    unit = Unit.objects.create(course=course, title='U1', order_index=1)
    lesson = Lesson.objects.create(
        unit=unit, title='L1', objective='find x', order_index=1,
    )
    session = TutorSession.objects.create(
        student=user, lesson=lesson, institution=institution,
    )
    for i in range(n_turns):
        # Each tutor turn has a preceding student turn so the snapshot
        # builder has something to anchor on (not required for the
        # filter test but keeps the fixture realistic).
        SessionTurn.objects.create(
            session=session, role='student', content=f'student {i}',
        )
        metadata = {'is_correct': True}
        if history_key_on:
            metadata['judge_history_turns'] = 4 if i > 0 else 0
        judge_outputs = (
            {'rule': {'violations': []}, 'coherence': {'violations': []}}
            if judge_outputs_on
            else {}
        )
        SessionTurn.objects.create(
            session=session, role='tutor', content=f'tutor reply {i}',
            metadata=metadata, judge_outputs=judge_outputs,
        )
    return session


class CandidateTurnsFullTrackingTest(TestCase):
    def test_filter_excludes_missing_judge_outputs(self):
        # Two sessions:
        #   A — full tracking (should be sampled)
        #   B — judge_outputs={} (legacy, should be excluded)
        _build_session_with_turns(n_turns=2, judge_outputs_on=True,
                                  history_key_on=True)
        _build_session_with_turns(n_turns=2, judge_outputs_on=False,
                                  history_key_on=True)
        eligible = list(candidate_tutor_turns(require_full_tracking=True))
        # Only the 2 turns from session A qualify
        self.assertEqual(len(eligible), 2)
        for turn in eligible:
            self.assertTrue(turn.judge_outputs)
            self.assertIn('judge_history_turns', turn.metadata)

    def test_filter_excludes_missing_history_key(self):
        # judge_outputs populated but metadata missing the history key.
        _build_session_with_turns(n_turns=2, judge_outputs_on=True,
                                  history_key_on=False)
        eligible = list(candidate_tutor_turns(require_full_tracking=True))
        self.assertEqual(len(eligible), 0)

    def test_include_legacy_picks_up_old_turns(self):
        # Two legacy sessions (no judge_outputs, no history key).
        _build_session_with_turns(n_turns=2, judge_outputs_on=False,
                                  history_key_on=False)
        # With require_full_tracking=True: empty
        self.assertEqual(
            candidate_tutor_turns(require_full_tracking=True).count(), 0,
        )
        # With require_full_tracking=False: returns the legacy turns
        self.assertEqual(
            candidate_tutor_turns(require_full_tracking=False).count(), 2,
        )


class SamplingViewTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin-test', email='a@b.c', password='x',
        )
        self.client.force_login(self.admin)
        # 3 eligible tutor turns
        _build_session_with_turns(n_turns=3, judge_outputs_on=True,
                                  history_key_on=True)

    def test_post_creates_items(self):
        url = reverse('dashboard:benchmark:sample_create')
        response = self.client.post(url, {'count': '3'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url, reverse('dashboard:benchmark:list'),
        )
        items = list(BenchmarkItem.objects.all())
        self.assertEqual(len(items), 3)
        # created_by should be the requesting super-admin
        for it in items:
            self.assertEqual(it.created_by_id, self.admin.id)

    def test_post_caps_count(self):
        url = reverse('dashboard:benchmark:sample_create')
        # Request 999, but only 3 eligible exist — should sample all 3.
        self.client.post(url, {'count': '999'})
        self.assertEqual(BenchmarkItem.objects.count(), 3)

    def test_post_idempotent_against_already_sampled(self):
        url = reverse('dashboard:benchmark:sample_create')
        self.client.post(url, {'count': '3'})
        first = BenchmarkItem.objects.count()
        # Second call: pool is empty after the first sampled all 3.
        self.client.post(url, {'count': '3'})
        self.assertEqual(BenchmarkItem.objects.count(), first)

    def test_post_invalid_count_defaults_to_10(self):
        url = reverse('dashboard:benchmark:sample_create')
        # 'abc' → fallback to 10, then clamped by eligible pool (3).
        self.client.post(url, {'count': 'abc'})
        self.assertEqual(BenchmarkItem.objects.count(), 3)

    def test_post_requires_login(self):
        self.client.logout()
        url = reverse('dashboard:benchmark:sample_create')
        response = self.client.post(url, {'count': '3'})
        # staff_member_required redirects to admin login, not 403.
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login', response.url)

    def test_get_not_allowed(self):
        url = reverse('dashboard:benchmark:sample_create')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)
