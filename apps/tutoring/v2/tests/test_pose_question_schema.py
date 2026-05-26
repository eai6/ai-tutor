"""Tool-schema rejection + two-phase commit semantics tests.

Phase 1 §4 / §4.2 / §4.3 contract tests. The grader's
``pre_pose_check`` is a NotImplementedError stub in Phase 1; these
tests use simple lambdas in place of the grader to assert routing
behavior, not grader outcomes.
"""

from unittest import TestCase
from unittest.mock import patch

from apps.tutoring.v2.contracts import (
    OpenQuestion,
    PendingPose,
    PosedQuestionLedgerEntry,
    QuestionRef,
    QuestionSource,
    SessionRuntimeState,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.tools.pose_question import (
    PoseQuestionToolArgs,
    ToolRejection,
    validate_pose,
)
from apps.tutoring.v2.tools.token_cache import token_cache


# ----------------------------------------------------------------------
# Test fixtures / helpers
# ----------------------------------------------------------------------


def _resolve_ok(question_ref: QuestionRef) -> str:
    return "42"


def _resolve_missing(question_ref: QuestionRef) -> str:
    raise LookupError(f"no row for {question_ref.composite_key()}")


def _resolve_empty(question_ref: QuestionRef) -> str:
    return ""


_called: dict[str, int] = {"count": 0}


def _stub_pre_pose_check(**kwargs):
    _called["count"] += 1
    raise NotImplementedError("Phase 2 supplies the real implementation")


def _bad_pre_pose_check(**kwargs):
    raise RuntimeError("not derivable from visible context")


# ----------------------------------------------------------------------
# Schema-layer rejection
# ----------------------------------------------------------------------


class SchemaRejectionTest(TestCase):
    def test_correct_answer_field_refused(self):
        with self.assertRaises(Exception):
            PoseQuestionToolArgs(
                question_ref=QuestionRef(
                    source=QuestionSource.LESSON_STEP, id=1
                ),
                correct_answer="42",  # extra='forbid'
            )

    def test_must_supply_exactly_one_provenance(self):
        with self.assertRaises(Exception):
            PoseQuestionToolArgs()
        with self.assertRaises(Exception):
            PoseQuestionToolArgs(
                question_ref=QuestionRef(
                    source=QuestionSource.LESSON_STEP, id=1
                ),
                pre_pose_token="t",
            )

    def test_valid_question_ref(self):
        args = PoseQuestionToolArgs(
            question_ref=QuestionRef(source=QuestionSource.LESSON_STEP, id=1),
            rendered_stem="What is 2+2?",
        )
        self.assertEqual(args.question_ref.id, 1)

    def test_valid_pre_pose_token(self):
        args = PoseQuestionToolArgs(
            pre_pose_token="some-token",
            rendered_stem="Inline q",
        )
        self.assertEqual(args.pre_pose_token, "some-token")


# ----------------------------------------------------------------------
# validate_pose — Phase A routing
# ----------------------------------------------------------------------


class ValidatePoseRoutingTest(TestCase):
    def setUp(self):
        token_cache._reset()
        _called["count"] = 0
        self.session_id = 11
        self.student_id = 22
        self.state = SessionRuntimeState()

    def _raw_bank(self, **overrides):
        base = {
            "question_ref": {"source": "lesson_step", "id": 1},
            "rendered_stem": "What is 2+2?",
            "attached_media_ids": [],
            "recent_transcript": [],
            "mcq_option_order": [],
        }
        base.update(overrides)
        return base

    def test_bank_path_returns_pending_pose(self):
        with patch.dict("os.environ", {"BANK_PREPOSE_RECHECK": "on"}):
            result = validate_pose(
                session_id=self.session_id,
                student_id=self.student_id,
                raw_args=self._raw_bank(),
                runtime_state=self.state,
                asked_questions={},
                resolve_canonical=_resolve_ok,
                pre_pose_check=_stub_pre_pose_check,
            )
        self.assertIsInstance(result, PendingPose)
        # In Phase 1 the grader is a stub; the test asserts routing.
        self.assertEqual(_called["count"], 1)
        self.assertEqual(result.canonical, "42")

    def test_bank_path_ref_unresolved(self):
        with patch.dict("os.environ", {"BANK_PREPOSE_RECHECK": "on"}):
            result = validate_pose(
                session_id=self.session_id,
                student_id=self.student_id,
                raw_args=self._raw_bank(),
                runtime_state=self.state,
                asked_questions={},
                resolve_canonical=_resolve_missing,
                pre_pose_check=_stub_pre_pose_check,
            )
        self.assertIsInstance(result, ToolRejection)
        self.assertEqual(result.reason, "ref_unresolved")

    def test_bank_path_empty_canonical_rejected(self):
        with patch.dict("os.environ", {"BANK_PREPOSE_RECHECK": "on"}):
            result = validate_pose(
                session_id=self.session_id,
                student_id=self.student_id,
                raw_args=self._raw_bank(),
                runtime_state=self.state,
                asked_questions={},
                resolve_canonical=_resolve_empty,
                pre_pose_check=_stub_pre_pose_check,
            )
        self.assertIsInstance(result, ToolRejection)
        self.assertEqual(result.reason, "ref_unresolved")

    def test_correct_answer_field_rejected_at_validate(self):
        raw = self._raw_bank(correct_answer="42")
        result = validate_pose(
            session_id=self.session_id,
            student_id=self.student_id,
            raw_args=raw,
            runtime_state=self.state,
            asked_questions={},
            resolve_canonical=_resolve_ok,
            pre_pose_check=_stub_pre_pose_check,
        )
        self.assertIsInstance(result, ToolRejection)
        self.assertEqual(result.reason, "schema_invalid")

    def test_pre_pose_token_path_peeks_without_consuming(self):
        token = token_cache.issue(
            session_id=self.session_id,
            canonical="9",
            visible_context_json="{}",
        )
        result = validate_pose(
            session_id=self.session_id,
            student_id=self.student_id,
            raw_args={
                "pre_pose_token": token,
                "rendered_stem": "inline?",
                "attached_media_ids": [],
                "recent_transcript": [],
                "mcq_option_order": [],
            },
            runtime_state=self.state,
            asked_questions={},
            resolve_canonical=_resolve_ok,
            pre_pose_check=_stub_pre_pose_check,
        )
        self.assertIsInstance(result, PendingPose)
        self.assertEqual(result.canonical, "9")
        # Token must still be present + unconsumed.
        entry = token_cache.peek(self.session_id, token)
        self.assertFalse(entry.consumed)
        # Bank-path derivability is skipped on the token path.
        self.assertEqual(_called["count"], 0)

    def test_expired_token_rejected(self):
        # Forge a token from a different session — verification fails.
        forged = token_cache.issue(
            session_id=99, canonical="x", visible_context_json="{}"
        )
        result = validate_pose(
            session_id=self.session_id,
            student_id=self.student_id,
            raw_args={
                "pre_pose_token": forged,
                "rendered_stem": "inline?",
            },
            runtime_state=self.state,
            asked_questions={},
            resolve_canonical=_resolve_ok,
            pre_pose_check=_stub_pre_pose_check,
        )
        self.assertIsInstance(result, ToolRejection)
        self.assertEqual(result.reason, "token_invalid")


# ----------------------------------------------------------------------
# Phase B commit — two-phase semantics
# ----------------------------------------------------------------------


class TwoPhaseCommitSemanticsTest(TestCase):
    """Phase A validation must NOT mutate state; only commit does.

    Uses a duck-typed session object so Django DB isn't required here.
    """

    def setUp(self):
        token_cache._reset()

    def test_validate_does_not_consume_token_or_touch_ledger(self):
        from apps.tutoring.v2.tools.token_cache import token_cache as tc

        token = tc.issue(session_id=1, canonical="z",
                         visible_context_json="{}")
        state = SessionRuntimeState()
        result = validate_pose(
            session_id=1,
            student_id=2,
            raw_args={"pre_pose_token": token, "rendered_stem": "?"},
            runtime_state=state,
            asked_questions={},
            resolve_canonical=_resolve_ok,
            pre_pose_check=_stub_pre_pose_check,
        )
        self.assertIsInstance(result, PendingPose)
        # Ledger untouched.
        self.assertEqual(state.posed_question_ledger, [])
        # Token unconsumed.
        self.assertFalse(tc.peek(1, token).consumed)
        # No open_question written.
        self.assertIsNone(state.open_question)

    def test_commit_consumes_token_and_appends_ledger(self):
        from apps.tutoring.v2.services.context_manager import ContextManager
        from apps.tutoring.v2.tools.token_cache import token_cache as tc

        # Fake session whose runtime_state is a dict; ContextManager
        # uses model_dump JSON.
        class _FakeSess:
            id = 11
            runtime_state: dict = {}

            def save(self, update_fields=None):
                pass

        sess = _FakeSess()
        token = tc.issue(session_id=11, canonical="9",
                         visible_context_json="{}")
        pending = PendingPose(
            question_ref=QuestionRef(
                source=QuestionSource.PRE_POSE_TOKEN, id=0
            ),
            canonical="9",
            rendered_stem="inline?",
            jaccard_signature="inline",
            visible_context=VisibleContextSnapshot(visible_prompt="inline?"),
            token=token,
        )
        cm = ContextManager(sess)
        state = cm.commit_pending_pose(pending)
        self.assertEqual(len(state.posed_question_ledger), 1)
        self.assertIsNotNone(state.open_question)
        self.assertEqual(state.open_question.canonical, "9")
        # Second commit on the same token must reject.
        from apps.tutoring.v2.tools.token_cache import TokenAlreadyConsumed

        with self.assertRaises(TokenAlreadyConsumed):
            cm.commit_pending_pose(pending)
