"""MathVerificationTool — constrained-DSL math verifier.

Per Phase 1 §5 (and §7 items 5 + 8):

  - Constrained JSON DSL with whitelisted opcodes
    (``add, sub, mul, div, pow, sqrt, log, sin, cos, tan, eq, solve``).
  - Single-file Python interpreter that walks the DSL (no ``exec``).
  - DSL-validation pass: structured variable-binding check (DSL
    variables must map to numbers/quantities named in the visible
    problem text) + a focused LLM-mediated branch for free-form
    cases the structured check can't decide (kept as a hook here so
    Phase 2 can swap in a real LLM call).
  - Composed grading pipeline: MathVerificationTool (problem →
    canonical) + ``student_working_analyzer`` (student prose → value)
    + comparator (SymPy + ±0.01).

The interpreter never imports ``exec`` / ``eval``. SymPy is used only
to compute final expression values (a far smaller attack surface
than ``sympify`` on raw strings).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import sympy


# ----------------------------------------------------------------------
# Whitelisted opcodes
# ----------------------------------------------------------------------


ARITHMETIC_OPS = {
    "add": lambda xs: sum(xs),
    "sub": lambda xs: xs[0] - sum(xs[1:]),
    "mul": lambda xs: math.prod(xs),
    "div": lambda xs: xs[0] / xs[1],
    "neg": lambda xs: -xs[0],
    "abs": lambda xs: abs(xs[0]),
    "pow": lambda xs: xs[0] ** xs[1],
    "sqrt": lambda xs: math.sqrt(xs[0]),
    "log": lambda xs: math.log(xs[0]) if len(xs) == 1 else math.log(xs[0], xs[1]),
    "exp": lambda xs: math.exp(xs[0]),
    "sin": lambda xs: math.sin(xs[0]),
    "cos": lambda xs: math.cos(xs[0]),
    "tan": lambda xs: math.tan(xs[0]),
    "min": lambda xs: min(xs),
    "max": lambda xs: max(xs),
    "round": lambda xs: round(xs[0]) if len(xs) == 1 else round(xs[0], int(xs[1])),
    "eq": lambda xs: xs[0] == xs[1],
    "lt": lambda xs: xs[0] < xs[1],
    "lte": lambda xs: xs[0] <= xs[1],
    "gt": lambda xs: xs[0] > xs[1],
    "gte": lambda xs: xs[0] >= xs[1],
}

# Solve is special — handled by sympy, not arithmetic ops.
WHITELIST = set(ARITHMETIC_OPS.keys()) | {"solve"}

# Maximum recursion depth — guards against accidental loops via
# self-referential variables.
_MAX_DEPTH = 64


# ----------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------


@dataclass
class MathTrace:
    """Step-by-step evaluation trace — surfaced to the conformance
    layer for tutor-claim adjudication (Phase 2 §2.4)."""

    steps: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        self.steps.append(line)


@dataclass
class MathVerificationResult:
    """Output of MathVerificationTool.evaluate()."""

    canonical_value: Any
    trace: MathTrace
    error: Optional[str] = None
    # When ``error`` is set, the verifier failed (DSL-invalid, etc.).
    # The grader maps this onto verdict=unverified.

    @property
    def ok(self) -> bool:
        return self.error is None


# ----------------------------------------------------------------------
# DSL validation
# ----------------------------------------------------------------------


class DSLValidationError(Exception):
    """Raised when the DSL violates the schema or contains a
    non-whitelisted opcode."""


def _validate_node(node: Any, depth: int = 0) -> None:
    """Structural validation of the DSL tree.

    Tree shape (one of):
      - number: int | float
      - variable reference: {"var": "x"}
      - operation: {"op": "<whitelisted>", "args": [<node>, ...]}
      - solve: {"op": "solve", "equation": "x + 1 = 3", "var": "x"}
    """
    if depth > _MAX_DEPTH:
        raise DSLValidationError(f"DSL nesting exceeds max depth {_MAX_DEPTH}")
    if isinstance(node, (int, float)):
        return
    if not isinstance(node, dict):
        raise DSLValidationError(f"node must be number or dict, got {type(node)}")
    if "var" in node:
        if not isinstance(node["var"], str) or not node["var"]:
            raise DSLValidationError("'var' must be a non-empty string")
        return
    op = node.get("op")
    if op is None:
        raise DSLValidationError(f"node missing 'op': {node!r}")
    if op not in WHITELIST:
        raise DSLValidationError(f"opcode '{op}' is not whitelisted")
    if op == "solve":
        if not isinstance(node.get("equation"), str):
            raise DSLValidationError("'solve' requires string 'equation'")
        if not isinstance(node.get("var"), str):
            raise DSLValidationError("'solve' requires string 'var'")
        return
    args = node.get("args", [])
    if not isinstance(args, list):
        raise DSLValidationError(f"'args' must be a list for op '{op}'")
    for child in args:
        _validate_node(child, depth + 1)


def _validate_variable_bindings(
    variables: dict[str, Any],
    problem_text: str,
) -> Optional[str]:
    """Structured check: DSL variables must map to numbers/quantities
    named in the visible problem text.

    Returns ``None`` if every binding is satisfied; otherwise returns
    a human-readable description of the first failure. The
    LLM-mediated branch is the caller's fallback when this returns
    a non-None reason.
    """
    if not isinstance(variables, dict):
        return "variables block must be a dict"
    text = (problem_text or "").lower()
    for name, value in variables.items():
        if not isinstance(name, str) or not name:
            return f"variable name must be non-empty string, got {name!r}"
        # Each numeric binding must appear (numerically) in the problem
        # text. We don't enforce naming — the LLM may name its
        # variable ``v0``; we do enforce that the *value* of v0
        # appears in the visible problem.
        if isinstance(value, (int, float)):
            if not _value_appears_in_text(value, text):
                return (
                    f"variable {name!r}={value!r} does not appear in problem text"
                )
        elif isinstance(value, str):
            # String values are treated as named quantities; they
            # must appear (case-insensitive substring) in the
            # problem text.
            if value.lower() not in text:
                return (
                    f"variable {name!r}={value!r} does not appear in problem text"
                )
        else:
            return f"variable {name!r} has unsupported type {type(value)}"
    return None


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _value_appears_in_text(value: float, text: str) -> bool:
    """Numeric containment with float tolerance."""
    for match in _NUM_RE.finditer(text):
        try:
            if math.isclose(float(match.group(0)), float(value), abs_tol=1e-9):
                return True
        except ValueError:
            continue
    return False


# ----------------------------------------------------------------------
# Interpreter — pure recursion, no exec / eval on user input
# ----------------------------------------------------------------------


def _evaluate(
    node: Any,
    variables: dict[str, Any],
    trace: MathTrace,
    depth: int = 0,
) -> Any:
    if depth > _MAX_DEPTH:
        raise DSLValidationError(f"runtime depth exceeded {_MAX_DEPTH}")
    if isinstance(node, (int, float)):
        return node
    op = node.get("op")
    # A bare {"var": "x"} reference. ``solve`` also carries a "var"
    # key (the variable to solve for) so disambiguate by checking
    # for an absent "op" first.
    if op is None and "var" in node:
        name = node["var"]
        if name not in variables:
            raise DSLValidationError(f"unbound variable {name!r}")
        return variables[name]

    if op == "solve":
        equation = node["equation"]
        var_name = node["var"]
        return _solve_equation(equation, var_name, trace)

    args = [_evaluate(a, variables, trace, depth + 1) for a in node["args"]]
    fn = ARITHMETIC_OPS[op]
    try:
        value = fn(args)
    except (ZeroDivisionError, ValueError, OverflowError) as exc:
        raise DSLValidationError(f"op '{op}' failed: {exc}")
    trace.add(f"{op}({', '.join(str(a) for a in args)}) = {value}")
    return value


def _solve_equation(equation: str, var_name: str, trace: MathTrace) -> Any:
    """SymPy-backed equation solver — restricted to a single equation
    and a single variable. Equation form: ``lhs = rhs``."""
    if "=" not in equation:
        raise DSLValidationError("solve.equation must contain '='")
    lhs_str, rhs_str = equation.split("=", 1)
    sym = sympy.Symbol(var_name)
    try:
        # sympify is bounded by the symbol locals — no arbitrary
        # name resolution against globals.
        lhs = sympy.sympify(lhs_str, locals={var_name: sym})
        rhs = sympy.sympify(rhs_str, locals={var_name: sym})
    except (sympy.SympifyError, SyntaxError, TypeError) as exc:
        raise DSLValidationError(f"sympy parse failed: {exc}")
    try:
        solutions = sympy.solve(lhs - rhs, sym)
    except Exception as exc:  # noqa: BLE001
        raise DSLValidationError(f"sympy solve failed: {exc}")
    trace.add(f"solve({equation}, {var_name}) = {solutions}")
    if not solutions:
        return None
    # Return the first solution as a float if it's numeric, else
    # leave as a sympy expression.
    first = solutions[0]
    try:
        return float(first)
    except (TypeError, ValueError):
        return first


# ----------------------------------------------------------------------
# Comparator — numeric tolerance + symbolic equivalence
# ----------------------------------------------------------------------


def values_equivalent(
    a: Any,
    b: Any,
    abs_tolerance: float = 0.01,
) -> bool:
    """Compare canonical vs. student value.

    Handles:
      - numeric (int/float) with ±0.01 default tolerance
      - symbolic (sympy expressions) via ``sympy.simplify(a - b) == 0``
    """
    if a is None or b is None:
        return a == b
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    try:
        af, bf = float(a), float(b)
        return math.isclose(af, bf, abs_tol=abs_tolerance)
    except (TypeError, ValueError):
        pass
    try:
        return sympy.simplify(sympy.sympify(a) - sympy.sympify(b)) == 0
    except (sympy.SympifyError, TypeError, ValueError):
        return False


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


class MathVerificationTool:
    """Constrained-DSL math canonical solver.

    Inputs:
      - ``problem_text`` — visible problem statement.
      - ``program`` — DSL program (see ``_validate_node`` docstring).

    The optional ``llm_validator`` is the focused LLM call used for
    free-form variable-binding cases the structured check can't
    decide (Phase 1 §5). Pass ``None`` to leave that branch unused
    (default for unit tests).
    """

    def __init__(
        self,
        llm_validator: Optional[Callable[[str, dict[str, Any]], Optional[str]]] = None,
    ) -> None:
        self._llm_validator = llm_validator

    def evaluate(
        self,
        problem_text: str,
        program: dict[str, Any],
    ) -> MathVerificationResult:
        """Evaluate a DSL program. Supports single- and multi-slot.

        Single-slot: ``program['expression']`` -> canonical_value is a
        scalar.

        Multi-slot: ``program['expressions']`` is a list of
        ``{"name": str, "expression": <node>}`` entries -> canonical_value
        is a list of ``{"name": str, "value": scalar}`` dicts in the
        original order. The grader treats a student's single-value
        answer as PARTIAL when it matches any one slot, CORRECT when
        the student supplies all slot values.
        """
        trace = MathTrace()
        try:
            variables = program.get("variables", {})
            expression = program.get("expression")
            expressions = program.get("expressions")

            if expression is None and expressions is None:
                return MathVerificationResult(
                    canonical_value=None,
                    trace=trace,
                    error="program missing 'expression' or 'expressions'",
                )
            if expression is not None and expressions is not None:
                return MathVerificationResult(
                    canonical_value=None,
                    trace=trace,
                    error="program has BOTH 'expression' and 'expressions'; pick one",
                )

            # Structural validation FIRST — opcode whitelist + tree
            # shape. A malformed program must be rejected on structural
            # grounds before any contextual variable-binding check;
            # otherwise a program with a bad opcode that happens to also
            # have a missing variable binding is reported as a binding
            # error rather than a whitelist failure, which masks the
            # real bug and makes the security-relevant whitelist
            # rejection silently invisible.
            if expression is not None:
                _validate_node(expression)
            else:
                if not isinstance(expressions, list) or not expressions:
                    return MathVerificationResult(
                        canonical_value=None,
                        trace=trace,
                        error="'expressions' must be a non-empty list",
                    )
                for idx, entry in enumerate(expressions):
                    if not isinstance(entry, dict):
                        return MathVerificationResult(
                            canonical_value=None,
                            trace=trace,
                            error=(
                                f"'expressions[{idx}]' must be an object "
                                "with name + expression"
                            ),
                        )
                    slot_expr = entry.get("expression")
                    if slot_expr is None:
                        return MathVerificationResult(
                            canonical_value=None,
                            trace=trace,
                            error=(
                                f"'expressions[{idx}]' missing 'expression'"
                            ),
                        )
                    _validate_node(slot_expr)

            # Variable-binding check — contextual, runs after structural
            # validation has passed. Identical for single- and multi-slot
            # programs.
            binding_error = _validate_variable_bindings(variables, problem_text)
            if binding_error is not None:
                if self._llm_validator is not None:
                    llm_verdict = self._llm_validator(problem_text, variables)
                    if llm_verdict is not None:
                        return MathVerificationResult(
                            canonical_value=None,
                            trace=trace,
                            error=f"variable_bindings_invalid:{llm_verdict}",
                        )
                else:
                    return MathVerificationResult(
                        canonical_value=None,
                        trace=trace,
                        error=f"variable_bindings_invalid:{binding_error}",
                    )

            # Single-slot path — evaluate.
            if expression is not None:
                value = _evaluate(expression, variables, trace)
                return MathVerificationResult(canonical_value=value, trace=trace)

            # Multi-slot path — evaluate each slot. Structural
            # validation already passed above; ``expressions`` here is
            # guaranteed to be a non-empty list of dicts each with an
            # ``expression`` sub-tree.
            slot_values: list[dict[str, Any]] = []
            for idx, entry in enumerate(expressions):
                node = entry["expression"]  # structural pass guarantees presence
                name = str(entry.get("name") or f"slot_{idx}").strip() or f"slot_{idx}"
                val = _evaluate(node, variables, trace)
                slot_values.append({"name": name, "value": val})
            return MathVerificationResult(canonical_value=slot_values, trace=trace)

        except DSLValidationError as exc:
            return MathVerificationResult(
                canonical_value=None,
                trace=trace,
                error=str(exc),
            )
