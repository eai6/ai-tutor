"""Lifted-forward utilities — re-exports of existing module-level helpers.

Per Phase 1 operating principle 2: deterministic utilities that
``refactor-analysis.md`` §3 deletion table calls out keep their
existing modules and get imported by the new services. They are NOT
re-implemented. This package is the stable import surface for Phase 2.

If you need to swap an implementation, change the re-export here; do
not branch the consumer.
"""

# Bank grader — numeric ±0.01, MCQ letter match (Phase 2 §2.1.1).
from apps.tutoring.bank_grader import (  # noqa: F401
    BankGradeResult,
    grade_bank_response,
    grade_lesson_step_response,
)
# Question abstraction.
from apps.tutoring.question import Question  # noqa: F401
# Working analyzer — bare-answer detection signal lives inside this
# module (Phase 2 §2.1.1).
from apps.tutoring import student_working_analyzer  # noqa: F401
# Repeated-question Jaccard signature (Phase 1 §4.3, Phase 2 §2.1.1).
from apps.tutoring import repeated_question  # noqa: F401
# Praise filter (Phase 2 §2.4 conformance check).
from apps.tutoring import praise_filter  # noqa: F401
# Answer-leak scoped post-check (Phase 2 §2.4 conformance).
from apps.tutoring import answer_leak  # noqa: F401

__all__ = [
    "BankGradeResult",
    "Question",
    "answer_leak",
    "grade_bank_response",
    "grade_lesson_step_response",
    "praise_filter",
    "repeated_question",
    "student_working_analyzer",
]
