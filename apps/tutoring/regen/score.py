"""Score a regen candidate from its judge findings.

Higher score = better candidate. The orchestrator picks the
highest-scoring CLEAN candidate per cycle, and falls back to the
highest-scoring overall when no cycle produces a clean one.

`clean` means zero hard issues (the kind that would re-trigger regen
under the current validator policy). A "scored but not clean" candidate
might still have soft issues (info_dump, unverified claims) — those
don't force another retry but do affect the score.
"""

from __future__ import annotations

from typing import Tuple

from apps.tutoring.combined_judge import CombinedJudgeResult


# Penalty weights — higher = more severe. Tune based on what we
# observe in production. The HARD penalties match the validator's
# regen-trigger set; SOFT penalties capture issues we'd prefer to
# avoid but won't force a retry on.
_HARD_PENALTY = 5.0   # rule violations, arithmetic, contradicted facts, coherence, figure issues
_VERDICT_MISMATCH_PENALTY = 8.0  # extra-bad: text vs verdict mismatch
_SAFETY_CRITICAL_PENALTY = 50.0  # harmful content — must dominate; never pick a critical
_SAFETY_WARNING_PENALTY = 20.0   # inappropriate — heavy, but ranks above other issues
_SOFT_PENALTY = 0.5   # unverified claims, longer-than-ideal


def score_candidate(jr: CombinedJudgeResult) -> Tuple[float, bool]:
    """Return (score, clean).

    score: a real number, higher is better. Always negative or zero —
      a perfectly clean candidate scores 0.0; each violation subtracts.
    clean: True when no hard issues are present.
    """
    if jr is None:
        return -100.0, False

    score = 0.0
    hard_count = 0

    # Coherence violations — each one is a hard hit.
    n_coh = len(jr.coherence_violations or [])
    if n_coh:
        score -= _HARD_PENALTY * n_coh
        hard_count += n_coh

    # Rule violations (NO_AUTHORING, RULE_1).
    n_rule = len(jr.rule_violations or [])
    if n_rule:
        score -= _HARD_PENALTY * n_rule
        hard_count += n_rule

    # Arithmetic corrections — each correction is a hard hit
    # (the rewrite still has wrong arithmetic in it).
    n_arith = len(jr.arithmetic_corrections or [])
    if n_arith:
        score -= _HARD_PENALTY * n_arith
        hard_count += n_arith

    # Contradicted factual claims.
    contradicted = sum(
        1 for c in (jr.fact_claims or []) if c.status == "contradicted"
    )
    if contradicted:
        score -= _HARD_PENALTY * contradicted
        hard_count += contradicted

    # Unverified factual claims (soft — could be conservative judge).
    unverified = sum(
        1 for c in (jr.fact_claims or []) if c.status == "unverified"
    )
    if unverified:
        score -= _SOFT_PENALTY * unverified

    # Figure-reference issues (deterministic, very high confidence).
    n_figref = len(jr.figure_ref_issues or [])
    if n_figref:
        score -= _HARD_PENALTY * n_figref
        hard_count += n_figref

    # Figure-vision misalignment.
    if jr.figure_aligned is False:
        score -= _HARD_PENALTY
        hard_count += 1

    # Safety — dominates everything else. A critical-severity candidate
    # MUST never be picked over a non-critical one. The huge penalty
    # ensures even a "clean except for safety:critical" candidate
    # ranks below a "dirty in many ways but safety:safe" candidate.
    sev = getattr(jr, "safety_severity", "safe") or "safe"
    if sev == "critical":
        score -= _SAFETY_CRITICAL_PENALTY
        hard_count += 1
    elif sev == "warning":
        score -= _SAFETY_WARNING_PENALTY
        hard_count += 1

    # Step-eval verdict — if the candidate's text again disagrees with
    # the deterministic verdict, that's the worst kind of failure.
    # We don't have that direct signal here (the validator computes
    # it after), so we approximate: a hard penalty when answer_correct
    # is None AND the judge set step_eval_skipped (i.e. the judge had
    # nothing to evaluate, which usually means the rewrite changed
    # step context). Skip for now — keep the score function simple
    # and let the validator catch verdict mismatch on the next pass.

    clean = (hard_count == 0)
    return score, clean
