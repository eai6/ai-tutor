"""Rule-compliance check for tutor responses (P5 of
memory/tutor_no_authoring_plan.md).

LLM-as-judge layer that verifies the tutor followed three rules the
deterministic regex layers can't fully cover:

  NO_AUTHORING — every question posed must come from the bank. The
                 tutor is allowed to pose a question only via
                 |||QUESTION:N|||; any standalone "?" sentence with
                 numbers not in the bank is a violation.
  ARITHMETIC   — any arithmetic claim ("X + Y = Z", "they sum to Z",
                 "subtracting gives Z") must be numerically correct.
                 Catches the prose-form failures that
                 verify_calculations regex misses.
  RULE_1       — praise like "exactly", "you've nailed it", "perfect
                 understanding" is forbidden when the student's last
                 answer was bare or wrong (math_teaching Rule 1
                 generalised to praise SYNONYMS, not just literal
                 blocklist words).

Designed to fail-closed: if the LLM client is missing or the call
fails, returns "skipped" rather than blocking the response.

Pattern mirrors apps/tutoring/fact_verifier.py — same structured-output
LLM-judge pattern, same metadata-friendly result type.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# Rule names — string constants so they serialise cleanly into
# SessionTurn.metadata.
RULE_NO_AUTHORING = "NO_AUTHORING"
RULE_ARITHMETIC = "ARITHMETIC"
RULE_RULE_1 = "RULE_1"

VALID_RULES = frozenset({RULE_NO_AUTHORING, RULE_ARITHMETIC, RULE_RULE_1})


# Cheap pre-filter — only invoke the judge when the response has SOMETHING
# the rules could plausibly apply to. Conversational turns
# ("Let's look at your working.") have no digits, no question marks,
# and no praise vocabulary; the judge would always return empty so the
# call is wasted budget.
_RELEVANT_CONTENT_RE = re.compile(
    r"\d|"                                              # any digit
    r"\?|"                                              # question mark
    # Word STEMS — no trailing \b so "nail" matches "nailed",
    # "exact" matches "exactly", "right" matches "rightly", etc.
    r"\b(?:exact|nail|brillian|perfect|excellent|"
    r"got it|right|smart|spot on|well done|"
    r"got the rule|understand|amazing|"
    r"find|calculate|what is|how many|solve)",
    re.IGNORECASE,
)


def _has_relevant_content(text: str) -> bool:
    """Return True when the response contains content the rules could
    apply to. Used to short-circuit the judge call on conversational
    turns. Slightly over-broad on purpose — false positives just mean
    one extra LLM call, false negatives mean a violation slips through."""
    return bool(text) and bool(_RELEVANT_CONTENT_RE.search(text))


@dataclass
class RuleViolation:
    rule: str  # one of VALID_RULES
    evidence: str = ""  # quoted span from the response
    suggested_fix: str = ""  # one-sentence rewrite


@dataclass
class RuleComplianceResult:
    violations: List[RuleViolation] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""

    @property
    def has_violations(self) -> bool:
        return len(self.violations) > 0

    @property
    def violated_rules(self) -> List[str]:
        return [v.rule for v in self.violations]

    def to_metadata(self) -> dict:
        return {
            "rule_check_skipped": self.skipped,
            "rule_check_skip_reason": self.skip_reason,
            "rule_violations": [
                {
                    "rule": v.rule,
                    "evidence": v.evidence,
                    "suggested_fix": v.suggested_fix,
                }
                for v in self.violations
            ],
        }


_JUDGE_SYSTEM = (
    "You are a strict reviewer for a math tutor. Your job is to decide "
    "whether the tutor's most recent response violated any of the rules "
    "below. Be conservative — only flag CLEAR violations with quotable "
    "evidence. When in doubt, return no violations.\n"
    "\n"
    "Rules to enforce:\n"
    "1. NO_AUTHORING — the tutor must NOT pose a new arithmetic question "
    "or invent numerical examples. Asking conceptual scaffolding questions "
    "(\"which rule applies?\") is allowed. Reciting a rule from the "
    "lesson (\"on a straight line, angles sum to 180°\") is allowed. "
    "Authoring a NEW concrete arithmetic problem (\"if one angle is 73°, "
    "what is the adjacent angle?\") IS a violation unless that exact "
    "stem appears in the bank below.\n"
    "2. ARITHMETIC — every arithmetic claim in the response must be "
    "numerically correct. \"65 + 125 = 190\" is correct. \"65 + 125 = "
    "180\" is a violation. Claims phrased in prose (\"they sum to 180°\", "
    "\"subtracting gives 17\") count too.\n"
    "3. RULE_1 — when the student's most recent answer was BARE (no "
    "working shown) OR WRONG, the tutor must NOT praise mastery in any "
    "phrasing. \"exactly\", \"you've nailed it\", \"you've got the rule\", "
    "\"you understand\", \"perfect\", \"smart\" are all violations in "
    "this context. Asking \"can you walk me through your steps?\" is "
    "the correct response and is NOT a violation."
)


def _build_judge_prompt(
    response_text: str,
    bank_stems: List[str],
    student_input: str,
    answer_was_bare: bool,
    answer_was_wrong: bool,
) -> str:
    """Render the user-side prompt for the judge LLM."""
    bank_block = "\n".join(f"  - {s.strip()[:200]}" for s in bank_stems[:10]) or "  (bank empty)"
    student_block = (student_input or "").strip()[:200] or "(none)"
    payload = {
        "tutor_response": response_text[:2000],
        "student_last_input": student_block,
        "student_answer_was_bare": answer_was_bare,
        "student_answer_was_wrong": answer_was_wrong,
        "verified_question_bank": bank_block,
    }
    return (
        "Review the tutor's response below for violations of NO_AUTHORING, "
        "ARITHMETIC, RULE_1.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Reply with ONLY a JSON object of the shape:\n"
        '  {"violations": [{"rule": "NO_AUTHORING"|"ARITHMETIC"|"RULE_1", '
        '"evidence": "<<=120-char quote from the response>", '
        '"suggested_fix": "<one-sentence rewrite>"}]}\n'
        "If no violations, return {\"violations\": []}."
    )


def check_rule_compliance(
    response_text: str,
    *,
    llm_client=None,
    bank_stems: Optional[List[str]] = None,
    student_input: str = "",
    answer_was_bare: bool = False,
    answer_was_wrong: bool = False,
    max_violations: int = 5,
) -> RuleComplianceResult:
    """LLM-as-judge check that the tutor followed the rules.

    Args:
      response_text: the tutor's clean response (after media/question
                     signal parsing).
      llm_client: BaseLLMClient. None disables the check.
      bank_stems: list of allowed-question stems for context — pulled
                  from the active session bank. Used by the judge to
                  know what counts as authoring.
      student_input: the student's last message (gives the judge
                     enough context to evaluate RULE_1).
      answer_was_bare: True when the student's last input was a naked
                       numeric answer (no working shown).
      answer_was_wrong: True when the deterministic answer-check said
                        the student's input was wrong.

    Returns a RuleComplianceResult. Caller wires violations into the
    validator's existing regeneration path.
    """
    result = RuleComplianceResult()
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

    user_prompt = _build_judge_prompt(
        response_text=response_text,
        bank_stems=bank_stems or [],
        student_input=student_input,
        answer_was_bare=answer_was_bare,
        answer_was_wrong=answer_was_wrong,
    )

    try:
        llm_response = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_JUDGE_SYSTEM,
            max_tokens=600,
        )
        raw = (llm_response.content or "").strip()
        # Strip markdown fences.
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
        items = data.get("violations") or []
        if not isinstance(items, list):
            raise ValueError("expected violations array")
        for item in items[:max_violations]:
            if not isinstance(item, dict):
                continue
            rule = (item.get("rule") or "").strip().upper()
            if rule not in VALID_RULES:
                continue
            evidence = str(item.get("evidence") or "")[:200]
            fix = str(item.get("suggested_fix") or "")[:300]
            result.violations.append(
                RuleViolation(rule=rule, evidence=evidence, suggested_fix=fix)
            )
    except Exception as e:
        logger.warning(f"[RuleCheck] judge failed: {e}")
        result.skipped = True
        result.skip_reason = f"judge_error: {type(e).__name__}"
    return result
