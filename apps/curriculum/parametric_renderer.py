"""Layer 4 — Parametric question schema + renderer.

The defensive layers (1, 2, 3) catch arithmetic errors *after* the
LLM has produced them. Layer 4 *eliminates* the error class for
question types we choose to templatize: the LLM emits a TEMPLATE
with named slots and an answer formula; code samples concrete
parameter values and computes the answer.

Errors become impossible by construction — the answer is whatever
the formula evaluates to with the chosen parameters.

V1 scope
--------
- Render once at content-generation time (per-attempt re-rendering
  is deferred to a follow-on phase L4.E).
- Math-only. Templates never apply to non-math content.
- Safe-eval AST walker for the answer formula. No sympy in v1 —
  the scope is "compute a number from named parameters", not
  symbolic isolation.
- Constraints are simple boolean expressions over the parameters
  (e.g. "a + b < 360"). Up to 50 resample attempts before giving
  up — caller decides what to do on give-up (skip the template
  and fall back to free-form generation).

See `memory/llm_arithmetic_defense_plan.md` (Layer 4 section).
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from apps.tutoring.student_working_analyzer import safe_eval_arithmetic

logger = logging.getLogger(__name__)


# ============================================================================
# Pydantic schema (LLM emits this; the renderer consumes it)
# ============================================================================


class ParameterSpec(BaseModel):
    """Range + step for one named parameter in a template.

    The LLM tells us "I want a parameter `a` that's a positive
    integer between 30 and 120 in steps of 5." The renderer samples
    a value from that range.
    """

    type: Literal["int", "float"] = "int"
    min: float = Field(description="Minimum value (inclusive)")
    max: float = Field(description="Maximum value (inclusive)")
    step: Optional[float] = Field(
        default=None,
        description=(
            "Discrete step. None = any value in range. Use 5 to get "
            "values like 30, 35, 40, ..."
        ),
    )

    @field_validator("max")
    @classmethod
    def _max_ge_min(cls, v, info):
        min_v = info.data.get("min")
        if min_v is not None and v < min_v:
            raise ValueError(f"max ({v}) must be >= min ({min_v})")
        return v


class ParametricQuestionTemplate(BaseModel):
    """A question whose numbers come from code, not the LLM.

    The LLM emits one of these objects when generating a math
    exit-ticket question for a templatable pattern (sum-to-360,
    linear equation, percentage-of, etc.). The renderer fills in
    concrete values, computes the answer, and produces a dict the
    same shape as a free-form question.

    Examples
    --------
    Three angles around a point:

        template_text = "Three angles around a point are {a}°, "
                        "{b}°, and x°. Find x."
        parameters = {
            "a": ParameterSpec(type="int", min=30, max=150, step=5),
            "b": ParameterSpec(type="int", min=30, max=150, step=5),
        }
        answer_formula = "360 - a - b"
        answer_unit = "°"
        explanation_template = (
            "Angles around a point sum to 360°. We compute "
            "x = 360 - {a} - {b} = {answer}°."
        )
        constraints = ["a + b < 350"]   # leave room for x
    """

    template_text: str = Field(
        description=(
            "Question stem with named slots in {braces}. Example: "
            "'Three angles around a point are {a}°, {b}°, and x°. "
            "Find x.'"
        )
    )
    parameters: Dict[str, ParameterSpec] = Field(
        description="Named parameter specs. Names must match the "
                    "{slots} in template_text and answer_formula."
    )
    answer_formula: str = Field(
        description=(
            "Pure-arithmetic expression using parameter names. "
            "Allowed: + - * / ** ( ). No function calls, no "
            "variables besides the named parameters. Example: "
            "'360 - a - b'."
        )
    )
    answer_unit: Optional[str] = Field(
        default=None,
        description="Suffix appended to the rendered answer "
                    "('°', '%', 'kg').",
    )
    explanation_template: str = Field(
        description=(
            "Explanation with {param} slots and a special {answer} "
            "slot for the computed value. Example: 'x = 360 - {a} "
            "- {b} = {answer}'."
        )
    )
    constraints: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional list of boolean expressions over parameters "
            "that must hold (samples are re-drawn until they do). "
            "Example: ['a + b < 350']."
        ),
    )


class ParametricMCQTemplate(BaseModel):
    """Templated MCQ. The correct answer AND 3 distractors all
    derive from the same parameter sample, so distractors stay
    plausible (same units, same magnitude). The server randomises
    which letter (A/B/C/D) the correct answer lands at per render.

    Example
    -------
        template_text = "Three angles around a point are {a}°, {b}°, "
                        "and x°. What is x?"
        parameters    = {"a": ParameterSpec(int, 30, 150, step=5),
                         "b": ParameterSpec(int, 30, 150, step=5)}
        correct_formula     = "360 - a - b"
        distractor_formulas = ["a + b",       # forgot to subtract
                               "180 - a - b", # wrong rule
                               "360 - a"]     # forgot one term
    """

    template_text: str = Field(
        description="Question stem with named slots in {braces}."
    )
    parameters: Dict[str, ParameterSpec]
    correct_formula: str = Field(
        description="Pure-arithmetic expression yielding the correct answer."
    )
    distractor_formulas: List[str] = Field(
        description=(
            "Exactly 3 expressions for the wrong options. Each must "
            "yield a value DIFFERENT from correct_formula across all "
            "samples (validator rejects ambiguous distractors)."
        ),
    )
    answer_unit: Optional[str] = Field(default=None)
    explanation_template: str = Field(
        description="Explanation with {param} + {answer} slots."
    )
    constraints: Optional[List[str]] = Field(default=None)

    @field_validator("distractor_formulas")
    @classmethod
    def _three_distractors(cls, v):
        if len(v) != 3:
            raise ValueError(
                f"distractor_formulas must have exactly 3 entries (got {len(v)})"
            )
        return v


class ParametricFillBlankTemplate(BaseModel):
    """Templated fill-in-blank. The stem contains one or more `___`
    slots; each is filled by evaluating the corresponding entry in
    blank_formulas. Answers are graded blank-by-blank.

    Example
    -------
        template_text  = "Two angles are {a}° and {b}°. The third "
                         "angle is ___° and the sum of all three is ___°."
        parameters     = {"a": ..., "b": ...}
        blank_formulas = ["360 - a - b", "360"]   # one per `___`, in order
    """

    template_text: str = Field(
        description=(
            "Stem with `___` for each blank. Number of `___` must "
            "match len(blank_formulas)."
        )
    )
    parameters: Dict[str, ParameterSpec]
    blank_formulas: List[str] = Field(
        description="One arithmetic formula per `___` slot, in order."
    )
    answer_unit: Optional[str] = Field(default=None)
    explanation_template: str
    constraints: Optional[List[str]] = Field(default=None)

    @field_validator("blank_formulas")
    @classmethod
    def _at_least_one_blank(cls, v):
        if not v:
            raise ValueError("blank_formulas must have at least one entry")
        return v


class ParametricMatchingTemplate(BaseModel):
    """Templated matching. The renderer samples the parameters
    `pair_count` times, producing a fresh (left, right) pair per
    sample. Distractors are extra wrong-side options drawn from
    additional samples or perturbations.

    Example
    -------
        framing_text  = "Match each angle pair to its sum."
        parameters    = {"a": ..., "b": ...}
        pair_count    = 5
        left_formula  = "{a}° + {b}°"   # display string for the left
        right_formula = "a + b"          # arithmetic for the right
        distractor_count = 2
    """

    framing_text: str = Field(
        description="Instruction text shown above the pair list."
    )
    parameters: Dict[str, ParameterSpec]
    pair_count: int = Field(
        default=4,
        ge=4, le=6,
        description="Number of pairs to render (4-6).",
    )
    left_formula: str = Field(
        description=(
            "String template for the left side of each pair. Uses "
            "{param} slots — substituted with each per-pair sample. "
            "Example: '{a}° + {b}°'."
        )
    )
    right_formula: str = Field(
        description="Arithmetic for the right side. Example: 'a + b'."
    )
    distractor_count: int = Field(default=2, ge=0, le=3)
    answer_unit: Optional[str] = Field(
        default=None,
        description="Unit suffix for the right-side rendered values "
                    "(e.g. '°' for angle sums).",
    )
    explanation_template: str
    constraints: Optional[List[str]] = Field(default=None)


class ParametricShortAnswerTemplate(BaseModel):
    """Two-field short answer (per the user's note in the v2 plan).

    The student fills two boxes:
      - final_answer:  numeric/short value, deterministically graded
                       against final_answer_formula
      - working:       prose, LLM-reviewed against canonical_working
                       (which the bank's `explanation` field also stores)

    This keeps the platform-wide rule intact (LLM never calculates
    correct answers) while still grading prose working — the LLM
    only compares the student's working against a reference text it
    didn't author. The reference is the canonical worked explanation
    produced at template creation time.
    """

    template_text: str = Field(
        description="The question prose (with {param} slots)."
    )
    parameters: Dict[str, ParameterSpec]
    final_answer_formula: str = Field(
        description="Arithmetic expression for the final-answer box."
    )
    canonical_working: str = Field(
        description=(
            "Reference text showing the steps a correct working "
            "should include. The runtime LLM compares the student's "
            "working against this. Uses {param} + {answer} slots."
        )
    )
    answer_unit: Optional[str] = Field(default=None)
    constraints: Optional[List[str]] = Field(default=None)


# Discriminated parse — content-gen routes by question_type.
def parse_template(question_type: str, data: dict):
    """Parse a template dict into the right Pydantic model based on
    question_type. Returns the model on success, raises Pydantic
    ValidationError on schema violation.

    Routing:
      'mcq'             -> ParametricMCQTemplate
      'fill_in_blank'   -> ParametricFillBlankTemplate
      'matching'        -> ParametricMatchingTemplate
      'short_answer'    -> ParametricShortAnswerTemplate
      'short_numeric'   -> ParametricQuestionTemplate (existing)
      anything else     -> raises ValueError
    """
    routing = {
        'mcq': ParametricMCQTemplate,
        'fill_in_blank': ParametricFillBlankTemplate,
        'matching': ParametricMatchingTemplate,
        'short_answer': ParametricShortAnswerTemplate,
        'short_numeric': ParametricQuestionTemplate,
    }
    cls = routing.get(question_type)
    if cls is None:
        raise ValueError(f"unknown question_type: {question_type!r}")
    return cls.model_validate(data)


# ============================================================================
# Sampling
# ============================================================================


_MAX_RESAMPLE_ATTEMPTS = 50


def _sample_one(spec: ParameterSpec, rng: random.Random) -> float:
    """Sample a single value from a ParameterSpec."""
    if spec.step is not None and spec.step > 0:
        # Discrete grid: pick a step then return min + k*step.
        n_steps = int((spec.max - spec.min) / spec.step)
        k = rng.randint(0, n_steps)
        v = spec.min + k * spec.step
    elif spec.type == "int":
        v = rng.randint(int(spec.min), int(spec.max))
    else:
        v = rng.uniform(spec.min, spec.max)
    if spec.type == "int":
        return int(round(v))
    return float(v)


def _check_constraint(expr: str, params: Dict[str, float]) -> bool:
    """Evaluate a constraint like 'a + b < 350' against the sampled
    parameters. Returns True iff the constraint holds. Returns
    False on parse failure (treat as "fails" rather than crash)."""
    # Substitute parameters and split on the comparator.
    substituted = expr
    for name, value in params.items():
        # Simple word-boundary replace; parameters are single letters
        # or short names by convention.
        substituted = _substitute_var(substituted, name, value)
    for op_str, py_op in (
        ("<=", "<="),
        (">=", ">="),
        ("==", "=="),
        ("!=", "!="),
        ("<", "<"),
        (">", ">"),
    ):
        if op_str in substituted:
            left, right = substituted.split(op_str, 1)
            l_val = safe_eval_arithmetic(left.strip())
            r_val = safe_eval_arithmetic(right.strip())
            if l_val is None or r_val is None:
                return False
            try:
                return bool(eval(f"{l_val} {py_op} {r_val}"))  # noqa: S307
            except Exception:
                return False
    # No comparator → treat as malformed (fail closed).
    return False


def _substitute_var(expr: str, name: str, value: float) -> str:
    """Replace whole-word parameter name with its numeric value.
    Handles `a`, `ab`, `b1` etc. — only matches when the name is
    not part of a longer identifier."""
    import re

    return re.sub(
        rf"\b{re.escape(name)}\b",
        repr(float(value)) if not float(value).is_integer() else str(int(value)),
        expr,
    )


def _sample_parameters(
    template: ParametricQuestionTemplate,
    rng: random.Random,
) -> Optional[Dict[str, float]]:
    """Sample concrete parameter values, retrying when constraints
    are violated. Returns None if we couldn't satisfy constraints
    after _MAX_RESAMPLE_ATTEMPTS attempts."""
    for attempt in range(_MAX_RESAMPLE_ATTEMPTS):
        params = {
            name: _sample_one(spec, rng)
            for name, spec in template.parameters.items()
        }
        if not template.constraints:
            return params
        if all(_check_constraint(c, params) for c in template.constraints):
            return params
    logger.warning(
        "[Layer4] couldn't satisfy constraints %r after %d attempts",
        template.constraints, _MAX_RESAMPLE_ATTEMPTS,
    )
    return None


# ============================================================================
# Answer computation
# ============================================================================


def _compute_answer(
    formula: str, params: Dict[str, float],
) -> Optional[float]:
    """Evaluate the answer formula with parameter substitution.
    Returns None if the formula has unsupported operations or
    references undefined names."""
    substituted = formula
    for name, value in params.items():
        substituted = _substitute_var(substituted, name, value)
    return safe_eval_arithmetic(substituted)


def _format_answer(value: float, unit: Optional[str]) -> str:
    """Render the answer as a string. Strips trailing .0 on integers
    so 85.0 displays as '85', and tacks on the unit if present."""
    s = f"{value:g}"  # %g trims trailing zeros
    if unit:
        s = f"{s}{unit}"
    return s


# ============================================================================
# Sanity-check / validator
# ============================================================================


from dataclasses import dataclass


@dataclass
class TemplateValidationError:
    """Structured failure reason from `validate_template`. Surfaced
    in lesson.metadata.verification_audit and embedded in the Layer
    3 retry constraint block so the LLM gets a precise error
    message."""
    kind: str       # see TemplateValidationError.KINDS
    message: str
    sample_params: Optional[Dict[str, float]] = None
    sample_index: Optional[int] = None  # which of the N samples failed

    KINDS = (
        'constraint_unsatisfiable',  # _sample_parameters returned None
        'formula_error',              # answer_formula didn't evaluate
        'missing_template_slot',      # template_text.format raised KeyError
        'missing_explanation_slot',   # explanation_template.format raised KeyError
        'non_finite_answer',          # NaN / inf
        'unreasonable_magnitude',     # |answer| > 1e9
        'parameter_spec_invalid',     # ParameterSpec validation failed
    )

    def to_audit_entry(self) -> Dict:
        return {
            'kind': self.kind,
            'message': self.message,
            'sample_params': self.sample_params,
            'sample_index': self.sample_index,
        }


_MAX_REASONABLE_MAGNITUDE = 1e9


def validate_template(
    template: ParametricQuestionTemplate,
    *,
    n_samples: int = 10,
) -> Optional[TemplateValidationError]:
    """Sample `n_samples` parameter sets, render each, verify the
    formula evaluates cleanly and the rendered text has no unfilled
    slots. Returns None on success or a TemplateValidationError
    describing the FIRST failure (so the caller can attach a
    specific reason to the retry constraint).

    All N samples must succeed — a partial pass is treated as a
    failure because it implies the parameter range admits values
    that break the formula.
    """
    rng = random.Random(0)  # deterministic so tests are stable
    for i in range(n_samples):
        params = _sample_parameters(template, rng)
        if params is None:
            return TemplateValidationError(
                kind='constraint_unsatisfiable',
                message=(
                    f"Couldn't satisfy constraints {template.constraints!r} "
                    f"in {_MAX_RESAMPLE_ATTEMPTS} attempts"
                ),
                sample_index=i,
            )

        # Compute answer
        answer = _compute_answer(template.answer_formula, params)
        if answer is None:
            return TemplateValidationError(
                kind='formula_error',
                message=(
                    f"answer_formula {template.answer_formula!r} "
                    f"failed to evaluate with params {params!r}"
                ),
                sample_params=params, sample_index=i,
            )
        # Reject NaN / inf
        if not math.isfinite(answer):
            return TemplateValidationError(
                kind='non_finite_answer',
                message=(
                    f"answer_formula produced non-finite value {answer!r} "
                    f"for params {params!r}"
                ),
                sample_params=params, sample_index=i,
            )
        if abs(answer) > _MAX_REASONABLE_MAGNITUDE:
            return TemplateValidationError(
                kind='unreasonable_magnitude',
                message=(
                    f"answer {answer:g} exceeds magnitude bound "
                    f"{_MAX_REASONABLE_MAGNITUDE:g} for params {params!r}"
                ),
                sample_params=params, sample_index=i,
            )

        # Try to fill the template_text slots
        try:
            template.template_text.format(**params)
        except (KeyError, IndexError, ValueError) as e:
            return TemplateValidationError(
                kind='missing_template_slot',
                message=(
                    f"template_text references a slot not in "
                    f"parameters: {e}"
                ),
                sample_params=params, sample_index=i,
            )

        # Try to fill the explanation_template slots
        try:
            answer_str = _format_answer(answer, template.answer_unit)
            template.explanation_template.format(**params, answer=answer_str)
        except (KeyError, IndexError, ValueError) as e:
            return TemplateValidationError(
                kind='missing_explanation_slot',
                message=(
                    f"explanation_template references a slot not in "
                    f"parameters or {{answer}}: {e}"
                ),
                sample_params=params, sample_index=i,
            )

    return None


# Need math.isfinite — pull at module level so we don't import inside the
# hot path.
import math


# ============================================================================
# Top-level rendering
# ============================================================================


def render_template(
    template: ParametricQuestionTemplate,
    *,
    seed: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Render a ParametricQuestionTemplate into a concrete question
    dict. Returns None if sampling or formula evaluation fails (the
    caller should fall back to free-form generation).

    Output dict shape mirrors what the exit-ticket persistence loop
    expects from a free-form LLM-generated question, plus a
    `template_data` field that captures the source template for
    retake re-rendering or teacher review.
    """
    rng = random.Random(seed)
    params = _sample_parameters(template, rng)
    if params is None:
        return None

    answer_value = _compute_answer(template.answer_formula, params)
    if answer_value is None:
        logger.warning(
            "[Layer4] formula %r failed to evaluate with params %r",
            template.answer_formula, params,
        )
        return None

    answer_str = _format_answer(answer_value, template.answer_unit)

    # Format the stem with parameter values, preserving any unit
    # suffixes baked into the template_text.
    try:
        question_text = template.template_text.format(**params)
        explanation = template.explanation_template.format(
            **params, answer=answer_str,
        )
    except KeyError as e:
        logger.warning(
            "[Layer4] template references unknown parameter %r", e,
        )
        return None

    return {
        "question_text": question_text,
        "question": question_text,  # legacy alias used by some persistence paths
        "correct_answer": answer_str,
        "explanation": explanation,
        "answer_data": {
            "computed": answer_value,
            "unit": template.answer_unit,
            "parameters": params,
            # Picked up by short-answer / data-interpretation graders
            # (apps/tutoring/summative_grading.py:198 reads
            # `ad.get('model_answer')`).
            "model_answer": answer_str,
            "keywords": [str(int(answer_value)) if float(answer_value).is_integer() else f"{answer_value:g}"],
        },
        # Persisted to ExitTicketQuestion.template_data for retake
        # re-rendering + teacher-review visibility.
        "template_data": template.model_dump(),
        # Question type defaults to short_numeric for templated math —
        # caller can override.
        "question_type": "short_numeric",
    }


def _sample_with_constraints_for_typed(
    template,
    rng: random.Random,
):
    """Generic sampler for any template with `parameters` and optional
    `constraints` fields. Returns a dict of param_name -> value, or
    None if constraints can't be satisfied within the resample limit.
    Identical behaviour to _sample_parameters but accepts the new
    template subclasses without inheriting from
    ParametricQuestionTemplate.
    """
    constraints = getattr(template, "constraints", None) or []
    parameters = template.parameters
    for _ in range(_MAX_RESAMPLE_ATTEMPTS):
        params = {name: _sample_one(spec, rng) for name, spec in parameters.items()}
        if all(_check_constraint(c, params) for c in constraints):
            return params
    logger.warning(
        "[Layer4] couldn't satisfy constraints %r after %d attempts",
        constraints, _MAX_RESAMPLE_ATTEMPTS,
    )
    return None


def render_mcq(
    template: ParametricMCQTemplate,
    *,
    seed: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Render a templated MCQ to a concrete question dict.

    Output shape:
      {
        question_text, question_type='mcq',
        option_a/b/c/d: str — rendered values
        correct_answer: 'A' | 'B' | 'C' | 'D'
        explanation, answer_data, template_data
      }
    Returns None on sampling / formula failure.
    """
    rng = random.Random(seed)
    params = _sample_with_constraints_for_typed(template, rng)
    if params is None:
        return None

    correct_value = _compute_answer(template.correct_formula, params)
    if correct_value is None:
        logger.warning(
            "[Layer4-MCQ] correct_formula %r failed for params %r",
            template.correct_formula, params,
        )
        return None

    distractor_values = []
    for f in template.distractor_formulas:
        v = _compute_answer(f, params)
        if v is None:
            logger.warning(
                "[Layer4-MCQ] distractor_formula %r failed for params %r",
                f, params,
            )
            return None
        distractor_values.append(v)

    # Reject ambiguous distractors at render time. The validator
    # extension (P2c) catches this at content-gen time too, but
    # belt-and-suspenders.
    EPS = 1e-6
    if any(abs(v - correct_value) < EPS for v in distractor_values):
        logger.warning(
            "[Layer4-MCQ] distractor collides with correct value for params %r",
            params,
        )
        return None
    if len(set(round(v, 6) for v in distractor_values)) < len(distractor_values):
        logger.warning(
            "[Layer4-MCQ] distractors collide with each other for params %r",
            params,
        )
        return None

    correct_str = _format_answer(correct_value, template.answer_unit)
    distractor_strs = [_format_answer(v, template.answer_unit) for v in distractor_values]

    # Randomise which letter the correct answer lands at.
    options_values = [correct_str] + distractor_strs
    indices = list(range(4))
    rng.shuffle(indices)
    correct_index = indices.index(0)  # 0 is the correct one
    correct_letter = chr(ord('A') + correct_index)
    shuffled = [options_values[i] for i in indices]

    try:
        question_text = template.template_text.format(**params)
        explanation = template.explanation_template.format(
            **params, answer=correct_str,
        )
    except KeyError as e:
        logger.warning(
            "[Layer4-MCQ] template references unknown parameter %r", e,
        )
        return None

    return {
        "question_text": question_text,
        "question": question_text,
        "question_type": "mcq",
        "option_a": shuffled[0],
        "option_b": shuffled[1],
        "option_c": shuffled[2],
        "option_d": shuffled[3],
        "correct_answer": correct_letter,
        "explanation": explanation,
        "answer_data": {
            "computed": correct_value,
            "correct_letter": correct_letter,
            "unit": template.answer_unit,
            "parameters": params,
        },
        "template_data": template.model_dump(),
    }


def render_fill_blank(
    template: ParametricFillBlankTemplate,
    *,
    seed: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Render a templated fill-in-blank to a concrete question dict.

    The stem keeps `___` placeholders intact for the student. The
    bank stores per-blank computed values for deterministic grading.

    Output answer_data:
      {
        text_template: stem with `___` slots,
        blanks: [computed values per blank, in order],
        accept_alternatives: [],
      }
    """
    rng = random.Random(seed)
    params = _sample_with_constraints_for_typed(template, rng)
    if params is None:
        return None

    blank_values = []
    for f in template.blank_formulas:
        v = _compute_answer(f, params)
        if v is None:
            logger.warning(
                "[Layer4-Fill] blank_formula %r failed for params %r",
                f, params,
            )
            return None
        blank_values.append(v)

    # Substitute parameters into stem (keep `___` intact for the UI).
    try:
        stem_filled = template.template_text.format(**params)
    except KeyError as e:
        logger.warning(
            "[Layer4-Fill] template references unknown parameter %r", e,
        )
        return None

    # Validate ___ count matches blank count.
    blank_count = stem_filled.count("___")
    if blank_count != len(blank_values):
        logger.warning(
            "[Layer4-Fill] stem has %d `___` slots but %d blank_formulas",
            blank_count, len(blank_values),
        )
        return None

    blank_strs = [_format_answer(v, template.answer_unit) for v in blank_values]

    try:
        explanation = template.explanation_template.format(
            **params,
            answer=blank_strs[0] if len(blank_strs) == 1 else " / ".join(blank_strs),
        )
    except KeyError as e:
        logger.warning(
            "[Layer4-Fill] explanation references unknown parameter %r", e,
        )
        return None

    return {
        "question_text": stem_filled,
        "question": stem_filled,
        "question_type": "fill_in_blank",
        "explanation": explanation,
        "answer_data": {
            "text_template": stem_filled,
            "blanks": blank_strs,
            "computed": blank_values,
            "accept_alternatives": [[] for _ in blank_strs],
            "parameters": params,
        },
        "template_data": template.model_dump(),
    }


def render_matching(
    template: ParametricMatchingTemplate,
    *,
    seed: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Render a templated matching question. Samples `pair_count`
    independent (left, right) pairs from the template's parameter
    spec. Optionally adds `distractor_count` extra wrong-side options.

    Output answer_data:
      {
        pairs: [{left, right}, ...],   # the correct pairing
        distractor_rights: [str, ...], # extra wrong-side options
      }
    """
    rng = random.Random(seed)
    pairs = []
    seen_lefts = set()
    seen_rights = set()
    # Sample distinct pairs — re-roll if a left or right collides.
    safety = template.pair_count * 10  # bounded retry loop
    while len(pairs) < template.pair_count and safety > 0:
        safety -= 1
        params = _sample_with_constraints_for_typed(template, rng)
        if params is None:
            return None
        right_value = _compute_answer(template.right_formula, params)
        if right_value is None:
            return None
        try:
            left_str = template.left_formula.format(**params)
        except KeyError as e:
            logger.warning(
                "[Layer4-Match] left_formula references unknown param %r", e,
            )
            return None
        right_str = _format_answer(right_value, getattr(template, 'answer_unit', None))
        if left_str in seen_lefts or right_str in seen_rights:
            continue
        seen_lefts.add(left_str)
        seen_rights.add(right_str)
        pairs.append({"left": left_str, "right": right_str})

    if len(pairs) < template.pair_count:
        logger.warning(
            "[Layer4-Match] could not produce %d distinct pairs",
            template.pair_count,
        )
        return None

    # Generate distractor rights — extra samples whose right-value
    # doesn't collide with any of the correct rights.
    distractors = []
    safety = template.distractor_count * 10
    while len(distractors) < template.distractor_count and safety > 0:
        safety -= 1
        params = _sample_with_constraints_for_typed(template, rng)
        if params is None:
            break
        v = _compute_answer(template.right_formula, params)
        if v is None:
            continue
        s = _format_answer(v, getattr(template, 'answer_unit', None))
        if s in seen_rights or s in distractors:
            continue
        distractors.append(s)

    explanation = template.explanation_template
    try:
        # Best-effort substitution — explanation may reference {param}
        # of the FIRST pair's params for illustrative purposes.
        first_params = (
            {} if not pairs else {}  # we don't track per-pair params; skip
        )
        explanation_filled = explanation
    except Exception:
        explanation_filled = explanation

    return {
        "question_text": template.framing_text,
        "question": template.framing_text,
        "question_type": "matching",
        "explanation": explanation_filled,
        "answer_data": {
            "pairs": pairs,
            "distractor_rights": distractors,
        },
        "template_data": template.model_dump(),
    }


def render_short_answer(
    template: ParametricShortAnswerTemplate,
    *,
    seed: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Render a templated short-answer question (two-field design).

    The student fills two boxes:
      - final_answer:  graded deterministically against the formula
      - working:       LLM-reviewed against canonical_working
                       (which the runtime tutor uses as the
                       reference text — never authored by the LLM
                       at grade time)

    Output answer_data:
      {
        model_answer: str — the final answer (deterministic grade)
        canonical_working: str — reference text for LLM working review
        computed: float — numeric value of the final answer
      }
    """
    rng = random.Random(seed)
    params = _sample_with_constraints_for_typed(template, rng)
    if params is None:
        return None

    final_value = _compute_answer(template.final_answer_formula, params)
    if final_value is None:
        logger.warning(
            "[Layer4-ShortAns] formula %r failed for params %r",
            template.final_answer_formula, params,
        )
        return None

    final_str = _format_answer(final_value, template.answer_unit)

    try:
        question_text = template.template_text.format(**params)
        canonical = template.canonical_working.format(
            **params, answer=final_str,
        )
    except KeyError as e:
        logger.warning(
            "[Layer4-ShortAns] template references unknown parameter %r", e,
        )
        return None

    return {
        "question_text": question_text,
        "question": question_text,
        "question_type": "short_answer",
        "explanation": canonical,
        "answer_data": {
            "model_answer": final_str,
            "canonical_working": canonical,
            "computed": final_value,
            "unit": template.answer_unit,
            "parameters": params,
        },
        "template_data": template.model_dump(),
    }


def render_typed(question_type: str, template, *, seed: Optional[int] = None):
    """Single dispatch entry — picks the right renderer based on
    the runtime type of `template`. Mirrors parse_template's routing.
    """
    if isinstance(template, ParametricMCQTemplate):
        return render_mcq(template, seed=seed)
    if isinstance(template, ParametricFillBlankTemplate):
        return render_fill_blank(template, seed=seed)
    if isinstance(template, ParametricMatchingTemplate):
        return render_matching(template, seed=seed)
    if isinstance(template, ParametricShortAnswerTemplate):
        return render_short_answer(template, seed=seed)
    if isinstance(template, ParametricQuestionTemplate):
        return render_template(template, seed=seed)
    raise ValueError(
        f"render_typed: don't know how to render {type(template).__name__}"
    )


# ============================================================================
# Phase L4.A — Template library: angles around a point sum to 360°
# ============================================================================
#
# These are illustrative templates the LLM can copy-and-adapt when
# generating a math exit ticket. A teacher can also use them
# directly to seed a question bank without LLM involvement at all.

ANGLES_AROUND_A_POINT = ParametricQuestionTemplate(
    template_text=(
        "Three angles around a point are {a}°, {b}°, and x°. "
        "Find x."
    ),
    parameters={
        "a": ParameterSpec(type="int", min=30, max=150, step=5),
        "b": ParameterSpec(type="int", min=30, max=150, step=5),
    },
    answer_formula="360 - a - b",
    answer_unit="°",
    explanation_template=(
        "Angles around a point sum to 360°. "
        "x = 360 - {a} - {b} = {answer}."
    ),
    constraints=["a + b < 350"],
)

ANGLES_AROUND_A_POINT_4 = ParametricQuestionTemplate(
    template_text=(
        "Four angles around a point are {a}°, {b}°, {c}°, and x°. "
        "Find x."
    ),
    parameters={
        "a": ParameterSpec(type="int", min=20, max=120, step=5),
        "b": ParameterSpec(type="int", min=20, max=120, step=5),
        "c": ParameterSpec(type="int", min=20, max=120, step=5),
    },
    answer_formula="360 - a - b - c",
    answer_unit="°",
    explanation_template=(
        "Angles around a point sum to 360°. "
        "x = 360 - {a} - {b} - {c} = {answer}."
    ),
    constraints=["a + b + c < 350"],
)


# Per-pattern catalog. Add to this dict as new pattern phases ship
# (L4.B linear, L4.C percentage, L4.D two-step word problems).
TEMPLATE_LIBRARY: Dict[str, ParametricQuestionTemplate] = {
    "angles_around_point_3": ANGLES_AROUND_A_POINT,
    "angles_around_point_4": ANGLES_AROUND_A_POINT_4,
}
