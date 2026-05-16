"""Rule-compliance judge — flags NO_AUTHORING / RULE_1 violations only.

Single-task focused prompt. ARITHMETIC was previously in this layer
but is fully deterministic now (apps/tutoring/llm_arithmetic_verifier
+ apps/tutoring/judges/arithmetic.py); the rule judge no longer
checks it.

Only runs when the response has content the rules could plausibly
apply to (digits, question marks, or praise vocabulary). The cheap
pre-filter in `_has_relevant_content` short-circuits otherwise.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Literal

from pydantic import BaseModel, Field

from apps.tutoring.judges._instructor_helper import (
    get_instructor_from_client,
    structured_completion,
)
from apps.tutoring.rule_compliance import (
    RULE_NO_AUTHORING,
    RULE_RULE_1,
    VALID_RULES,
    RuleViolation,
    _has_relevant_content,
)
from apps.tutoring.tracing import traced_judge

logger = logging.getLogger(__name__)


@dataclass
class RuleResult:
    violations: List[RuleViolation] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


_SYSTEM = (
    "You are a focused rule-compliance reviewer for a tutoring response. "
    "Flag violations of two rules ONLY:\n"
    "\n"
    "NO_AUTHORING — the tutor must NOT introduce concrete numerical "
    "values that aren't in input.bank_stems. Hypothetical scaffolding "
    "with invented numbers (\"if angles measure 100°, 120°, 80° — do "
    "they sum to 360°?\") IS a violation. ALLOWED: pure conceptual "
    "scaffolding (\"which rule applies?\"), reciting a rule without "
    "specific numerical setup, posing a question via the pose_question "
    "tool, or reusing a stem that appears verbatim in input.bank_stems. "
    "SKIP entirely when input.bank_offered is false (the tutor had no "
    "bank to draw from).\n"
    "\n"
    "RULE_1 — applies ONLY when input.subject_is_math is true. On math "
    "turns where input.student_answer_was_bare or "
    "input.student_answer_was_wrong is true, the tutor must NOT praise "
    "mastery in any phrasing. \"exactly\", \"perfect\", \"you've nailed "
    "it\", \"you've got the rule\", \"you understand\", \"smart\", "
    "\"spot on\" are violations in that math-bare context. Asking the "
    "student to walk through their work is the correct response — NOT "
    "a violation. On non-math turns (geography, science, history, "
    "language), praising a correct conceptual answer is normal "
    "teaching, NOT a RULE_1 violation. SKIP RULE_1 when "
    "subject_is_math is false.\n"
    "\n"
    "Return violations=[] when nothing to flag.\n"
)

from apps.tutoring.judges._prompt_meta import prompt_fingerprint
PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_SYSTEM)


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_RULE_CODE = Literal["NO_AUTHORING", "RULE_1"]


class _RuleViolationItem(BaseModel):
    rule: _RULE_CODE = Field(description="Which rule was violated.")
    evidence: str = Field(
        description="≤120-char quote from the tutor response.",
        max_length=200,
    )
    suggested_fix: str = Field(
        default="",
        description="One-sentence rewrite suggestion.",
        max_length=300,
    )


class _RuleVerdict(BaseModel):
    violations: List[_RuleViolationItem] = Field(
        default_factory=list,
        description="Zero or more rule violations identified in the tutor response.",
    )


@traced_judge('rule')
def run_rule_judge(
    response_text: str,
    *,
    bank_stems: List[str],
    student_input: str,
    answer_was_bare: bool,
    answer_was_wrong: bool,
    subject_is_math: bool = True,
    bank_offered: bool = True,
    llm_client=None,
    max_violations: int = 5,
    prior_exchanges: str = "",
) -> RuleResult:
    """Single-task rule-compliance check."""
    result = RuleResult()
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if llm_client is None:
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result
    if not _has_relevant_content(response_text):
        result.skipped = True
        result.skip_reason = "no_relevant_content"
        return result

    bank_block = (
        "\n".join(f"  - {s.strip()[:200]}" for s in bank_stems[:10])
        or "  (bank empty)"
    )
    payload = {
        "tutor_response": response_text[:2000],
        "student_last_input": (student_input or "").strip()[:200] or "(none)",
        "prior_exchanges": (prior_exchanges or "")[:1600] or "(none)",
        "student_answer_was_bare": answer_was_bare,
        "student_answer_was_wrong": answer_was_wrong,
        "verified_question_bank": bank_block,
        "subject_is_math": bool(subject_is_math),
        "bank_offered": bool(bank_offered),
    }
    user_prompt = (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Review the tutor response for rule violations. Use "
        "input.prior_exchanges to calibrate RULE_1: praise such as "
        "\"great work\" can be valid when the most recent prior STUDENT "
        "turn in prior_exchanges showed correct working, even when "
        "input.student_last_input is itself a non-answer "
        "(acknowledgement, follow-up question)."
    )

    instructor_client = get_instructor_from_client(llm_client)
    if instructor_client is None:
        result.skipped = True
        result.skip_reason = "instructor_unavailable"
        return result

    try:
        verdict = structured_completion(
            instructor_client,
            _RuleVerdict,
            system_prompt=_SYSTEM,
            user_prompt=user_prompt,
            max_tokens=3500,
            provider=str(getattr(llm_client.config, 'provider', '')),
        )
    except Exception as e:
        logger.warning("[RuleJudge] call failed: %s", e)
        result.skipped = True
        result.skip_reason = f"llm_error: {type(e).__name__}"
        return result

    for item in (verdict.violations or [])[:max_violations]:
        rule = item.rule
        # Programmatic safety net: drop RULE_1 on non-math, drop
        # NO_AUTHORING when no bank was offered.
        if not subject_is_math and rule == RULE_RULE_1:
            logger.info("[RuleJudge] dropping RULE_1 on non-math turn")
            continue
        if not bank_offered and rule == RULE_NO_AUTHORING:
            logger.info("[RuleJudge] dropping NO_AUTHORING when bank not offered")
            continue
        result.violations.append(RuleViolation(
            rule=rule,
            evidence=(item.evidence or "")[:200],
            suggested_fix=(item.suggested_fix or "")[:300],
        ))
    return result
