"""Pydantic schemas for content-generation LLM calls.

Used with `instructor.from_provider(...).chat.completions.create(
    response_model=...)` so generated content is validated against
the schema before return — eliminates the `parse_llm_json` failure
class (LLM returns an array containing malformed objects → entire
gen call drops the whole set).

**Discriminated union pattern (2026-05-16)**: each question_type
has its own model with the per-type fields marked REQUIRED. The
public `GeneratedExitQuestion` is a `Union[...]` discriminated on
`question_type`. instructor + Pydantic then enforce per-type
shape STRICTLY — when the LLM omits a required field, instructor's
`max_retries` re-prompts with the validation error instead of
silently validating + downstream dropping. The PRE-refactor flat
schema with all-optional fields papered over LLM omissions: e.g. a
math question without a `template` parsed fine, then was rejected
at Layer 4, with no retry pressure on the LLM (pilot e2e 2026-05-16
showed 100% silent rejection on math).

**Field aliases**: the MATH_EXIT_TICKET_PROMPT teaches the LLM
keys like `question`, `correct`, `template` — different from the
schema's `question_text`, `correct_answer`, `template_data`. Each
mismatched field gets a Pydantic `alias=` so the LLM's natural
output validates without "unknown field" errors, while consumers
downstream see the schema's canonical names.

**`extra='forbid'`**: each model rejects extra fields so unknown
keys raise validation errors instead of being silently dropped.
This is what catches alias-drift bugs at the instructor layer
(retry fires, LLM corrects) rather than silently downstream.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


_DIFFICULTY = Literal["easy", "medium", "hard"]
_MCQ_LETTER = Literal["A", "B", "C", "D"]


# ---------------------------------------------------------------------------
# Common base — fields shared by every question_type
# ---------------------------------------------------------------------------
class _BaseQuestion(BaseModel):
    """Fields every question_type has. Per-type models inherit + add
    type-specific required fields + their own model_validator.

    `populate_by_name=True` so the LLM can write either the alias
    (prompt-native name like `question`) OR the field name
    (`question_text`) and Pydantic accepts both. `extra='forbid'` so
    unknown keys are surfaced as validation errors and instructor's
    `max_retries` actually fires.
    """
    model_config = ConfigDict(
        populate_by_name=True,
        extra='forbid',
    )

    question_text: str = Field(
        alias='question',
        description="The question stem / prompt shown to the student.",
        max_length=2000,
    )
    explanation: str = Field(
        default='',
        description="One short sentence explaining the correct answer.",
        max_length=1000,
    )
    concept_tag: str = Field(
        default='',
        max_length=500,
        description=(
            "Single concept tag this question targets (e.g. "
            "'angles_around_point'). Used for bank-by-step routing."
        ),
    )
    enabling_objective: str = Field(
        default='',
        max_length=500,
        description=(
            "The specific enabling objective this question assesses "
            "(must match one from the lesson's EO list)."
        ),
    )
    difficulty: _DIFFICULTY = Field(default='medium')
    terminal_objective: str = Field(default='', max_length=500)
    source: str = Field(default='', max_length=200)

    # Layer-4 parametric template source. MANDATORY in math
    # (enforced by the math-question sub-models' validators below).
    # Aliased so the LLM can write the natural `template` key per
    # MATH_EXIT_TICKET_PROMPT.
    template_data: Optional[Dict[str, Any]] = Field(
        default=None,
        alias='template',
        description=(
            "Parametric template for math questions. Shape: "
            "{template_text, params, answer_formula, "
            "explanation_template, ...}. See MATH_EXIT_TICKET_PROMPT "
            "for the per-q_type schemas + worked examples."
        ),
    )


# ---------------------------------------------------------------------------
# Per-type question models — each has a Literal question_type
# discriminator so Pydantic + instructor route the LLM output to the
# right shape.
# ---------------------------------------------------------------------------
class MCQQuestion(_BaseQuestion):
    """Multiple-choice question: requires 4 options + correct letter."""
    question_type: Literal['mcq'] = 'mcq'
    option_a: str = Field(max_length=500, description="Option A text.")
    option_b: str = Field(max_length=500, description="Option B text.")
    option_c: str = Field(max_length=500, description="Option C text.")
    option_d: str = Field(max_length=500, description="Option D text.")
    correct_answer: _MCQ_LETTER = Field(
        alias='correct',
        description="Letter A/B/C/D pointing to the correct option.",
    )

    @model_validator(mode='after')
    def _check_mcq(self):
        if not all([
            self.option_a.strip(),
            self.option_b.strip(),
            self.option_c.strip(),
            self.option_d.strip(),
        ]):
            raise ValueError(
                "MCQ requires non-empty option_a, option_b, "
                "option_c, option_d"
            )
        return self


class FillInBlankQuestion(_BaseQuestion):
    """Fill-in-the-blank: requires answer_data with text_template + blanks."""
    question_type: Literal['fill_in_blank'] = 'fill_in_blank'
    answer_data: Dict[str, Any] = Field(
        description=(
            "Required keys: text_template (str with ___ markers), "
            "blanks (list[str] — one expected answer per blank). "
            "Optional: accept_alternatives (list[list[str]] — per-blank "
            "synonyms)."
        ),
    )

    @model_validator(mode='after')
    def _check_fib(self):
        if 'text_template' not in self.answer_data:
            raise ValueError(
                "fill_in_blank.answer_data requires 'text_template' key"
            )
        blanks = self.answer_data.get('blanks')
        if not blanks or not isinstance(blanks, list):
            raise ValueError(
                "fill_in_blank.answer_data requires non-empty 'blanks' list"
            )
        return self


class MatchingQuestion(_BaseQuestion):
    """Matching: requires answer_data with pairs (list of {left,right})."""
    question_type: Literal['matching'] = 'matching'
    answer_data: Dict[str, Any] = Field(
        description=(
            "Required keys: pairs (list[dict] with keys 'left' + 'right'). "
            "Optional: distractor_rights (extra right-side options not "
            "in the canonical pairs)."
        ),
    )

    @model_validator(mode='after')
    def _check_matching(self):
        pairs = self.answer_data.get('pairs')
        if not pairs or not isinstance(pairs, list):
            raise ValueError(
                "matching.answer_data requires non-empty 'pairs' list"
            )
        for i, p in enumerate(pairs):
            if not isinstance(p, dict) or 'left' not in p or 'right' not in p:
                raise ValueError(
                    f"matching pair {i} requires both 'left' and 'right' keys"
                )
        return self


class ShortAnswerQuestion(_BaseQuestion):
    """Short-answer free text: requires answer_data with model_answer."""
    question_type: Literal['short_answer'] = 'short_answer'
    answer_data: Dict[str, Any] = Field(
        description=(
            "Required keys: model_answer (str). "
            "Optional: keywords (list[str] — concepts the answer must "
            "convey), min_keywords (int)."
        ),
    )

    @model_validator(mode='after')
    def _check_short_answer(self):
        model_answer = self.answer_data.get('model_answer')
        if not model_answer or not isinstance(model_answer, str):
            raise ValueError(
                "short_answer.answer_data requires non-empty "
                "'model_answer' string"
            )
        return self


class DataInterpretationQuestion(_BaseQuestion):
    """Data-interpretation: same as short_answer + a data_description."""
    question_type: Literal['data_interpretation'] = 'data_interpretation'
    answer_data: Dict[str, Any] = Field(
        description=(
            "Required keys: data_description (str — the table/chart "
            "the student interprets), model_answer (str). "
            "Optional: keywords (list[str])."
        ),
    )

    @model_validator(mode='after')
    def _check_data_interp(self):
        if not self.answer_data.get('data_description'):
            raise ValueError(
                "data_interpretation.answer_data requires "
                "'data_description' string"
            )
        if not self.answer_data.get('model_answer'):
            raise ValueError(
                "data_interpretation.answer_data requires "
                "'model_answer' string"
            )
        return self


class ShortNumericQuestion(_BaseQuestion):
    """Short-numeric: requires EITHER template_data (math, preferred)
    OR answer_data with expected_value (non-math fallback)."""
    question_type: Literal['short_numeric'] = 'short_numeric'
    answer_data: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional fallback for non-math short_numeric. Required "
            "keys when set: expected_value (number), tolerance (number). "
            "Math short_numeric questions should use template_data "
            "instead (deterministic answer computation)."
        ),
    )

    @model_validator(mode='after')
    def _check_short_numeric(self):
        # Either template_data (math) or answer_data with expected_value.
        if self.template_data:
            return self
        if self.answer_data and 'expected_value' in self.answer_data:
            return self
        raise ValueError(
            "short_numeric requires EITHER template_data (math, "
            "preferred — parametric template with formula) OR "
            "answer_data.expected_value (non-math fallback)."
        )


# ---------------------------------------------------------------------------
# Discriminated union — the public type the LLM emits.
# ---------------------------------------------------------------------------
GeneratedExitQuestion = Annotated[
    Union[
        MCQQuestion,
        FillInBlankQuestion,
        MatchingQuestion,
        ShortAnswerQuestion,
        DataInterpretationQuestion,
        ShortNumericQuestion,
    ],
    Field(discriminator='question_type'),
]


class GeneratedExitTicket(BaseModel):
    """The full exit-ticket payload returned by one LLM call.

    `extra='forbid'` so the LLM can't smuggle unknown top-level keys.
    """
    model_config = ConfigDict(extra='forbid')

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
    model_config = ConfigDict(extra='forbid')
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
    model_config = ConfigDict(extra='forbid')
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
    model_config = ConfigDict(extra='forbid')
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
    model_config = ConfigDict(extra='forbid')
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
    "MCQQuestion",
    "FillInBlankQuestion",
    "MatchingQuestion",
    "ShortAnswerQuestion",
    "DataInterpretationQuestion",
    "ShortNumericQuestion",
    "GranularSubSkill",
    "GranularSubSkillList",
    "ExtractedCompetency",
    "ExtractedCompetencyList",
]
