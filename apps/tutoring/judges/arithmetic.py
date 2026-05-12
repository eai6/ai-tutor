"""Arithmetic-correction judge — flags arithmetic claims in the tutor's
prose that don't compute.

Fully deterministic on the simple cases (X + Y = Z chains) via the
existing `verify_arithmetic_claims` helper. Falls to LLM only when the
deterministic checker can't parse the expression.

Note: most arithmetic verification has already moved to deterministic
code (apps/tutoring/llm_arithmetic_verifier — historically that was
LLM-based but has a deterministic fast path now). This module is a
thin wrapper that returns the same shape combined_judge used to
produce so the orchestrator can plug it in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from apps.tutoring.llm_arithmetic_verifier import (
    _HAS_NUMBER_RE,
    verify_arithmetic_claims,
)
from apps.tutoring.tracing import traced_judge

logger = logging.getLogger(__name__)


@dataclass
class ArithmeticResult:
    corrected_response: str = ""
    corrections: List[dict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


@traced_judge('arithmetic')
def run_arithmetic_judge(
    response_text: str,
    *,
    llm_client=None,
    subject_is_math: bool = True,
) -> ArithmeticResult:
    """Single-task arithmetic verification.

    Fast-paths via the existing deterministic / LLM hybrid in
    `verify_arithmetic_claims`. Skips when:
      - response is empty
      - subject isn't math (no arithmetic claims expected)
      - response has no numbers
    """
    result = ArithmeticResult(corrected_response=response_text or "")
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if not subject_is_math:
        result.skipped = True
        result.skip_reason = "non_math_subject"
        return result
    if not _HAS_NUMBER_RE.search(response_text):
        result.skipped = True
        result.skip_reason = "no_numbers"
        return result

    try:
        corrected, corrections = verify_arithmetic_claims(
            response_text, llm_client=llm_client,
        )
    except Exception as e:
        logger.warning("[ArithmeticJudge] call failed: %s", e)
        result.skipped = True
        result.skip_reason = f"verify_error: {type(e).__name__}"
        return result

    result.corrected_response = corrected
    result.corrections = list(corrections or [])
    return result
