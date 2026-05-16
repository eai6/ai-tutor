"""Pydantic schemas for structured-output regen calls.

Used with `instructor.from_provider(...).chat.completions.create(
    response_model=...
)` so the LLM output is validated against the schema before return —
eliminates the unparseable_json failure class.

See auto-memory/feedback_use_instructor_for_structured_output.md for
why this lives separately and the broader rollout convention.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MCQRewrite(BaseModel):
    """Rewritten MCQ exit-ticket question. Returned by judge-driven
    exit-Q auto regen (`run_exit_question_regen`).
    """
    question_text: str = Field(
        description="The rewritten question stem.",
        max_length=600,
    )
    option_a: str = Field(description="Answer choice A.", max_length=200)
    option_b: str = Field(description="Answer choice B.", max_length=200)
    option_c: str = Field(description="Answer choice C.", max_length=200)
    option_d: str = Field(description="Answer choice D.", max_length=200)
    correct_answer: Literal["A", "B", "C", "D"] = Field(
        description="Letter of the option that is actually correct.",
    )
    explanation: str = Field(
        default="",
        description=(
            "One short sentence on why the correct option is right. "
            "Used by the tutor when the student answers."
        ),
        max_length=500,
    )


__all__ = ["MCQRewrite"]
