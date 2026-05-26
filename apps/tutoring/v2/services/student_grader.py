"""StudentGrader — central correctness service for the v2 engine.

Three responsibilities (refactor-analysis §3):
  1. Student-answer grading (math + non-math paths).
  2. Pre-pose check: derivability invariant + signed pre_pose_token.
  3. Tutor-claim adjudication: same grounded-adjudication machinery as
     the non-math student-grading path.

Phase 1: signatures only. Phase 2: full implementation.
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import (
    GradingRequest,
    GradingResult,
    PendingPose,
    QuestionRef,
    TutoringContext,
)


class StudentGrader:
    """Skeleton. Implemented in Phase 2."""

    def grade_student_response(
        self,
        context: TutoringContext,
        request: GradingRequest,
    ) -> GradingResult:
        """Grade the student's response. Math/non-math/unverified branches."""
        raise NotImplementedError("StudentGrader.grade_student_response — Phase 2")

    def pre_pose_check(
        self,
        context: TutoringContext,
        question_ref: QuestionRef,
        canonical: str,
        visible_prompt: str,
        attached_media_ids: list[int],
        recent_transcript: list[str],
    ) -> str:
        """Derivability gate. Returns a signed single-use token on pass.

        Per Phase 1 §4 / §4.2, the token is signed and cached by
        ``apps.tutoring.v2.tools.token_cache``. Hidden KB chunks must
        be suppressed during the derivation check.
        """
        raise NotImplementedError("StudentGrader.pre_pose_check — Phase 2")

    def adjudicate_tutor_claim(
        self,
        context: TutoringContext,
        claim: str,
    ) -> dict:
        """Returns {status: supported|contradicted|unverified, citation: str}."""
        raise NotImplementedError("StudentGrader.adjudicate_tutor_claim — Phase 2")
