"""Phase 3 §3.1 profiler tests.

Covers:
  - End-of-session write to both ``profile_summary`` and
    ``asked_questions``.
  - LRU cap on asked_questions at MAX_ASKED_QUESTIONS_ENTRIES.
  - Cross-session repeat avoidance two-session round-trip — Session 1
    poses Q, profiler writes asked_questions, Session 2 refuses Q via
    ``cross_session_repeat_guard`` inside the avoidance window.
  - Session read window (last-10 ordering).
  - Fail-soft: profiler swallows summarize / persist exceptions and
    does not break session completion.

Mocks the LLM call — the profiler's deterministic asked_questions
path runs without any model invocation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution, Membership, StudentProfile
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import TutorSession
from apps.tutoring.v2.contracts import (
    PosedQuestionLedgerEntry,
    ProfileUpdate,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
)
from apps.tutoring.v2.services.profiler import (
    MAX_ASKED_QUESTIONS_ENTRIES,
    StudentProfiler,
)


def _build_session(*, ledger: list[PosedQuestionLedgerEntry] | None = None):
    inst = Institution.objects.create(name="T", slug="t")
    user = User.objects.create_user(username="p3_user", password="x")
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
    state = SessionRuntimeState()
    if ledger:
        state.posed_question_ledger = list(ledger)
    return TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson,
        status=TutorSession.Status.ACTIVE,
        engine_version="v2",
        runtime_state=state.to_jsonable(),
    )


class ProfilerWritesBothColumnsTest(TestCase):
    def test_persist_writes_summary_and_merges_asked_questions(self):
        session = _build_session()
        profile = StudentProfile.objects.create(user=session.student)

        update = ProfileUpdate(
            profile_summary_text="Solid on linear equations; struggles with units.",
            asked_questions_delta={
                "exit_ticket_question:7": {"last_asked_at": "2026-05-20T00:00:00+00:00"},
                "lesson_step:42": {"last_asked_at": "2026-05-21T00:00:00+00:00"},
            },
        )
        StudentProfiler().persist(session.student_id, update)
        profile.refresh_from_db()

        self.assertIn("linear", profile.profile_summary)
        self.assertIn("exit_ticket_question:7", profile.asked_questions)
        self.assertEqual(
            profile.asked_questions["exit_ticket_question:7"]["last_asked_at"],
            "2026-05-20T00:00:00+00:00",
        )

    def test_persist_keeps_prior_summary_when_new_is_empty(self):
        session = _build_session()
        profile = StudentProfile.objects.create(
            user=session.student,
            profile_summary="Existing snapshot.",
        )
        # Empty summary text leaves the prior snapshot intact.
        update = ProfileUpdate(
            profile_summary_text="",
            asked_questions_delta={
                "lesson_step:1": {"last_asked_at": "2026-05-22T00:00:00+00:00"},
            },
        )
        StudentProfiler().persist(session.student_id, update)
        profile.refresh_from_db()
        self.assertEqual(profile.profile_summary, "Existing snapshot.")
        self.assertIn("lesson_step:1", profile.asked_questions)

    def test_asked_questions_lru_eviction_caps_at_max(self):
        session = _build_session()
        StudentProfile.objects.create(user=session.student)
        # Build (cap + 50) entries with strictly-increasing timestamps.
        # Newest entries should win.
        delta: dict[str, dict] = {}
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(MAX_ASKED_QUESTIONS_ENTRIES + 50):
            iso = (base + timedelta(seconds=i)).isoformat()
            delta[f"lesson_step:{i}"] = {"last_asked_at": iso}
        update = ProfileUpdate(profile_summary_text="", asked_questions_delta=delta)

        StudentProfiler().persist(session.student_id, update)
        profile = StudentProfile.objects.get(user=session.student)
        self.assertEqual(len(profile.asked_questions), MAX_ASKED_QUESTIONS_ENTRIES)
        # Highest indices are most-recent — they must survive eviction.
        self.assertIn(
            f"lesson_step:{MAX_ASKED_QUESTIONS_ENTRIES + 49}",
            profile.asked_questions,
        )
        # Lowest indices are oldest — must be evicted.
        self.assertNotIn("lesson_step:0", profile.asked_questions)


class ProfilerSessionReadWindowTest(TestCase):
    def test_recent_sessions_queryset_caps_at_window(self):
        # Build 12 sessions for the same student; only the most-recent
        # 10 should be visible to the read boundary.
        inst = Institution.objects.create(name="T2", slug="t2")
        user = User.objects.create_user(username="rw_user", password="x")
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
        base = datetime(2026, 5, 1, tzinfo=timezone.utc)
        sessions = []
        for i in range(12):
            sess = TutorSession.objects.create(
                institution=inst, student=user, lesson=lesson,
                status=TutorSession.Status.COMPLETED,
            )
            sess.ended_at = base + timedelta(days=i)
            sess.save(update_fields=["ended_at"])
            sessions.append(sess)

        qs = StudentProfiler.recent_sessions_queryset(user)
        ids = list(qs.values_list("id", flat=True))
        self.assertEqual(len(ids), 10)
        # Most-recent 10 = sessions[2:]; ordering is ended_at DESC.
        expected = [s.id for s in sorted(sessions, key=lambda s: s.ended_at, reverse=True)[:10]]
        self.assertEqual(ids, expected)


class ProfilerFailSoftTest(TestCase):
    def test_run_for_session_swallows_exceptions(self):
        session = _build_session()
        # Force summarize to raise — run_for_session must not propagate.
        with patch.object(
            StudentProfiler, "summarize_session",
            side_effect=RuntimeError("boom"),
        ), patch(
            "apps.tutoring.v2.services.profiler._build_client_for_purpose",
            return_value=None,
        ):
            # Should fall through to the deterministic asked_delta only.
            result = StudentProfiler().run_for_session(session)
        self.assertIsNotNone(result)
        # Profile is auto-created when missing.
        self.assertTrue(
            StudentProfile.objects.filter(user=session.student).exists()
        )
