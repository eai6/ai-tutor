"""Phase 3 §3.4 kill-switch + resume tests.

Covers:
  - Kill-switch: with the default ``NEW_TUTOR`` flipped on, an
    explicit ``NEW_TUTOR=off`` routes new sessions back to legacy
    while in-flight v2 sessions keep their stickiness.
  - Resume sticky: a session started on legacy resumes on legacy;
    a session started on v2 resumes on v2 — regardless of the
    current ``NEW_TUTOR`` value.
  - Resume artifact preservation: when an open_question is set, the
    resume opener surfaces the rendered stem + attached media IDs +
    MCQ option order verbatim — no canonical leak, no new pre-pose.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import TutorSession
from apps.tutoring.v2.contracts import (
    OpenQuestion,
    QuestionSource,
    SessionRuntimeState,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.routing import (
    ensure_engine_version_set,
    is_v2_session,
    v2_resume_dispatch,
)


def _build_session(*, engine_version: str = ""):
    inst = Institution.objects.create(name="T", slug="t")
    user = User.objects.create_user(username=f"u_{engine_version or 'fresh'}", password="x")
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
        engine_version=engine_version,
    )


class KillSwitchTest(TestCase):
    def test_default_routes_new_session_to_v2(self):
        session = _build_session()
        # Phase 3 default: NEW_TUTOR unset → on.
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("NEW_TUTOR", None)
            ensure_engine_version_set(session)
        session.refresh_from_db()
        self.assertEqual(session.engine_version, "v2")

    def test_explicit_off_routes_new_session_to_legacy(self):
        session = _build_session()
        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            ensure_engine_version_set(session)
        session.refresh_from_db()
        self.assertEqual(session.engine_version, "legacy")

    def test_off_does_not_break_in_flight_v2_session(self):
        # A pre-existing v2 session must continue on v2 even when the
        # kill switch is pulled mid-pilot.
        session = _build_session(engine_version="v2")
        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            ensure_engine_version_set(session)
        session.refresh_from_db()
        self.assertEqual(session.engine_version, "v2")
        self.assertTrue(is_v2_session(session))


class ResumeStickyTest(TestCase):
    def test_session_on_legacy_resumes_on_legacy(self):
        session = _build_session(engine_version="legacy")
        with patch.dict("os.environ", {"NEW_TUTOR": "on"}):
            ensure_engine_version_set(session)
        session.refresh_from_db()
        self.assertEqual(session.engine_version, "legacy")
        self.assertFalse(is_v2_session(session))

    def test_session_on_v2_resumes_on_v2(self):
        session = _build_session(engine_version="v2")
        with patch.dict("os.environ", {"NEW_TUTOR": "off"}):
            ensure_engine_version_set(session)
        session.refresh_from_db()
        self.assertEqual(session.engine_version, "v2")
        self.assertTrue(is_v2_session(session))


class ResumeArtifactPreservationTest(TestCase):
    def test_open_question_re_rendered_with_options_and_media_no_canonical_leak(self):
        # Build a v2 session with an OpenQuestion that has MCQ options
        # + attached media + a private canonical.
        session = _build_session(engine_version="v2")
        state = SessionRuntimeState(
            open_question=OpenQuestion(
                source=QuestionSource.EXIT_TICKET_QUESTION,
                id=42,
                canonical="x = 12",  # PRIVATE — must NOT appear in resume output
                rendered_stem="What is the value of x when 2x + 3 = 27?",
                jaccard_signature="sig",
                visible_context_at_pose=VisibleContextSnapshot(
                    visible_prompt="Solve for x.",
                    attached_media_ids=[101, 102],
                    recent_transcript=["[tutor] Set up the equation."],
                    mcq_option_order=["A) 6", "B) 12", "C) 15", "D) 24"],
                ),
            ),
        )
        session.runtime_state = state.to_jsonable()
        session.save(update_fields=["runtime_state"])

        envelope = v2_resume_dispatch(session)

        # Verbatim stem.
        self.assertIn("What is the value of x when 2x + 3 = 27?", envelope["message"])
        # MCQ options preserved in order.
        self.assertEqual(
            envelope["mcq_options"],
            ["A) 6", "B) 12", "C) 15", "D) 24"],
        )
        # Media IDs preserved.
        self.assertEqual(envelope["attached_media_ids"], [101, 102])
        # Private canonical does NOT leak.
        self.assertNotIn("x = 12", envelope["message"])
        # Source + id surfaced for the frontend artifact panel.
        self.assertEqual(
            envelope["open_question"]["source"], "exit_ticket_question",
        )
        self.assertEqual(envelope["open_question"]["id"], 42)

    def test_no_open_question_poses_next_question_on_resume(self):
        """With no committed open question, resume continues the lesson by
        posing the next question (open_question_authority_redesign.md §5
        step 3) — not the old dead-end statement. Delegates to the
        state-driven opening-turn path; mocked here so the unit test
        makes no LLM calls.
        """
        session = _build_session(engine_version="v2")
        posed = {
            "session_id": session.id,
            "message": "A bearing of 180° points in which compass direction?",
            "phase": "engage",
            "selected_move": "confirm_and_advance",
        }
        with patch(
            "apps.tutoring.v2.routing.v2_start_dispatch", return_value=posed,
        ) as start_mock:
            envelope = v2_resume_dispatch(session)
        start_mock.assert_called_once_with(session)
        # Resume delegated to the pose-next path and tagged the envelope.
        self.assertEqual(
            envelope["message"],
            "A bearing of 180° points in which compass direction?",
        )
        self.assertTrue(envelope["resume"])
        # No artifact-preservation fields when there was no open question.
        self.assertNotIn("mcq_options", envelope)
        self.assertNotIn("attached_media_ids", envelope)
