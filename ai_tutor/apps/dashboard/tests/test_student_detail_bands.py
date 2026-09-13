"""The student page, banded by what a teacher would act on.

It used to carry three stat tiles that counted the same rows three ways, above
a Course Progress panel and a Recent Sessions panel that listed the same
lessons twice in different shapes. These tests hold the replacement to the
claims it makes: one ordering, a denominator that describes the student rather
than the catalogue, and a way to actually read what happened.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import StudentLessonProgress, TutorSession


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
    course = Course.objects.create(title='Geography S3', institution=school)
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    for i in range(4):
        Lesson.objects.create(unit=unit, title=f'Lesson {i}', objective='o',
                              order_index=i, is_published=True)
    return course


@pytest.fixture
def student(db, school):
    user = User.objects.create_user('amara', 'a@example.com', 'pw',
                                    first_name='Amara')
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STUDENT)
    StudentProfile.objects.create(user=user, grade_level='S3')
    return user


def _progress(student, lesson, school, **kw):
    return StudentLessonProgress.objects.create(
        student=student, lesson=lesson, institution=school, **kw)


@pytest.mark.django_db
class TestTheBands:

    def test_an_unfinished_lesson_is_open_and_a_passed_one_is_mastered(
            self, client, teacher, student, course, school):
        lessons = list(course.units.first().lessons.all())
        _progress(student, lessons[0], school, mastery_level='in_progress',
                  best_score=0.55)
        _progress(student, lessons[1], school, mastery_level='mastered',
                  best_score=0.92)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        stuck = [p.lesson_id for p in response.context['stuck_lessons']]
        mastered = [p.lesson_id for p in response.context['mastered_lessons_list']]
        assert stuck == [lessons[0].id]
        assert mastered == [lessons[1].id]

    def test_a_course_with_no_progress_is_named_not_counted(
            self, client, teacher, student, course, school):
        """The old page drew a 0/N bar per untouched course, where N counted
        every authored lesson including drafts — a fact about the catalogue,
        not about this student."""
        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        untouched = [c.title for c in response.context['untouched_courses']]
        assert untouched == ['Geography S3']
        body = response.content.decode()
        assert '0/4' not in body and '0 / 4' not in body

    def test_the_same_lesson_is_not_listed_twice(
            self, client, teacher, student, course, school):
        """Course Progress and Recent Sessions used to be the same rows in two
        shapes. A lesson belongs to exactly one band."""
        lessons = list(course.units.first().lessons.all())
        for lesson in lessons[:3]:
            _progress(student, lesson, school, mastery_level='in_progress',
                      best_score=0.4)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        stuck = {p.lesson_id for p in response.context['stuck_lessons']}
        mastered = {p.lesson_id for p in response.context['mastered_lessons_list']}
        assert not (stuck & mastered)
        assert len(stuck) == 3

    def test_most_recently_worked_comes_first(
            self, client, teacher, student, course, school):
        from django.utils import timezone
        from datetime import timedelta
        lessons = list(course.units.first().lessons.all())
        old = _progress(student, lessons[0], school, mastery_level='in_progress')
        new = _progress(student, lessons[1], school, mastery_level='in_progress')
        StudentLessonProgress.objects.filter(pk=old.pk).update(
            last_attempt_at=timezone.now() - timedelta(days=30))
        StudentLessonProgress.objects.filter(pk=new.pk).update(
            last_attempt_at=timezone.now())

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))
        order = [p.lesson_id for p in response.context['stuck_lessons']]
        assert order[0] == lessons[1].id


@pytest.mark.django_db
class TestTheEvidenceIsReachable:

    def test_an_unfinished_lesson_still_offers_its_transcript(
            self, client, teacher, student, course, school):
        """last_completion_session is only set when a lesson is COMPLETED —
        which a stuck student is not. Those are the transcripts a teacher most
        wants, so the row falls back to the latest session on that lesson."""
        lesson = course.units.first().lessons.first()
        _progress(student, lesson, school, mastery_level='in_progress')
        session = TutorSession.objects.create(student=student, lesson=lesson,
                                              institution=school)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        p = response.context['stuck_lessons'][0]
        assert p.transcript_session_id == session.id
        assert reverse('dashboard:session_chat_history',
                       args=[session.id]) in response.content.decode()

    def test_a_lesson_with_no_session_offers_no_link(
            self, client, teacher, student, course, school):
        lesson = course.units.first().lessons.first()
        _progress(student, lesson, school, mastery_level='in_progress')

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))
        assert response.context['stuck_lessons'][0].transcript_session_id is None


@pytest.mark.django_db
class TestWeakConceptsKeepTheirNumbers:

    def test_the_percentage_survives_to_the_template(
            self, client, teacher, student, course, school, monkeypatch):
        """per_concept_breakdown() works out a percentage per concept and the
        page used to keep only the names — leaving a teacher reading
        "Scale & ratio" with no idea whether that meant 68% or 12%."""
        # The view imports these inside the function, so patch them where
        # they are defined rather than on the views module.
        from ai_tutor.apps.tutoring import competency

        lesson = course.units.first().lessons.first()
        _progress(student, lesson, school, mastery_level='in_progress',
                  best_score=0.5)

        monkeypatch.setattr(competency, 'best_attempt', lambda s, l: object())
        monkeypatch.setattr(competency, 'per_concept_breakdown', lambda a: [
            {'concept': 'Scale and ratio', 'pct': 0.34},
            {'concept': 'Grid references', 'pct': 0.41},
            {'concept': 'Map symbols', 'pct': 0.88},
        ])

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        rows = response.context['stuck_lessons'][0].weak_concept_rows
        assert [r['concept'] for r in rows] == ['Scale and ratio', 'Grid references']
        assert [r['pct'] for r in rows] == [34, 41]
        assert 'Scale and ratio 34%' in response.content.decode()

    def test_a_lesson_with_no_attempt_carries_no_concept_rows(
            self, client, teacher, student, course, school):
        lesson = course.units.first().lessons.first()
        _progress(student, lesson, school, mastery_level='in_progress')

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))
        assert response.context['stuck_lessons'][0].weak_concept_rows == []
