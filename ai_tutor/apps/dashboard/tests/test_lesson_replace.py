"""Replacing a course's lessons from a corrected curriculum document.

The re-parse has always been additive, and for a good reason: it once called
``course.units.all().delete()``, and because TutorSession.lesson,
StudentLessonProgress.lesson and ExitTicket.lesson all CASCADE off Lesson, that
wiped a pilot's whole competency history.

So replace does not delete. A lesson the new document no longer lists is
PARKED — unpublished, filed away from the course's lesson table — and its row
stays whole so every transcript and mastery record still resolves.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.dashboard.views import retire_lessons_not_in
from ai_tutor.apps.tutoring.models import StudentLessonProgress, TutorSession


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie-rep')


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
    unit = Unit.objects.create(course=course, title='Maps', order_index=0)
    for i, title in enumerate(['Grid references', 'Map scale', 'Contours']):
        Lesson.objects.create(unit=unit, title=title, objective='o',
                              order_index=i, is_published=True)
    return course


def _structure(*titles):
    return {'units': [{'title': 'Maps',
                       'lessons': [{'title': t} for t in titles]}]}


@pytest.mark.django_db
class TestReplaceParksWhatTheDocumentDropped:

    def test_a_dropped_lesson_is_retired_not_deleted(self, course):
        retired = retire_lessons_not_in(course, _structure('Grid references'))

        assert retired == 2
        assert Lesson.objects.filter(unit__course=course).count() == 3
        kept = Lesson.objects.get(title='Grid references')
        assert kept.retired_at is None and kept.is_published is True
        for title in ('Map scale', 'Contours'):
            gone = Lesson.objects.get(title=title)
            assert gone.retired_at is not None
            assert gone.is_published is False, 'a parked lesson must not be startable'

    def test_the_students_record_survives(self, course, school):
        """The whole reason this is a park and not a delete."""
        student = User.objects.create_user('amara', 'a@example.com', 'pw')
        Membership.objects.create(user=student, institution=school,
                                  role=Membership.Role.STUDENT)
        StudentProfile.objects.create(user=student, grade_level='S3')
        dropped = Lesson.objects.get(title='Contours')
        session = TutorSession.objects.create(student=student, lesson=dropped,
                                              institution=school)
        StudentLessonProgress.objects.create(student=student, lesson=dropped,
                                             institution=school,
                                             mastery_level='mastered',
                                             best_score=0.9)

        retire_lessons_not_in(course, _structure('Grid references'))

        assert TutorSession.objects.filter(pk=session.pk).exists()
        assert StudentLessonProgress.objects.filter(
            student=student, lesson=dropped, mastery_level='mastered').exists()

    def test_an_empty_parse_retires_nothing(self, course):
        """A document that yielded no lessons is a failed parse, not an
        instruction to empty the course."""
        assert retire_lessons_not_in(course, {'units': []}) == 0
        assert retire_lessons_not_in(course, None) == 0
        assert Lesson.objects.filter(unit__course=course,
                                     retired_at__isnull=False).count() == 0

    def test_matching_ignores_case_and_spacing(self, course):
        """Same key the merge itself uses — a title that round-tripped through
        a parser with different whitespace is still the same lesson."""
        retire_lessons_not_in(course, _structure('  grid   REFERENCES ',
                                                 'Map scale', 'Contours'))
        assert Lesson.objects.filter(unit__course=course,
                                     retired_at__isnull=False).count() == 0

    def test_running_it_twice_is_stable(self, course):
        retire_lessons_not_in(course, _structure('Grid references'))
        again = retire_lessons_not_in(course, _structure('Grid references'))
        assert again == 0, 'already-parked lessons are not re-parked'


@pytest.mark.django_db
class TestTheCoursePageFilesThemAway:

    def test_a_parked_lesson_leaves_the_lesson_table(self, client, teacher,
                                                     course):
        retire_lessons_not_in(course, _structure('Grid references'))

        client.force_login(teacher)
        response = client.get(reverse('dashboard:course_detail', args=[course.id]))
        body = response.content.decode()

        assert 'Grid references' in body
        assert 'Map scale' not in body
        assert response.context['retired_count'] == 2

    def test_nothing_parked_is_not_mentioned(self, client, teacher, course):
        client.force_login(teacher)
        response = client.get(reverse('dashboard:course_detail', args=[course.id]))
        assert response.context['retired_count'] == 0


@pytest.mark.django_db
class TestTheFormOffersBothModes:

    def test_add_is_the_default(self, client, teacher, course):
        client.force_login(teacher)
        body = client.get(reverse('dashboard:course_detail',
                                  args=[course.id])).content.decode()
        assert 'name="reparse_mode_choice" value="add" checked' in body
        assert 'name="reparse_mode_choice" value="replace"' in body

    def test_both_forms_carry_the_choice(self, client, teacher, course):
        """The HTML form= attribute points at one form; the linked-document
        path and the upload path both need it."""
        client.force_login(teacher)
        body = client.get(reverse('dashboard:course_detail',
                                  args=[course.id])).content.decode()
        assert body.count('type="hidden" name="reparse_mode" value="add"') == 2
