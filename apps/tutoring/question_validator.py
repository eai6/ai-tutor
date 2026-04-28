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

    # 2) fill-in-blank numeric verification
    if qtype == 'fill_in_blank':
        ad = q.get('answer_data') or {}
        blanks = ad.get('blanks') or []
        ok, err = verify_fill_in_blank(q.get('question') or q.get('question_text', ''), blanks)
        if not ok:
            return err

    # 3) mcq arithmetic sanity
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

    return None
