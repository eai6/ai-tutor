"""v2 tool layer — pose_question + math verification.

Post-v2-prune step 4: the pose_question tool now takes
``topic_or_subskill`` + ``difficulty_hint`` + ``reason`` and the
backend picks an undelivered LessonStep. Two-phase commit, repeat
guards, and the per-turn rejection feedback loop are gone — the LLM
cannot propose an invalid slot because it never names one.
"""

from apps.tutoring.v2.tools.math_verification import MathVerificationTool
from apps.tutoring.v2.tools.pose_question import (
    POSE_QUESTION_LLM_TOOL_NAME,
    PoseSelection,
    build_pending_pose,
    build_pose_question_tool,
    extract_mcq_letters,
    lookup_lesson_step,
    select_pose_slot,
)

__all__ = [
    "MathVerificationTool",
    "POSE_QUESTION_LLM_TOOL_NAME",
    "PoseSelection",
    "build_pending_pose",
    "build_pose_question_tool",
    "extract_mcq_letters",
    "lookup_lesson_step",
    "select_pose_slot",
]
