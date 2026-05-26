"""Repeat-guard isolation tests.

Per Phase 1 §4.3: both guards run identically regardless of
``BANK_PREPOSE_RECHECK``. The derivability check is the only
validation that flag can disable; repeat prevention is always on.
"""

from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from apps.tutoring.v2.contracts import (
    PosedQuestionLedgerEntry,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
)
from apps.tutoring.v2.tools.repeat_guards import (
    canonicalize_stem,
    cross_session_repeat_guard,
    in_session_repeat_guard,
)
from apps.tutoring.v2.tools.pose_question import validate_pose
from apps.tutoring.v2.tools.token_cache import token_cache


class InSessionRepeatGuardTest(TestCase):
    def test_same_ref_refused(self):
        ledger = [
            PosedQuestionLedgerEntry(
                source=QuestionSource.LESSON_STEP,
                id=1,
                jaccard_signature="what is 2 2",
            )
        ]
        result = in_session_repeat_guard(
            candidate_signature="what is 2 2",
            ledger=ledger,
            candidate_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=1),
        )
        self.assertTrue(result.refused)
        self.assertIn("same_ref", result.reason)

    def test_jaccard_paraphrase_refused(self):
        ledger = [
            PosedQuestionLedgerEntry(
                source=QuestionSource.LESSON_STEP,
                id=1,
                jaccard_signature=canonicalize_stem("Find the area of a square with side 5"),
            )
        ]
        candidate_sig = canonicalize_stem(
            "Find the area of a square with side 5"
        )
        result = in_session_repeat_guard(
            candidate_signature=candidate_sig,
            ledger=ledger,
            candidate_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=2),
        )
        self.assertTrue(result.refused)
        self.assertIn("jaccard", result.reason)

    def test_distinct_question_allowed(self):
        ledger = [
            PosedQuestionLedgerEntry(
                source=QuestionSource.LESSON_STEP,
                id=1,
                jaccard_signature=canonicalize_stem("What is 2+2?"),
            )
        ]
        result = in_session_repeat_guard(
            candidate_signature=canonicalize_stem(
                "Name the capital of Tanzania"
            ),
            ledger=ledger,
            candidate_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=2),
        )
        self.assertFalse(result.refused)


class CrossSessionRepeatGuardTest(TestCase):
    def test_empty_map_allows(self):
        result = cross_session_repeat_guard(
            candidate_ref=QuestionRef(source=QuestionSource.EXIT_TICKET_QUESTION, id=7),
            asked_questions={},
        )
        self.assertFalse(result.refused)

    def test_inside_window_refused(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=2)).isoformat()
        result = cross_session_repeat_guard(
            candidate_ref=QuestionRef(source=QuestionSource.EXIT_TICKET_QUESTION, id=7),
            asked_questions={"exit_ticket_question:7": {"last_asked_at": recent}},
            now=now,
        )
        self.assertTrue(result.refused)
        self.assertIn("cross_session_repeat", result.reason)

    def test_outside_window_allowed(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=30)).isoformat()
        result = cross_session_repeat_guard(
            candidate_ref=QuestionRef(source=QuestionSource.EXIT_TICKET_QUESTION, id=7),
            asked_questions={"exit_ticket_question:7": {"last_asked_at": old}},
            now=now,
        )
        self.assertFalse(result.refused)

    def test_missing_key_allowed(self):
        result = cross_session_repeat_guard(
            candidate_ref=QuestionRef(source=QuestionSource.EXIT_TICKET_QUESTION, id=99),
            asked_questions={"exit_ticket_question:7": {"last_asked_at": "2026-05-20T00:00:00"}},
        )
        self.assertFalse(result.refused)


class RepeatGuardsIndependentOfBankRecheckTest(TestCase):
    """Both guards run identically with BANK_PREPOSE_RECHECK on AND off."""

    def setUp(self):
        token_cache._reset()
        self.state = SessionRuntimeState(
            posed_question_ledger=[
                PosedQuestionLedgerEntry(
                    source=QuestionSource.LESSON_STEP,
                    id=1,
                    jaccard_signature=canonicalize_stem("What is 2+2?"),
                )
            ]
        )

    def _raw(self):
        return {
            "question_ref": {"source": "lesson_step", "id": 1},
            "rendered_stem": "What is 2+2?",
        }

    def test_in_session_guard_fires_with_flag_off(self):
        with patch.dict("os.environ", {"BANK_PREPOSE_RECHECK": "off"}):
            from apps.tutoring.v2.tools.pose_question import ToolRejection

            result = validate_pose(
                session_id=1,
                student_id=2,
                raw_args=self._raw(),
                runtime_state=self.state,
                asked_questions={},
                resolve_canonical=lambda r: "4",
                pre_pose_check=lambda **kw: None,
            )
            self.assertIsInstance(result, ToolRejection)
            self.assertEqual(result.reason, "in_session_repeat")

    def test_in_session_guard_fires_with_flag_on(self):
        with patch.dict("os.environ", {"BANK_PREPOSE_RECHECK": "on"}):
            from apps.tutoring.v2.tools.pose_question import ToolRejection

            result = validate_pose(
                session_id=1,
                student_id=2,
                raw_args=self._raw(),
                runtime_state=self.state,
                asked_questions={},
                resolve_canonical=lambda r: "4",
                pre_pose_check=lambda **kw: None,
            )
            self.assertIsInstance(result, ToolRejection)
            self.assertEqual(result.reason, "in_session_repeat")

    def test_cross_session_guard_fires_with_flag_off(self):
        from apps.tutoring.v2.tools.pose_question import ToolRejection

        empty_state = SessionRuntimeState()
        now_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with patch.dict("os.environ", {"BANK_PREPOSE_RECHECK": "off"}):
            result = validate_pose(
                session_id=1,
                student_id=2,
                raw_args={
                    "question_ref": {"source": "lesson_step", "id": 1},
                    "rendered_stem": "Some other question",
                },
                runtime_state=empty_state,
                asked_questions={"lesson_step:1": {"last_asked_at": now_iso}},
                resolve_canonical=lambda r: "4",
                pre_pose_check=lambda **kw: None,
            )
            self.assertIsInstance(result, ToolRejection)
            self.assertEqual(result.reason, "cross_session_repeat")


class BankPreposeRecheckFlagRoutingTest(TestCase):
    """Phase 1: with the flag on, the derivability routing happens;
    with the flag off, it does not."""

    def setUp(self):
        token_cache._reset()
        self._calls: list[int] = []

    def _hook(self, **kw):
        self._calls.append(1)
        # Phase 1 grader stub raises NotImplementedError — but the
        # tool layer swallows that specifically.
        raise NotImplementedError("stub")

    def _raw(self):
        return {
            "question_ref": {"source": "lesson_step", "id": 5},
            "rendered_stem": "novel question",
        }

    def test_flag_on_routes_to_pre_pose_check(self):
        with patch.dict("os.environ", {"BANK_PREPOSE_RECHECK": "on"}):
            validate_pose(
                session_id=1,
                student_id=2,
                raw_args=self._raw(),
                runtime_state=SessionRuntimeState(),
                asked_questions={},
                resolve_canonical=lambda r: "X",
                pre_pose_check=self._hook,
            )
        self.assertEqual(len(self._calls), 1)

    def test_flag_off_skips_pre_pose_check(self):
        with patch.dict("os.environ", {"BANK_PREPOSE_RECHECK": "off"}):
            validate_pose(
                session_id=1,
                student_id=2,
                raw_args=self._raw(),
                runtime_state=SessionRuntimeState(),
                asked_questions={},
                resolve_canonical=lambda r: "X",
                pre_pose_check=self._hook,
            )
        self.assertEqual(len(self._calls), 0)
