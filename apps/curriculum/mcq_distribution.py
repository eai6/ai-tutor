"""Post-generation audit + (optional) rebalance for MCQ correct-letter
distribution.

Prior generations had ~60% of correct answers landing on B because the
prompt's example used B as a placeholder (fixed in commit c56c804 of
v0.1.0). The exit-ticket prompt (apps/tutoring/management/commands/
generate_exit_tickets.py) now asks Claude to self-balance during
generation, but we run a deterministic post-gen audit too — belt and
braces.

API:
    from apps.curriculum.mcq_distribution import audit_distribution, rebalance_distribution

    counts = audit_distribution(questions)
    # counts: {"A": 8, "B": 9, "C": 9, "D": 9}

    rebalanced = rebalance_distribution(questions)
    # In-place permutation of which option ends up at A/B/C/D so the
    # bank is uniform. Educationally identical (same options + same
    # correct content), just shuffled.

The audit logs at INFO when balanced and WARNING when any letter
exceeds the configurable threshold (default 35% of the bank).

Part of M5-prep of memory/portuguese_mozambique_pilot_plan.md.
"""
from __future__ import annotations

import logging
import random
from collections import Counter
from typing import Iterable

logger = logging.getLogger(__name__)


_LETTERS = ("A", "B", "C", "D")


def audit_distribution(
    questions: Iterable[dict],
    *,
    warn_threshold: float = 0.35,
    label: str = "exit-ticket",
) -> dict[str, int]:
    """Tally the correct-letter distribution across an MCQ bank.

    Args:
        questions: iterable of MCQ dicts. Each must have a ``correct``
            key whose value is one of A/B/C/D.
        warn_threshold: fraction of the bank above which a single
            letter triggers a WARNING log. Default 0.35 (~12 out of 35).
        label: tag used in log messages for traceability.

    Returns:
        Dict of {letter: count} including 0-counts for any unused
        letter. MCQs without a valid ``correct`` field are ignored
        but counted in the log as ``invalid``.
    """
    mcqs = [
        q for q in questions
        if (q.get("question_type") or "").lower() == "mcq"
    ]
    counter: Counter = Counter()
    invalid = 0
    for q in mcqs:
        letter = (q.get("correct") or "").strip().upper()
        if letter in _LETTERS:
            counter[letter] += 1
        else:
            invalid += 1

    distribution = {letter: counter.get(letter, 0) for letter in _LETTERS}
    total = sum(distribution.values())

    if total == 0:
        logger.info("[%s] mcq distribution audit — 0 MCQs in bank", label)
        return distribution

    biased = [
        letter for letter, n in distribution.items()
        if n / total > warn_threshold
    ]
    if biased:
        logger.warning(
            "[%s] mcq distribution biased — %s exceed %.0f%% threshold. "
            "Counts: %s (of %d total, %d invalid). "
            "Consider calling rebalance_distribution() on the bank.",
            label, biased, warn_threshold * 100,
            distribution, total, invalid,
        )
    else:
        logger.info(
            "[%s] mcq distribution OK — %s (of %d total, %d invalid)",
            label, distribution, total, invalid,
        )

    return distribution


def rebalance_distribution(
    questions: list[dict],
    *,
    rng: random.Random | None = None,
    label: str = "exit-ticket",
) -> int:
    """Permute which option (A/B/C/D) holds the correct answer in
    each MCQ so the bank's correct-letter distribution is uniform.

    Educationally identical to the input — the four option *texts*
    travel together, only their labels (A/B/C/D) change. The
    ``correct`` field is updated to point at wherever the original
    correct option ends up.

    The function mutates each MCQ dict in place AND returns the
    number of questions actually re-lettered (i.e. where the correct
    letter changed). MCQs without a valid ``correct`` letter are
    skipped.

    Algorithm:
        1. Compute a target distribution: each letter gets either
           floor(N/4) or ceil(N/4) correct answers.
        2. Assign each MCQ to a target letter using a random
           shuffle of the position slots so the assignment is
           reproducible-with-seed but not biased.
        3. For each MCQ, if its current correct letter ≠ target,
           swap the current correct option's text with whatever
           text currently sits at the target letter.

    Args:
        questions: list of MCQ dicts. Mutated in place.
        rng: optional ``random.Random`` for reproducible shuffles.
            Defaults to ``random`` module's global state.
        label: tag used in log messages.

    Returns:
        Count of MCQs whose correct letter was changed.
    """
    if rng is None:
        rng = random

    mcqs = [
        q for q in questions
        if (q.get("question_type") or "").lower() == "mcq"
        and (q.get("correct") or "").strip().upper() in _LETTERS
    ]
    n = len(mcqs)
    if n == 0:
        return 0

    # Build the target letter sequence: floor(n/4) of each letter,
    # then distribute the remainder. Shuffle so two adjacent MCQs
    # don't always land on the same target.
    base, rem = divmod(n, 4)
    target_sequence = []
    for letter in _LETTERS:
        target_sequence.extend([letter] * base)
    # Remainder slots get assigned to a random subset of letters.
    for letter in rng.sample(_LETTERS, rem):
        target_sequence.append(letter)
    rng.shuffle(target_sequence)

    changes = 0
    for q, target in zip(mcqs, target_sequence):
        current = (q.get("correct") or "").strip().upper()
        if current == target:
            continue
        # Swap the option texts: whatever's at `current` swaps with
        # whatever's at `target`. The CONTENT stays the same; only
        # the A/B/C/D label changes.
        current_key = f"option_{current.lower()}"
        target_key = f"option_{target.lower()}"
        if current_key in q and target_key in q:
            q[current_key], q[target_key] = q[target_key], q[current_key]
            q["correct"] = target
            changes += 1

    if changes:
        logger.info(
            "[%s] rebalanced mcq distribution — %d/%d questions re-lettered",
            label, changes, n,
        )
    return changes
