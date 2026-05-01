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
