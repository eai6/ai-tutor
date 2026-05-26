"""Round-trip tests for every Pydantic contract.

serialize → JSON-able dict → deserialize → equality.
"""

from datetime import datetime
from unittest import TestCase

from apps.tutoring.v2.contracts import (
    BareAnswerCounters,
    GradingRequest,
    GradingResult,
    ObjectiveProgress,
    OpenQuestion,
    PendingPose,
    PosedQuestionLedgerEntry,
    ProfileUpdate,
    QuestionRef,
    QuestionSource,
    RemediationState,
    ResumeMarker,
    SafetyValveCounters,
    SessionRuntimeState,
    TutoringContext,
    Verdict,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.contracts.tutoring import StudentSafeFeedback


class SessionRuntimeStateRoundtripTest(TestCase):
    def test_empty_state_roundtrip(self):
        state = SessionRuntimeState()
        payload = state.to_jsonable()
        self.assertIsInstance(payload, dict)
        revived = SessionRuntimeState.from_jsonable(payload)
        self.assertEqual(state, revived)

    def test_from_jsonable_tolerates_empty(self):
        self.assertEqual(SessionRuntimeState.from_jsonable(None),
                         SessionRuntimeState())
        self.assertEqual(SessionRuntimeState.from_jsonable({}),
                         SessionRuntimeState())

    def test_populated_state_roundtrip(self):
        snap = VisibleContextSnapshot(
            visible_prompt="What is 2+3?",
            attached_media_ids=[7, 8],
            recent_transcript=["s: hi", "t: hello"],
            mcq_option_order=["A", "B"],
        )
        oq = OpenQuestion(
            source=QuestionSource.LESSON_STEP,
            id=42,
            canonical="5",
            rendered_stem="What is 2+3?",
            jaccard_signature="what is 2 3",
            visible_context_at_pose=snap,
        )
        ledger = [
            PosedQuestionLedgerEntry(
                source=QuestionSource.EXIT_TICKET_QUESTION,
                id=7,
                jaccard_signature="x y z",
            )
        ]
        op = {"obj1": ObjectiveProgress(objective="obj1", attempts=2, correct=1)}
        state = SessionRuntimeState(
            open_question=oq,
            attempts_on_open_question=2,
            posed_question_ledger=ledger,
            objective_progress=op,
            media_shown=[1, 2, 3],
            remediation_state=RemediationState(misconception="off-by-one",
                                               fired_at_turn=5),
            current_move="scaffold_hint",
            move_history=["pose_question", "scaffold_hint"],
            unverified_run_length=1,
            safety_valve_counters=SafetyValveCounters(turns_in_session=4),
            resume_marker=ResumeMarker(last_step_index=2, last_move="pose_question"),
            bare_answer_counts_by_objective={"obj1": 3},
        )
        payload = state.to_jsonable()
        revived = SessionRuntimeState.from_jsonable(payload)
        self.assertEqual(revived.open_question.id, 42)
        self.assertEqual(revived.open_question.source,
                         QuestionSource.LESSON_STEP)
        self.assertEqual(revived.bare_answer_counts_by_objective, {"obj1": 3})
        self.assertEqual(revived.current_move, "scaffold_hint")
        self.assertEqual(revived.posed_question_ledger[0].source,
                         QuestionSource.EXIT_TICKET_QUESTION)


class QuestionRefTest(TestCase):
    def test_composite_key(self):
        ref = QuestionRef(source=QuestionSource.EXIT_TICKET_QUESTION, id=7)
        self.assertEqual(ref.composite_key(), "exit_ticket_question:7")

    def test_composite_key_lesson_step(self):
        ref = QuestionRef(source=QuestionSource.LESSON_STEP, id=42)
        self.assertEqual(ref.composite_key(), "lesson_step:42")

    def test_roundtrip(self):
        ref = QuestionRef(source=QuestionSource.INLINE_GENERATED, id=99)
        revived = QuestionRef.model_validate(ref.model_dump(mode="json"))
        self.assertEqual(ref, revived)


class PendingPoseTest(TestCase):
    def test_roundtrip(self):
        pending = PendingPose(
            question_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=1),
            canonical="42",
            rendered_stem="What is the answer?",
            jaccard_signature="what is the answer",
            visible_context=VisibleContextSnapshot(visible_prompt="?"),
        )
        revived = PendingPose.model_validate(pending.model_dump(mode="json"))
        self.assertEqual(revived, pending)


class GradingContractsTest(TestCase):
    def test_grading_result_default_bare_answer_false(self):
        gr = GradingResult(verdict=Verdict.CORRECT)
        self.assertFalse(gr.bare_answer)

    def test_grading_result_with_redacted_feedback(self):
        gr = GradingResult(
            verdict=Verdict.WRONG,
            student_safe_feedback=StudentSafeFeedback(
                what_right="set up correctly",
                what_missing="forgot to carry",
                first_misconception_redacted="check carrying step",
            ),
            bare_answer=True,
        )
        revived = GradingResult.model_validate(gr.model_dump(mode="json"))
        self.assertEqual(revived.verdict, Verdict.WRONG)
        self.assertTrue(revived.bare_answer)
        self.assertEqual(revived.student_safe_feedback.what_missing,
                         "forgot to carry")

    def test_grading_request_frozen(self):
        oq = OpenQuestion(source=QuestionSource.LESSON_STEP, id=1)
        req = GradingRequest(open_question=oq, student_input="5", is_math=True)
        with self.assertRaises(Exception):
            req.student_input = "no"  # frozen

    def test_tutoring_context_frozen(self):
        ctx = TutoringContext(
            session_id=1,
            student_id=2,
            institution_id=3,
            lesson_id=4,
            runtime_state=SessionRuntimeState(),
        )
        with self.assertRaises(Exception):
            ctx.session_id = 99

    def test_profile_update_default(self):
        upd = ProfileUpdate()
        self.assertEqual(upd.profile_summary_text, "")
        self.assertEqual(upd.asked_questions_delta, {})
