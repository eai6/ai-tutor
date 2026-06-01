"""Tests for the MCQ correct-letter distribution audit + rebalance.

Part of M5-prep of memory/portuguese_mozambique_pilot_plan.md. Belt-
and-braces for the B-bias issue: even when the prompt asks the LLM
to self-balance, the post-gen verifier catches drift.
"""
from __future__ import annotations

import random
from collections import Counter

from django.test import TestCase

from apps.curriculum.mcq_distribution import (
    audit_distribution, rebalance_distribution,
)


def _mcq(letter: str, idx: int = 0) -> dict:
    """Tiny MCQ fixture builder. The four option_x texts are unique
    so we can verify after rebalance that the CONTENT travelled
    correctly when the labels shuffled."""
    return {
        "question_type": "mcq",
        "question": f"Q{idx}: stem",
        "option_a": f"q{idx}_a_text",
        "option_b": f"q{idx}_b_text",
        "option_c": f"q{idx}_c_text",
        "option_d": f"q{idx}_d_text",
        "correct": letter,
    }


class AuditDistributionTest(TestCase):
    def test_balanced_bank_logs_info(self):
        bank = (
            [_mcq("A", i) for i in range(9)]
            + [_mcq("B", i) for i in range(9)]
            + [_mcq("C", i) for i in range(9)]
            + [_mcq("D", i) for i in range(8)]
        )
        counts = audit_distribution(bank, label="test-balanced")
        self.assertEqual(counts, {"A": 9, "B": 9, "C": 9, "D": 8})

    def test_b_biased_bank_returns_high_b_count(self):
        bank = [_mcq("B", i) for i in range(20)] + [_mcq("A", i) for i in range(15)]
        counts = audit_distribution(bank, label="test-b-biased")
        # 20/35 = 57% — well above the 35% threshold
        self.assertEqual(counts["B"], 20)
        self.assertEqual(counts["A"], 15)

    def test_empty_bank_returns_zero_counts(self):
        counts = audit_distribution([], label="test-empty")
        self.assertEqual(counts, {"A": 0, "B": 0, "C": 0, "D": 0})

    def test_non_mcq_questions_are_ignored(self):
        bank = [
            {"question_type": "fill_in_blank", "correct": "B"},
            _mcq("A", 1),
            _mcq("A", 2),
        ]
        counts = audit_distribution(bank, label="test-non-mcq")
        self.assertEqual(counts, {"A": 2, "B": 0, "C": 0, "D": 0})

    def test_invalid_correct_field_skipped_silently(self):
        bank = [
            _mcq("A", 1),
            {"question_type": "mcq", "correct": "Z"},  # invalid
            _mcq("B", 2),
        ]
        counts = audit_distribution(bank, label="test-invalid")
        self.assertEqual(counts, {"A": 1, "B": 1, "C": 0, "D": 0})


class RebalanceDistributionTest(TestCase):
    def test_severely_biased_bank_gets_uniform_after_rebalance(self):
        # 20 B + 15 A → should redistribute to ~9/9/9/8
        bank = [_mcq("B", i) for i in range(20)] + [_mcq("A", i) for i in range(20, 35)]
        rng = random.Random(42)  # reproducible
        rebalance_distribution(bank, rng=rng, label="test-rebalance")

        counts = Counter(q["correct"] for q in bank)
        # After rebalance, every letter should have between floor(35/4)=8
        # and ceil(35/4)=9 correct answers.
        for letter in ("A", "B", "C", "D"):
            self.assertGreaterEqual(counts[letter], 8, f"{letter} too few")
            self.assertLessEqual(counts[letter], 9, f"{letter} too many")

    def test_option_text_travels_with_correct_marker(self):
        """When a question is re-lettered, the CONTENT of the correct
        option must move with it — we only permute LABELS, not which
        text is actually correct."""
        q = _mcq("B", 0)
        # Original correct text:
        correct_text = q["option_b"]  # "q0_b_text"
        rng = random.Random(0)
        bank = [q]
        rebalance_distribution(bank, rng=rng, label="test-text-travels")

        new_correct_letter = q["correct"]
        new_correct_text = q[f"option_{new_correct_letter.lower()}"]
        self.assertEqual(
            new_correct_text, correct_text,
            "After rebalance, the option at the new correct letter "
            "must still hold the original correct content.",
        )

    def test_rebalance_returns_change_count(self):
        bank = [_mcq("B", i) for i in range(20)]
        rng = random.Random(0)
        changed = rebalance_distribution(bank, rng=rng, label="test-count")
        # All 20 started as B; after rebalance, some will have moved.
        self.assertGreater(changed, 0)
        self.assertLessEqual(changed, 20)

    def test_already_balanced_bank_no_changes_needed(self):
        # Carefully constructed balanced bank that happens to match
        # one possible random target sequence — if the rebalance
        # algorithm picks a different target, it'll still re-letter
        # some. So we just assert the function doesn't break things.
        bank = (
            [_mcq("A", i) for i in range(9)]
            + [_mcq("B", i) for i in range(9)]
            + [_mcq("C", i) for i in range(9)]
            + [_mcq("D", i) for i in range(8)]
        )
        rng = random.Random(0)
        rebalance_distribution(bank, rng=rng, label="test-already-balanced")
        # Distribution stays within tolerance even after a possibly
        # unnecessary rebalance.
        counts = Counter(q["correct"] for q in bank)
        for letter in ("A", "B", "C", "D"):
            self.assertGreaterEqual(counts[letter], 8)
            self.assertLessEqual(counts[letter], 9)

    def test_empty_bank_is_safe(self):
        changed = rebalance_distribution([], label="test-empty")
        self.assertEqual(changed, 0)
