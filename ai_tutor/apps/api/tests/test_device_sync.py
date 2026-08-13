"""Device sync: pushing offline work into the cloud.

Two properties carry this feature. Idempotency, because a classroom uplink
drops mid-request and the device WILL retry — a duplicate session is worse than
a missing one, since it silently doubles every figure a teacher reads. And the
institution boundary, because a device authenticates as itself and then names
the student it is writing for: nothing stops a compromised one from naming a
student at another school except this check.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Device, Institution, Membership
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import SessionTurn, TutorSession


@pytest.fixture(autouse=True)
def _reset_throttle():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def school(db):
    return Institution.objects.create(name='School A', slug='school-a')


@pytest.fixture
def other_school(db):
    return Institution.objects.create(name='School B', slug='school-b')


@pytest.fixture
def lesson(school):
    course = Course.objects.create(title='Geography', institution=school)
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    return Lesson.objects.create(unit=unit, title='Understanding Maps',
                                 objective='Read a map', order_index=1,
                                 is_published=True)


def _student(institution, username):
    user = User.objects.create_user(username=username)
    Membership.objects.create(user=user, institution=institution,
                              role='student', is_active=True)
    return user


@pytest.fixture
def enrolled(client, school):
    device = Device.objects.create(institution=school,
                                   enrolment_code=Device.generate_code(),
                                   name='Lab laptop 1')
    r = client.post(reverse('api:device_enrol'), {'code': device.enrolment_code},
                    content_type='application/json')
    return device, r.json()['token']


def _push(client, token, **overrides):
    body = {
        'client_uuid': str(uuid.uuid4()),
        'kind': 'session',
        'payload': {'lesson_id': None, 'turns': [
            {'role': 'tutor', 'content': 'What is a compass rose?'},
            {'role': 'student', 'content': 'It shows direction'},
        ]},
    }
    body.update(overrides)
    return client.post(reverse('api:device_sync'), body,
                       content_type='application/json',
                       HTTP_AUTHORIZATION=f'Device {token}')


@pytest.mark.django_db
class TestDeviceSync:
    def test_pushes_a_session_with_its_turns(self, client, enrolled, school, lesson):
        _, token = enrolled
        student = _student(school, 'alice')
        r = _push(client, token, server_user_id=student.id,
                  payload={'lesson_id': lesson.id, 'turns': [
                      {'role': 'tutor', 'content': 'Q?'},
                      {'role': 'student', 'content': 'A'},
                  ]})
        assert r.status_code == 201
        session = TutorSession.objects.get(id=r.json()['session_id'])
        assert session.student_id == student.id
        assert session.institution_id == school.id
        assert session.turns.count() == 2

    def test_a_retry_does_not_duplicate(self, client, enrolled, school, lesson):
        """The property the whole design rests on: a dropped response is safe."""
        _, token = enrolled
        student = _student(school, 'alice')
        cu = str(uuid.uuid4())
        payload = {'lesson_id': lesson.id, 'turns': [{'role': 'tutor', 'content': 'Q?'}]}

        first = _push(client, token, client_uuid=cu, server_user_id=student.id,
                      payload=payload)
        second = _push(client, token, client_uuid=cu, server_user_id=student.id,
                       payload=payload)

        assert first.status_code == 201
        assert second.status_code == 409
        assert TutorSession.objects.count() == 1
        assert SessionTurn.objects.count() == 1

    def test_cannot_write_for_a_student_at_another_school(
            self, client, enrolled, other_school, lesson):
        """The institution boundary. A device names its own student; nothing
        else stops it naming someone else's."""
        _, token = enrolled
        outsider = _student(other_school, 'bob')
        r = _push(client, token, server_user_id=outsider.id,
                  payload={'lesson_id': lesson.id, 'turns': []})
        assert r.status_code == 403
        assert TutorSession.objects.count() == 0

    def test_an_unknown_student_is_refused(self, client, enrolled, lesson):
        _, token = enrolled
        r = _push(client, token, server_user_id=999999,
                  payload={'lesson_id': lesson.id, 'turns': []})
        assert r.status_code == 403

    def test_an_inactive_membership_is_refused(self, client, enrolled, school, lesson):
        _, token = enrolled
        student = _student(school, 'gone')
        Membership.objects.filter(user=student).update(is_active=False)
        r = _push(client, token, server_user_id=student.id,
                  payload={'lesson_id': lesson.id, 'turns': []})
        assert r.status_code == 403

    def test_a_revoked_device_cannot_push(self, client, enrolled, school, lesson):
        device, token = enrolled
        student = _student(school, 'alice')
        device.refresh_from_db()
        device.status = Device.Status.REVOKED
        device.save()
        r = _push(client, token, server_user_id=student.id,
                  payload={'lesson_id': lesson.id, 'turns': []})
        assert r.status_code == 401
        assert TutorSession.objects.count() == 0

    def test_no_token_cannot_push(self, client, school, lesson):
        student = _student(school, 'alice')
        r = client.post(reverse('api:device_sync'),
                        {'client_uuid': str(uuid.uuid4()), 'kind': 'session',
                         'server_user_id': student.id,
                         'payload': {'lesson_id': lesson.id}},
                        content_type='application/json')
        assert r.status_code == 401

    def test_missing_fields_are_rejected(self, client, enrolled):
        _, token = enrolled
        r = client.post(reverse('api:device_sync'), {'kind': 'session'},
                        content_type='application/json',
                        HTTP_AUTHORIZATION=f'Device {token}')
        assert r.status_code == 400

    def test_unsupported_kind_is_rejected(self, client, enrolled, school):
        _, token = enrolled
        student = _student(school, 'alice')
        r = _push(client, token, kind='nonsense', server_user_id=student.id)
        assert r.status_code == 400

    def test_turns_are_marked_as_offline_with_the_device_id(
            self, client, enrolled, school, lesson):
        """A teacher looking at a transcript should be able to tell it was
        produced offline, and on which machine."""
        _, token = enrolled
        student = _student(school, 'alice')
        r = _push(client, token, server_user_id=student.id,
                  payload={'lesson_id': lesson.id,
                           'turns': [{'role': 'tutor', 'content': 'Q?'}]})
        turn = TutorSession.objects.get(id=r.json()['session_id']).turns.first()
        assert turn.generated_offline is True
        assert turn.metadata['source'] == 'desktop_offline'
