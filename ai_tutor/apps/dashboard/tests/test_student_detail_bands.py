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

from datetime import timedelta

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

    def test_an_untouched_course_shows_its_denominator(
            self, client, teacher, student, course, school):
        """The denominator is the point of the course tabs — "0/4" says this
        student has four lessons waiting. What the old page got wrong was not
        showing a fraction, it was counting DRAFTS in it (next test)."""
        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        tab = next(t for t in response.context['course_tabs']
                   if t['course'].id == course.id)
        assert (tab['mastered_count'], tab['total']) == (0, 4)
        assert tab['not_started'] == 4
        assert tab['last_worked_at'] is None
        assert '0/4' in response.content.decode()

    def test_drafts_are_not_in_the_denominator(
            self, client, teacher, student, course, school):
        """A student cannot open a draft, so a draft cannot be part of what
        they have left. Counting them made the fraction describe the authoring
        backlog — one local course carries 174 authored lessons and 0
        published, which would have read 0/174 for every student in it."""
        unit = course.units.first()
        for i in range(6):
            Lesson.objects.create(unit=unit, title=f'Draft {i}', objective='o',
                                  order_index=10 + i, is_published=False)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        tab = next(t for t in response.context['course_tabs']
                   if t['course'].id == course.id)
        assert tab['total'] == 4, 'the six drafts must not be counted'

    def test_a_course_with_nothing_published_gets_no_tab(
            self, client, teacher, student, school):
        """0/0 is not a fraction a teacher can act on, and a course with no
        published lesson is one the student cannot enter at all."""
        empty = Course.objects.create(title='Unpublished S3', institution=school)
        unit = Unit.objects.create(course=empty, title='U', order_index=0)
        Lesson.objects.create(unit=unit, title='Draft', objective='o',
                              order_index=0, is_published=False)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        titles = [t['course'].title for t in response.context['course_tabs']]
        assert 'Unpublished S3' not in titles

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


@pytest.mark.django_db
class TestTheDictAnswersFormatDoesNotCrashThePage:
    """The 500 on /dashboard/students/<id>/, held at the page rather than the
    helper.

    ``ExitTicketAttempt.answers`` comes in two shapes: the retired
    conversational_tutor wrote a list of per-question dicts, and simple_tutor
    has written ``{'per_question': [...], 'eo_competency': {...}}`` since
    2026-05-26. Iterating the dict yields its string KEYS, so
    ``per_concept_breakdown``'s ``a.get("concept_tag")`` raised
    ``AttributeError: 'str' object has no attribute 'get'`` and took the whole
    page down for any student whose latest attempt was written by the live
    engine — which, by now, is most of them.

    ``competency.answer_rows`` normalises both. This test exists because the
    fix was made once on ``main`` (ec24e9d) and never reached this branch, so
    the deployment kept crashing on a bug that was already solved.
    """

    def _attempt(self, student, lesson, school, answers):
        from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketAttempt
        from django.utils import timezone
        ticket = ExitTicket.objects.create(lesson=lesson, passing_score=8,
                                           questions_per_attempt=10)
        return ExitTicketAttempt.objects.create(
            student=student, exit_ticket=ticket, answers=answers,
            score=2, passed=False, completed_at=timezone.now())

    SIMPLE_TUTOR_DICT = {
        'per_question': [
            {'concept_tag': 'Scale and ratio', 'correct': False},
            {'concept_tag': 'Scale and ratio', 'correct': False},
            {'concept_tag': 'Map symbols', 'correct': True},
        ],
        'eo_competency': {'EO1': {'correct': 1, 'total': 3}},
    }

    def test_a_simple_tutor_attempt_renders(self, client, teacher, student,
                                            course, school):
        lesson = course.units.first().lessons.first()
        _progress(student, lesson, school, mastery_level='in_progress',
                  best_score=0.2)
        self._attempt(student, lesson, school, self.SIMPLE_TUTOR_DICT)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail',
                                      args=[student.id]))

        assert response.status_code == 200
        rows = response.context['stuck_lessons'][0].weak_concept_rows
        assert [r['concept'] for r in rows] == ['Scale and ratio']
        assert rows[0]['pct'] == 0

    def test_a_legacy_list_attempt_still_renders(self, client, teacher, student,
                                                 course, school):
        lesson = course.units.first().lessons.first()
        _progress(student, lesson, school, mastery_level='in_progress',
                  best_score=0.2)
        self._attempt(student, lesson, school,
                      self.SIMPLE_TUTOR_DICT['per_question'])

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail',
                                      args=[student.id]))
        assert response.status_code == 200
        rows = response.context['stuck_lessons'][0].weak_concept_rows
        assert [r['concept'] for r in rows] == ['Scale and ratio']

    def test_a_malformed_attempt_is_survivable(self, client, teacher, student,
                                               course, school):
        """Whatever else lands in that column, the page is not the place to
        find out about it."""
        lesson = course.units.first().lessons.first()
        _progress(student, lesson, school, mastery_level='in_progress')
        self._attempt(student, lesson, school, {'per_question': 'not a list'})

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail',
                                      args=[student.id]))
        assert response.status_code == 200
        assert response.context['stuck_lessons'][0].weak_concept_rows == []


@pytest.mark.django_db
class TestMasteryIsScopedToACourse:
    """"Mastered — 10 lessons" is a number with no denominator.

    On the student that prompted this, all ten belonged to one course out of
    five, and that course has twenty-five published lessons. Ten of twenty-five
    in Perseverence Geography is a different fact from "ten lessons", and it is
    the one a teacher is actually asking for — so the page is one tab per
    course, each carrying its own fraction.
    """

    def _second_course(self, school):
        course = Course.objects.create(title='Maths S3', institution=school)
        unit = Unit.objects.create(course=course, title='Numbers', order_index=1)
        for i in range(10):
            Lesson.objects.create(unit=unit, title=f'Maths {i}', objective='o',
                                  order_index=i, is_published=True)
        return course

    def test_each_course_counts_only_its_own_lessons(
            self, client, teacher, student, course, school):
        maths = self._second_course(school)
        geo_lessons = list(course.units.first().lessons.all())
        for lesson in geo_lessons[:3]:
            _progress(student, lesson, school, mastery_level='mastered',
                      best_score=0.9)
        _progress(student, maths.units.first().lessons.first(), school,
                  mastery_level='mastered', best_score=0.8)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        tabs = {t['course'].title: t for t in response.context['course_tabs']}
        assert (tabs['Geography S3']['mastered_count'],
                tabs['Geography S3']['total']) == (3, 4)
        assert (tabs['Maths S3']['mastered_count'],
                tabs['Maths S3']['total']) == (1, 10)

    def test_the_three_numbers_add_up_to_the_course(
            self, client, teacher, student, course, school):
        lessons = list(course.units.first().lessons.all())
        _progress(student, lessons[0], school, mastery_level='mastered',
                  best_score=0.9)
        _progress(student, lessons[1], school, mastery_level='in_progress')

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        t = next(t for t in response.context['course_tabs']
                 if t['course'].id == course.id)
        assert t['mastered_count'] + t['open_count'] + t['not_started'] == t['total']

    def test_an_unpublished_lesson_the_student_passed_does_not_overflow(
            self, client, teacher, student, course, school):
        """Unpublishing a lesson after a student passed it leaves more progress
        rows than published lessons. A bar past 100%, or "5 of 4 passed", reads
        as a bug in the page rather than a fact about the course."""
        lessons = list(course.units.first().lessons.all())
        for lesson in lessons:
            _progress(student, lesson, school, mastery_level='mastered',
                      best_score=0.9)
        Lesson.objects.filter(pk=lessons[0].pk).update(is_published=False)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        t = next(t for t in response.context['course_tabs']
                 if t['course'].id == course.id)
        assert t['total'] == 3
        assert t['not_started'] == 0
        assert t['mastered_pct'] <= 100
        assert t['mastered_pct'] + t['open_pct'] <= 100

    def test_the_course_being_worked_on_comes_first(
            self, client, teacher, student, course, school):
        """The tab bar opens on whichever course the student last touched, not
        on whichever course sorts first alphabetically."""
        from django.utils import timezone
        maths = self._second_course(school)
        old = _progress(student, course.units.first().lessons.first(), school,
                        mastery_level='mastered', best_score=0.9)
        new = _progress(student, maths.units.first().lessons.first(), school,
                        mastery_level='in_progress')
        StudentLessonProgress.objects.filter(pk=old.pk).update(
            last_attempt_at=timezone.now() - timedelta(days=60))
        StudentLessonProgress.objects.filter(pk=new.pk).update(
            last_attempt_at=timezone.now())

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))
        assert response.context['course_tabs'][0]['course'].title == 'Maths S3'

    def test_untouched_courses_sort_after_the_worked_ones(
            self, client, teacher, student, course, school):
        self._second_course(school)
        _progress(student, course.units.first().lessons.first(), school,
                  mastery_level='in_progress')

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))
        titles = [t['course'].title for t in response.context['course_tabs']]
        assert titles == ['Geography S3', 'Maths S3']

    def test_a_lesson_belongs_to_exactly_one_band_within_its_course(
            self, client, teacher, student, course, school):
        lessons = list(course.units.first().lessons.all())
        _progress(student, lessons[0], school, mastery_level='mastered',
                  best_score=0.9)
        _progress(student, lessons[1], school, mastery_level='in_progress')

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[student.id]))

        t = next(t for t in response.context['course_tabs']
                 if t['course'].id == course.id)
        mastered = {p.lesson_id for p in t['mastered']}
        open_rows = {p.lesson_id for p in t['open']}
        assert not (mastered & open_rows)

    def test_a_student_with_no_available_course_gets_a_sentence_not_a_blank(
            self, client, teacher, school):
        user = User.objects.create_user('solo', 's@example.com', 'pw',
                                        first_name='Solo')
        Membership.objects.create(user=user, institution=school,
                                  role=Membership.Role.STUDENT)
        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_detail', args=[user.id]))

        assert response.context['course_tabs'] == []
        assert 'No course with published lessons' in response.content.decode()
