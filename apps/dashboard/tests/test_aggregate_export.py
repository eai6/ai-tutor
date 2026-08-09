"""The aggregate export, for sharing figures outside the platform.

The whole value of this export is what it does NOT contain. If a student name,
username or free-text answer can reach the file, it stops being shareable
without a data-transfer agreement and becomes a disclosure.

So the central test does not check the happy path — it plants identifying data
in the database and asserts none of it appears in the output. Reviewer
discipline is not a control; this is.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import SessionTurn, TutorSession


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
def admin(db):
    user = User.objects.create_user(username='admin', password='x',
                                    is_staff=True, is_superuser=True)
    return user


def _seed_students(school, lesson, n, name_prefix='Pupil'):
    made = []
    for i in range(n):
        u = User.objects.create_user(
            username=f'student{i}', first_name=f'{name_prefix}{i}',
            last_name='Surname', email=f'student{i}@school.test',
        )
        Membership.objects.create(user=u, institution=school,
                                  role='student', is_active=True)
        session = TutorSession.objects.create(
            student=u, lesson=lesson, institution=school)
        SessionTurn.objects.create(
            session=session, role='student',
            content='my mother works at the harbour',      # free-text PII
        )
        made.append(u)
    return made


@pytest.mark.django_db
class TestNoIdentifyingData:
    def test_no_student_names_usernames_or_free_text_appear(
            self, client, admin, school, lesson):
        """The property the whole export rests on."""
        _seed_students(school, lesson, 8, name_prefix='Distinctive')
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv')).content.decode()

        assert 'Distinctive' not in body           # first names
        assert 'Surname' not in body               # last names
        assert 'student0' not in body              # usernames
        assert '@school.test' not in body          # emails
        assert 'harbour' not in body               # free-text answers

    def test_no_student_ids_are_emitted(self, client, admin, school, lesson):
        """An integer id is a re-identification key if the recipient also holds
        a roster."""
        students = _seed_students(school, lesson, 8)
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv')).content.decode()
        header = body.splitlines()[4] if len(body.splitlines()) > 4 else ''
        assert 'student_id' not in header
        assert 'user_id' not in header

    def test_one_row_per_group_not_per_student(self, client, admin, school, lesson):
        _seed_students(school, lesson, 8)
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv')).content.decode()
        data_lines = [l for l in body.splitlines()
                      if l and not l.startswith('#') and 'group' not in l]
        assert len(data_lines) <= 2          # one school (+ possible blank)


@pytest.mark.django_db
class TestSmallGroupSuppression:
    def test_a_group_below_the_threshold_is_omitted(
            self, client, admin, school, lesson):
        """Two students, a mean and a pass count is close to describing an
        individual."""
        _seed_students(school, lesson, 2)
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv')).content.decode()
        assert 'Test School' not in body
        assert 'omitted' in body              # and the reader is told

    def test_a_group_at_the_threshold_is_included(
            self, client, admin, school, lesson):
        _seed_students(school, lesson, 5)
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv')).content.decode()
        assert 'Test School' in body


@pytest.mark.django_db
class TestExportBehaviour:
    def test_requires_staff(self, client, school):
        r = client.get(reverse('dashboard:aggregate_export_csv'))
        assert r.status_code in (302, 403)

    def test_downloads_as_a_named_csv(self, client, admin, school, lesson):
        _seed_students(school, lesson, 6)
        client.force_login(admin)
        r = client.get(reverse('dashboard:aggregate_export_csv'))
        assert r['Content-Type'] == 'text/csv'
        assert 'attachment; filename=' in r['Content-Disposition']

    def test_states_its_window_and_that_rows_are_suppressed(
            self, client, admin, school, lesson):
        """A reader months later must not mistake a suppressed total for a
        real one."""
        _seed_students(school, lesson, 6)
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv'),
                          {'start': '2026-07-01', 'end': '2026-07-31'}).content.decode()
        assert '2026-07-01' in body and '2026-07-31' in body
        assert 'Aggregate only' in body

    def test_group_by_lesson(self, client, admin, school, lesson):
        _seed_students(school, lesson, 6)
        client.force_login(admin)
        body = client.get(reverse('dashboard:aggregate_export_csv'),
                          {'group': 'lesson'}).content.decode()
        assert 'Understanding Maps' in body
        assert 'lesson_id' in body

    def test_a_reversed_range_is_swapped_not_rejected(
            self, client, admin, school, lesson):
        _seed_students(school, lesson, 6)
        client.force_login(admin)
        r = client.get(reverse('dashboard:aggregate_export_csv'),
                       {'start': '2026-07-31', 'end': '2026-07-01'})
        assert r.status_code == 200

    def test_garbage_dates_fall_back(self, client, admin, school, lesson):
        _seed_students(school, lesson, 6)
        client.force_login(admin)
        r = client.get(reverse('dashboard:aggregate_export_csv'),
                       {'start': 'nonsense'})
        assert r.status_code == 200
