"""Which students see a course.

A course's grade list decides BOTH what it matches for material inheritance
AND which students can see it in the catalog. The Edit Course form only says
the first, so a teacher wanting an S3 course visible to S4 and S5 has no way
to know the tick-boxes already do that.
"""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie')


def _student(school, username, grade):
    user = User.objects.create_user(username, f'{username}@e.com', 'pw')
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STUDENT)
    StudentProfile.objects.create(user=user, grade_level=grade)
    return user


def _course(school, grade_level, title='Mathematics S3'):
    course = Course.objects.create(
        title=title, institution=school, subject_code='mathematics',
        grade_level=grade_level, is_published=True)
    unit = Unit.objects.create(course=course, title='U1', order_index=0)
    Lesson.objects.create(unit=unit, title='L1', objective='o',
                          order_index=0, is_published=True)
    return course


def _sees(client, student, course):
    client.force_login(student)
    body = client.get(reverse('tutoring:catalog')).content.decode()
    return course.title in body


def test_one_grade_is_visible_only_to_that_grade(client, school):
    course = _course(school, 'S3')
    assert _sees(client, _student(school, 'a', 'S3'), course)
    assert not _sees(client, _student(school, 'b', 'S4'), course)


def test_ticking_several_grades_opens_it_to_all_of_them(client, school):
    """The answer to "what if I want S4 and S5 students to see this too" —
    tick them. Already supported; only the form's wording hid it."""
    course = _course(school, 'S3,S4,S5')
    assert _sees(client, _student(school, 'c', 'S3'), course)
    assert _sees(client, _student(school, 'd', 'S4'), course)
    assert _sees(client, _student(school, 'e', 'S5'), course)
    assert not _sees(client, _student(school, 'f', 'S1'), course)


def test_no_grade_at_all_is_visible_to_everyone(client, school):
    """Documented fallback: an unclassified course is not hidden from all."""
    course = _course(school, '')
    assert _sees(client, _student(school, 'g', 'S2'), course)


def test_a_course_storing_the_label_is_visible_to_the_code(client, school):
    """Same clash as material sharing: the parser writes 'Secondary 3' out of
    the syllabus, students carry the configured code 'S3'. Comparing raw
    strings hides the course from exactly the students it is for."""
    course = _course(school, 'Secondary 3', title='Parser Named Course')
    assert _sees(client, _student(school, 'h', 'S3'), course)


def test_a_ranged_course_is_visible_across_the_range(client, school):
    course = _course(school, 'S1-S3', title='Ranged Course')
    assert _sees(client, _student(school, 'i', 'S2'), course)
    assert not _sees(client, _student(school, 'j', 'S5'), course)
