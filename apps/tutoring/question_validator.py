"""Deterministic validator for LLM-generated exit-ticket questions.

The LLM occasionally generates questions whose options/answers don't
match the math, then tries to "fix" the mismatch in the explanation
field — leaving a multi-paragraph monologue ("Wait, that's not… let
me revise… let me try… Yes!"). This module catches those cases and
drops the question before it lands in front of a student.

Three filters, all deterministic — no LLM call:

  1. Rationalization patterns in the explanation field.
  2. Hard length cap on the explanation field (~800 chars).
  3. Numeric verification for fill-in-blank questions:
       - parse equations like "85° + 92° + 78° + ___ = 360°"
       - evaluate, compare to the stored answer
  4. MCQ sanity: the correct option's value must match a value the
     question actually computes to (when extractable).

Returns `None` for valid questions, or a reason string when the
question should be dropped. Callers should log + skip.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple


# ─── Patterns that signal LLM "I made a mistake, let me fix it" ──────

_RATIONALIZATION_PATTERNS = (
    re.compile(r"\blet me (?:recalculate|revise|try|fix|reconsider|change)\b", re.I),
    re.compile(r"\bwait[,!]\b", re.I),
    re.compile(r"\bthat['']s not (?:right|correct|matching|in the options)\b", re.I),
    re.compile(r"\bdoesn['']t match (?:the )?option", re.I),
    re.compile(r"\bgetting (?:close|closer)\b", re.I),
    re.compile(r"\bhmm,?\b", re.I),
    re.compile(r"\bthis is incorrect\b", re.I),
    re.compile(r"\bstill (?:not|doesn['']t) match", re.I),
    re.compile(r"\byes!\s*$"),  # explanations that end with "Yes!"
    re.compile(r"\b(let me try|let me use):", re.I),
)

# Some explanations naturally have multi-step working — that's fine.
# We don't penalize length unless it's clearly rambling.
_MAX_EXPLANATION_CHARS = 800


def explanation_looks_broken(explanation: str) -> Optional[str]:
    """Return reason string if explanation looks like rationalization,
    else None."""
    if not explanation:
        return None
    for pat in _RATIONALIZATION_PATTERNS:
        if pat.search(explanation):
            return f'rationalization phrase in explanation ({pat.pattern!r})'
    if len(explanation) > _MAX_EXPLANATION_CHARS:
        return f'explanation rambling ({len(explanation)} > {_MAX_EXPLANATION_CHARS} chars)'
    return None


# ─── Fill-in-blank math verification ─────────────────────────────────

# Match patterns like "85° + 92° + 78° + ___ = 360°" (with or without °).
# The equation is a sum of numbers + one blank = total.
_EQUATION_RE = re.compile(
    r'((?:-?\d+(?:\.\d+)?\s*°?\s*[+]\s*)+)'  # sum of numbers
    r'(?:_+|\?\?+|[a-zA-Z])\s*°?\s*=\s*'      # blank or single var
    r'(-?\d+(?:\.\d+)?)\s*°?'                  # total
)


def _to_float(s: str) -> Optional[float]:
    s = (s or '').strip().replace('°', '')
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def verify_fill_in_blank(question_text: str, blanks: list) -> Tuple[bool, Optional[str]]:
    """For fill-in-blank questions where the blank holds a number,
    parse the equation in the stem and check the stored answer.

    Returns (ok, reason). `ok=True` means we couldn't disprove it OR
    we verified it matches. `ok=False` means we computed a different
    answer.
    """
    if not blanks or len(blanks) != 1:
        return True, None  # multi-blank questions: skip the check
    expected = _to_float(str(blanks[0]))
    if expected is None:
        return True, None  # non-numeric blank: skip
    m = _EQUATION_RE.search(question_text or '')
    if not m:
        return True, None  # no parseable equation: skip
    sum_expr, total_str = m.group(1), m.group(2)
    nums = [
        _to_float(n)
        for n in re.split(r'\s*\+\s*', sum_expr.strip().rstrip('+').strip())
        if n.strip()
    ]
    if any(n is None for n in nums):
        return True, None
    total = _to_float(total_str)
    if total is None:
        return True, None
    computed = total - sum(nums)
    if abs(computed - expected) > 0.5:  # half-degree tolerance
        return False, f'fill-in-blank: computed {computed:g} but answer is {expected:g}'
    return True, None


# ─── MCQ sanity ──────────────────────────────────────────────────────

# A loose check: if the question contains a clear arithmetic equation
# and the correct option is a number, the option should equal the
# computed value. We don't try to solve algebra here — just sums.
def verify_mcq_arithmetic(question_text: str, options: dict, correct_letter: str) -> Tuple[bool, Optional[str]]:
    """Light check for MCQ math. Returns (ok, reason)."""
    if not correct_letter:
        return True, None
    correct_letter = correct_letter.upper().strip()
    opt_text = options.get(correct_letter.lower()) or options.get(correct_letter)
    if not opt_text:
        return True, None
    # extract first number from the correct option
    m = re.search(r'-?\d+(?:\.\d+)?', str(opt_text))
    if not m:
        return True, None
    expected = float(m.group(0))

    # Simple equation in question stem: "X + Y + ... = total" → blank
    eq = _EQUATION_RE.search(question_text or '')
    if not eq:
        return True, None
    sum_expr, total_str = eq.group(1), eq.group(2)
    nums = [
        _to_float(n)
        for n in re.split(r'\s*\+\s*', sum_expr.strip().rstrip('+').strip())
        if n.strip()
    ]
    if any(n is None for n in nums):
        return True, None
    total = _to_float(total_str)
    if total is None:
        return True, None
    computed = total - sum(nums)
    if abs(computed - expected) > 0.5:
        return False, f'mcq option {correct_letter} = {expected:g} but math gives {computed:g}'
    return True, None


# ─── Layer 2 — Cross-validate answer keys against question stems ────
#
# Layer 2 catches the most damaging bug class: a wrong answer key.
# A student does the math right and gets marked wrong because the
# LLM-generated `correct_answer` field disagrees with what the
# question's own stem actually computes to.
#
# Approach: detect the arithmetic shape of the stem with a small
# library of regex patterns. Compute the expected answer in code.
# Compare to whatever the question stores as the answer (correct
# letter's option for MCQ, blanks[0] for fill-in-blank,
# correct_answer for short-answer / data-interpretation).
#
# Out-of-pattern questions (word problems, multi-step problems,
# anything we don't recognise) pass through. Layer 4 (parametric
# templates) is the long-term path to closing the gap for those —
# v1 just covers the patterns the LLM gets wrong most often.


# Pattern B — pure additive sum. Three or more numbers connected by
# `+` (or `-`), at least anywhere in the stem. No equals, no blank.
# The expected answer is just the sum.
#   Example: "What is 95° + 70° + 110°?" → expected 275
# We require ≥ 3 operands so accidental matches in word problems
# ("the chairs were 3 + 4 = 7 in number") don't fire — most pure-
# sum exam questions list 3+ values.
_PURE_SUM_RE = re.compile(
    r'(-?\d+(?:\.\d+)?(?:\s*°?\s*[+\-]\s*-?\d+(?:\.\d+)?){2,})'
    r'(?:\s*°?\s*=\s*\?|\s*°?\s*\?|\s*[?.]?\s*$|\s*°?\s*$)'
)


# Pattern C — multiplication chain. Two or more numbers multiplied.
#   Example: "Calculate 8 × 7 × 3" → expected 168
_MULT_CHAIN_RE = re.compile(
    r'(-?\d+(?:\.\d+)?(?:\s*[×*]\s*-?\d+(?:\.\d+)?){1,})'
    r'(?:\s*=\s*\?|\s*\?|\s*[?.]?\s*$)'
)


# Pattern D — simple linear equation. We isolate a single-letter
# variable on one side of an `=` with one operation.
#   Example: "Solve x + 5 = 12" → expected 7
# Two variants: var-first ("x + 5 = 12") and num-first ("5 + x = 12").
_LINEAR_VAR_FIRST_RE = re.compile(
    r'\b([a-zA-Z])\s*([+\-×*/])\s*(-?\d+(?:\.\d+)?)\s*=\s*(-?\d+(?:\.\d+)?)'
)
_LINEAR_NUM_FIRST_RE = re.compile(
    r'(-?\d+(?:\.\d+)?)\s*([+\-×*/])\s*([a-zA-Z])\s*=\s*(-?\d+(?:\.\d+)?)'
)


def _sum_operands(expr: str) -> Optional[float]:
    """Sum a chain like '95 + 70 + 110' or '95 - 30 + 5'. Returns
    None on parse failure."""
    expr_clean = re.sub(r'°', '', expr).strip()
    # Tokenise: leading number, then (op num)+
    m = re.match(r'\s*(-?\d+(?:\.\d+)?)\s*(.*)$', expr_clean)
    if not m:
        return None
    total = _to_float(m.group(1))
    if total is None:
        return None
    rest = m.group(2)
    for op, n in re.findall(r'([+\-])\s*(-?\d+(?:\.\d+)?)', rest):
        v = _to_float(n)
        if v is None:
            return None
        total = total + v if op == '+' else total - v
    return total


def _mult_operands(expr: str) -> Optional[float]:
    """Multiply a chain like '8 × 7 × 3'. Returns None on parse failure."""
    nums = [_to_float(n) for n in re.findall(r'-?\d+(?:\.\d+)?', expr)]
    if not nums or any(n is None for n in nums):
        return None
    product = 1.0
    for n in nums:
        product *= n
    return product


def _solve_linear(op: str, lhs: float, rhs_total: float, var_first: bool) -> Optional[float]:
    """Solve for the variable in:
      var_first=True  →  x op lhs = rhs_total       (e.g. x + 5 = 12)
      var_first=False →  lhs op x = rhs_total       (e.g. 5 + x = 12)

    `lhs` is the literal number adjacent to the variable. Returns
    None if the equation isn't solvable in the rationals (e.g. /0)
    or if op isn't one of the four supported.
    """
    if op == '+':
        return rhs_total - lhs
    if op == '-':
        # x - lhs = rhs  →  x = rhs + lhs
        # lhs - x = rhs  →  x = lhs - rhs
        return rhs_total + lhs if var_first else lhs - rhs_total
    if op in ('×', '*'):
        if lhs == 0:
            return None
        return rhs_total / lhs
    if op == '/':
        # x / lhs = rhs  →  x = rhs * lhs
        # lhs / x = rhs  →  x = lhs / rhs
        if not var_first and rhs_total == 0:
            return None
        return rhs_total * lhs if var_first else lhs / rhs_total
    return None


def _try_extract_expected_answer(stem: str) -> Optional[Tuple[str, float]]:
    """Walk the patterns A→D in order. Return (pattern_name, computed)
    for the first pattern that matches, else None.

    Order matters: more specific patterns first so e.g. a sum-with-
    blank (`a + b + ___ = total`) doesn't get false-matched by the
    pure-sum pattern.
    """
    if not stem:
        return None

    # Pattern A is handled by the existing fill-in-blank path; we
    # don't repeat it here (cross_check_question routes by qtype).

    # Pattern D — linear equation. Try both variable positions.
    for var_first, regex in (
        (True, _LINEAR_VAR_FIRST_RE),
        (False, _LINEAR_NUM_FIRST_RE),
    ):
        m = regex.search(stem)
        if not m:
            continue
        if var_first:
            _var, op, lhs_s, rhs_s = m.groups()
        else:
            lhs_s, op, _var, rhs_s = m.groups()
        lhs = _to_float(lhs_s)
        rhs = _to_float(rhs_s)
        if lhs is None or rhs is None:
            continue
        result = _solve_linear(op, lhs, rhs, var_first)
        if result is None:
            continue
        return ('linear', result)

    # Pattern C — multiplication chain.
    m = _MULT_CHAIN_RE.search(stem)
    if m:
        product = _mult_operands(m.group(1))
        if product is not None:
            return ('mult', product)

    # Pattern B — pure additive sum.
    m = _PURE_SUM_RE.search(stem)
    if m:
        total = _sum_operands(m.group(1))
        if total is not None:
            return ('sum', total)

    return None


def _extract_stored_answer(q: dict) -> Optional[float]:
    """Pull the numeric answer the question claims is correct, given
    its question_type. Returns None when the answer isn't stored
    numerically (free-text rubric, matching pairs, etc.)."""
    qtype = (q.get('question_type') or 'mcq').lower()

    if qtype == 'mcq':
        letter = (q.get('correct') or q.get('correct_answer') or '').upper().strip()
        if not letter:
            return None
        opt_text = q.get(f'option_{letter.lower()}') or q.get(letter)
        if not opt_text:
            return None
        m = re.search(r'-?\d+(?:\.\d+)?', str(opt_text))
        return _to_float(m.group(0)) if m else None

    if qtype == 'fill_in_blank':
        ad = q.get('answer_data') or {}
        blanks = ad.get('blanks') or []
        if len(blanks) != 1:
            return None
        return _to_float(str(blanks[0]))

    if qtype in ('short_answer', 'short_numeric', 'data_interpretation'):
        # `correct_answer` may be the field, or `answer_data.expected`.
        for src in (
            q.get('correct_answer'),
            q.get('answer'),
            (q.get('answer_data') or {}).get('expected'),
            (q.get('answer_data') or {}).get('answer'),
        ):
            if src:
                v = _to_float(str(src))
                if v is not None:
                    return v
        return None

    return None


def cross_check_question(
    q: dict, question_index: int = 0,
) -> Optional[dict]:
    """Compare the question's stored answer to what the stem actually
    computes to. Returns a structured audit entry on mismatch, else
    None.

    Audit entry shape:
      {
        'question_index': int,
        'pattern': 'sum'|'mult'|'linear'|'sum_with_blank',
        'computed': float,
        'claimed': float,
        'reason': str,
      }
    """
    stem = q.get('question_text') or q.get('question') or ''

    # Sum-with-blank (Pattern A) is exercised by the existing
    # verify_fill_in_blank / verify_mcq_arithmetic paths. Try it
    # explicitly here too, in case the caller is using the unified
    # `cross_check_question` API for routing.
    qtype = (q.get('question_type') or 'mcq').lower()
    if qtype == 'fill_in_blank':
        ad = q.get('answer_data') or {}
        blanks = ad.get('blanks') or []
        ok, err = verify_fill_in_blank(stem, blanks)
        if not ok and err:
            # Parse the err to get computed/expected for the audit.
            m = re.search(r'computed (-?\d+(?:\.\d+)?) but answer is (-?\d+(?:\.\d+)?)', err)
            if m:
                return {
                    'question_index': question_index,
                    'pattern': 'sum_with_blank',
                    'computed': float(m.group(1)),
                    'claimed': float(m.group(2)),
                    'reason': err,
                }

    # Patterns B / C / D — generic stem-vs-stored-answer check.
    extracted = _try_extract_expected_answer(stem)
    if extracted is None:
        return None
    pattern_name, computed = extracted

    stored = _extract_stored_answer(q)
    if stored is None:
        return None  # nothing to compare against

    if abs(computed - stored) < 0.5:  # half-unit tolerance
        return None

    return {
        'question_index': question_index,
        'pattern': pattern_name,
        'computed': computed,
        'claimed': stored,
        'reason': (
            f'{qtype}: stem ({pattern_name}) computes {computed:g}, '
            f'but stored answer is {stored:g}'
        ),
    }


# ─── Public API ──────────────────────────────────────────────────────

def is_broken(q: dict) -> Optional[str]:
    """Return a reason string if the question should be dropped, else None.

    Args:
      q: the LLM's question dict (or a model.to_dict-style dict).
         Required-ish keys:
           question / question_text  (the stem)
           explanation                (free text)
           question_type              (mcq | fill_in_blank | …)
           answer_data                (dict; blanks for fill-in-blank)
           correct                    (letter for mcq) — or correct_answer
           option_a..option_d         (mcq options)
    """
    qtype = (q.get('question_type') or 'mcq').lower()
    explanation = q.get('explanation') or ''

    # 1) explanation looks broken
    reason = explanation_looks_broken(explanation)
    if reason:
        return reason

    # 2) fill-in-blank numeric verification (Pattern A)
    if qtype == 'fill_in_blank':
        ad = q.get('answer_data') or {}
        blanks = ad.get('blanks') or []
        ok, err = verify_fill_in_blank(q.get('question') or q.get('question_text', ''), blanks)
        if not ok:
            return err

    # 3) mcq arithmetic sanity (Pattern A)
    if qtype == 'mcq':
        correct = (q.get('correct') or q.get('correct_answer') or '').upper().strip()
        options = {
            'A': q.get('option_a'),
            'B': q.get('option_b'),
            'C': q.get('option_c'),
            'D': q.get('option_d'),
        }
        ok, err = verify_mcq_arithmetic(
            q.get('question') or q.get('question_text', ''),
            options, correct,
        )
        if not ok:
            return err

    # 4) Layer 2 — patterns B / C / D cross-check.
    audit = cross_check_question(q)
    if audit is not None:
        return audit['reason']

    return None
