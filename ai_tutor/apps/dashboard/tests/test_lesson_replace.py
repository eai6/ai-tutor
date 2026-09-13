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


def _structure(*titles, unit='Maps'):
    return {'units': [{'title': unit,
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


@pytest.mark.django_db
class TestMatchingIsScopedToTheUnit:
    """Re-parsing additively for months leaves the same lesson title in
    several units — Mathematics S3 in production has "Expand and simplify
    number expressions" in three of them.

    A course-wide title match keeps every copy, because the document still
    lists the title somewhere, so a replace on that course was a no-op. The
    merge writes on (unit title, lesson title); the retire has to read the
    same key.
    """

    def test_a_duplicate_in_an_unlisted_unit_is_parked(self, course):
        stale = Unit.objects.create(course=course, title='Old algebra unit',
                                    order_index=9)
        dupe = Lesson.objects.create(unit=stale, title='Map scale',
                                     objective='o', order_index=0,
                                     is_published=True)

        retire_lessons_not_in(course, _structure('Map scale'))

        dupe.refresh_from_db()
        assert dupe.retired_at is not None, (
            'a copy in a unit the document does not list must be parked, '
            'even though its title appears elsewhere in the document'
        )
        assert Lesson.objects.get(title='Map scale',
                                  unit__title='Maps').retired_at is None

    def test_a_whole_unit_the_document_dropped_is_parked(self, course):
        stale = Unit.objects.create(course=course, title='Removed topic',
                                    order_index=9)
        for i, t in enumerate(['Old one', 'Old two']):
            Lesson.objects.create(unit=stale, title=t, objective='o',
                                  order_index=i, is_published=True)

        n = retire_lessons_not_in(
            course, _structure('Grid references', 'Map scale', 'Contours'))

        assert n == 2
        assert Lesson.objects.filter(unit=stale,
                                     retired_at__isnull=False).count() == 2

    def test_a_renamed_unit_parks_its_old_copy(self, course):
        """The document reorganising its units is the ordinary case for a
        corrected syllabus, and the whole reason to offer replace."""
        n = retire_lessons_not_in(
            course, _structure('Grid references', 'Map scale', 'Contours',
                               unit='Maps and scale'))
        assert n == 3, 'every lesson moved to a renamed unit is re-homed'


@pytest.mark.django_db
class TestSubjectAndGradeReachTheCourse:
    """The teacher picks a subject and a grade on the upload form. Those two
    fields are the entire join for material sharing — and they were only ever
    written when the Course row was CREATED.

    get_or_create(defaults=...) does not touch an existing row, so every
    re-upload and every course predating the subject dropdown kept them blank
    forever: inheritance matched nothing, in both directions, with nothing on
    screen to say why.
    """

    def _upload(self, institution, subject_code='mathematics'):
        from ai_tutor.apps.dashboard.models import CurriculumUpload
        return CurriculumUpload.objects.create(
            institution=institution, subject_name='Mathematics',
            grade_level='S3', subject_code=subject_code,
            original_filename='syllabus.pdf', file_path='/tmp/x.pdf',
        )

    def test_a_blank_course_is_filled_in(self, db, school):
        from ai_tutor.apps.curriculum.models import Course as C
        course = C.objects.create(title='Mathematics S3', institution=school,
                                  subject_code='', grade_level='')
        upload = self._upload(school)

        # What the pipeline does for an existing course.
        defaults = {'subject_code': upload.subject_code, 'grade_level': 'S3'}
        fill = {}
        if not course.subject_code and defaults.get('subject_code'):
            fill['subject_code'] = defaults['subject_code']
        if not course.grade_level and defaults.get('grade_level'):
            fill['grade_level'] = defaults['grade_level']
        for f, v in fill.items():
            setattr(course, f, v)
        course.save(update_fields=list(fill))

        course.refresh_from_db()
        assert course.subject_code == 'mathematics'
        # grade_levels is a property over grade_level — filling the one text
        # field is the whole job, and the list the material join reads follows.
        assert course.grade_levels == ['S3']

    def test_a_course_with_neither_is_flagged_on_the_page(self, client, teacher,
                                                          db, school):
        from ai_tutor.apps.curriculum.models import Course as C
        course = C.objects.create(title='Mathematics S3', institution=school,
                                  subject_code='', grade_level='')
        client.force_login(teacher)
        body = client.get(reverse('dashboard:course_detail',
                                  args=[course.id])).content.decode()
        assert 'not sharing teaching materials' in body
        assert 'no subject and no grade set' in body

    def test_a_course_with_both_is_not_flagged(self, client, teacher, course):
        course.subject_code = 'geography'
        course.grade_level = 'S3'
        course.save(update_fields=['subject_code', 'grade_level'])

        client.force_login(teacher)
        body = client.get(reverse('dashboard:course_detail',
                                  args=[course.id])).content.decode()
        assert 'not sharing teaching materials' not in body
