"""Pydantic schemas for content-generation LLM calls.

Used with `instructor.from_provider(...).chat.completions.create(
    response_model=...)` so generated content is validated against
the schema before return — eliminates the `parse_llm_json` failure
class (LLM returns an array containing malformed objects → entire
gen call drops the whole set).

Heterogeneous question types share a flat schema with optional
fields; the downstream pipeline already routes by `question_type`
and reads only the fields applicable to each type, so this keeps
the contract simple. Per-type validation lives in the existing
`safe_questions` filter + Layer 3 retry path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


_QUESTION_TYPE = Literal[
    "mcq",
    "fill_in_blank",
    "matching",
    "short_answer",
    "data_interpretation",
    "short_numeric",
]

_DIFFICULTY = Literal["easy", "medium", "hard"]


class GeneratedExitQuestion(BaseModel):
    """One generated exit-ticket question.

    Flat heterogeneous shape: every field is optional except
    `question_type` and `question_text`. The downstream persistence
    loop in `content_generator._generate_exit_ticket` already routes
    by question_type and reads only the fields applicable to each
    type, so this keeps the schema simple. Per-type field
    completeness is enforced by the existing `safe_questions` filter
    + Layer 3 retry path.
    """
    question_type: _QUESTION_TYPE = Field(
        description=(
            "Which question format this is. Drives which other fields "
            "are required: mcq → option_a..d + correct_answer; "
            "fill_in_blank / short_answer / matching → answer_data "
            "with type-specific keys."
        ),
    )
    question_text: str = Field(
        description="The question stem / prompt shown to the student.",
        max_length=2000,
    )

    # MCQ-specific fields (empty for non-MCQ types).
    option_a: str = Field(default='', max_length=500)
    option_b: str = Field(default='', max_length=500)
    option_c: str = Field(default='', max_length=500)
    option_d: str = Field(default='', max_length=500)
    correct_answer: str = Field(
        default='',
        description=(
            "MCQ only: the letter A/B/C/D pointing to the correct "
            "option. Empty for non-MCQ types."
        ),
        max_length=4,
    )

    # Non-MCQ type-specific payload — heterogeneous so kept as a
    # free dict. The shape per type:
    #   fill_in_blank: {text_template, blanks, accept_alternatives}
    #   short_answer:  {model_answer, keywords, min_keywords}
    #   matching:      {pairs (list of {left,right}), distractor_rights}
    #   short_numeric: {expected_value, tolerance, ...}
    #   data_interpretation: {data_description, model_answer, keywords}
    answer_data: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Type-specific answer payload. Required for non-MCQ "
            "types; the structure varies by question_type — see the "
            "ExitTicketQuestion model docstring for the per-type "
            "key shape."
        ),
    )

    explanation: str = Field(
        default='',
        description="One short sentence explaining the correct answer.",
        max_length=1000,
    )

    # Curriculum-linkage fields. Both should be set by the LLM but
    # default to empty so partial outputs don't fail validation; the
    # downstream loop will surface drift via the
    # enabling_objective_validation audit blob.
    concept_tag: str = Field(default='', max_length=500)
    enabling_objective: str = Field(default='', max_length=500)
    difficulty: _DIFFICULTY = Field(default='medium')

    # Layer-4 parametric template source (optional — set when the
    # LLM emits a templated question instead of a fully-rendered one).
    template_data: Optional[Dict[str, Any]] = Field(default=None)

    # Forensic-only fields kept for the audit blob.
    terminal_objective: str = Field(default='', max_length=500)
    source: str = Field(default='', max_length=200)


class GeneratedExitTicket(BaseModel):
    """The full exit-ticket payload returned by one LLM call."""
    questions: List[GeneratedExitQuestion] = Field(
        description=(
            "10-35 questions covering the lesson's enabling "
            "objectives. Mix of types per the prompt's "
            "format-distribution rules."
        ),
        min_length=1,
        max_length=40,
    )


class GranularSubSkill(BaseModel):
    """One atomic sub-skill from the EO expansion call."""
    text: str = Field(
        description=(
            "An action-verb-led, measurable sub-skill (8-200 chars). "
            "E.g. 'Label the inner core on a cross-section diagram.' "
            "Avoid weak openers (understand, know, appreciate)."
        ),
        min_length=8,
        max_length=200,
    )


class GranularSubSkillList(BaseModel):
    """Wrapper so instructor can drive a single chat.completions.create
    call returning a list (instructor requires a top-level object)."""
    sub_skills: List[GranularSubSkill] = Field(
        description=(
            "5-10 atomic sub-skills that decompose the lesson's "
            "broader enabling objectives. Each one is a single "
            "teaching-step-sized skill the pre-test can target."
        ),
        min_length=1,
        max_length=15,
    )


class ExtractedCompetency(BaseModel):
    """One competency extracted from the curriculum KB."""
    text: str = Field(
        description="The competency statement (action verb + content).",
        max_length=500,
    )
    type: Literal["knowledge", "skill", "attitude"] = Field(
        default="knowledge",
        description="Competency type.",
    )
    bloom_level: Literal[
        "remember", "understand", "apply", "analyze", "evaluate", "create",
    ] = Field(
        default="understand",
        description="Bloom's taxonomy level.",
    )
    source_code: str = Field(
        default="",
        description="Original curriculum code if visible (e.g. K408, S401).",
        max_length=50,
    )
    strand: str = Field(
        default="",
        description=(
            "Topic/strand this belongs to (e.g. 'Number', "
            "'Map Skills', 'Population')."
        ),
        max_length=200,
    )


class ExtractedCompetencyList(BaseModel):
    """Wrapper for the competency-extraction call."""
    competencies: List[ExtractedCompetency] = Field(
        description=(
            "Every specific, measurable competency extracted from the "
            "source material. Do NOT summarize or merge."
        ),
        min_length=1,
        max_length=200,
    )


__all__ = [
    "GeneratedExitQuestion",
    "GeneratedExitTicket",
    "GranularSubSkill",
    "GranularSubSkillList",
    "ExtractedCompetency",
    "ExtractedCompetencyList",
]
