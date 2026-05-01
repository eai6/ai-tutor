"""Layer S — Student working analyzer.

Deterministically extracts and verifies a student's mathematical
working step-by-step at session time, then injects a structured
signal block into the tutor LLM's system prompt so it can give
precise, step-aware feedback.

This module is the *runtime* counterpart to the content-generation
defenses in `apps/curriculum/content_generator.py`. It runs on
every student turn during a math tutoring session, alongside the
existing `_deterministic_math_check` and `_is_bare_math_answer` in
`apps/tutoring/conversational_tutor.py`.

Why it exists
-------------

Two production bugs the existing layers cannot catch:

1. The student writes a wrong intermediate step but somehow lands
   on a right final answer. The tutor confirms "great!" without
   diagnosing the broken working.

2. The student stops partway through a multi-step solution. The
   tutor jumps ahead and writes "so x = 85" — solving the rest
   of the problem for them.

Both are solved by step-by-step verification with a "first error"
signal and a partial-vs-complete state distinction. See the plan:
`memory/llm_arithmetic_defense_plan.md` (Layer S section).

Five terminal states
--------------------

  NO_WORKING        zero equations extracted
  PARTIAL_CORRECT   all steps right; last claim ≠ expected
  PARTIAL_WRONG     errors AND last claim is intermediate
  COMPLETE_CORRECT  all right + last claim ≈ expected
  COMPLETE_WRONG    clean math but final claim ≠ expected

The extractor is deliberately separator-agnostic — it walks
expression characters from each `=` sign rather than splitting on
newlines / semicolons / commas. Students writing "95+70=165;
165+110=275" and "95+70=165 then 165+110=275" produce identical
output.

Design + open questions live in `memory/llm_arithmetic_defense_plan.md`.
"""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set, Tuple


# ============================================================================
# State enum
# ============================================================================


class WorkingState(str, Enum):
    """Five terminal states a student reply can land in.

    Inheriting from str so the enum value serializes cleanly as a
    string in `SessionTurn.metadata` JSONField.

    v1 note: `COMPLETE_WRONG` is defined for forward-compatibility
    but `analyze_working` does not currently produce it — the cheap
    heuristic collapses "math clean, last_claim ≠ expected" into
    `PARTIAL_CORRECT`, since the user's pedagogical directive is
    that the tutor's behaviour is the same in both cases ("ask the
    student to walk through their reasoning"). The block builder
    still handles `COMPLETE_WRONG` correctly so we can plug in a
    smarter heuristic later (e.g. checking for arithmetic-path
    feasibility from last_claim to expected) without API changes.
    """

    NO_WORKING = "no_working"
    PARTIAL_CORRECT = "partial_correct"
    PARTIAL_WRONG = "partial_wrong"
    COMPLETE_CORRECT = "complete_correct"
    COMPLETE_WRONG = "complete_wrong"


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class Step:
    """One `<expr> = <claim>` extracted from the student's input.

    Attributes
    ----------
    idx
        1-indexed position in the student's working (display order).
    expr
        The arithmetic expression as written, normalized (×→*, ÷→/).
    claim
        The result the student claimed, as a string (preserves their
        formatting like '275' vs '275.0').
    computed
        What the expression actually evaluates to. None if the
        expression couldn't be safely evaluated.
    ok
        True iff `computed` ≈ float(claim) within 0.01 absolute.
    span
        (start, end) char positions in the original (un-normalized)
        student input. Useful for debugging / future highlighting.
    depends_on
        idx of a prior step whose `claim` value appears as an operand
        in this step's expression. None if no upstream dependency.
        Drives the "propagated" detection in `analyze_chain`.
    """

    idx: int
    expr: str
    claim: str
    computed: Optional[float] = None
    ok: bool = False
    span: Tuple[int, int] = (0, 0)
    depends_on: Optional[int] = None


@dataclass
class WorkingAnalysis:
    """Top-level result of `analyze_working`. Everything the tutor LLM
    and the teacher monitor need to know about the student's reply."""

    state: WorkingState
    steps: List[Step] = field(default_factory=list)
    first_error_idx: Optional[int] = None
    propagated_idxs: Set[int] = field(default_factory=set)
    final_claim: Optional[float] = None
    expected_answer: Optional[float] = None
    raw_input: str = ""


# ============================================================================
# Safe arithmetic evaluator (AST walker)
# ============================================================================
#
# We need to compute the value of student-supplied expressions like
# "95 + 70 + 110" or "(360 - 95) / 5" without giving the student a
# remote-code-execution. `eval()` is off-limits.
#
# Implementation: parse with `ast`, walk the tree, allow only a tiny
# whitelist of node types (BinOp, UnaryOp, numeric Constant). Anything
# else returns None — the caller treats unevaluable steps the same
# way as missing claims (skip).
#
# This evaluator is intended to be reused by Layer 4 (parametric
# question renderer). Keep it decoupled from any tutor-specific
# state.

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def safe_eval_arithmetic(expr: str) -> Optional[float]:
    """Evaluate a pure-arithmetic expression. Return None on failure
    or on disallowed content (variables, function calls, etc.)."""
    if not expr or not expr.strip():
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError):
        return None
    return _walk(tree.body)


def _walk(node) -> Optional[float]:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        return None
    if isinstance(node, ast.BinOp):
        op_fn = _ALLOWED_BINOPS.get(type(node.op))
        if op_fn is None:
            return None
        left = _walk(node.left)
        right = _walk(node.right)
        if left is None or right is None:
            return None
        try:
            return float(op_fn(left, right))
        except (ZeroDivisionError, OverflowError, ValueError):
            return None
    if isinstance(node, ast.UnaryOp):
        op_fn = _ALLOWED_UNARYOPS.get(type(node.op))
        if op_fn is None:
            return None
        operand = _walk(node.operand)
        if operand is None:
            return None
        try:
            return float(op_fn(operand))
        except (OverflowError, ValueError):
            return None
    # Anything else (Name, Call, Attribute, Subscript, ...) is rejected.
    return None


# ============================================================================
# Pre-processing — normalize the student's input
# ============================================================================
#
# Students write `×` for ×, `÷` for ÷, `−` (Unicode minus) for `-`.
# Python's parser only accepts ASCII `* / -`. Normalize those before
# trying to evaluate.
#
# Also strip noise around numbers — `°`, `$`, `%`, common units —
# so `38°` parses as `38` and `$30 + $40 = $70` extracts cleanly.

_OPERATOR_NORMALIZATIONS = [
    ("×", "*"),
    ("·", "*"),
    ("÷", "/"),
    ("−", "-"),  # Unicode minus → ASCII hyphen-minus
    ("–", "-"),  # en dash sometimes used as minus
    ("—", "-"),  # em dash too
]

# Noise characters that appear adjacent to numbers but are not
# arithmetic. Stripped before walking expression characters so the
# walker doesn't terminate on them.
_NOISE_CHAR_RE = re.compile(r"[°$%]")
# Common units stripped as whole words. NB: this is best-effort —
# a student writing "5 cm + 3 cm = 8 cm" gets normalized to
# "5 + 3 = 8". A student writing "5cm+3cm=8cm" gets the same.
_UNIT_WORD_RE = re.compile(
    r"\b(kg|cm|mm|km|m|s|g|l|ml)\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Replace Unicode operators + strip noise. Returns a string the
    same length OR shorter than the input — never longer.

    The same length constraint matters because we report
    `Step.span` against the ORIGINAL input. After normalization the
    spans would shift if we lengthened the string. We keep them in
    sync by only ever deleting / substituting equal-width chars.

    Operator substitutions are 1:1 character (× → *), strip ops
    delete a fixed-width run. Both preserve other character
    positions.
    """
    out = text
    for old, new in _OPERATOR_NORMALIZATIONS:
        if len(old) == len(new):
            out = out.replace(old, new)
        else:  # pragma: no cover — all current normalizations are 1:1
            raise ValueError(
                f"_normalize: replacement {old!r}→{new!r} would shift spans"
            )
    # Replace noise chars with space (preserves length).
    out = _NOISE_CHAR_RE.sub(" ", out)
    # Replace unit words with same-length spaces (preserves length).
    out = _UNIT_WORD_RE.sub(lambda m: " " * (m.end() - m.start()), out)
    return out


# ============================================================================
# Step extraction
# ============================================================================

# Characters that may appear inside an arithmetic expression after
# normalization. The walker stops when it sees anything else.
_EXPR_CHARS = set("0123456789+-*/.() \t")

# A "claim" is `= <number>` immediately following an expression.
# We deliberately don't anchor to line/whitespace boundaries — the
# walker handles all separators uniformly via `_EXPR_CHARS` membership.
_CLAIM_RE = re.compile(r"=\s*(-?\d+(?:\.\d+)?)")

# A valid expression must contain at least one binary operator
# (otherwise it's just a number, not a step).
_HAS_OPERATOR_RE = re.compile(r"\d\s*[+\-*/]\s*\d")


def _trim_leading_non_expr(expr: str) -> str:
    """A valid arithmetic expression starts with a digit, `(`, or `-`
    (unary minus). Trim any leading characters that aren't expression-
    starters — these are sentence-terminator punctuation (`.`, `,`,
    `;`) that the walker captured because they're allowed mid-decimal
    but happen to land at the start when separating two equations.
    """
    while expr and expr[0] not in "0123456789(-":
        expr = expr[1:].lstrip()
    return expr


def extract_steps(student_input: str) -> List[Step]:
    """Extract every `<expr> = <number>` step from the student's
    reply, in order of appearance. Separator-agnostic — newlines,
    semicolons, commas, periods, prose connectives, and even
    no-separator-at-all all work.

    The algorithm:
      1. Normalize the input (Unicode ops → ASCII, strip noise).
      2. For every `=<number>` in the normalized text, walk left
         through arithmetic chars until we hit a non-arithmetic
         character OR cross into the previous step's claim range —
         that's the start of the expression.
      3. Trim any leading sentence punctuation from the captured
         expression (e.g. ". 165+110" → "165+110").
      4. Validate: expression must contain at least one binary op
         (skip pure numbers, variable assignments like `x = 85`).

    Sequential-equals chains (`a op b = c op d = e`) — where the
    same number is the previous step's claim AND the next step's
    first operand — are *not* fully extracted in v1. The walker
    correctly identifies the first step (`a op b = c`); the second
    step's expression starts with `op d`, fails the operator check,
    and is dropped. This is acceptable because students rarely
    write run-on sequential equals; tutor output uses them more
    often and is handled by `verify_calculations` separately.
    See plan section "Out of scope for Layer S (v1)".
    """
    if not student_input:
        return []

    normalized = _normalize(student_input)

    raw_steps: List[Step] = []
    last_consumed_end = 0  # exclusive upper bound of last claim end

    for m in _CLAIM_RE.finditer(normalized):
        eq_start = m.start()
        claim_str = m.group(1)
        claim_end = m.end()

        # Walk left from eq_start. Stop conditions:
        #   - hit a non-arithmetic character
        #   - cross into the previous step's claim range
        #     (last_consumed_end is the position right after the
        #     previous claim's last char; we never walk past it)
        i = eq_start - 1
        while i >= last_consumed_end and normalized[i] in _EXPR_CHARS:
            i -= 1
        expr_start = max(i + 1, last_consumed_end)
        expr = _trim_leading_non_expr(
            normalized[expr_start:eq_start].strip()
        )

        # Validate — must have at least one operator (skips bare
        # numbers and the `+ d = e` fragments produced by run-on
        # sequential-equals chains).
        if not _HAS_OPERATOR_RE.search(expr):
            continue

        raw_steps.append(
            Step(
                idx=len(raw_steps) + 1,
                expr=expr,
                claim=claim_str,
                span=(expr_start, claim_end),
            )
        )
        last_consumed_end = claim_end

    return raw_steps


# ============================================================================
# Verification
# ============================================================================


def verify_steps(steps: List[Step]) -> None:
    """Compute each step's expression and compare to the claimed
    value. Mutates `steps` in place: sets `computed` and `ok`."""
    for step in steps:
        step.computed = safe_eval_arithmetic(step.expr)
        if step.computed is None:
            step.ok = False  # treat unevaluable as not-OK; caller can decide
            continue
        try:
            claim_val = float(step.claim)
        except ValueError:
            step.ok = False
            continue
        step.ok = abs(step.computed - claim_val) < 0.01


# ============================================================================
# Chain analysis
# ============================================================================


def _expression_operands(expr: str) -> List[float]:
    """Return every numeric literal that appears in expr, as floats.

    Used to detect "step N's expression contains step M's claim
    value" → step N depends on step M.
    """
    out: List[float] = []
    for m in re.finditer(r"-?\d+(?:\.\d+)?", expr):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            pass
    return out


def analyze_chain(steps: List[Step]) -> Tuple[Optional[int], Set[int]]:
    """Walk steps in order, link each step to a prior step whose
    `claim` value appears as an operand. Return:

      - first_error_idx — 1-based idx of the first step that
        evaluated to a wrong claim (i.e. step.ok is False AND
        step.computed is not None). Distinct from "couldn't
        evaluate" (where we don't really know).
      - propagated_idxs — set of step idxs whose error chain leads
        back to first_error_idx (these are arithmetically right
        but used a wrong upstream value, OR are themselves wrong
        but downstream of an earlier error).
    """
    if not steps:
        return None, set()

    # Wire up `depends_on` edges first.
    for step in steps:
        operands = _expression_operands(step.expr)
        for prior in steps:
            if prior.idx >= step.idx:
                break
            try:
                prior_claim = float(prior.claim)
            except ValueError:
                continue
            if any(abs(o - prior_claim) < 0.01 for o in operands):
                step.depends_on = prior.idx
                # Pick the most recent prior — keep walking.
        # (We could pick the first match instead; using the most
        # recent ensures shorter chains, which is what we usually
        # want for diagnostic reporting.)

    # Find the first step with a wrong claim that we could actually
    # verify. "computed is None + ok=False" doesn't count — we
    # don't know if it was right.
    first_error_idx: Optional[int] = None
    for step in steps:
        if step.computed is not None and not step.ok:
            first_error_idx = step.idx
            break

    if first_error_idx is None:
        return None, set()

    # Walk the chain forward from the first error. A step is
    # "propagated" iff:
    #   1. It is internally CORRECT on its own arithmetic (ok=True)
    #   2. It depends (transitively) on a step already in the
    #      error_set
    #
    # Why the ok=True gate: a step that's wrong on its own
    # arithmetic is an INDEPENDENT error, not a propagated one,
    # even if it coincidentally uses a value that matches an
    # upstream wrong claim. The pedagogical signal we want is
    # "where did the chain start to go wrong?" — which is
    # answered by first_error_idx alone for independent-error
    # cases. Propagation is reserved for "the math is right but
    # the inputs were poisoned upstream."
    propagated: Set[int] = set()
    error_set = {first_error_idx}
    for step in steps:
        if step.idx <= first_error_idx:
            continue
        if not step.ok:
            continue  # independent error, not propagation
        # Chase depends_on ancestors. If any is in error_set, this
        # step is propagated.
        ancestor = step.depends_on
        seen = set()
        while ancestor is not None and ancestor not in seen:
            seen.add(ancestor)
            if ancestor in error_set:
                propagated.add(step.idx)
                # Treat propagated as also-tainted so later steps
                # depending on this one inherit propagation.
                error_set.add(step.idx)
                break
            ancestor_step = next((s for s in steps if s.idx == ancestor), None)
            ancestor = ancestor_step.depends_on if ancestor_step else None

    return first_error_idx, propagated


# ============================================================================
# Top-level entry point
# ============================================================================


def _parse_expected(expected_answer: Optional[str]) -> Optional[float]:
    """Parse the lesson's expected_answer to a float for comparison.
    Reuses the same fraction/mixed-number parser the runtime math
    check uses, when available."""
    if expected_answer is None:
        return None
    s = str(expected_answer).strip()
    if not s:
        return None
    # Strip common units / symbols matching _NOISE_CHAR_RE.
    s_clean = _NOISE_CHAR_RE.sub("", s)
    s_clean = _UNIT_WORD_RE.sub("", s_clean).strip()
    # Try direct float parse first.
    try:
        return float(s_clean)
    except ValueError:
        pass
    # Defer to the existing math-answer parser for fractions /
    # mixed numbers / etc.
    try:
        from apps.tutoring.grader import extract_number  # noqa: WPS433
        v = extract_number(s_clean)
        if v is not None:
            return float(v)
    except Exception:
        pass
    return None


def analyze_working(
    student_input: str,
    expected_answer: Optional[str] = None,
) -> WorkingAnalysis:
    """Top-level entry. Extract → verify → chain → state-machine.

    Returns a WorkingAnalysis suitable for both prompt-block
    rendering (via build_working_analysis_block, S2) and persistence
    to SessionTurn.metadata for the teacher monitor (S6).
    """
    raw = student_input or ""
    expected_val = _parse_expected(expected_answer)

    steps = extract_steps(raw)

    if not steps:
        return WorkingAnalysis(
            state=WorkingState.NO_WORKING,
            steps=[],
            expected_answer=expected_val,
            raw_input=raw,
        )

    verify_steps(steps)
    first_error_idx, propagated = analyze_chain(steps)

    final_claim: Optional[float] = None
    try:
        final_claim = float(steps[-1].claim)
    except ValueError:
        final_claim = None

    has_errors = first_error_idx is not None

    # Completeness: does the last claim equal the expected answer?
    is_complete = (
        expected_val is not None
        and final_claim is not None
        and abs(final_claim - expected_val) < 0.01
    )

    if has_errors:
        # Errors trump completeness — focus on the first error.
        if is_complete:
            # Edge case: errors upstream but final claim happens to
            # match expected. The student's working is broken even
            # though they "got there." Treat as PARTIAL_WRONG so
            # the tutor addresses the broken step.
            state = WorkingState.PARTIAL_WRONG
        else:
            state = WorkingState.PARTIAL_WRONG
    else:
        # No errors. Distinguish complete vs partial.
        if is_complete:
            state = WorkingState.COMPLETE_CORRECT
        elif expected_val is None:
            # No expected answer to compare against. Default to
            # PARTIAL_CORRECT — the tutor will ask what's next,
            # which is the right move when we don't know if
            # they're done.
            state = WorkingState.PARTIAL_CORRECT
        else:
            # All steps right but last claim ≠ expected.
            # Cheap heuristic per memory/llm_arithmetic_defense_plan.md:
            # default to PARTIAL_CORRECT; the tutor's behaviour is
            # the same in PARTIAL vs COMPLETE_WRONG (active
            # questioning either way), so don't over-diagnose.
            state = WorkingState.PARTIAL_CORRECT

    return WorkingAnalysis(
        state=state,
        steps=steps,
        first_error_idx=first_error_idx,
        propagated_idxs=propagated,
        final_claim=final_claim,
        expected_answer=expected_val,
        raw_input=raw,
    )


# ============================================================================
# System prompt block builder (S2)
# ============================================================================
#
# Renders a `<student_working_analysis>` block to inject into the
# tutor LLM's system prompt. One template per state. The ACTION lines
# are non-negotiable directives — the LLM should treat them as
# binding rules for this turn.
#
# All five blocks share a common header (steps + verdict +
# expected/claim comparison) so the LLM can read the situation
# quickly. The ACTION section is what differs.


def _format_steps_block(steps: List[Step], propagated: Set[int]) -> str:
    """Render the per-step list with ✓/✗ markers and propagation
    annotation. Returns a multi-line string (no trailing newline).
    """
    if not steps:
        return "  (no steps extracted)"
    lines = []
    for s in steps:
        marker = "✓" if s.ok else "✗"
        suffix = ""
        if not s.ok and s.computed is not None:
            # Show what the math actually produces (helps the LLM
            # phrase its question precisely without revealing the
            # value to the student — the LLM decides what to do
            # with this info).
            correct = f"{s.computed:g}"
            suffix = f"  (correct: {correct})"
        elif s.idx in propagated:
            suffix = "  — propagates from earlier error"
        lines.append(
            f"  Step {s.idx}: {s.expr} = {s.claim}   {marker}{suffix}"
        )
    return "\n".join(lines)


def _format_comparison(analysis: WorkingAnalysis) -> str:
    """Render the expected-vs-claim comparison line."""
    if analysis.expected_answer is None:
        return "  (no expected answer recorded for this step)"
    last = analysis.final_claim
    last_str = f"{last:g}" if last is not None else "?"
    exp_str = f"{analysis.expected_answer:g}"
    return (
        f"  expected_answer:        {exp_str}\n"
        f"  student's last claim:   {last_str}"
    )


# ACTION text per state. Edits to these strings change tutor
# behaviour; tests in test_student_working_analyzer.py assert key
# phrases ("DO NOT compute", "show me your working", etc.) so the
# pedagogical contract stays intact.

_ACTION_NO_WORKING = """\
ACTION (apply Rule 1 with separator request):
- Do NOT confirm or deny their answer — they showed no
  verifiable working.
- Politely ask them to write each step on its own line, like:
       95 + 70 = 165
       165 + 110 = 275
       360 - 275 = 85
  This way you can check each step with them.
- Frame it pedagogically: "I want to walk through this step by
  step with you" — not "the system can't parse your answer.\""""

_ACTION_PARTIAL_CORRECT = """\
ACTION:
- Acknowledge specifically WHAT step they got right (name it).
- Ask them what comes next.
- Remind them to keep showing each step so you can check
  together.
- DO NOT compute the remaining step for them.
- DO NOT state the final answer.
- DO NOT say "great, so x = ..." — that would solve their
  problem for them."""

_ACTION_PARTIAL_WRONG = """\
ACTION:
- Address the FIRST_ERROR before talking about completeness.
- Do NOT state the correct value yet — ask them to recompute
  the specific step that went wrong.
- Once they fix the error, then prompt them to continue with
  the next step."""

_ACTION_COMPLETE_CORRECT = """\
ACTION:
- Confirm the answer.
- Be specific about WHICH steps you're praising — name them.
- DO NOT just say "great, next problem!" — the goal is
  learning, not correctness. Ask the student to articulate
  WHY this approach works:
    * "Why did you subtract from 360 instead of some other
       number?"
    * "How would you check this answer?"
    * "What rule did you use?"
- Only after they articulate the reasoning do you move on."""

_ACTION_COMPLETE_WRONG = """\
ACTION:
- Do NOT focus on the arithmetic — it's right.
- The student's setup is wrong (they used the wrong operation
  or wrong formula). Ask them to explain WHY they chose this
  approach.
- Walk them back to the problem statement and help them
  re-derive the correct setup.
- Do NOT state the correct setup — let them work it out."""


_VERDICT_HEADERS = {
    WorkingState.NO_WORKING: (
        "Verdict: NO_WORKING\n"
        "The student gave no verifiable equations — either a bare\n"
        "answer or pure prose."
    ),
    WorkingState.PARTIAL_CORRECT: (
        "Verdict: PARTIAL_CORRECT\n"
        "The student's arithmetic is correct, but they have not\n"
        "reached the final answer. The last claim is an\n"
        "intermediate value."
    ),
    WorkingState.PARTIAL_WRONG: (
        "Verdict: PARTIAL_WRONG\n"
        "The student's working contains an arithmetic error.\n"
        "Address it before discussing completeness."
    ),
    WorkingState.COMPLETE_CORRECT: (
        "Verdict: COMPLETE_CORRECT\n"
        "Every step is internally correct AND the final claim\n"
        "matches the expected answer."
    ),
    WorkingState.COMPLETE_WRONG: (
        "Verdict: COMPLETE_WRONG\n"
        "The student's arithmetic is internally correct but they\n"
        "did not reach the expected answer — the setup is wrong,\n"
        "not the math."
    ),
}

_ACTION_BLOCKS = {
    WorkingState.NO_WORKING: _ACTION_NO_WORKING,
    WorkingState.PARTIAL_CORRECT: _ACTION_PARTIAL_CORRECT,
    WorkingState.PARTIAL_WRONG: _ACTION_PARTIAL_WRONG,
    WorkingState.COMPLETE_CORRECT: _ACTION_COMPLETE_CORRECT,
    WorkingState.COMPLETE_WRONG: _ACTION_COMPLETE_WRONG,
}


def build_working_analysis_block(analysis: WorkingAnalysis) -> str:
    """Render the `<student_working_analysis>` system-prompt block
    for the given analysis. Returns the full block (with opening
    and closing tags) ready to append to the system prompt.

    The block is positioned LAST in the system prompt so it has
    highest salience for the LLM (immediately before generation).
    """
    steps_block = _format_steps_block(analysis.steps, analysis.propagated_idxs)
    verdict = _VERDICT_HEADERS[analysis.state]
    action = _ACTION_BLOCKS[analysis.state]

    # First-error annotation (only when present + meaningful).
    first_error_line = ""
    if analysis.first_error_idx is not None:
        first_error_line = (
            f"\nFIRST ERROR: Step {analysis.first_error_idx} "
            "— address this step before anything else."
        )

    # Show the raw student input for NO_WORKING so the LLM can
    # echo it back verbatim per Rule 1.
    raw_line = ""
    if analysis.state == WorkingState.NO_WORKING and analysis.raw_input:
        raw = analysis.raw_input.strip()[:200]
        raw_line = f'\nStudent input (verbatim): "{raw}"'

    body = (
        f"Steps extracted: {len(analysis.steps)}\n"
        f"{steps_block}"
        f"{first_error_line}"
        f"{raw_line}\n\n"
        f"Comparison to expected answer:\n"
        f"{_format_comparison(analysis)}\n\n"
        f"{verdict}\n\n"
        f"{action}"
    )
    return f"<student_working_analysis>\n{body}\n</student_working_analysis>"
