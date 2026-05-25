"""Two-LLM canonical answer commitment for chat-authored questions.

When the tutor authors a free-form prose question (i.e. NOT via
``pose_question`` / ``pose_inline_question``), there is no answer key.
Historically ``grade_chat_authored_question`` would derive the canonical
answer at grade time with a prompt that explicitly said "be generous on
phrasing" — the same model family that didn't know the answer when the
question was authored is graded against itself, with the student's reply
already in context. That path graded wrong answers as correct with
suspicious frequency (the v3 benchmark showed 0% specificity on
``answer_correct``).

This module commits the canonical answer BEFORE the student's response
is graded, in a separate judge-purpose LLM call (temperature 0). The
caller stores the committed canonical alongside the in-flight question
state and passes it through to the grader, which then matches against
the frozen canonical instead of re-deriving.

Pattern mirrors ``apps/tutoring/fact_verifier.py``:
  - Single public entry: ``commit_canonical``
  - Dataclass result with explicit ``confidence`` + ``skipped`` fields
  - Non-blocking failure: returns ``skipped=True`` on any error so the
    caller can fall back to the legacy derive-at-grade-time path
  - JUDGE purpose ModelConfig → temperature forced to 0 at the client

Cost: one extra LLM call per chat-authored question per session. Bank
questions (the preferred path per ``socratic_rules``) skip this entirely
because they already have verified answer keys.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional


logger = logging.getLogger(__name__)


# When the committer's self-reported confidence is below this floor we
# treat the commitment as not trustworthy enough to use as a grading
# anchor. Callers should fall back to the legacy chat-authored grader
# (or, ideally, force the tutor onto a bank / pose_inline_question
# pull on the next turn — that fallback is the caller's responsibility).
CONFIDENCE_FLOOR = 0.6


@dataclass
class CanonicalCommitment:
    """Frozen canonical answer for a chat-authored tutor question.

    Returned by ``commit_canonical``. Caller persists this on the
    in-flight question state so the grader receives the committed
    canonical instead of deriving one on the fly.
    """
    canonical: str = ""
    acceptable_variants: List[str] = field(default_factory=list)
    answer_type: str = "concept"      # 'numeric' | 'short_answer' | 'concept'
    confidence: float = 0.0           # 0.0 - 1.0 (committer self-report)
    reasoning: str = ""               # one-line trace, NOT shown to student
    skipped: bool = False
    skip_reason: Optional[str] = None

    @property
    def trustworthy(self) -> bool:
        """True when the commitment can be used as a grading anchor.

        Skipped commitments, empty canonicals, and low-confidence
        results all return False so the caller can fall back cleanly.
        """
        return (
            not self.skipped
            and bool(self.canonical.strip())
            and self.confidence >= CONFIDENCE_FLOOR
        )


_COMMITTER_SYSTEM = (
    "You commit the CORRECT ANSWER to a tutor's free-form question BEFORE the "
    "student replies. A downstream grader will judge the student's response "
    "against your committed canonical — so your commitment IS the answer key.\n"
    "\n"
    "Requirements:\n"
    "- canonical: the single most precise correct answer to the question. If "
    "the question is numeric, the exact value (with units if relevant). If "
    "short-answer, the canonical phrase. If conceptual, the core idea in "
    "one sentence.\n"
    "- acceptable_variants: 0-5 alternative correct phrasings a student might "
    "reasonably give. Example: question 'What is the capital of France?' → "
    "canonical 'Paris', acceptable_variants []. Question 'What does a compass "
    "rose show?' → canonical 'directions / cardinal points', acceptable_variants "
    "['the directions', 'N/S/E/W', 'cardinal and intercardinal directions'].\n"
    "- answer_type: 'numeric' for numbers, 'short_answer' for named entities / "
    "short phrases, 'concept' for explanations.\n"
    "- confidence: 0.0-1.0 — how certain you are the committed canonical is "
    "actually correct given the curriculum context. Be honest. If the question "
    "is ambiguous or you'd need to consult something to be sure, output a low "
    "confidence (< 0.6) and the caller will fall back instead of using your "
    "commitment as truth.\n"
    "- reasoning: ONE short sentence explaining the canonical, citing the "
    "curriculum context if it supports the answer.\n"
    "\n"
    "Critical rules:\n"
    "- DO NOT be generous on phrasing here — that's the grader's job. Commit "
    "the SPECIFIC correct answer; the grader compares the student's response "
    "to it.\n"
    "- DO NOT make up information. If the curriculum context doesn't support "
    "an answer and you don't know it confidently, output confidence < 0.5 with "
    "your best guess as canonical.\n"
    "- DO NOT see a student response (there isn't one yet — this commitment "
    "happens before the student replies)."
)


def _build_user_prompt(question_text: str, lesson_context: str, kb_chunks: str) -> str:
    """Assemble the user-side prompt for the committer LLM call."""
    parts = [
        "Commit the correct answer to this tutor-authored question. Reply with "
        "ONLY a JSON object — no markdown fences, no preamble. Schema:\n"
        '{"canonical": str, "acceptable_variants": list[str], '
        '"answer_type": "numeric"|"short_answer"|"concept", '
        '"confidence": float (0.0-1.0), "reasoning": str}',
        "",
        f"QUESTION:\n{question_text.strip()[:1500]}",
    ]
    if lesson_context and lesson_context.strip():
        parts.extend(["", f"LESSON CONTEXT:\n{lesson_context.strip()[:1500]}"])
    if kb_chunks and kb_chunks.strip():
        parts.extend(["", f"CURRICULUM EVIDENCE:\n{kb_chunks.strip()[:2000]}"])
    return "\n".join(parts)


def _coerce_result(raw: str) -> CanonicalCommitment:
    """Parse the LLM's JSON response into a CanonicalCommitment.

    Defensive: strips markdown fences, defaults missing fields, clamps
    confidence to [0, 1], coerces variants to a string list.
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except Exception as exc:
        logger.warning(
            "[CanonicalCommit] JSON parse failed: %s — raw=%r",
            exc, text[:200],
        )
        return CanonicalCommitment(
            skipped=True,
            skip_reason=f"json_parse:{type(exc).__name__}",
        )
    if not isinstance(data, dict):
        return CanonicalCommitment(skipped=True, skip_reason="not_a_dict")

    canonical = str(data.get("canonical", "") or "").strip()
    variants_raw = data.get("acceptable_variants") or []
    if isinstance(variants_raw, list):
        variants = [str(v).strip() for v in variants_raw if str(v).strip()][:5]
    else:
        variants = []
    answer_type = str(data.get("answer_type", "concept")).strip().lower()
    if answer_type not in {"numeric", "short_answer", "concept"}:
        answer_type = "concept"
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(data.get("reasoning", "") or "").strip()[:300]

    return CanonicalCommitment(
        canonical=canonical,
        acceptable_variants=variants,
        answer_type=answer_type,
        confidence=confidence,
        reasoning=reasoning,
    )


def commit_canonical(
    question_text: str,
    *,
    llm_client,
    lesson_context: str = "",
    kb_chunks: str = "",
    max_tokens: int = 500,
) -> CanonicalCommitment:
    """Commit the canonical answer for a chat-authored question.

    Args:
        question_text: the tutor's prose question (the stem).
        llm_client: a ``BaseLLMClient`` instance. Caller picks the
            ModelConfig — JUDGE purpose recommended for temp=0.
        lesson_context: short overview of the current lesson — title,
            objective, current step. Anchors the committer to the
            lesson's intended scope.
        kb_chunks: optional retrieved curriculum context. Use when the
            answer depends on lesson-specific facts (place names, dates,
            local references).
        max_tokens: response cap. 500 is generous for the schema; raise
            if reasoning fields get truncated.

    Returns:
        ``CanonicalCommitment``. On any error returns ``skipped=True``
        so the caller can fall back to the legacy derive-at-grade-time
        path without crashing the turn.
    """
    if not question_text or not question_text.strip():
        return CanonicalCommitment(
            skipped=True, skip_reason="empty_question_text",
        )
    if llm_client is None:
        return CanonicalCommitment(
            skipped=True, skip_reason="no_llm_client",
        )

    user_prompt = _build_user_prompt(
        question_text=question_text,
        lesson_context=lesson_context,
        kb_chunks=kb_chunks,
    )
    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_COMMITTER_SYSTEM,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        logger.warning(
            "[CanonicalCommit] LLM call failed: %s: %s",
            type(exc).__name__, str(exc)[:200],
        )
        return CanonicalCommitment(
            skipped=True,
            skip_reason=f"llm_crash:{type(exc).__name__}",
        )

    result = _coerce_result((getattr(response, "content", "") or ""))
    logger.info(
        "[CanonicalCommit] q=%r → canonical=%r confidence=%.2f variants=%d "
        "trustworthy=%s",
        question_text[:80], result.canonical[:80], result.confidence,
        len(result.acceptable_variants), result.trustworthy,
    )
    return result
