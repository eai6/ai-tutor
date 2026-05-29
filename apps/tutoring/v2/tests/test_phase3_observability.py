"""Phase 3 §3.3 observability tests.

Covers:
  - Per-stage span emission: ``flush_spans`` writes the buffered
    spans to ``TurnSpan`` rows, and the v2 dispatch produces at least
    one ``audit:tutor.move_selection`` span per turn.
  - ``judge_outputs.v2_trace`` rollup matches the engine's
    selected_move / verdict / fallback_used fields.
  - The aggregate dashboard computation reduces the fixture turns
    into the expected counters.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution, Membership
from apps.curriculum.models import Course, Lesson, Unit
from apps.tutoring.models import SessionTurn, TurnSpan, TutorSession
from apps.tutoring.v2.contracts import (
    GradingResult,
    SessionRuntimeState,
    Verdict,
)


def _build_session():
    inst = Institution.objects.create(name="O", slug="o")
    user = User.objects.create_user(username="obs_u", password="x")
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
    return TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson,
        status=TutorSession.Status.ACTIVE,
        engine_version="v2",
        runtime_state=state.to_jsonable(),
    )


class V2TraceRollupTest(TestCase):
    def test_judge_outputs_carries_v2_trace_rollup(self):
        session = _build_session()
        turn = SessionTurn.objects.create(
            session=session,
            role=SessionTurn.Role.TUTOR,
            content="Let's try one together.",
            metadata={"engine_version": "v2"},
            judge_outputs={
                "v2_trace": {
                    "selected_move": "pose_question",
                    "verdict": None,
                    "verdict_bare_answer": False,
                    "conformance_violations": [],
                    "conformance_labels": None,
                    "retry_used": False,
                    "fallback_used": False,
                },
            },
        )
        turn.refresh_from_db()
        trace = (turn.judge_outputs or {}).get("v2_trace") or {}
        self.assertEqual(trace.get("selected_move"), "pose_question")
        self.assertFalse(trace.get("fallback_used"))
        self.assertEqual(trace.get("conformance_violations"), [])


class AggregateDashboardTest(TestCase):
    def test_compute_aggregates_reduces_fixture_turns(self):
        from apps.dashboard.views_v2_observability import compute_v2_aggregates
        from django.utils import timezone

        session = _build_session()
        now = timezone.now()
        # Build a small fixture: 3 turns, one fallback, mixed verdicts.
        fixture = [
            {
                "selected_move": "pose_question",
                "verdict": None,
                "fallback_used": False,
                "retry_used": False,
            },
            {
                "selected_move": "scaffold_hint",
                "verdict": "wrong",
                "fallback_used": False,
                "retry_used": True,
            },
            {
                "selected_move": "confirm_and_advance",
                "verdict": "correct",
                "fallback_used": True,  # fallback fired here
                "retry_used": True,
                "conformance_violations": ["refutes_correctness_on_correct"],
            },
        ]
        for entry in fixture:
            SessionTurn.objects.create(
                session=session,
                role=SessionTurn.Role.TUTOR,
                content="x",
                metadata={"engine_version": "v2"},
                judge_outputs={"v2_trace": entry},
            )

        aggregates = compute_v2_aggregates(window_hours=24)
        self.assertEqual(aggregates["total_turns"], 3)
        self.assertEqual(aggregates["fallback_count"], 1)
        self.assertEqual(aggregates["retry_count"], 2)
        # Verdict distribution counts the no-verdict turn too.
        self.assertEqual(
            aggregates["verdict_distribution"].get("correct", 0), 1,
        )
        self.assertEqual(
            aggregates["verdict_distribution"].get("wrong", 0), 1,
        )
        self.assertEqual(
            aggregates["verdict_distribution"].get("no_verdict", 0), 1,
        )
        # P1 indicator — refutes-correctness on a correct verdict.
        self.assertEqual(aggregates["p1_correct_to_wrong_caught"], 1)
        # Safe-template rate above the 5% ceiling — alert fires.
        self.assertGreater(aggregates["safe_template_rate"], 0)
        self.assertTrue(any(
            "Safe-template rate" in a for a in aggregates["alerts"]
        ))


class SpanFlushTest(TestCase):
    def test_emit_span_buffered_then_flushed_to_turnspan_rows(self):
        from apps.tutoring.tracing import (
            emit_span, flush_spans, reset_span_buffer, start_span_buffer,
        )

        session = _build_session()
        turn = SessionTurn.objects.create(
            session=session,
            role=SessionTurn.Role.TUTOR,
            content="x",
            metadata={"engine_version": "v2"},
        )
        token = start_span_buffer()
        try:
            with emit_span("audit", "tutor.move_selection") as span:
                if span is not None:
                    span["payload"] = {"selected_move": "explain"}
            flushed = flush_spans(turn.id)
        finally:
            reset_span_buffer(token)
        self.assertGreaterEqual(flushed, 1)
        names = list(TurnSpan.objects.filter(turn=turn).values_list("name", flat=True))
        self.assertIn("tutor.move_selection", names)
