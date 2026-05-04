"""Socratic tutor response validator.

Assumes the tutor is wrong until proven otherwise. Every tutor response
runs through this pipeline before it is saved to DB or sent to the
student. Issues are either soft-fixed (strip the offending fragment)
or logged for teacher visibility.

V1 layers (this module):
  L1 STRUCTURAL — does the response end with a question? info-dump score?
  L2 PEDAGOGICAL — praise present + correctness signal said wrong/bare?
                   strip the praise (extends the math-only fix to ALL
                   subjects).

Future layers (V2-V4, see memory/socratic_validator_plan.md):
  L3 CORRECTNESS — extend LLM evaluator to all subjects (already in place)
  L4 FACTUAL — RAG-verify numeric/named claims against curriculum KB
  L5 REGENERATE — retry once with validator issues injected as constraints
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from apps.tutoring.praise_filter import strip_praise_if_wrong, _PRAISE_RE
from apps.tutoring.fact_verifier import verify_response, FactCheckResult


# Issues we record. Strings rather than enums so they serialize cleanly
# into SessionTurn.metadata JSONField.
ISSUE_NO_QUESTION = "no_question"
ISSUE_UNFOUNDED_PRAISE_STRIPPED = "unfounded_praise_stripped"
ISSUE_INFO_DUMP = "info_dump_warning"
ISSUE_NUMERIC_CLAIM_UNVERIFIED = "numeric_claim_unverified"
ISSUE_NUMERIC_CLAIM_CONTRADICTED = "numeric_claim_contradicted"
# P5 — rule-compliance violations (LLM-as-judge). See
# memory/tutor_no_authoring_plan.md.
ISSUE_AUTHORING_VIOLATION = "authoring_violation"
ISSUE_ARITHMETIC_VIOLATION = "arithmetic_violation"
ISSUE_RULE1_VIOLATION = "rule1_violation"


@dataclass
class ValidationResult:
    """Outcome of validating a tutor response."""
    content: str
    issues: List[str] = field(default_factory=list)
    layers_run: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    # Issues considered "soft" — logged for teacher review but don't
    # flip `passed` to False. Numeric_claim_unverified is soft because
    # the LLM judge often defaults to "unverified" when retrieved
    # evidence is sparse, not because the tutor is wrong.
    _SOFT_ISSUES = frozenset({
        ISSUE_INFO_DUMP,
        ISSUE_NUMERIC_CLAIM_UNVERIFIED,
    })

    # Issues that should trigger regeneration (V3) — strong evidence
    # the tutor is wrong, can't be patched in place.
    _REGEN_ISSUES = frozenset({
        ISSUE_NUMERIC_CLAIM_CONTRADICTED,
        ISSUE_AUTHORING_VIOLATION,
        ISSUE_ARITHMETIC_VIOLATION,
        ISSUE_RULE1_VIOLATION,
    })

    @property
    def passed(self) -> bool:
        return all(i in self._SOFT_ISSUES for i in self.issues)

    @property
    def needs_regeneration(self) -> bool:
        return any(i in self._REGEN_ISSUES for i in self.issues)


# Patterns that constitute a question (broader than '?' alone — some
# Socratic prompts read as imperatives like "Walk me through it.").
_QUESTION_RE = re.compile(
    r"\?\s*$|"
    r"\b(walk me through|tell me|explain|describe|why|how|what|which|when|where|"
    r"can you|could you|would you|try to|let's check|let's see|show me|"
    r"think about|what do you think|give it a try)\b",
    re.IGNORECASE,
)

# Heuristic: a response is "info-dumpy" when it contains many distinct
# named concepts (proper nouns, acronyms, numbers) without any question
# at the end. Cheap and biased toward false negatives — meant to flag
# obvious lectures, not all rich responses.
_NAMED_CONCEPT_RE = re.compile(r"\b[A-Z]{2,5}\b|\b\d+(?:\.\d+)?(?:%|st|nd|rd|th)?\b")


def _ends_with_question(text: str) -> bool:
    if not text:
        return False
    # Look at last sentence-ish chunk.
    tail = text.strip().splitlines()[-1] if "\n" in text else text.strip()
    return bool(_QUESTION_RE.search(tail))


def _info_dump_score(text: str) -> int:
    """Return number of named concepts (acronyms, numbers, percentages)
    that appear in the response. >= 5 is the threshold for an info dump."""
    if not text:
        return 0
    return len(_NAMED_CONCEPT_RE.findall(text))


def validate_tutor_response(
    response: str,
    is_correct: Optional[bool],
    bare_answer: bool,
    step_type: Optional[str] = None,
    lesson=None,
    llm_client=None,
    fact_check: bool = True,
    student_input: Optional[str] = None,
    rule_check: bool = True,
    bank_stems: Optional[List[str]] = None,
    arithmetic_corrections: Optional[List[Dict]] = None,
    bank_signal_used: Optional[bool] = None,
    combined_result=None,
) -> ValidationResult:
    """Run V1+V2 validator layers over a tutor response.

    Args:
      response: the cleaned tutor reply (after media-signal parsing).
      is_correct: result of the most recent answer evaluation, or None
                  when no evaluation was performed (e.g. teach step,
                  warmup, no expected answer).
      bare_answer: True when the student replied with a naked numeric
                   answer on a practice/quiz step.
      step_type: 'teach' | 'worked_example' | 'practice' | 'quiz' | 'summary'.
      lesson: Lesson model instance — required for L4 fact-check.
      llm_client: BaseLLMClient — required for L4 LLM judge.
      fact_check: when False, skips L4 entirely (used in tests / when
                  callers want fast-path validation).

    Returns:
      ValidationResult with the (possibly modified) content, the list
      of issues encountered, and the layer trace.
    """
    issues: List[str] = []
    layers_run: List[str] = []
    content = response or ""
    extra_meta: dict = {}

    # L1 — structural
    layers_run.append("structural")
    if step_type in {"practice", "quiz"} and not _ends_with_question(content):
        issues.append(ISSUE_NO_QUESTION)
    info_score = _info_dump_score(content)
    if info_score >= 6 and not _ends_with_question(content):
        issues.append(ISSUE_INFO_DUMP)

    # L2 — pedagogical praise gate (universal; previously math-only)
    layers_run.append("pedagogical")
    should_strip = False
    if is_correct is False:
        should_strip = True
    elif bare_answer:
        # Bare answers must not be praised regardless of correctness
        # (math_teaching Rule 1 generalized).
        should_strip = True

    if should_strip and _PRAISE_RE.search(content):
        # By the time the validator runs, the engine's deterministic
        # math-check pass has already stripped praise on bare-correct
        # turns. Anything reaching this layer with `is_correct=False`
        # is genuinely wrong; bare answers without any canonical
        # correctness signal use the "bare_unknown" opener.
        praise_context = "wrong" if is_correct is False else "bare_unknown"
        new_content, stripped = strip_praise_if_wrong(
            content,
            is_correct=False,
            context=praise_context,
            student_input=student_input,
        )
        if stripped:
            content = new_content
            issues.append(ISSUE_UNFOUNDED_PRAISE_STRIPPED)

    # L4 + L5 — factual claims and rule compliance.
    #
    # Preferred path: caller already ran apps.tutoring.combined_judge,
    # passes the result via `combined_result`, and we just consume the
    # verdicts. This is what conversational_tutor.py does on math turns
    # — collapses three serial LLM calls (arithmetic + fact + rule) into
    # one.
    #
    # Legacy path (no combined_result): run the two judges separately.
    # Used for non-math callers and tests that haven't migrated.
    if combined_result is not None:
        layers_run.append("fact_check_combined")
        layers_run.append("rule_check_combined")
        extra_meta.update(combined_result.to_metadata())
        if combined_result.contradicted_claims:
            issues.append(ISSUE_NUMERIC_CLAIM_CONTRADICTED)
        if combined_result.unverified_claims:
            issues.append(ISSUE_NUMERIC_CLAIM_UNVERIFIED)
        if combined_result.has_violations:
            from apps.tutoring.rule_compliance import (
                RULE_ARITHMETIC,
                RULE_NO_AUTHORING,
                RULE_RULE_1,
            )
            if RULE_NO_AUTHORING in combined_result.violated_rules:
                issues.append(ISSUE_AUTHORING_VIOLATION)
            if RULE_ARITHMETIC in combined_result.violated_rules:
                issues.append(ISSUE_ARITHMETIC_VIOLATION)
            if RULE_RULE_1 in combined_result.violated_rules:
                issues.append(ISSUE_RULE1_VIOLATION)
    else:
        if fact_check and lesson is not None and llm_client is not None:
            layers_run.append("fact_check")
            fc: FactCheckResult = verify_response(
                content, lesson=lesson, llm_client=llm_client,
            )
            extra_meta.update(fc.to_metadata())
            if fc.contradicted_claims:
                issues.append(ISSUE_NUMERIC_CLAIM_CONTRADICTED)
            unverified = [c for c in fc.claims if c.status == "unverified"]
            if unverified:
                issues.append(ISSUE_NUMERIC_CLAIM_UNVERIFIED)

        if (
            rule_check
            and lesson is not None
            and llm_client is not None
        ):
            try:
                is_math_lesson = lesson.unit.course.is_math
            except Exception:
                is_math_lesson = False
            if is_math_lesson:
                layers_run.append("rule_check")
                from apps.tutoring.rule_compliance import (
                    RULE_ARITHMETIC,
                    RULE_NO_AUTHORING,
                    RULE_RULE_1,
                    check_rule_compliance,
                )
                rc = check_rule_compliance(
                    content,
                    llm_client=llm_client,
                    bank_stems=bank_stems or [],
                    student_input=student_input or "",
                    answer_was_bare=bool(bare_answer),
                    answer_was_wrong=(is_correct is False),
                )
                extra_meta.update(rc.to_metadata())
                if rc.has_violations:
                    if RULE_NO_AUTHORING in rc.violated_rules:
                        issues.append(ISSUE_AUTHORING_VIOLATION)
                    if RULE_ARITHMETIC in rc.violated_rules:
                        issues.append(ISSUE_ARITHMETIC_VIOLATION)
                    if RULE_RULE_1 in rc.violated_rules:
                        issues.append(ISSUE_RULE1_VIOLATION)

    # L6 — deterministic gates that don't rely on the LLM judge.
    #
    # (a) arithmetic_corrections from the LLM arithmetic verifier:
    #     when the verifier flagged ANY claim as wrong, force regen.
    #     The verifier was previously logging-only; now it's a hard
    #     trigger so bad math never ships even if the rule_compliance
    #     judge is lenient.
    if arithmetic_corrections:
        layers_run.append("arithmetic_corrections")
        issues.append(ISSUE_ARITHMETIC_VIOLATION)
        extra_meta["arithmetic_corrections"] = list(arithmetic_corrections)

    # (b) AUTHORING gate: math + ANY phase + the response contains a
    #     question with specific numerical values + the LLM did NOT
    #     emit a bank pull signal (|||QUESTION:N||| or |||QUESTION_EO:N|||).
    #
    #     Was previously gated on practice/quiz only. Field bug: the
    #     LLM was authoring during engage/warmup phases (where the
    #     gate didn't fire) — paraphrasing later steps' questions
    #     instead of pulling from the bank. Extending to all phases
    #     means the LLM has to use |||QUESTION:N||| to pose a
    #     numerical question regardless of which step it's on.
    #
    #     Conceptual / non-numerical questions ("which rule applies?")
    #     pass _has_numerical_question and are not blocked.
    try:
        is_math_for_authoring = lesson.unit.course.is_math
    except Exception:
        is_math_for_authoring = False
    if (
        is_math_for_authoring
        and bank_signal_used is False
        and _has_numerical_question(content)
    ):
        layers_run.append("authoring_gate")
        if ISSUE_AUTHORING_VIOLATION not in issues:
            issues.append(ISSUE_AUTHORING_VIOLATION)
        extra_meta["authoring_gate_fired"] = True
        extra_meta["authoring_gate_step_type"] = step_type or ""

    return ValidationResult(
        content=content,
        issues=issues,
        layers_run=layers_run,
        metadata={
            "info_concept_count": info_score,
            "ends_with_question": _ends_with_question(content),
            **extra_meta,
        },
    )


# A "math question signature" — at least one '?' AND at least two
# digit groups (numbers separated by ops, units, words) inside the
# 200 chars before the question mark. Tuned to catch authored
# practice questions ("if angles are 60° and x°, find x?") while
# letting purely conceptual questions ("which rule applies?") through.
_NUMERICAL_QUESTION_RE = re.compile(
    r"(?:\d[\d.]*\s*[°%a-zA-Z]?[\s,].*?){2,}\?",
    re.DOTALL,
)


def _has_numerical_question(text: str) -> bool:
    if not text:
        return False
    return bool(_NUMERICAL_QUESTION_RE.search(text))
