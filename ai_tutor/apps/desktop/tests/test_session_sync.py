"""Queueing finished sessions for the server.

The producer half of sync. Before this existed the outbox was never written to,
so a correctly configured device still delivered nothing — these tests exist to
keep that from silently coming back.
"""
import pytest
from unittest import mock

from ai_tutor.apps.desktop import session_sync
from ai_tutor.apps.desktop.models import SyncOutbox


@pytest.fixture
def signal_connected():
    """Connect the signal the way apps.py does on a desktop build.

    The real connection happens in AppConfig.ready() only when DESKTOP_BUILD is
    true, which the test settings are not — so tests wire it explicitly rather
    than relying on import order.
    """
    from django.db.models.signals import post_save
    from ai_tutor.apps.tutoring.models import TutorSession

    post_save.connect(session_sync.on_session_saved, sender=TutorSession,
                      dispatch_uid='test_desktop_session_sync')
    yield
    post_save.disconnect(sender=TutorSession, dispatch_uid='test_desktop_session_sync')


@pytest.fixture
def session(db, django_user_model):
    from ai_tutor.apps.accounts.models import Institution
    from ai_tutor.apps.curriculum.models import Course, Unit, Lesson
    from ai_tutor.apps.tutoring.models import TutorSession

    inst = Institution.objects.create(name='Test School', slug='test-school')
    course = Course.objects.create(title='Geography', institution=inst)
    unit = Unit.objects.create(title='U1', course=course, order_index=1)
    lesson = Lesson.objects.create(title='L1', unit=unit, order_index=1)
    user = django_user_model.objects.create_user('student1')
    return TutorSession.objects.create(
        student=user, lesson=lesson, institution=inst,
        status=TutorSession.Status.ACTIVE)


def _add_turns(session, n=3):
    from ai_tutor.apps.tutoring.models import SessionTurn
    for i in range(n):
        SessionTurn.objects.create(
            session=session,
            role='student' if i % 2 == 0 else 'tutor',
            content=f'turn {i}')


@pytest.mark.django_db
class TestQueueingOnCompletion:

    def test_an_active_session_is_not_queued(self, session, signal_connected):
        _add_turns(session)
        session.save()
        assert SyncOutbox.objects.count() == 0

    def test_completing_a_session_queues_it(self, session, signal_connected):
        from ai_tutor.apps.tutoring.models import TutorSession
        _add_turns(session)
        session.status = TutorSession.Status.COMPLETED
        session.save()

        assert SyncOutbox.objects.count() == 1
        item = SyncOutbox.objects.get()
        assert item.kind == 'session'
        assert len(item.payload['turns']) == 3
        assert item.payload['lesson_id'] == session.lesson_id

    def test_saving_a_completed_session_twice_queues_it_once(self, session, signal_connected):
        """The engine saves a finished session more than once — status first,
        then engine_state. Each save must not add another outbox row."""
        from ai_tutor.apps.tutoring.models import TutorSession
        _add_turns(session)
        session.status = TutorSession.Status.COMPLETED
        session.save()
        session.save()
        session.save()

        assert SyncOutbox.objects.count() == 1

    def test_a_reloaded_session_is_not_queued_again(self, session, signal_connected):
        """The marker has to survive in the database, not just in memory."""
        from ai_tutor.apps.tutoring.models import TutorSession
        _add_turns(session)
        session.status = TutorSession.Status.COMPLETED
        session.save()

        again = TutorSession.objects.get(pk=session.pk)
        again.save()

        assert SyncOutbox.objects.count() == 1

    def test_a_session_created_already_completed_is_queued(self, session, signal_connected):
        """Skipping creates would silently drop an imported or replayed lesson."""
        from ai_tutor.apps.accounts.models import Institution
        from ai_tutor.apps.tutoring.models import TutorSession

        SyncOutbox.objects.all().delete()
        TutorSession.objects.create(
            student=session.student, lesson=session.lesson,
            institution=session.institution,
            status=TutorSession.Status.COMPLETED)
        assert SyncOutbox.objects.count() == 1

    def test_a_queueing_failure_never_breaks_the_lesson(self, session, signal_connected):
        """The student has finished either way, and the work is saved locally."""
        from ai_tutor.apps.tutoring.models import TutorSession
        _add_turns(session)
        with mock.patch.object(session_sync, 'enqueue_session',
                               side_effect=RuntimeError('disk full')):
            session.status = TutorSession.Status.COMPLETED
            session.save()          # must not raise

        session.refresh_from_db()
        assert session.status == TutorSession.Status.COMPLETED


@pytest.mark.django_db
class TestPayload:

    def test_turns_are_in_order_with_roles(self, session):
        _add_turns(session, n=4)
        payload = session_sync.payload_for(session)
        assert [t['role'] for t in payload['turns']] == \
            ['student', 'tutor', 'student', 'tutor']
        assert [t['content'] for t in payload['turns']] == \
            ['turn 0', 'turn 1', 'turn 2', 'turn 3']

    def test_every_turn_carries_its_own_uuid(self, session):
        """Per-turn ids are what stop a partially-applied retry duplicating."""
        _add_turns(session, n=3)
        uuids = [t['client_uuid'] for t in session_sync.payload_for(session)['turns']]
        assert len(set(uuids)) == 3

    def test_carries_the_roster_id_not_the_local_one(self, session, signal_connected):
        """The server knows students by its own ids, not this device's."""
        from ai_tutor.apps.desktop.models import RosterEntry
        RosterEntry.objects.create(server_user_id=4242, display_name='S',
                                   username='s', local_user=session.student)
        from ai_tutor.apps.tutoring.models import TutorSession
        session.status = TutorSession.Status.COMPLETED
        session.save()
        assert SyncOutbox.objects.get().server_user_id == 4242
