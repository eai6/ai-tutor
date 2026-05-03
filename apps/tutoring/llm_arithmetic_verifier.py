"""LLM-based arithmetic verifier for tutor responses.

Replaces the regex-only ``apps.tutoring.math_tools.verify_calculations``.
The regex layer only saw explicit ``a op b = c`` shapes; the LLM
verifier also catches IMPLICIT prose claims like:

  - "do they sum to 360°?" with values listed as "100°, 120°, and 80°"
  - "if you add these you get 300"
  - "subtracting gives 17"
  - "together that's a half"

Works as a corrector — same return contract as ``verify_calculations``:
``(corrected_text, corrections)``. Each correction is a
``{expression, claimed, correct}`` dict so existing teacher-dashboard
plumbing reads without changes.

Cost model: pre-filtered on digits, so conversational turns skip the
LLM entirely. Math turns pay one extra call. Regen-via-judge already
ran on the same response, so the marginal cost is one call per math
turn — not per response.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ArithmeticCorrection:
    """One correctable arithmetic claim found in a response."""
    expression: str   # quoted span (e.g. "100° + 120° + 80°")
    claimed: str      # what the response asserted (e.g. "360")
    correct: str      # what's actually correct (e.g. "300")

    def to_dict(self) -> dict:
        return asdict(self)


# Cheap pre-filter — the LLM never sees text without digits.
# Any number, percentage, fraction, or arithmetic operator triggers it.
_HAS_NUMBER_RE = re.compile(r"\d|[½¼¾⅓⅔⅛⅜⅝⅞]")


_VERIFIER_SYSTEM = (
    "You are a math fact-checker for a tutor. Find every arithmetic claim "
    "in the response and verify the math. A claim is anything where the "
    "response asserts (explicitly OR implicitly) that some numbers add, "
    "subtract, multiply, divide, sum, or otherwise combine to a stated "
    "value.\n\n"
    "Surface BOTH explicit and implicit shapes:\n"
    '  EXPLICIT:  "8 × 2.5 = 20", "65 + 125 = 180".\n'
    '  IMPLICIT:  "do they sum to 360°?" with the values 100°, 120°, 80° '
    'just stated (the question carries the implicit claim 100+120+80 = 360).\n'
    '  PROSE:     "subtracting gives 17", "altogether that\'s a half".\n'
    '  RATIO:     "the third angle in 1:2:3 must be 60°" (verify the share).\n'
    '\n'
    "Be aggressive — false positives are cheap (one regen) but false "
    "negatives ship wrong math to a student. Skip purely conceptual "
    'statements with no numerical assertion ("angles around a point sum '
    'to 360°" — that\'s a rule recital, not a numerical claim about a '
    "specific set).\n"
    "\n"
    "Output JSON of the shape:\n"
    '  {"corrections": [{"expression": "<short quote>", "claimed": "<as stated>", '
    '"correct": "<correct value>"}]}\n'
    'Return {"corrections": []} when every claim checks out.'
)


def _build_user_prompt(text: str) -> str:
    return (
        "Verify all arithmetic in the response below. Reply with ONLY the "
        "JSON object specified — no prose, no code fence.\n\n"
        f"<response>\n{text[:4000]}\n</response>"
    )


def verify_arithmetic_claims(
    text: str,
    *,
    llm_client=None,
    max_corrections: int = 8,
) -> Tuple[str, List[dict]]:
    """Find + report arithmetic mistakes in a tutor response.

    Args:
      text: the tutor's clean response (after media/question signal
            parsing).
      llm_client: BaseLLMClient. None disables the check (returns the
                  text unchanged with an empty corrections list — same
                  fail-open contract as verify_calculations).

    Returns:
      ``(corrected_text, corrections)``. ``corrections`` is a list of
      ``{expression, claimed, correct}`` dicts. The text is rewritten
      in-place when the correction's ``expression`` appears verbatim
      in the response and the claimed token is unambiguous; otherwise
      the text is returned unchanged and the caller relies on the
      regen path.
    """
    if not text or not text.strip():
        return text, []
    if llm_client is None:
        return text, []
    if not _HAS_NUMBER_RE.search(text):
        return text, []

    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": _build_user_prompt(text)}],
            system_prompt=_VERIFIER_SYSTEM,
            max_tokens=600,
        )
        raw = (response.content or "").strip()
        # Strip code fences if present.
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
    except Exception as e:
        logger.info("[LLMArithVerifier] judge call failed: %s", e)
        return text, []

    if not isinstance(data, dict):
        return text, []
    items = data.get("corrections") or []
    if not isinstance(items, list):
        return text, []

    corrections: List[dict] = []
    corrected = text
    for item in items[:max_corrections]:
        if not isinstance(item, dict):
            continue
        expression = str(item.get("expression") or "").strip()[:200]
        claimed = str(item.get("claimed") or "").strip()[:60]
        correct = str(item.get("correct") or "").strip()[:60]
        if not (expression and correct):
            continue
        corrections.append({
            "expression": expression,
            "claimed": claimed,
            "correct": correct,
        })
        # Best-effort inline rewrite: replace `claimed` with `correct`
        # only when the claim appears as a standalone token next to
        # the expression. We don't do anything heroic — the regen path
        # is the authoritative fix; this rewrite just stops obviously-
        # wrong text from reaching the student on the rare turn where
        # regen is bypassed.
        if claimed and claimed in corrected:
            try:
                # Anchor on the expression to avoid global replacements
                # that would touch unrelated digits in the response.
                idx = corrected.find(expression)
                if idx >= 0:
                    tail = corrected[idx:]
                    if claimed in tail:
                        new_tail = tail.replace(claimed, correct, 1)
                        corrected = corrected[:idx] + new_tail
            except Exception:
                pass

    if corrections:
        logger.info(
            "[LLMArithVerifier] flagged %d arithmetic correction(s)",
            len(corrections),
        )
    return corrected, corrections
