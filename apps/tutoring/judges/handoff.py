"""
DEPRECATED (Phase 3 §3.5 — refactor implementation plan).

This module is part of the legacy tutoring pipeline. The v2 grader /
tutor / conformance engine in ``apps.tutoring.v2`` replaces it. Kept
loaded for resume of in-flight legacy sessions and as the kill-switch
fallback (``NEW_TUTOR=off``). **Do not add new features here.**

Deletion gate (Phase 3 §3.5):
  1. v2 has served prod traffic ≥ 4 weeks post-cutover.
  2. Zero kill-switch flips during that window.
  3. Three consecutive weekly benchmark runs within ±2 pp of
     cutover numbers on each P1 category.
  4. No open P1 incidents tied to the v2 engine.

Original module docstring follows:

Handoff judge — flags tutor turns that don't hand the floor back
to the student (no question, no clear next-step prompt, dangling
transition with no follow-through).

Pilot 2026-05-17 lesson 540 session 52 turn 886 surfaced the gap that
motivated this judge: tutor said "Now let me ask you about a different
map feature:" and ENDED — no question rendered, no pose_question
called. Regex-only validators slip past these cases because phrases
like "Let's try a different angle" earlier in the response satisfy a
naive call-to-action check, while the actual parting line is a
broken promise.

Single LLM judgment per turn. The judge sees the whole tutor response
(and optionally the prior tutor turn for cross-turn handoff context)
and returns a boolean + reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from apps.tutoring.judges._instructor_helper import (
    get_instructor_from_client,
    structured_completion,
)
from apps.tutoring.tracing import traced_judge

logger = logging.getLogger(__name__)


@dataclass
class HandoffResult:
    handed_off: bool = True   # default-pass on skip
    reason: str = ""
    skipped: bool = False
    skip_reason: str = ""


_SYSTEM = (
    "You are a focused tutor-turn handoff judge. Decide whether the "
    "tutor's response HANDS THE FLOOR BACK to the student — does the "
    "student know what to do or say next?\n"
    "\n"
    "A handoff requires the response to end with one of:\n"
    "  - An ACTUAL QUESTION the student is meant to answer (single "
    "question or multi-part with one focus)\n"
    "  - A clear DIRECTIVE for the student to do something specific "
    "next (\"try this problem\", \"pick one\", \"tell me what you "
    "notice about the figure\")\n"
    "  - A short rhetorical / reflective question followed by the "
    "actual question (\"Notice the pattern? Now solve x + 5 = 12.\")\n"
    "\n"
    "Things that DO NOT count as a handoff:\n"
    "  - Promise of a question without delivering it (\"Now let me "
    "ask you about a different feature:\" with nothing after)\n"
    "  - Pure praise / acknowledgement (\"Great work!\", \"Exactly "
    "right!\") with no next-step prompt\n"
    "  - A teaching paragraph that just ends — no invitation\n"
    "  - A transition that announces the next topic but doesn't ask "
    "anything (\"Let's move on to scale.\")\n"
    "  - A dangling colon or ellipsis after a setup phrase\n"
    "\n"
    "Note on bank questions: if the tutor's response references a "
    "bank question that the engine will render OUTSIDE this text "
    "(via pose_question tool — see 'bank_will_render' input), the "
    "bank question itself counts as the handoff. The text doesn't "
    "need to repeat it. When bank_will_render=true, only flag when "
    "the text is overtly inconsistent with handing off (e.g. ends "
    "mid-sentence or says 'no more questions today').\n"
    "\n"
    "Be CONSERVATIVE on borderline cases. If the response ends with "
    "a real question or a clear next-action directive, it's a "
    "handoff. Only flag handed_off=false when the parting line "
    "leaves the student genuinely without direction.\n"
    "\n"
    "When handed_off=false, the reason should briefly name WHAT was "
    "missing (≤140 chars): \"dangling colon with no question\", "
    "\"pure acknowledgement, no next-step\", \"promised next Q but "
    "didn't deliver\".\n"
)

from apps.tutoring.judges._prompt_meta import prompt_fingerprint
PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_SYSTEM)


class _HandoffVerdict(BaseModel):
    handed_off: bool = Field(
        description=(
            "True when the tutor response hands the floor back to the "
            "student via a real question or clear next-action "
            "directive. False when the response leaves the student "
            "without direction (dangling promise, pure ack, etc.)."
        ),
    )
    reason: str = Field(
        default="",
        description=(
            "When handed_off=false, ≤140 chars naming WHAT is missing. "
            "When true, leave empty."
        ),
    )


def _too_short_to_judge(text: str) -> bool:
    """Skip when text is too short to reasonably contain a handoff
    failure — single-word or empty responses are handled elsewhere."""
    if not text:
        return True
    return len(text.strip()) < 40


@traced_judge('handoff')
def run_handoff_judge(
    response_text: str,
    *,
    llm_client=None,
    bank_will_render: bool = False,
) -> HandoffResult:
    """Decide whether the tutor's response hands the floor back to
    the student. Returns HandoffResult(handed_off=True|False, reason).

    Skips (default-pass) when:
      - response is empty / too short to be a real turn
      - llm_client / instructor unavailable
      - LLM call errors

    `bank_will_render`: when True, the engine has a separate
    pose_question that will render a bank Q outside this text — the
    judge treats the bank Q as the handoff and only flags overt
    text inconsistencies.
    """
    result = HandoffResult()
    if _too_short_to_judge(response_text):
        result.skipped = True
        result.skip_reason = "too_short"
        return result
    if llm_client is None:
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result

    instructor_client = get_instructor_from_client(llm_client)
    if instructor_client is None:
        result.skipped = True
        result.skip_reason = "instructor_unavailable"
        return result

    bank_block = (
        f"bank_will_render: {str(bool(bank_will_render)).lower()}\n\n"
    )
    user_prompt = (
        f"{bank_block}"
        f"TUTOR_RESPONSE (the one to review):\n"
        f"{response_text[:2500]}\n\n"
        "Did this response hand the floor back to the student? "
        "Output handed_off=true with empty reason when yes, "
        "handed_off=false with a brief reason when no."
    )

    try:
        verdict = structured_completion(
            instructor_client,
            _HandoffVerdict,
            system_prompt=_SYSTEM,
            user_prompt=user_prompt,
            max_tokens=300,
            provider=str(getattr(llm_client.config, 'provider', '')),
        )
    except Exception as e:
        logger.warning("[HandoffJudge] call failed: %s", e)
        result.skipped = True
        result.skip_reason = f"llm_error: {type(e).__name__}"
        return result

    result.handed_off = bool(verdict.handed_off)
    result.reason = (verdict.reason or "").strip()[:200]
    return result
