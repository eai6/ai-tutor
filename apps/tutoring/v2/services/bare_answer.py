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

# Also treat single MCQ option keys (letters A-E or digits 1-9) as
# "bare" — a wrong MCQ-option answer should still bias toward
# diagnostic phrasing. Range matches the module-level
# ``student_grader.MCQ_LETTER_CHARS`` / ``MCQ_DIGIT_CHARS`` constants;
# extending either range requires editing those constants and this
# regex together. (Imported in StudentGrader's _is_mcq_option_canonical;
# kept inline here to avoid a circular import.)
_BARE_OPTION_RE = re.compile(r"^\s*[A-Ea-e1-9]\s*\.?\s*$")


def is_bare_answer(student_input: str) -> bool:
    """Return True iff the student response is a single value with no working.

    Used by ``StudentGrader`` math path to set the ``bare_answer`` flag
    on the returned ``GradingResult``. Non-math grading leaves the
    flag False.

    Recognized as bare:
      - integers / decimals / simple fractions (± a short unit suffix)
      - single MCQ option key (letters A-E or digits 1-9)
    """
    if not student_input:
        return False
    text = student_input.strip()
    if not text:
        return False
    if _BARE_NUMERIC_RE.match(text):
        return True
    if _BARE_OPTION_RE.match(text):
        return True
    return False
