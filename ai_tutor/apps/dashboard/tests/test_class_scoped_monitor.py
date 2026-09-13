"""The live monitor and the session report, scoped to a class.

Both were a bare ``TutorSession.objects.filter(lesson=lesson)``. Two things
were wrong with that, and the second is why it was reported.

**It crossed schools.** No institution filter at all, so on a platform-wide
course a teacher at one school watched every other school's students work the
same lesson. That is the leak CLAUDE.md's multi-tenancy rule exists to prevent.

**It described a group that no longer exists.** A class roster changes —
students are promoted out, new ones arrive — so a report keyed to whoever once
sat the lesson drifts further from the class it is meant to describe every
term. Scoping to the roster also makes an absence visible: a sessions-only
query can never say who did NOT start.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import TutorSession


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie-cs')


@pytest.fixture
def other_school(db):
    return Institution.objects.create(name='Mont Fleuri', slug='mont-fleuri-cs')


@pytest.fixture
def teacher(db, school):
    """A REGULAR teacher, not a superadmin.

    is_staff=True routes through get_staff_context's superadmin branch, which
    defaults to all-schools mode — so a super-admin seeing every school is
    correct, and a test that used one could never catch the leak. The teacher
    who actually opens these pages in a pilot school is this one.
    """
    user = User.objects.create_user('teach', 't@example.com', 'pw')
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STAFF)
    return user


@pytest.fixture
def lesson(db):
    """Platform-wide course — institution=None — which is the shape that made
    the leak reachable."""
    course = Course.objects.create(title='Geography S1-S5', institution=None,
                                   grade_level='S1,S2,S3,S4,S5')
    unit = Unit.objects.create(course=course, title='Maps', order_index=0)
    return Lesson.objects.create(unit=unit, title='Grid references',
                                 objective='Read a grid reference',
                                 order_index=0, is_published=True)


def _student(school, name, grade='S3'):
    user = User.objects.create_user(name, f'{name}@example.com', 'pw',
                                    first_name=name.title(), last_name='Test')
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STUDENT)
    StudentProfile.objects.create(user=user, grade_level=grade)
    return user


def _worked(student, lesson, school, status='completed'):
    return TutorSession.objects.create(student=student, lesson=lesson,
                                       institution=school, status=status)


def _monitor(client, lesson, grade=None):
    url = reverse('dashboard:lesson_monitor', args=[lesson.id])
    return client.get(f'{url}?class={grade}' if grade else url)


def _report(client, lesson, grade=None):
    url = reverse('dashboard:lesson_session_report', args=[lesson.id])
    return client.get(f'{url}?class={grade}' if grade else url)


@pytest.mark.django_db
class TestItDoesNotCrossSchools:

    def test_the_monitor_shows_only_this_school(self, client, teacher, school,
                                                other_school, lesson):
        mine = _student(school, 'amara')
        theirs = _student(other_school, 'jeanluc')
        _worked(mine, lesson, school)
        _worked(theirs, lesson, other_school)

        client.force_login(teacher)
        names = {s['student_name'] for s in _monitor(client, lesson).context['sessions']}
        assert 'Amara Test' in names
        assert 'Jeanluc Test' not in names, 'another school\'s student is visible'

    def test_the_report_counts_only_this_school(self, client, teacher, school,
                                                other_school, lesson):
        _worked(_student(school, 'amara'), lesson, school)
        _worked(_student(other_school, 'jeanluc'), lesson, other_school)

        client.force_login(teacher)
        assert _report(client, lesson).context['total_students'] == 1


@pytest.mark.django_db
class TestTheRosterLeadsAndPastStudentsFollow:

    def test_a_promoted_student_leaves_the_counts(self, client, teacher, school,
                                                  lesson):
        """They worked the lesson in S3 and are in S4 now. The S3 report is
        about S3 as it stands, so they are not in its numbers."""
        amara = _student(school, 'amara', grade='S3')
        marie = _student(school, 'marie', grade='S3')
        _worked(amara, lesson, school)
        _worked(marie, lesson, school)
        marie.student_profile.grade_level = 'S4'
        marie.student_profile.save(update_fields=['grade_level'])

        client.force_login(teacher)
        report = _report(client, lesson, 'S3')

        assert report.context['total_students'] == 1
        assert [s.id for s in report.context['former_students']] == [marie.id]

        monitor = _monitor(client, lesson, 'S3')
        assert [s['session'].student_id for s in monitor.context['sessions']] == [amara.id]
        assert [s['session'].student_id
                for s in monitor.context['former_sessions']] == [marie.id]

    def test_their_work_is_still_reachable(self, client, teacher, school, lesson):
        """Not deleted — a teacher comparing this term with last should still
        find it, one disclosure away."""
        marie = _student(school, 'marie', grade='S4')
        _worked(marie, lesson, school)

        client.force_login(teacher)
        body = _monitor(client, lesson, 'S3').content.decode()
        assert 'no longer in S3' in body
        assert 'Marie' in body

    def test_a_new_arrival_is_counted_from_the_day_they_arrive(
            self, client, teacher, school, lesson):
        """The roster is read live, so nothing has to be recomputed when a
        student moves into the class."""
        jean = _student(school, 'jeanluc', grade='S2')
        _worked(jean, lesson, school)

        client.force_login(teacher)
        assert _report(client, lesson, 'S3').context['total_students'] == 0

        jean.student_profile.grade_level = 'S3'
        jean.student_profile.save(update_fields=['grade_level'])
        assert _report(client, lesson, 'S3').context['total_students'] == 1


@pytest.mark.django_db
class TestAnAbsenceBecomesVisible:
    """The number the page could not produce before it knew a roster."""

    def test_students_who_never_opened_it_are_named(self, client, teacher,
                                                    school, lesson):
        _worked(_student(school, 'amara'), lesson, school)
        kelly = _student(school, 'kelly')
        nadia = _student(school, 'nadia')

        client.force_login(teacher)
        report = _report(client, lesson, 'S3')

        assert {s.id for s in report.context['not_started_students']} == {kelly.id, nadia.id}
        body = report.content.decode()
        assert 'never opened this lesson' in body
        assert 'Kelly' in body and 'Nadia' in body

    def test_the_monitor_names_them_too(self, client, teacher, school, lesson):
        _worked(_student(school, 'amara'), lesson, school)
        kelly = _student(school, 'kelly')

        client.force_login(teacher)
        monitor = _monitor(client, lesson, 'S3')
        assert [s.id for s in monitor.context['not_started']] == [kelly.id]
        assert 'has not opened this lesson' in monitor.content.decode()

    def test_without_a_class_there_is_nobody_to_miss(self, client, teacher,
                                                     school, lesson):
        """No roster, no absence — the school-wide view can only report what
        happened, and saying otherwise would be inventing a denominator."""
        _worked(_student(school, 'amara'), lesson, school)
        _student(school, 'kelly')

        client.force_login(teacher)
        assert _report(client, lesson).context['not_started_students'] == []
        assert _monitor(client, lesson).context['not_started'] == []
        assert _monitor(client, lesson).context['roster_size'] is None


@pytest.mark.django_db
class TestThePicker:

    def test_it_offers_the_classes_that_have_students(self, client, teacher,
                                                      school, lesson):
        _student(school, 'amara', grade='S3')
        _student(school, 'jeanluc', grade='S1')
        _student(school, 'nograde', grade='')

        client.force_login(teacher)
        assert _monitor(client, lesson).context['class_choices'] == ['S1', 'S3']

    def test_the_two_pages_link_to_each_other_within_the_class(
            self, client, teacher, school, lesson):
        """Switching between Monitor and Report must not silently widen the
        scope back to the whole school."""
        _worked(_student(school, 'amara'), lesson, school)
        client.force_login(teacher)

        report_url = reverse('dashboard:lesson_session_report', args=[lesson.id])
        monitor_url = reverse('dashboard:lesson_monitor', args=[lesson.id])
        assert f'{report_url}?class=S3' in _monitor(client, lesson, 'S3').content.decode()
        assert f'{monitor_url}?class=S3' in _report(client, lesson, 'S3').content.decode()

    def test_an_unknown_class_is_an_empty_class_not_an_error(
            self, client, teacher, school, lesson):
        _worked(_student(school, 'amara'), lesson, school)
        client.force_login(teacher)

        response = _report(client, lesson, 'S9')
        assert response.status_code == 200
        assert response.context['total_students'] == 0
        assert response.context['roster_size'] == 0
