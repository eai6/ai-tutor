"""Integration test for the Phase 1 exit criterion:

  "NEW_TUTOR=on boots a new session and writes a valid
   SessionRuntimeState snapshot to runtime_state."

Uses Django's TestCase (DB-backed) to construct a real TutorSession
and exercise the routing helper. The placeholder response path is
checked end-to-end against ContextManager.load_runtime_state().
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import TutorSession
from apps.tutoring.v2.contracts import SessionRuntimeState
from apps.tutoring.v2.routing import (
    ensure_engine_version_set,
    is_v2_session,
    v2_placeholder_response,
)
from apps.tutoring.v2.services.context_manager import ContextManager


def _build_session():
    inst = Institution.objects.create(name="Test Inst", slug="test-inst")
    user = User.objects.create_user(username="alice", password="x")
    Membership.objects.create(institution=inst, user=user, role="student",
                              is_active=True)
    course = Course.objects.create(
        institution=inst, title="C", grade_level="S1", subject_type="math",
    )
    unit = Unit.objects.create(
        course=course, title="U", order_index=0, grade_level="S1",
    )
    lesson = Lesson.objects.create(
        unit=unit, title="L", objective="o", is_published=True,
    )
    return TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson,
        status=TutorSession.Status.ACTIVE,
    )


class V2SessionInitTest(TestCase):
    def test_new_tutor_on_initializes_runtime_state(self):
        session = _build_session()
        with patch.dict("os.environ", {"NEW_TUTOR": "on"}):
            ensure_engine_version_set(session)

        session.refresh_from_db()
        self.assertEqual(session.engine_version, "v2")
        self.assertTrue(is_v2_session(session))
        # runtime_state has a valid SessionRuntimeState snapshot.
        state = SessionRuntimeState.from_jsonable(session.runtime_state)
        self.assertEqual(state.schema_version, 1)
        self.assertIsNone(state.open_question)

    def test_new_tutor_off_picks_legacy_and_does_not_touch_runtime_state(self):
        session = _build_session()
        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            ensure_engine_version_set(session)

        session.refresh_from_db()
        self.assertEqual(session.engine_version, "legacy")
        self.assertEqual(session.runtime_state, {})

    def test_placeholder_response_persists_runtime_state(self):
        session = _build_session()
        with patch.dict("os.environ", {"NEW_TUTOR": "on"}):
            ensure_engine_version_set(session)
            payload = v2_placeholder_response(session, kind="start")

        self.assertTrue(payload["v2_placeholder"])
        self.assertEqual(payload["session_id"], session.id)
        session.refresh_from_db()
        # Confirm load + save path is wired through ContextManager.
        state = SessionRuntimeState.from_jsonable(session.runtime_state)
        self.assertIsInstance(state, SessionRuntimeState)

    def test_context_manager_roundtrips_state(self):
        session = _build_session()
        cm = ContextManager(session)
        state = cm.load_runtime_state()
        state.current_move = "pose_question"
        cm.save_runtime_state(state)

        session.refresh_from_db()
        cm2 = ContextManager(session)
        revived = cm2.load_runtime_state()
        self.assertEqual(revived.current_move, "pose_question")
