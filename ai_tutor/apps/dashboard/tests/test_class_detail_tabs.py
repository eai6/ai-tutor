"""The class page: what the class is doing, who has gone quiet, and the roster.

It used to stack a course catalogue — identical for every class in the grade —
above the roster that is the actual class, and drop the activity card entirely
on a quiet week. These tests hold the replacement to the numbers it now claims.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import (SessionParticipant,
                                           StudentLessonProgress, TutorSession)


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie')


@pytest.fixture
def teacher(db, school):
    user = User.objects.create_user('teach', 't@example.com', 'pw', is_staff=True)
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STAFF)
    return user


@pytest.fixture
def course(db, school):
    course = Course.objects.create(title='Geography S3', institution=school,
                                   grade_level='S3')
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    for i in range(4):
        Lesson.objects.create(unit=unit, title=f'Lesson {i}', objective='o',
                              order_index=i, is_published=True)
    return course


def _student(school, name, grade='S3'):
    user = User.objects.create_user(name, f'{name}@example.com', 'pw',
                                    first_name=name.title())
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STUDENT)
    StudentProfile.objects.create(user=user, grade_level=grade)
    return user


def _get(client, teacher, grade='S3'):
    client.force_login(teacher)
    return client.get(reverse('dashboard:class_detail', args=[grade]))


@pytest.mark.django_db
class TestWhoHasGoneQuiet:
    """The question a teacher opens a class page to answer. The roster used to
    carry no signal at all — you had to click into each student in turn."""

    def test_a_student_who_never_worked_is_listed(self, client, teacher, school,
                                                  course):
        amara = _student(school, 'amara')
        response = _get(client, teacher)

        quiet = [s.id for s in response.context['inactive_students']]
        assert quiet == [amara.id]
        assert response.context['students'][0].last_worked_at is None

    def test_a_student_who_worked_today_is_not_listed(self, client, teacher,
                                                      school, course):
        amara = _student(school, 'amara')
        TutorSession.objects.create(student=amara, institution=school,
                                    lesson=course.units.first().lessons.first())
        response = _get(client, teacher)
        assert response.context['inactive_students'] == []
        assert response.context['active_this_week'] == 1

    def test_a_long_gap_counts_as_quiet(self, client, teacher, school, course):
        amara = _student(school, 'amara')
        s = TutorSession.objects.create(student=amara, institution=school,
                                        lesson=course.units.first().lessons.first())
        TutorSession.objects.filter(pk=s.pk).update(
            started_at=timezone.now() - timedelta(days=40))

        response = _get(client, teacher)
        assert [s.id for s in response.context['inactive_students']] == [amara.id]
        assert response.context['active_this_week'] == 0


@pytest.mark.django_db
class TestGroupSessionsCount:
    """A student who joined a groupmate's session was invisible here, because
    the session belongs to the other student — so a class doing paired work
    looked idle."""

    def test_a_secondary_participant_counts_as_active(self, client, teacher,
                                                      school, course):
        host = _student(school, 'amara')
        joiner = _student(school, 'jeanluc')
        lesson = course.units.first().lessons.first()
        session = TutorSession.objects.create(student=host, lesson=lesson,
                                              institution=school)
        SessionParticipant.objects.create(session=session, student=joiner,
                                          is_active=True)

        response = _get(client, teacher)

        assert response.context['inactive_students'] == [], \
            'the student who joined a groupmate looked idle'
        assert response.context['active_this_week'] == 2

    def test_the_lesson_shows_in_this_week(self, client, teacher, school, course):
        host = _student(school, 'amara')
        joiner = _student(school, 'jeanluc')
        lesson = course.units.first().lessons.first()
        session = TutorSession.objects.create(student=host, lesson=lesson,
                                              institution=school)
        SessionParticipant.objects.create(session=session, student=joiner,
                                          is_active=True)

        response = _get(client, teacher)
        lessons = [r['lesson'].id for r in response.context['recent_activity']]
        assert lessons == [lesson.id]
        # Counted once, not once per participant.
        assert response.context['recent_activity'][0]['session_count'] == 1


@pytest.mark.django_db
class TestTheQuietWeekStillRendersACard:

    def test_the_activity_card_survives_an_empty_week(self, client, teacher,
                                                      school, course):
        """It used to vanish entirely, which reads as a page that failed to
        load rather than a class that had a quiet week."""
        _student(school, 'amara')
        response = _get(client, teacher)
        body = response.content.decode()
        assert response.context['recent_activity'] == []
        assert 'Lessons worked on this week' in body
        assert 'Nothing worked on yet this week.' in body


@pytest.mark.django_db
class TestPromotionIsNoLongerOffered:
    """Promote / demote were removed from the dashboard. The endpoint still
    exists, so this holds the UI to the decision rather than the routing."""

    def test_the_class_page_offers_no_promote_controls(self, client, teacher,
                                                       school, course):
        _student(school, 'amara')
        response = _get(client, teacher)
        body = response.content.decode()
        assert 'promote-form' not in body
        assert 'name="student_ids"' not in body
        assert reverse('dashboard:promote_students') not in body

    def test_the_student_page_offers_no_promote_or_delete(self, client, teacher,
                                                          school, course):
        amara = _student(school, 'amara')
        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[amara.id]))
        body = response.content.decode()
        assert reverse('dashboard:promote_students') not in body
        assert reverse('dashboard:delete_student', args=[amara.id]) not in body
