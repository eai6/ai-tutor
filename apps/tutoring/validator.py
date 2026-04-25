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
from typing import List, Optional

from apps.tutoring.praise_filter import strip_praise_if_wrong, _PRAISE_RE


# Issues we record. Strings rather than enums so they serialize cleanly
# into SessionTurn.metadata JSONField.
ISSUE_NO_QUESTION = "no_question"
ISSUE_UNFOUNDED_PRAISE_STRIPPED = "unfounded_praise_stripped"
ISSUE_INFO_DUMP = "info_dump_warning"
ISSUE_NUMERIC_CLAIM_UNVERIFIED = "numeric_claim_unverified"  # used by V2


@dataclass
class ValidationResult:
    """Outcome of validating a tutor response."""
    content: str
    issues: List[str] = field(default_factory=list)
    layers_run: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # No issues at all = clean pass. info_dump_warning is "soft" — not
        # a fail.
        return all(i == ISSUE_INFO_DUMP for i in self.issues)


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
) -> ValidationResult:
    """Run V1 layers over a tutor response.

    Args:
      response: the cleaned tutor reply (after media-signal parsing).
      is_correct: result of the most recent answer evaluation, or None
                  when no evaluation was performed (e.g. teach step,
                  warmup, no expected answer).
      bare_answer: True when the student replied with a naked numeric
                   answer on a practice/quiz step.
      step_type: 'teach' | 'worked_example' | 'practice' | 'quiz' | 'summary'.

    Returns:
      ValidationResult with the (possibly modified) content, the list
      of issues encountered, and the layer trace.
    """
    issues: List[str] = []
    layers_run: List[str] = []
    content = response or ""

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
        new_content, stripped = strip_praise_if_wrong(content, is_correct=False)
        if stripped:
            content = new_content
            issues.append(ISSUE_UNFOUNDED_PRAISE_STRIPPED)

    return ValidationResult(
        content=content,
        issues=issues,
        layers_run=layers_run,
        metadata={
            "info_concept_count": info_score,
            "ends_with_question": _ends_with_question(content),
        },
    )
