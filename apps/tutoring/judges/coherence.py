"""Coherence judge — flags self-contradictions inside a single tutor
response.

Production transcripts (Savy Eva pilot, 2026-05-04) showed the tutor
saying things like "let's explore three angles" then immediately
posing a TWO-angle question — the student replied "YOU SAID 3
ANGLES". Other observed failure modes:

  - "Excellent! ✓ correct ... Not quite!" praise / contradict in
    the same response
  - changes the setup mid-explanation ("the angle is 50°" then "now
    consider when it's 65°" without scaffolding)
  - references concepts the student hasn't been introduced to yet
  - states a rule then immediately gives a counter-example without
    flagging it as one

Single-task focused prompt. The judge produces a list of violation
strings — the validator decides whether to escalate to regen.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List
from apps.tutoring.tracing import traced_judge

logger = logging.getLogger(__name__)


@dataclass
class CoherenceResult:
    violations: List[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


_SYSTEM = (
    "You are a focused coherence reviewer for a tutoring response. "
    "Flag SELF-CONTRADICTIONS and INCOHERENT FRAMING — places where "
    "the tutor's response isn't a single coherent piece of teaching. "
    "Three scopes count:\n"
    "  - WITHIN tutor_response (single-response contradiction)\n"
    "  - BETWEEN tutor_response and the most recent TUTOR turn in "
    "prior_exchanges (cross-turn flip — the tutor reversed a stance, "
    "setup, or numerical value across consecutive turns)\n"
    "  - STRUCTURAL — the response poses TWO OR MORE DISTINCT "
    "questions in parallel, leaving the student unsure which to "
    "answer first. A coherent turn asks ONE focused question.\n"
    "When prior_exchanges is empty / not provided, only the within-"
    "response and structural scopes apply.\n"
    "\n"
    "Examples that ARE coherence violations:\n"
    "  - introduces a setup with N items, then poses a question with M "
    "items (N != M) without explaining the change\n"
    "  - praises the student as correct then says 'not quite' / 'that's "
    "wrong' / corrects them in the same response\n"
    "  - states a rule, then states a contradicting rule without "
    "flagging the second as an exception or counter-example\n"
    "  - changes a concrete value mid-explanation (\"the angle is 50°… "
    "so we have 65° + x = 180°\") without flagging it\n"
    "  - tells the student to do X, then immediately tells them to "
    "do not-X\n"
    "  - poses TWO distinct questions in parallel — e.g. asks a "
    "conceptual question (\"what was the first thing you noticed?\") "
    "AND a separate practice problem (\"If x + 15 = 40, what is x?\") "
    "in the same response. Flag as: \"two parallel questions: <Q1>; "
    "<Q2>\". Single-question scaffolding is FINE (see non-violations).\n"
    "\n"
    "Examples that are NOT violations (do NOT flag these):\n"
    "  - posing a follow-up question after explaining (normal scaffolding)\n"
    "  - acknowledging a partial answer then asking for the rest\n"
    "  - stating a rule then giving an EXAMPLE that uses it\n"
    "  - explicitly contrasting two cases (\"unlike X, in Y…\") — that's "
    "a teaching contrast, not a contradiction\n"
    "  - a SINGLE question that has multiple parts but one focus "
    "(\"what is x, and how did you find it?\") — that's one question\n"
    "  - rhetorical / reflective question followed by the real "
    "question (\"Notice the pattern? Now solve x + 5 = 12.\") — one\n"
    "    teaching beat, one ask.\n"
    "\n"
    "Be CONSERVATIVE. Only flag clear, demonstrable issues a student "
    "would notice. When in doubt, return [].\n"
    "\n"
    "Output JSON ONLY (no prose, no code fence):\n"
    "{\"violations\": [\"<short description, <= 140 chars, naming WHAT "
    "contradicts WHAT — or for parallel questions, name BOTH>\"]}\n"
    "Empty array when nothing to flag.\n"
)

from apps.tutoring.judges._prompt_meta import prompt_fingerprint
PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_SYSTEM)


# Cheap pre-gate: skip the LLM when the response is too short to
# plausibly contradict itself, OR is clearly conversational filler.
def _has_enough_content(text: str) -> bool:
    if not text:
        return False
    # Require at least two sentences AND > 200 chars — single-sentence
    # acknowledgements ("Great work!") can't contradict themselves.
    sentence_count = len(re.findall(r'[.!?]\s', text)) + 1
    return len(text.strip()) >= 200 and sentence_count >= 2


@traced_judge('coherence')
def run_coherence_judge(
    response_text: str,
    *,
    llm_client=None,
    max_violations: int = 4,
    prior_exchanges: str = "",
) -> CoherenceResult:
    """Single-task coherence check.

    Skips when:
      - response is empty / too short to contradict itself
      - llm_client is None
    """
    result = CoherenceResult()
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if llm_client is None:
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result
    if not _has_enough_content(response_text):
        result.skipped = True
        result.skip_reason = "too_short_for_contradiction"
        return result

    # Documents-first / instructions-last: prior_exchanges + the
    # response go up top; the task line sits at the bottom where the
    # model reads it most strongly.
    prior_block = (
        f"PRIOR_EXCHANGES (last turns, for cross-turn check):\n"
        f"{prior_exchanges}\n\n"
        if prior_exchanges else ""
    )
    user_prompt = (
        f"{prior_block}"
        f"TUTOR_RESPONSE (the one to review):\n{response_text[:2500]}\n\n"
        "Flag self-contradictions inside TUTOR_RESPONSE, and any "
        "contradiction between TUTOR_RESPONSE and the most recent "
        "TUTOR turn in PRIOR_EXCHANGES. Reply with ONLY the JSON "
        "object specified."
    )

    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_SYSTEM,
            max_tokens=400,
        )
        raw = (response.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[CoherenceJudge] call failed: %s", e)
        result.skipped = True
        result.skip_reason = f"llm_error: {type(e).__name__}"
        return result

    items = data.get("violations") or []
    if not isinstance(items, list):
        return result

    for item in items[:max_violations]:
        text = str(item or "").strip()
        if text:
            result.violations.append(text[:200])
    return result
