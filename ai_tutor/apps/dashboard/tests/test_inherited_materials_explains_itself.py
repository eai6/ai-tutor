"""The course page says WHY no platform-wide materials reach a course.

The badge used to render only on a match. Every other outcome — no
platform-wide course for the subject, one that exists but covers other
grades, one that matches but is empty, or one holding materials that cannot
be found because it has no subject of its own — rendered as nothing at all,
which is indistinguishable from a broken page. "Why am I still not seeing the
platform-wide widget" had four different answers and the page gave none.
"""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.models import Course
from ai_tutor.apps.dashboard.models import TeachingMaterialUpload
from ai_tutor.apps.dashboard.views import _inherited_materials_summary


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
    return Course.objects.create(
        title='Mathematics S3', institution=school,
        subject_code='mathematics', grade_level='S3')


def _platform_course(**kwargs):
    kwargs.setdefault('title', 'Platform Maths')
    kwargs.setdefault('grade_level', 'S3')
    return Course.objects.create(institution=None, **kwargs)


def _material(course, title='Textbook'):
    return TeachingMaterialUpload.objects.create(
        course=course, institution=None, title=title,
        original_filename='t.pdf', file_path='/tmp/t.pdf',
    )


def test_no_platform_course_for_the_subject(course):
    summary = _inherited_materials_summary(course)
    assert summary['status'] == 'no_platform_course'


def test_a_platform_course_that_covers_other_grades(course):
    _platform_course(subject_code='mathematics', grade_level='S5')
    summary = _inherited_materials_summary(course)
    assert summary['status'] == 'grade_mismatch'
    assert [c.title for c in summary['subject_courses']] == ['Platform Maths']


def test_a_matching_platform_course_with_nothing_in_it(course):
    _platform_course(subject_code='mathematics', grade_level='S3')
    summary = _inherited_materials_summary(course)
    assert summary['status'] == 'matched_but_empty'
    assert summary['material_count'] == 0


def test_materials_that_exist_but_cannot_be_found(course):
    """The state worth naming loudest: a platform-wide course HAS materials
    but no subject of its own, so the query that joins on subject_code cannot
    see it. The library is not empty — it is unreachable, and that is fixable.
    """
    orphan = _platform_course(title='Maths Textbooks', subject_code='')
    _material(orphan)

    summary = _inherited_materials_summary(course)
    assert summary['status'] == 'platform_courses_unclassified'
    assert [c.title for c in summary['unclassified_courses']] == ['Maths Textbooks']


def test_the_happy_path_still_reports_a_count(course):
    matching = _platform_course(subject_code='mathematics', grade_level='S3')
    _material(matching)
    summary = _inherited_materials_summary(course)
    assert summary['status'] == 'matched'
    assert summary['material_count'] == 1


@pytest.mark.parametrize('setup,expected', [
    ('none', 'There is no platform-wide course for this subject'),
    ('unclassified', 'no subject set'),
    ('empty', 'no teaching materials have been uploaded to it'),
])
def test_the_page_renders_the_explanation(client, teacher, course, setup, expected):
    if setup == 'unclassified':
        _material(_platform_course(title='Maths Textbooks', subject_code=''))
    elif setup == 'empty':
        _platform_course(subject_code='mathematics', grade_level='S3')

    client.force_login(teacher)
    body = client.get(
        reverse('dashboard:course_detail', args=[course.id])).content.decode()
    assert 'No platform-wide materials reach this course yet' in body
    assert expected in body
