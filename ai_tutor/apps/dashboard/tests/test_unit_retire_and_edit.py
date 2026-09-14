"""A replace must not leave empty units behind, and units must be editable.

Reported after a regenerate/replace: the old units were still on the page,
each reading "0 lessons". retire_lessons_not_in parked the lessons and left
their units standing, so a course accumulates a heading from every syllabus
version it has ever been re-parsed against, with no way to clear them.
"""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.dashboard.views import retire_lessons_not_in


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
    course = Course.objects.create(title='Mathematics S3', institution=school,
                                   subject_code='mathematics', grade_level='S3')
    old = Unit.objects.create(course=course, title='Old Unit', order_index=0)
    Lesson.objects.create(unit=old, title='Old Lesson', objective='o',
                          order_index=0, is_published=True)
    keep = Unit.objects.create(course=course, title='Kept Unit', order_index=1)
    Lesson.objects.create(unit=keep, title='Kept Lesson', objective='o',
                          order_index=0, is_published=True)
    return course


NEW_DOC = {'units': [
    {'title': 'Kept Unit', 'lessons': [{'title': 'Kept Lesson'}]},
]}


def test_a_replace_parks_the_unit_it_emptied(course):
    retire_lessons_not_in(course, NEW_DOC)

    old = Unit.objects.get(title='Old Unit')
    assert old.retired_at is not None, 'an emptied unit must not stay on the page'
    assert Unit.objects.get(title='Kept Unit').retired_at is None


def test_it_parks_nothing_the_document_still_lists(course):
    """A unit the document names is kept even while momentarily empty — it is
    about to be refilled, and sweeping it would fight the merge."""
    Unit.objects.create(course=course, title='Listed But Empty', order_index=2)
    doc = {'units': [
        {'title': 'Kept Unit', 'lessons': [{'title': 'Kept Lesson'}]},
        {'title': 'Listed But Empty', 'lessons': [{'title': 'Coming Soon'}]},
    ]}

    retire_lessons_not_in(course, doc)

    assert Unit.objects.get(title='Listed But Empty').retired_at is None


def test_a_unit_keeping_one_live_lesson_is_not_parked(course):
    """Only fully emptied units go. A partial replace leaves the unit."""
    old = Unit.objects.get(title='Old Unit')
    Lesson.objects.create(unit=old, title='Survivor', objective='o', order_index=1)
    doc = {'units': [
        {'title': 'Kept Unit', 'lessons': [{'title': 'Kept Lesson'}]},
        {'title': 'Old Unit', 'lessons': [{'title': 'Survivor'}]},
    ]}

    retire_lessons_not_in(course, doc)

    assert Unit.objects.get(title='Old Unit').retired_at is None


def test_nothing_is_deleted(course):
    """Unit and Lesson both CASCADE into transcripts, mastery rows and
    exit-ticket attempts. Parking is the whole point."""
    retire_lessons_not_in(course, NEW_DOC)

    assert Unit.objects.filter(title='Old Unit').exists()
    assert Lesson.objects.filter(title='Old Lesson').exists()


def test_a_parked_unit_is_off_the_course_page(client, teacher, course):
    retire_lessons_not_in(course, NEW_DOC)

    client.force_login(teacher)
    body = client.get(
        reverse('dashboard:course_detail', args=[course.id])).content.decode()
    assert 'Kept Unit' in body
    assert 'parked unit' in body      # listed in the parked section, not the table


def test_a_teacher_can_rename_a_unit(client, teacher, course):
    unit = Unit.objects.get(title='Old Unit')
    client.force_login(teacher)
    client.post(reverse('dashboard:unit_edit', args=[unit.id]),
                {'title': 'Renamed Unit'}, follow=True)

    assert Unit.objects.get(id=unit.id).title == 'Renamed Unit'


def test_renaming_to_blank_is_refused(client, teacher, course):
    unit = Unit.objects.get(title='Old Unit')
    client.force_login(teacher)
    client.post(reverse('dashboard:unit_edit', args=[unit.id]),
                {'title': '   '}, follow=True)

    assert Unit.objects.get(id=unit.id).title == 'Old Unit'


def test_a_teacher_can_remove_a_unit_by_hand(client, teacher, course):
    unit = Unit.objects.get(title='Old Unit')
    client.force_login(teacher)
    client.post(reverse('dashboard:unit_delete', args=[unit.id]), follow=True)

    unit.refresh_from_db()
    assert unit.retired_at is not None
    lesson = Lesson.objects.get(title='Old Lesson')
    assert lesson.retired_at is not None and lesson.is_published is False


def test_a_parked_unit_can_be_restored(client, teacher, course):
    unit = Unit.objects.get(title='Old Unit')
    client.force_login(teacher)
    client.post(reverse('dashboard:unit_delete', args=[unit.id]), follow=True)
    client.post(reverse('dashboard:unit_edit', args=[unit.id]),
                {'title': unit.title, 'restore': '1'}, follow=True)

    assert Unit.objects.get(id=unit.id).retired_at is None


def test_restoring_brings_its_lessons_back_too(client, teacher, course):
    """Restoring a unit without its lessons hands back an empty unit, which
    is not what restore means. Only the lessons parked in the SAME moment
    come back — anything parked by an earlier replace stays parked."""
    from django.utils import timezone

    unit = Unit.objects.get(title='Old Unit')
    earlier = Lesson.objects.create(
        unit=unit, title='Parked Earlier', objective='o', order_index=5,
        retired_at=timezone.now(), is_published=False)

    client.force_login(teacher)
    client.post(reverse('dashboard:unit_delete', args=[unit.id]), follow=True)
    client.post(reverse('dashboard:unit_edit', args=[unit.id]),
                {'title': unit.title, 'restore': '1'}, follow=True)

    assert Lesson.objects.get(title='Old Lesson').retired_at is None
    earlier.refresh_from_db()
    assert earlier.retired_at is not None, 'an earlier parking is not undone here'


def test_another_schools_unit_cannot_be_touched(client, db, school):
    """Scoped to ONE school deliberately: `is_staff=True` routes through the
    superadmin all-schools branch, where institution is None and nothing is
    filtered — so a staff fixture would pass this against unscoped code.
    A super-admin editing any school's unit is correct; a teacher doing it is
    a cross-tenant leak, and only the second is what this pins."""
    scoped = User.objects.create_user('classteacher', 'c@example.com', 'pw')
    Membership.objects.create(user=scoped, institution=school,
                              role=Membership.Role.STAFF)

    other = Institution.objects.create(name='Praslin', slug='praslin')
    their_course = Course.objects.create(title='Theirs', institution=other)
    their_unit = Unit.objects.create(course=their_course, title='Theirs',
                                     order_index=0)

    client.force_login(scoped)
    r = client.post(reverse('dashboard:unit_delete', args=[their_unit.id]))

    assert r.status_code == 404
    their_unit.refresh_from_db()
    assert their_unit.retired_at is None


# ---------------------------------------------------------------------------
# Permanent delete. Everything else on this page parks, because TutorSession,
# StudentLessonProgress, ExitTicket and LessonPackVersion all CASCADE off
# Lesson — deleting a unit takes student work with it.
# ---------------------------------------------------------------------------

def test_an_unused_parked_unit_can_be_deleted(client, teacher, course):
    """The case behind the request: units left by an earlier re-parse that no
    student ever opened."""
    unit = Unit.objects.get(title='Old Unit')
    client.force_login(teacher)
    client.post(reverse('dashboard:unit_delete', args=[unit.id]), follow=True)
    client.post(reverse('dashboard:unit_purge', args=[unit.id]), follow=True)

    assert not Unit.objects.filter(id=unit.id).exists()
    assert not Lesson.objects.filter(title='Old Lesson').exists()


def test_a_unit_a_student_used_is_refused(client, teacher, course, school):
    """The guard that matters. Refused, not confirmed — a dialog is not
    enough protection for a transcript that cannot be recovered."""
    from ai_tutor.apps.tutoring.models import TutorSession

    unit = Unit.objects.get(title='Old Unit')
    lesson = Lesson.objects.get(title='Old Lesson')
    student = User.objects.create_user('stud', 's@example.com', 'pw')
    TutorSession.objects.create(student=student, lesson=lesson,
                                institution=school)

    client.force_login(teacher)
    client.post(reverse('dashboard:unit_delete', args=[unit.id]), follow=True)
    r = client.post(reverse('dashboard:unit_purge', args=[unit.id]), follow=True)

    assert Unit.objects.filter(id=unit.id).exists()
    assert Lesson.objects.filter(id=lesson.id).exists()
    assert TutorSession.objects.count() == 1
    assert 'cannot be deleted' in r.content.decode()


def test_a_live_unit_cannot_be_deleted_outright(client, teacher, course):
    """Park first. Two deliberate steps, so nothing goes in one click."""
    unit = Unit.objects.get(title='Old Unit')
    client.force_login(teacher)
    r = client.post(reverse('dashboard:unit_purge', args=[unit.id]), follow=True)

    assert Unit.objects.filter(id=unit.id).exists()
    assert 'Remove the unit first' in r.content.decode()


def test_another_schools_unit_cannot_be_deleted(client, db, school):
    scoped = User.objects.create_user('classteacher2', 'c2@example.com', 'pw')
    Membership.objects.create(user=scoped, institution=school,
                              role=Membership.Role.STAFF)
    other = Institution.objects.create(name='Praslin', slug='praslin2')
    their_course = Course.objects.create(title='Theirs', institution=other)
    from django.utils import timezone
    their_unit = Unit.objects.create(course=their_course, title='Theirs',
                                     order_index=0, retired_at=timezone.now())

    client.force_login(scoped)
    r = client.post(reverse('dashboard:unit_purge', args=[their_unit.id]))

    assert r.status_code == 404
    assert Unit.objects.filter(id=their_unit.id).exists()
