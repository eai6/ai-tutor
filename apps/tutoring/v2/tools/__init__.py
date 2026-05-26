"""v2 tool layer — pose_question + pose_inline_question with backend
provenance enforcement.

Per Phase 1 §4:
  - Tool schemas accept only ``question_ref`` (typed) or
    ``pre_pose_token`` (opaque). They REFUSE ``correct_answer``.
  - Two-phase commit — Phase A validates without mutating state and
    without consuming the single-use token; Phase B commits via
    ``ContextManager.commit_pending_pose(...)`` after structural
    conformance approves the response.
  - Repeat guards (``in_session_repeat_guard``,
    ``cross_session_repeat_guard``) run at the tool boundary
    *independently* of ``BANK_PREPOSE_RECHECK`` (Phase 1 §4.3).

Legacy ``conversational_tutor.py`` keeps its raw-``correct_answer``
schema and stays unaffected.
"""

from apps.tutoring.v2.tools.math_verification import MathVerificationTool
from apps.tutoring.v2.tools.pose_question import (
    POSE_QUESTION_LLM_TOOL_NAME,
    PoseInlineQuestionTool,
    PoseQuestionTool,
    PoseQuestionToolArgs,
    ToolRejection,
    build_anthropic_pose_question_tool,
    make_resolve_canonical_for_lesson,
    validate_pose,
)
from apps.tutoring.v2.tools.repeat_guards import (
    cross_session_repeat_guard,
    in_session_repeat_guard,
)
from apps.tutoring.v2.tools.token_cache import (
    TokenAlreadyConsumed,
    TokenInvalid,
    token_cache,
)

__all__ = [
    "MathVerificationTool",
    "POSE_QUESTION_LLM_TOOL_NAME",
    "PoseInlineQuestionTool",
    "PoseQuestionTool",
    "PoseQuestionToolArgs",
    "ToolRejection",
    "TokenAlreadyConsumed",
    "TokenInvalid",
    "build_anthropic_pose_question_tool",
    "cross_session_repeat_guard",
    "in_session_repeat_guard",
    "make_resolve_canonical_for_lesson",
    "token_cache",
    "validate_pose",
]
