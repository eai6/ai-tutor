"""A Tanzania account must not see Seychelles.

Asserted per surface rather than once, because each surface reaches the data
by a different path — a school FK, a relation through a course, a session's
own institution — and a fix to one does not fix the others.

Every test here is a leak test. A failure is a cross-border data exposure,
not a broken assertion, so none of them should be relaxed to make a build
green.
"""

import pytest
from django.contrib.auth import get_user_model

from ai_tutor.apps.accounts.models import (
    Country, CountryMembership, Institution, Membership, StudentProfile)
from ai_tutor.apps.accounts.scope import get_staff_context
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import SessionTurn, TutorSession

User = get_user_model()

SECRET = 'Baie Lazare'          # a Seychelles name Tanzania must never see
MINE = 'Dodoma'                 # a Tanzania name the ministry should see


@pytest.fixture
def world(db):
    """Two countries, a school each, and a full record in both."""
    sc = Country.objects.create(name='Seychelles', slug='sc')
    tz = Country.objects.create(name='Tanzania', slug='tz')
    built = {'sc_country': sc, 'tz_country': tz}

    for key, country, label in (('sc', sc, SECRET), ('tz', tz, MINE)):
        school = Institution.objects.create(
            name=f'{label} School', slug=f'{key}-school', country=country)
        course = Course.objects.create(
            title=f'{label} Course', institution=school, country=country,
            is_published=True)
        unit = Unit.objects.create(course=course, title=f'{label} Unit', order_index=1)
        lesson = Lesson.objects.create(
            unit=unit, title=f'{label} Lesson', objective='x', order_index=1,
            is_published=True)

        student = User.objects.create_user(
            username=f'{key}-student', first_name=label, last_name='Student')
        Membership.objects.create(
            user=student, institution=school, role=Membership.Role.STUDENT)
        StudentProfile.objects.create(
            user=student, school=school, student_id=f'{key}-001')

        session = TutorSession.objects.create(
            institution=school, student=student, lesson=lesson,
            is_flagged=True, flag_reason=f'{label} flag', flag_reviewed=False)
        # The safety badge counts sessions with a flagged TURN, not a flagged
        # session — the session flag alone leaves the count at zero.
        SessionTurn.objects.create(
            session=session, role='assistant', content=f'{label} turn',
            is_flagged=True, flag_type='harmful')

        built[key] = {
            'country': country, 'school': school, 'course': course,
            'lesson': lesson, 'student': student, 'session': session,
        }

    # A course shared across Seychelles — no institution, so under the old
    # rule it was visible to every school on the platform.
    built['shared_sc'] = Course.objects.create(
        title=f'{SECRET} National Curriculum', institution=None, country=sc,
        is_published=True)
    return built


@pytest.fixture
def ministry(world, client):
    """Logged in as the Tanzania country account."""
    user = User.objects.create_user(username='tz-ministry', password='pw')
    CountryMembership.objects.create(user=user, country=world['tz_country'])
    client.force_login(user)
    return user


def _body(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f'{path} -> {resp.status_code}'
    return resp.content.decode()


# ---------------------------------------------------------------------------
# One test per surface
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_school(client, world, ministry):
    body = _body(client, '/dashboard/schools/')
    assert MINE in body
    assert SECRET not in body


@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_students(client, world, ministry):
    body = _body(client, '/dashboard/students/')
    assert SECRET not in body


@pytest.mark.django_db
def test_it_cannot_open_the_other_countrys_student(client, world, ministry):
    resp = client.get(f"/dashboard/students/{world['sc']['student'].pk}/")
    assert resp.status_code in (302, 403, 404)


@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_courses(client, world, ministry):
    body = _body(client, '/dashboard/curriculum/')
    assert SECRET not in body


@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_national_curriculum(client, world, ministry):
    """The shared course is the one the old rule leaked: no institution used
    to mean visible to the whole platform."""
    body = _body(client, '/dashboard/curriculum/')
    assert world['shared_sc'].title not in body


@pytest.mark.django_db
def test_it_cannot_open_the_other_countrys_course(client, world, ministry):
    resp = client.get(f"/dashboard/curriculum/course/{world['sc']['course'].pk}/")
    assert resp.status_code in (302, 403, 404)


@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_flagged_sessions(client, world, ministry):
    body = _body(client, '/dashboard/flagged/')
    assert SECRET not in body


@pytest.mark.django_db
def test_it_cannot_open_the_other_countrys_flagged_session(client, world, ministry):
    resp = client.get(f"/dashboard/flagged/{world['sc']['session'].pk}/")
    assert resp.status_code in (302, 403, 404)


@pytest.mark.django_db
def test_it_cannot_see_the_other_countrys_staff(client, world, ministry):
    teacher = User.objects.create_user(username=f'{SECRET}-teacher')
    Membership.objects.create(
        user=teacher, institution=world['sc']['school'],
        role=Membership.Role.STAFF)

    body = _body(client, '/dashboard/staff/')
    assert SECRET not in body


@pytest.mark.django_db
def test_the_flag_badge_counts_only_its_own_country(client, world, ministry, rf):
    """The badge is the number that is on screen on every page."""
    request = rf.get('/dashboard/')
    request.user = ministry
    request.session = {}

    assert get_staff_context(request)['unreviewed_flag_count'] == 1


@pytest.mark.django_db
def test_switching_to_another_countrys_school_id_is_refused(client, world, ministry, rf):
    """`selected_school_id` is user input — the school switcher posts it.
    Setting it to a school in another country must not widen the session."""
    request = rf.get('/dashboard/')
    request.user = ministry
    request.session = {'selected_school_id': str(world['sc']['school'].pk)}

    ctx = get_staff_context(request)

    assert ctx['institution'] is None
    assert world['sc']['school'] not in ctx['all_schools']


@pytest.mark.django_db
def test_switching_school_through_the_view_does_not_widen_it(client, world, ministry):
    """The same thing again, through the real POST route."""
    client.post('/dashboard/switch-school/',
                {'school_id': str(world['sc']['school'].pk)})

    body = _body(client, '/dashboard/students/')
    assert SECRET not in body


@pytest.mark.django_db
def test_it_cannot_add_a_school_into_the_other_country(client, world, ministry):
    client.post('/dashboard/schools/create/',
                {'name': 'Trojan', 'slug': 'trojan', 'country': world['sc']['country'].pk})

    assert Institution.objects.get(slug='trojan').country == world['tz_country']
