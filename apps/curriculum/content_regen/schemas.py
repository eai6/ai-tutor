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


class FillInBlankRewrite(BaseModel):
    """Rewritten fill-in-the-blank question. Returned by judge-driven
    auto regen (`run_fill_in_blank_regen`).
    """
    question_text: str = Field(
        description="The rewritten question stem / prompt.",
        max_length=600,
    )
    text_template: str = Field(
        description=(
            "The fill-in-the-blank template using ___ for each blank. "
            "Must have the same number of ___ markers as `blanks` "
            "entries below."
        ),
        max_length=1200,
    )
    blanks: list[str] = Field(
        description=(
            "Correct value for each blank, in left-to-right order. "
            "Length must match the number of ___ markers in "
            "text_template."
        ),
        min_length=1,
        max_length=6,
    )
    accept_alternatives: list[list[str]] = Field(
        default_factory=list,
        description=(
            "For each blank, a list of acceptable variants "
            "(synonyms / abbreviations / spelling variants). Outer "
            "list length should match `blanks`; inner lists may be "
            "empty when the blank has no common variants."
        ),
    )
    explanation: str = Field(
        default="",
        description="One short sentence explaining the correct answer(s).",
        max_length=500,
    )


class ShortAnswerRewrite(BaseModel):
    """Rewritten short-answer question. Returned by judge-driven
    auto regen (`run_short_answer_regen`).
    """
    question_text: str = Field(
        description="The rewritten open-ended question prompt.",
        max_length=600,
    )
    model_answer: str = Field(
        description=(
            "The canonical correct answer. Must itself contain (or "
            "imply) the keywords required by the grader so a student "
            "matching the keyword threshold has effectively given the "
            "model answer."
        ),
        max_length=800,
    )
    keywords: list[str] = Field(
        description=(
            "Expected terms the keyword-grader looks for. Should be "
            "specific to the model answer, not generic stopwords."
        ),
        min_length=1,
        max_length=15,
    )
    min_keywords: int = Field(
        default=1,
        description=(
            "How many keywords a student answer must contain to be "
            "marked correct. Usually 1-3."
        ),
        ge=1,
        le=15,
    )
    explanation: str = Field(
        default="",
        description="One short sentence explaining the model answer.",
        max_length=500,
    )


class MatchingPair(BaseModel):
    """One left↔right canonical pair in a matching question."""
    left: str = Field(
        description="Left-side item (the thing being matched).",
        max_length=200,
    )
    right: str = Field(
        description="Right-side item (the canonical match for `left`).",
        max_length=200,
    )


class MatchingRewrite(BaseModel):
    """Rewritten matching question. Returned by judge-driven auto
    regen (`run_matching_regen`).
    """
    question_text: str = Field(
        description="The rewritten matching prompt / instructions.",
        max_length=600,
    )
    pairs: list[MatchingPair] = Field(
        description=(
            "Canonical left↔right pairs. Each `left` must have ONE "
            "and only one correct `right` under any reasonable reading."
        ),
        min_length=2,
        max_length=8,
    )
    distractor_rights: list[str] = Field(
        default_factory=list,
        description=(
            "Extra right-side options that are plausibly wrong but "
            "NOT valid matches for any left item. Empty list is fine."
        ),
        max_length=6,
    )
    explanation: str = Field(
        default="",
        description="One short sentence on the matching logic / rule.",
        max_length=500,
    )


__all__ = [
    "MCQRewrite",
    "FillInBlankRewrite",
    "ShortAnswerRewrite",
    "MatchingPair",
    "MatchingRewrite",
]
