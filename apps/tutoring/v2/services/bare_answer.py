"""Bare-answer detection on the math path — Phase 2 §2.1.1.

Deterministic; runs before any LLM call. The flag is consumed by the
move *prompt*, not by move *selection*:

  - correct + bare_answer → move=confirm_and_advance, prompt biases
    toward a brief "because…" affirmation.
  - wrong + bare_answer → move=scaffold_hint, prompt biases toward
    "show your working" diagnostic phrasing.

Per the CLAUDE.md math-tutoring rule (2026-05-17 reversal): a correct
bare answer gets confirmed + advanced, not probed; a wrong bare
answer triggers a single ask-for-working as diagnosis.
"""

from __future__ import annotations

import re

# Match a single value with no surrounding working. Tolerates:
#   - integers, decimals (e.g. 42, -3.14)
#   - simple fractions (e.g. 3/4, -1/2)
#   - units / symbols (°, %, cm, m, kg, etc.) appended
_BARE_NUMERIC_RE = re.compile(
    r"""
    ^                          # start
    \s*
    (?:                        # one of:
        -?\d+(?:\.\d+)?        # decimal/int
        |
        -?\d+\s*/\s*\d+        # simple fraction
    )
    \s*
    (?:°|%|degrees|cm|mm|m|km|kg|g|°C|°F|/\d+)?  # optional unit/symbol
    \s*
    \.?                        # optional trailing period
    \s*
    $                          # end
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Also treat single-letter MCQ-style answers as "bare" — the math
# path doesn't see them, but a wrong MCQ-letter answer in a math
# context (e.g. multiple-choice algebra) should still bias toward
# diagnostic phrasing.
_BARE_LETTER_RE = re.compile(r"^\s*[A-Da-d]\s*\.?\s*$")


def is_bare_answer(student_input: str) -> bool:
    """Return True iff the student response is a single value with no working.

    Used by ``StudentGrader`` math path to set the ``bare_answer`` flag
    on the returned ``GradingResult``. Non-math grading leaves the
    flag False.
    """
    if not student_input:
        return False
    text = student_input.strip()
    if not text:
        return False
    if _BARE_NUMERIC_RE.match(text):
        return True
    if _BARE_LETTER_RE.match(text):
        return True
    return False
