"""
Interactive widget schemas — declarative specs for data-driven visualizations.

Widgets are declarative JSON specs that map to pre-built frontend components.
The LLM (or a human author) fills typed slots; no arbitrary code is emitted.

Design reference: design/INTERACTIVE_VISUALIZATIONS.md

Initial widget library:
    - composite_index_explorer  (HDI, BMI, weighted indices)
    - function_plotter           (y = f(x, params) with sliders)
    - fraction_decimal_percent   (synchronized fraction/decimal/percent visuals)

Each widget type has a typed ``params`` model. A ``MediaWidget`` ties a
``widget_type`` to its params plus presentation metadata (title, caption,
alt_text) that mirrors how images are described.

The Python-side job here is validation — reject malformed specs before they
reach the student. Formula evaluation itself runs on the client with a
whitelisted evaluator; the Python validator only checks that an expression
parses cleanly and uses only allowed identifiers.
"""

from __future__ import annotations

import ast
import math
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


WIDGET_TYPES = (
    "composite_index_explorer",
    "function_plotter",
    "fraction_decimal_percent",
)


# ---------------------------------------------------------------------------
# Safe expression validation
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS = (
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv,
)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)
_ALLOWED_COMPARE = (
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)
_ALLOWED_FUNCS = frozenset({
    "abs", "min", "max", "round", "sqrt", "log", "log10", "exp",
    "sin", "cos", "tan", "asin", "acos", "atan", "pow",
})
_ALLOWED_CONSTS = frozenset({"pi", "e"})


def validate_expression(expr: str, allowed_names: List[str]) -> None:
    """Parse ``expr`` and confirm only whitelisted names/ops/funcs are used.

    Raises ``ValueError`` with a precise reason on rejection. This is an
    authoring-time guardrail; the frontend runs its own safe evaluator at
    render time. Keeping both sides strict prevents spec drift.
    """
    if not expr or not isinstance(expr, str):
        raise ValueError("expression must be a non-empty string")
    if len(expr) > 400:
        raise ValueError("expression too long (>400 chars)")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"expression does not parse: {exc.msg}") from exc

    allowed = set(allowed_names) | _ALLOWED_CONSTS

    # Operator singletons (ast.Add, ast.Mult, ...) are attributes of their
    # parent BinOp/UnaryOp/Compare/BoolOp. ast.walk yields them as nodes in
    # their own right — skip them, since we validate the parent's `.op` field.
    _OPERATOR_SINGLETONS = (
        ast.operator, ast.unaryop, ast.cmpop, ast.boolop, ast.expr_context,
    )

    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Load)):
            continue
        if isinstance(node, _OPERATOR_SINGLETONS):
            continue
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                raise ValueError(f"binary op {type(node.op).__name__} not allowed")
            continue
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARYOPS):
                raise ValueError(f"unary op {type(node.op).__name__} not allowed")
            continue
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if not isinstance(op, _ALLOWED_COMPARE):
                    raise ValueError(f"compare op {type(op).__name__} not allowed")
            continue
        if isinstance(node, ast.IfExp):
            continue
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            continue
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float, bool)):
                raise ValueError("only numeric literals allowed")
            continue
        if isinstance(node, ast.Name):
            # Function names (walked separately from their Call parent) are
            # allowed if whitelisted. The Call handler below still enforces
            # that only names appearing in _ALLOWED_FUNCS can be called.
            if node.id not in allowed and node.id not in _ALLOWED_FUNCS:
                raise ValueError(
                    f"unknown identifier '{node.id}'; allowed: {sorted(allowed)}"
                )
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                fname = getattr(node.func, "id", type(node.func).__name__)
                raise ValueError(f"function '{fname}' not allowed")
            continue
        raise ValueError(f"node type {type(node).__name__} not allowed")


def eval_expression(expr: str, env: Dict[str, float]) -> float:
    """Evaluate a pre-validated expression with numeric env.

    Use only for author-time unit tests and teacher-preview rendering on the
    server. The client is the source of truth at runtime.
    """
    safe_funcs = {
        "abs": abs, "min": min, "max": max, "round": round, "pow": pow,
        "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "asin": math.asin, "acos": math.acos, "atan": math.atan,
    }
    safe_consts = {"pi": math.pi, "e": math.e}
    return eval(  # noqa: S307 — validated AST above
        compile(ast.parse(expr, mode="eval"), "<expr>", "eval"),
        {"__builtins__": {}},
        {**safe_funcs, **safe_consts, **env},
    )


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

class ReferencePoint(BaseModel):
    """Named reference marker on a widget (e.g., 'Norway HDI = 0.966')."""
    label: str = Field(min_length=1, max_length=80)
    value: float


# ---------------------------------------------------------------------------
# composite_index_explorer
# ---------------------------------------------------------------------------

class CompositeIndexInput(BaseModel):
    """One slider/input feeding the composite formula."""
    key: str = Field(
        min_length=1, max_length=32,
        pattern=r"^[a-z_][a-z0-9_]*$",
        description="Identifier used inside formula (e.g., 'income_idx').",
    )
    label: str = Field(min_length=1, max_length=80)
    min: float
    max: float
    default: float
    step: float = 1.0
    unit: str = ""

    @model_validator(mode="after")
    def _check_ranges(self) -> "CompositeIndexInput":
        if self.min >= self.max:
            raise ValueError(f"input '{self.key}': min must be < max")
        if not (self.min <= self.default <= self.max):
            raise ValueError(
                f"input '{self.key}': default {self.default} outside [{self.min}, {self.max}]"
            )
        if self.step <= 0:
            raise ValueError(f"input '{self.key}': step must be > 0")
        return self


class CompositeIndexBand(BaseModel):
    """Threshold band shown when the computed score lands in its range."""
    label: str = Field(min_length=1, max_length=60)
    min: float
    color: str = Field(default="#a1a1aa", pattern=r"^#[0-9a-fA-F]{6}$")


class CompositeIndexParams(BaseModel):
    inputs: List[CompositeIndexInput] = Field(min_length=1, max_length=6)
    formula: str = Field(min_length=1, max_length=400)
    output_label: str = "Score"
    output_min: float = 0.0
    output_max: float = 1.0
    precision: int = Field(default=3, ge=0, le=6)
    bands: List[CompositeIndexBand] = Field(default_factory=list)
    references: List[ReferencePoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "CompositeIndexParams":
        keys = [inp.key for inp in self.inputs]
        if len(set(keys)) != len(keys):
            raise ValueError("input keys must be unique")
        validate_expression(self.formula, allowed_names=keys)
        if self.output_min >= self.output_max:
            raise ValueError("output_min must be < output_max")
        if self.bands:
            sorted_bands = sorted(self.bands, key=lambda b: b.min)
            if [b.min for b in self.bands] != [b.min for b in sorted_bands]:
                raise ValueError("bands must be listed in ascending min order")
        return self


# ---------------------------------------------------------------------------
# function_plotter
# ---------------------------------------------------------------------------

class PlotterParam(BaseModel):
    """A named scalar parameter the student can slide (e.g., slope m)."""
    key: str = Field(
        min_length=1, max_length=16,
        pattern=r"^[a-z_][a-z0-9_]*$",
    )
    label: str = Field(min_length=1, max_length=60)
    min: float
    max: float
    default: float
    step: float = 0.1

    @model_validator(mode="after")
    def _check(self) -> "PlotterParam":
        if self.min >= self.max:
            raise ValueError(f"param '{self.key}': min must be < max")
        if not (self.min <= self.default <= self.max):
            raise ValueError(
                f"param '{self.key}': default {self.default} outside [{self.min}, {self.max}]"
            )
        if self.step <= 0:
            raise ValueError(f"param '{self.key}': step must be > 0")
        return self


class PlotterReferencePoint(BaseModel):
    x: float
    y: float
    label: str = Field(max_length=60, default="")


class FunctionPlotterParams(BaseModel):
    expression: str = Field(min_length=1, max_length=400)
    x_min: float = -10.0
    x_max: float = 10.0
    y_min: Optional[float] = None  # None → auto-fit
    y_max: Optional[float] = None
    x_label: str = "x"
    y_label: str = "y"
    parameters: List[PlotterParam] = Field(default_factory=list, max_length=4)
    reference_points: List[PlotterReferencePoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "FunctionPlotterParams":
        if self.x_min >= self.x_max:
            raise ValueError("x_min must be < x_max")
        if self.y_min is not None and self.y_max is not None and self.y_min >= self.y_max:
            raise ValueError("y_min must be < y_max")
        keys = [p.key for p in self.parameters]
        if len(set(keys)) != len(keys):
            raise ValueError("parameter keys must be unique")
        if "x" in keys:
            raise ValueError("'x' is reserved for the plot variable")
        validate_expression(self.expression, allowed_names=["x", *keys])
        return self


# ---------------------------------------------------------------------------
# fraction_decimal_percent
# ---------------------------------------------------------------------------

class FractionDecimalPercentParams(BaseModel):
    """A synchronized view of the same value as fraction, decimal, and percent.

    Student moves a slider; all three representations update together.
    """
    denominator: int = Field(default=10, ge=2, le=100)
    default_numerator: int = Field(default=1, ge=0)
    show_bar: bool = True
    show_pie: bool = True
    show_number_line: bool = True

    @model_validator(mode="after")
    def _check(self) -> "FractionDecimalPercentParams":
        if self.default_numerator > self.denominator:
            raise ValueError("default_numerator cannot exceed denominator")
        if not (self.show_bar or self.show_pie or self.show_number_line):
            raise ValueError("at least one visual representation must be enabled")
        return self


# ---------------------------------------------------------------------------
# MediaWidget — the envelope stored in LessonStep.media["widgets"]
# ---------------------------------------------------------------------------

WidgetParams = Union[
    CompositeIndexParams,
    FunctionPlotterParams,
    FractionDecimalPercentParams,
]

_PARAMS_BY_TYPE: Dict[str, type[BaseModel]] = {
    "composite_index_explorer": CompositeIndexParams,
    "function_plotter": FunctionPlotterParams,
    "fraction_decimal_percent": FractionDecimalPercentParams,
}


class MediaWidget(BaseModel):
    """A widget attached to a lesson step.

    Mirrors ``MediaImage`` in structure: presentation metadata up top
    (title/caption/alt_text) plus a typed payload (``widget_type`` + ``params``).
    """
    widget_type: Literal[
        "composite_index_explorer",
        "function_plotter",
        "fraction_decimal_percent",
    ]
    title: str = Field(min_length=1, max_length=120)
    caption: str = ""
    alt_text: str = Field(min_length=1, max_length=300)
    params: Dict[str, Any]

    @model_validator(mode="after")
    def _typecheck_params(self) -> "MediaWidget":
        model = _PARAMS_BY_TYPE[self.widget_type]
        try:
            validated = model.model_validate(self.params)
        except Exception as exc:
            raise ValueError(
                f"invalid params for widget_type={self.widget_type}: {exc}"
            ) from exc
        # Normalize to dict form for JSON storage
        self.params = validated.model_dump()
        return self

    @field_validator("widget_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in WIDGET_TYPES:
            raise ValueError(f"unknown widget_type: {v}")
        return v


def validate_widget_dict(data: Dict[str, Any]) -> MediaWidget:
    """Parse and validate a raw widget dict (e.g., from JSON storage).

    Convenience for callers that have a plain dict and want a typed error.
    """
    return MediaWidget.model_validate(data)
