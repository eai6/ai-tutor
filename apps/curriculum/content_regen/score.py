"""Score regen candidates so we can pick the BEST one when no
candidate is fully clean.

Scoring intent: when both candidates have violations, pick the one
that has fewer / less severe issues. Numbers chosen so:
  - Clean candidate (no violations) > any candidate with violations
  - Skipped judge (couldn't verify) > candidate with violations
    (we don't penalise infrastructure failures)
  - CONTRADICTED weighted heavier than UNSUPPORTED (the former is a
    confirmed wrong fact; the latter is just unverified)
"""

from __future__ import annotations

from typing import Any, Dict


# Per-violation penalties. Higher = worse. Clean candidate scores 1.0.
# Each violation subtracts its weight; we clamp to 0 at the bottom.
_VIOLATION_WEIGHTS = {
    # Hard-fail codes (factual wrongness — student would see a wrong fact)
    "STEP_FACT_CONTRADICTED": 0.5,
    # Soft codes (judge couldn't confirm, but didn't contradict either)
    "STEP_FACT_UNSUPPORTED": 0.15,
    # Image-side codes (used when the same scorer is reused for figures)
    "FIGURE_OFF_OBJECTIVE": 0.5,
    "FIGURE_FACTUAL_ERROR": 0.5,
    "FIGURE_LABEL_INACCURATE": 0.4,
    "FIGURE_PEDAGOGICALLY_WEAK": 0.15,
    "FIGURE_VISUAL_QUALITY": 0.3,
    # Image-prompt PRE-gen codes (also reusable here)
    "PROMPT_VAGUE": 0.3,
    "PROMPT_HALLUCINATION_TRIGGER": 0.5,
    "PROMPT_OFF_TOPIC": 0.5,
    "PROMPT_WRONG_VISUAL_TYPE": 0.3,
    "PROMPT_GRADE_MISMATCH": 0.3,
    "PROMPT_RELIES_ON_TEXT_IN_IMAGE": 0.3,
}


def score_candidate(verdict: Dict[str, Any]) -> float:
    """Score a candidate's verdict on [0.0, 1.0].

    1.0 = clean (no violations, judge passed).
    Skipped judge → 0.5 (we couldn't verify; treat as neutral, not
    penalised, since infra failure is not the model's fault).
    Otherwise = 1.0 - sum(weights of each violation), clamped to 0.

    Used by the regen orchestrator to pick the best candidate when no
    cycle produces a clean one — caller still flags
    content_quality_status='auto_flagged' but ships the least-bad
    candidate (better than the original which presumably had more
    violations to begin with).
    """
    if not isinstance(verdict, dict):
        return 0.0

    if verdict.get('skipped'):
        # Judge couldn't run — neutral score; don't pretend we know.
        return 0.5

    violations = verdict.get('violations') or []
    if not violations and verdict.get('passed', True):
        return 1.0

    penalty = 0.0
    for code in violations:
        if not isinstance(code, str):
            continue
        penalty += _VIOLATION_WEIGHTS.get(code.strip().upper(), 0.25)

    return max(0.0, 1.0 - penalty)


__all__ = ["score_candidate"]
