"""The data export: raw rows that must rebuild the dashboard, and reveal nobody.

Two obligations pull against each other here.

REPLICATION — a recipient must be able to recompute every chart on the
dashboard from this file alone. So the tests do not check that the export
"looks right"; they recompute the dashboard's own figures from the CSV and
assert the two agree. A report whose numbers cannot be checked is worth less
than no report.

ANONYMISATION — no names, usernames, emails, real ids or free text may appear.
The test plants identifying data and asserts none of it survives. Reviewer
discipline is not a control.
"""
from __future__ import annotations

import csv
import io

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import (
    ExitTicket, ExitTicketAttempt, SessionTurn, TutorSession,
)


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Test School', slug='test-school')


@pytest.fixture
def lesson(school):
    course = Course.objects.create(title='Geography', institution=school)
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    return Lesson.objects.create(unit=unit, title='Understanding Maps',
                                 objective='Read a map', order_index=1,
                                 is_published=True)


@pytest.fixture
def ticket(lesson):
    return ExitTicket.objects.create(lesson=lesson, passing_score=8,
                                     questions_per_attempt=10)


@pytest.fixture
def admin(db):
    return User.objects.create_user(username='admin', password='x',
                                    is_staff=True, is_superuser=True)


def _seed(school, lesson, ticket, n=6, scores=(9, 7, 5, 10, 3, 8)):
    """n students, one session each, one practice attempt each."""
    for i in range(n):
        u = User.objects.create_user(
            username=f'pupil{i}', first_name=f'Distinctive{i}',
            last_name='Surname', email=f'pupil{i}@school.test')
        Membership.objects.create(user=u, institution=school,
                                  role='student', is_active=True)
        sess = TutorSession.objects.create(student=u, lesson=lesson,
                                           institution=school)
        SessionTurn.objects.create(session=sess, role='student',
                                   content='my mother works at the harbour')
        ExitTicketAttempt.objects.create(
            exit_ticket=ticket, student=u, session=sess, purpose='practice',
            score=scores[i % len(scores)], passed=scores[i % len(scores)] >= 8,
            completed_at=timezone.now(),
        )


def _sections(body):
    """Split the CSV into {section_name: [row, ...]}."""
    out, current = {}, None
    for row in csv.reader(io.StringIO(body)):
        if not row:
            continue
        cell = row[0]
        if cell.startswith('##'):
            current = cell.strip('# ').strip()
            out[current] = []
        elif cell.startswith('#') or current is None:
            continue
        else:
            out[current].append(row)
    return out


@pytest.mark.django_db
class TestAnonymisation:
    def test_no_names_usernames_emails_or_free_text(
            self, client, admin, school, lesson, ticket):
        _seed(school, lesson, ticket)
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv')).content.decode()

        assert 'Distinctive' not in body
        assert 'Surname' not in body
        assert 'pupil0' not in body
        assert '@school.test' not in body
        assert 'harbour' not in body

    def test_student_keys_are_hashed_not_real_ids(
            self, client, admin, school, lesson, ticket):
        _seed(school, lesson, ticket, n=6)
        real_ids = {str(u.id) for u in User.objects.filter(username__startswith='pupil')}
        client.force_login(admin)
        rows = _sections(
            client.get(reverse('dashboard:aggregate_export_csv')).content.decode()
        )['SESSIONS'][1:]
        keys = {r[1] for r in rows}
        assert all(k.startswith('u_') for k in keys)
        assert not (keys & real_ids)

    def test_the_salt_changes_between_exports(
            self, client, admin, school, lesson, ticket):
        """Two files must not be joinable back into a longitudinal record."""
        _seed(school, lesson, ticket, n=6)
        client.force_login(admin)
        url = reverse('dashboard:aggregate_export_csv')
        a = _sections(client.get(url).content.decode())['SESSIONS'][1:]
        b = _sections(client.get(url).content.decode())['SESSIONS'][1:]
        assert {r[1] for r in a} != {r[1] for r in b}

    def test_the_same_student_is_stable_within_one_file(
            self, client, admin, school, lesson, ticket):
        """...but paired learning gain must still be computable."""
        _seed(school, lesson, ticket, n=6)
        client.force_login(admin)
        s = _sections(client.get(reverse('dashboard:aggregate_export_csv')).content.decode())
        session_keys = {r[1] for r in s['SESSIONS'][1:]}
        attempt_keys = {r[1] for r in s['EXIT_TICKET_ATTEMPTS'][1:]}
        assert attempt_keys and attempt_keys <= session_keys

    def test_dates_are_day_resolution_not_timestamps(
            self, client, admin, school, lesson, ticket):
        """An exact time is close to a fingerprint against a class timetable."""
        _seed(school, lesson, ticket)
        client.force_login(admin)
        rows = _sections(
            client.get(reverse('dashboard:aggregate_export_csv')).content.decode()
        )['SESSIONS'][1:]
        assert all(len(r[5]) == 10 and ':' not in r[5] for r in rows)


@pytest.mark.django_db
class TestReplicatesTheDashboard:
    """Recompute the dashboard's figures from the CSV and compare."""

    def _both(self, client, admin, school, lesson, ticket):
        _seed(school, lesson, ticket, n=6, scores=(9, 7, 5, 10, 3, 8))
        client.force_login(admin)
        csv_body = client.get(reverse('dashboard:aggregate_export_csv')).content.decode()
        page = client.get(reverse('dashboard:home')).content.decode()
        return _sections(csv_body), page

    def test_session_count_matches(self, client, admin, school, lesson, ticket):
        s, _ = self._both(client, admin, school, lesson, ticket)
        assert len(s['SESSIONS']) - 1 == TutorSession.objects.count()

    def test_reach_rate_is_recomputable(self, client, admin, school, lesson, ticket):
        s, _ = self._both(client, admin, school, lesson, ticket)
        rows = s['SESSIONS'][1:]
        reached = sum(1 for r in rows if r[6] == 'yes')
        from apps.dashboard.views import _exit_ticket_stats
        expected = _exit_ticket_stats(TutorSession.objects.all())
        assert reached == expected['sessions_reached']

    def test_mean_and_median_are_recomputable(
            self, client, admin, school, lesson, ticket):
        s, _ = self._both(client, admin, school, lesson, ticket)
        pcts = sorted(int(r[9]) for r in s['EXIT_TICKET_ATTEMPTS'][1:])
        mean = round(sum(pcts) / len(pcts))
        median = pcts[len(pcts) // 2]
        from apps.dashboard.views import _exit_ticket_stats
        expected = _exit_ticket_stats(TutorSession.objects.all())
        assert mean == expected['avg_pct']
        assert median == expected['median_pct']

    def test_the_score_histogram_is_recomputable(
            self, client, admin, school, lesson, ticket):
        s, _ = self._both(client, admin, school, lesson, ticket)
        buckets = [0] * 10
        for r in s['EXIT_TICKET_ATTEMPTS'][1:]:
            buckets[min(int(r[9]) // 10, 9)] += 1
        from apps.dashboard.views import _exit_ticket_stats
        expected = [b['count'] for b in
                    _exit_ticket_stats(TutorSession.objects.all())['distribution']]
        assert buckets == expected

    def test_sessions_over_time_is_recomputable(
            self, client, admin, school, lesson, ticket):
        s, _ = self._both(client, admin, school, lesson, ticket)
        from collections import Counter
        by_day = Counter(r[5] for r in s['SESSIONS'][1:])
        today = timezone.now().date().isoformat()
        assert by_day[today] == TutorSession.objects.count()

    def test_pass_count_is_recomputable(self, client, admin, school, lesson, ticket):
        s, _ = self._both(client, admin, school, lesson, ticket)
        passed = sum(1 for r in s['EXIT_TICKET_ATTEMPTS'][1:] if r[10] == 'yes')
        from apps.dashboard.views import _exit_ticket_stats
        assert passed == _exit_ticket_stats(TutorSession.objects.all())['passed']

    def test_diagnostic_attempts_are_included_for_learning_gain(
            self, client, admin, school, lesson, ticket):
        """Gain pairs a diagnostic against a later practice attempt — the file
        is useless for that if it only carries practice rows."""
        _seed(school, lesson, ticket, n=6)
        student = User.objects.filter(username='pupil0').first()
        ExitTicketAttempt.objects.create(
            exit_ticket=ticket, student=student, purpose='diagnostic',
            score=3, passed=False, completed_at=timezone.now(),
        )
        client.force_login(admin)
        rows = _sections(
            client.get(reverse('dashboard:aggregate_export_csv')).content.decode()
        )['EXIT_TICKET_ATTEMPTS'][1:]
        assert any(r[5] == 'diagnostic' for r in rows)


@pytest.mark.django_db
class TestAccess:
    def test_requires_staff(self, client, school):
        r = client.get(reverse('dashboard:aggregate_export_csv'))
        assert r.status_code in (302, 403)

    def test_downloads_as_a_named_csv(self, client, admin, school, lesson, ticket):
        _seed(school, lesson, ticket)
        client.force_login(admin)
        r = client.get(reverse('dashboard:aggregate_export_csv'))
        assert r['Content-Type'] == 'text/csv'
        assert 'attachment; filename=' in r['Content-Disposition']

    def test_honours_the_requested_window(self, client, admin, school, lesson, ticket):
        _seed(school, lesson, ticket)
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv'),
                          {'start': '2020-01-01', 'end': '2020-01-31'}).content.decode()
        assert '2020-01-01' in body
        assert len(_sections(body).get('SESSIONS', [])) <= 1     # header only
