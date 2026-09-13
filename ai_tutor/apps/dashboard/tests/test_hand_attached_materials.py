"""Attaching platform-wide materials to one course by hand.

A union with the automatic subject+grade match, never a replacement. The
tests that matter are the ones about what it must NOT do: reach another
school's material, quietly detach on a re-parse, or be cosmetic (visible on
the page but invisible to the tutoring engine).
"""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.knowledge_base import CurriculumKnowledgeBase
from ai_tutor.apps.curriculum.models import Course
from ai_tutor.apps.dashboard.models import TeachingMaterialUpload


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie')


@pytest.fixture
def other_school(db):
    return Institution.objects.create(name='Praslin', slug='praslin')


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


def _material(institution=None, course=None, title='Textbook'):
    return TeachingMaterialUpload.objects.create(
        institution=institution, course=course, title=title,
        original_filename='t.pdf', file_path='/tmp/t.pdf',
    )


def _attach(client, teacher, course, ids):
    client.force_login(teacher)
    return client.post(
        reverse('dashboard:course_shared_materials', args=[course.id]),
        {'material_ids': [str(i) for i in ids]}, follow=True)


def test_attaching_makes_the_material_visible_to_the_engine(client, teacher, course):
    """The point of the feature. A material the page lists but retrieval never
    sees would be worse than no feature at all."""
    orphan = _material(title='Maths Textbook')   # platform-wide, on no course
    assert CurriculumKnowledgeBase._hand_attached_upload_ids(course) == set()

    _attach(client, teacher, course, [orphan.id])

    course.refresh_from_db()
    ids = CurriculumKnowledgeBase._global_upload_ids_matching_course(course)
    assert orphan.id in ids


def test_a_course_with_no_subject_still_gets_its_hand_attached_material(
        client, teacher, school):
    """The early return for a blank subject_code used to short-circuit before
    the union — and an unclassified course is the one most likely to have
    something attached by hand."""
    unclassified = Course.objects.create(
        title='Mystery', institution=school, subject_code='', grade_level='')
    orphan = _material(title='Textbook')

    _attach(client, teacher, unclassified, [orphan.id])

    ids = CurriculumKnowledgeBase._global_upload_ids_matching_course(unclassified)
    assert ids == {orphan.id}


def test_another_schools_material_cannot_be_attached(
        client, teacher, course, other_school):
    """Cross-tenant leak. The posted id is not trusted — the queryset filters
    to platform-wide, so another school's material is simply not found."""
    theirs = _material(institution=other_school, title='Praslin Notes')

    _attach(client, teacher, course, [theirs.id])

    course.refresh_from_db()
    assert list(course.shared_materials.all()) == []


def test_attaching_never_shrinks_the_automatic_match(client, teacher, course):
    """Union, not override."""
    platform_course = Course.objects.create(
        title='Platform Maths', institution=None,
        subject_code='mathematics', grade_level='S3')
    by_rule = _material(course=platform_course, title='Rule Book')
    by_hand = _material(title='Hand Book')

    _attach(client, teacher, course, [by_hand.id])

    course.refresh_from_db()
    ids = CurriculumKnowledgeBase._global_upload_ids_matching_course(course)
    assert {by_rule.id, by_hand.id} <= ids


def test_a_reparse_does_not_detach_anything(client, teacher, course):
    """course_edit is also the re-parse form's target and posts a subset of
    fields. If the checkbox list lived there it would arrive empty every time
    and silently detach everything — which is why this has its own endpoint."""
    orphan = _material(title='Textbook')
    _attach(client, teacher, course, [orphan.id])

    client.post(
        reverse('dashboard:course_edit', args=[course.id]),
        {'action': 'reparse', 'title': course.title,
         'description': '', 'grade_level': course.grade_level},
        follow=True)

    course.refresh_from_db()
    assert [m.id for m in course.shared_materials.all()] == [orphan.id]


def test_unchecking_everything_detaches(client, teacher, course):
    orphan = _material(title='Textbook')
    _attach(client, teacher, course, [orphan.id])

    _attach(client, teacher, course, [])

    course.refresh_from_db()
    assert list(course.shared_materials.all()) == []


# ---------------------------------------------------------------------------
# Which materials a course may attach. "Does it matter if it's for all schools
# or just one school?" — yes, and the same-school case was wrongly refused.
# ---------------------------------------------------------------------------

def test_a_schools_own_material_is_attachable_to_its_own_course(
        client, teacher, course, school):
    """The common case, and the one that was missing. School A attaching
    School A's material to School A's course crosses no boundary at all; it
    was refused for a leak that cannot happen."""
    ours = _material(institution=school, title='Belonie Worksheets')

    _attach(client, teacher, course, [ours.id])

    course.refresh_from_db()
    assert [m.id for m in course.shared_materials.all()] == [ours.id]


def test_it_is_offered_in_the_picker_too(client, teacher, course, school):
    """The picker and the endpoint must agree — offering something the save
    then drops is worse than not offering it."""
    from django.urls import reverse

    ours = _material(institution=school, title='Belonie Worksheets')
    client.force_login(teacher)
    body = client.get(
        reverse('dashboard:course_detail', args=[course.id])).content.decode()
    assert 'Belonie Worksheets' in body


def test_another_schools_material_is_still_refused(
        client, teacher, course, other_school):
    theirs = _material(institution=other_school, title='Praslin Notes')

    _attach(client, teacher, course, [theirs.id])

    course.refresh_from_db()
    assert list(course.shared_materials.all()) == []


def test_a_material_on_a_platform_course_is_attachable_whatever_its_own_school(
        client, teacher, course, other_school):
    """The sharing rule joins on course_id alone, so this is already visible
    to every school — the picker must offer what the rule shares."""
    platform_course = Course.objects.create(
        title='Platform Maths', institution=None,
        subject_code='mathematics', grade_level='S3')
    shared = _material(institution=other_school, course=platform_course,
                       title='Shared Textbook')

    _attach(client, teacher, course, [shared.id])

    course.refresh_from_db()
    assert [m.id for m in course.shared_materials.all()] == [shared.id]


def test_a_courseless_material_from_another_school_is_not_attachable(
        client, teacher, course, other_school):
    """The `course__isnull=False` guard. Without it, the platform-course
    clause also matches a material with NO course, handing one school's
    private file to another."""
    theirs = _material(institution=other_school, course=None,
                       title='Praslin Private')

    _attach(client, teacher, course, [theirs.id])

    course.refresh_from_db()
    assert list(course.shared_materials.all()) == []
