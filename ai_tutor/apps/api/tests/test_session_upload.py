"""POST /api/v1/sessions/upload/ — a session finished offline.

The offline desktop build's only sync call. It is authenticated as the student,
so the interesting cases are the ones where a client tries to be someone else
or to reach another school's content.
"""
import pytest
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import SessionTurn, TutorSession


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Anse Royale', slug='anse-royale')


@pytest.fixture
def other_school(db):
    return Institution.objects.create(name='Other School', slug='other')


def _lesson(institution, title='Rivers'):
    course = Course.objects.create(title='Geography', institution=institution)
    unit = Unit.objects.create(title='U1', course=course, order_index=1)
    return Lesson.objects.create(title=title, unit=unit, order_index=1,
                                 is_published=True)


@pytest.fixture
def student(db, django_user_model, school):
    user = django_user_model.objects.create_user('pupil', password='pw-12345678')
    Membership.objects.create(user=user, institution=school, role='student',
                              is_active=True)
    return user


def _body(lesson, **over):
    body = {
        'client_uuid': 'uuid-1',
        'lesson_id': lesson.id,
        'status': 'completed',
        'turns': [
            {'role': 'student', 'content': 'Why do rivers meander?'},
            {'role': 'tutor', 'content': 'Erosion on the outer bank.'},
        ],
    }
    body.update(over)
    return body


@pytest.mark.django_db
class TestUpload:

    def test_creates_the_session_for_the_signed_in_student(self, client, student, school):
        lesson = _lesson(school)
        client.force_login(student)
        r = client.post(reverse('api:session_upload'), _body(lesson),
                        content_type='application/json')

        assert r.status_code == 201
        session = TutorSession.objects.get(pk=r.json()['session_id'])
        assert session.student_id == student.id
        assert session.status == TutorSession.Status.COMPLETED
        assert session.turns.count() == 2
        assert all(t.generated_offline for t in session.turns.all())

    def test_requires_authentication(self, client, student, school):
        lesson = _lesson(school)
        r = client.post(reverse('api:session_upload'), _body(lesson),
                        content_type='application/json')
        assert r.status_code in (401, 403)
        assert TutorSession.objects.count() == 0

    def test_a_client_cannot_upload_as_another_student(self, client, student,
                                                       school, django_user_model):
        """There is no field for it — the token decides. This pins that."""
        other = django_user_model.objects.create_user('someone-else')
        Membership.objects.create(user=other, institution=school, role='student',
                                  is_active=True)
        lesson = _lesson(school)
        client.force_login(student)

        r = client.post(reverse('api:session_upload'),
                        _body(lesson, student=other.id, server_user_id=other.id),
                        content_type='application/json')

        assert r.status_code == 201
        assert TutorSession.objects.get().student_id == student.id

    def test_refuses_a_lesson_from_another_school(self, client, student, other_school):
        lesson = _lesson(other_school, title='Not yours')
        client.force_login(student)
        r = client.post(reverse('api:session_upload'), _body(lesson),
                        content_type='application/json')
        assert r.status_code == 404
        assert TutorSession.objects.count() == 0

    def test_accepts_platform_wide_content(self, client, student):
        """institution=None means every school, and offline devices get it too."""
        lesson = _lesson(None)
        client.force_login(student)
        r = client.post(reverse('api:session_upload'), _body(lesson),
                        content_type='application/json')
        assert r.status_code == 201

    def test_a_retry_does_not_duplicate_the_lesson(self, client, student, school):
        """A lost response must cost one 409, not a second copy of the work."""
        lesson = _lesson(school)
        client.force_login(student)
        first = client.post(reverse('api:session_upload'), _body(lesson),
                            content_type='application/json')
        second = client.post(reverse('api:session_upload'), _body(lesson),
                             content_type='application/json')

        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()['session_id'] == first.json()['session_id']
        assert TutorSession.objects.count() == 1

    @pytest.mark.parametrize('missing', ['client_uuid', 'lesson_id'])
    def test_rejects_a_payload_it_cannot_dedupe_or_place(self, client, student,
                                                         school, missing):
        lesson = _lesson(school)
        client.force_login(student)
        body = _body(lesson)
        del body[missing]
        r = client.post(reverse('api:session_upload'), body,
                        content_type='application/json')
        assert r.status_code == 400

    def test_rejects_an_unknown_status(self, client, student, school):
        lesson = _lesson(school)
        client.force_login(student)
        r = client.post(reverse('api:session_upload'),
                        _body(lesson, status='banana'),
                        content_type='application/json')
        assert r.status_code == 400

    def test_turns_keep_their_order(self, client, student, school):
        lesson = _lesson(school)
        client.force_login(student)
        r = client.post(reverse('api:session_upload'), _body(lesson),
                        content_type='application/json')
        turns = TutorSession.objects.get(pk=r.json()['session_id']).turns.order_by('id')
        assert [t.role for t in turns] == ['student', 'tutor']
        assert turns[0].content.startswith('Why do rivers')
