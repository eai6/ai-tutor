"""The headline figures on the overview page.

The signal row is four tiles' worth of history in three tiles. Two changes are
pinned here:

  * The mean-exit-ticket tile was removed. The figure is still computed —
    attention.py raises it as a triage item when it falls below the floor —
    but it no longer occupies a slot in a row of context.

  * "Students mastering" was all-time and now moves with the period picker,
    counted over the same sessions as the tiles beside it so the row shares one
    denominator.
"""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.dashboard.views import _exit_ticket_stats
from ai_tutor.apps.tutoring.models import TutorSession

User = get_user_model()
BACKEND = 'django.contrib.auth.backends.ModelBackend'


class TestExitTicketStats:

    def test_no_sessions_is_zero_not_a_crash(self, db):
        stats = _exit_ticket_stats(TutorSession.objects.none())
        assert stats['mastering_pct'] == 0
        assert stats['students_attempted'] == 0
        assert stats['students_mastered'] == 0

    def test_mastery_is_reported_over_the_same_session_set(self, db):
        """Same queryset in, so the tile cannot describe a different population
        from the reach-rate beside it."""
        stats = _exit_ticket_stats(TutorSession.objects.none())
        for key in ('sessions_started', 'reach_pct', 'attempts',
                    'students_attempted', 'students_mastered', 'mastering_pct'):
            assert key in stats


class TestSignalRow:

    @pytest.fixture
    def admin(self, db):
        inst = Institution.objects.create(name='Alpha', slug='alpha', is_active=True)
        user = User.objects.create_user(
            username='root', email='r@example.com', password='pw', is_staff=True)
        Membership.objects.create(
            user=user, institution=inst, role='staff', is_active=True)
        client = Client()
        client.force_login(user, backend=BACKEND)
        return client

    def test_the_mean_tile_is_gone(self, admin):
        body = admin.get(reverse('dashboard:home')).content.decode()
        assert 'Mean exit ticket' not in body

    def test_students_mastering_still_renders(self, admin):
        body = admin.get(reverse('dashboard:home')).content.decode()
        assert 'Students mastering' in body

    def test_its_hint_no_longer_claims_to_be_all_time(self, admin):
        """The old copy told the reader the figure ignores the period picker.
        It no longer does, and a stale explanation is worse than none."""
        body = admin.get(reverse('dashboard:home')).content.decode()
        assert 'this one is all-time' not in body

    def test_the_page_renders_for_every_period(self, admin):
        """The tile is windowed now, so it runs against each preset."""
        for preset in ('7d', '14d', '30d', '90d'):
            r = admin.get(reverse('dashboard:home'), {'period': preset})
            assert r.status_code == 200, preset
