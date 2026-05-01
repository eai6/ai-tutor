"""Tests for _normalize_enabling_objective — the deterministic
validation step that snaps LLM-emitted enabling_objective values
to one of the lesson's canonical EOs (or drops the tag if no match).

Used by both:
  - exit-ticket question persistence (apps/curriculum/content_generator.py)
  - lesson step persistence (_save_steps_to_db)
"""

from django.test import SimpleTestCase

from apps.curriculum.content_generator import _normalize_enabling_objective


class NormalizeEnablingObjectiveTest(SimpleTestCase):
    EOS = [
        "Define cost price, selling price, profit, loss, and discount",
        "Calculate profit or loss from given cost price and selling price",
        "Identify and avoid common mistakes in profit and loss calculations",
        "Apply profit and loss concepts in reverse (find cost price from profit information)",
        "Distinguish between profit/loss and discount in a multi-condition scenario",
    ]

    def test_exact_match_returns_exact(self):
        eo = "Calculate profit or loss from given cost price and selling price"
        out, status = _normalize_enabling_objective(eo, self.EOS)
        self.assertEqual(status, "exact")
        self.assertEqual(out, eo)

    def test_case_drift_snaps_to_canonical(self):
        # Different case
        out, status = _normalize_enabling_objective(
            "calculate profit or loss from given cost price and selling price",
            self.EOS,
        )
        self.assertEqual(status, "snapped")
        self.assertEqual(
            out,
            "Calculate profit or loss from given cost price and selling price",
        )

    def test_trailing_punctuation_snaps(self):
        out, status = _normalize_enabling_objective(
            "Define cost price, selling price, profit, loss, and discount.",
            self.EOS,
        )
        self.assertEqual(status, "snapped")
        self.assertEqual(
            out,
            "Define cost price, selling price, profit, loss, and discount",
        )

    def test_extra_whitespace_snaps(self):
        out, status = _normalize_enabling_objective(
            "  Define   cost price, selling price, profit, loss, and discount  ",
            self.EOS,
        )
        self.assertEqual(status, "snapped")

    def test_substring_match_snaps_when_unique(self):
        # LLM truncated the EO — only one canonical contains this
        out, status = _normalize_enabling_objective(
            "Apply profit and loss concepts in reverse",
            self.EOS,
        )
        self.assertEqual(status, "snapped")
        self.assertEqual(
            out,
            "Apply profit and loss concepts in reverse (find cost price from profit information)",
        )

    def test_paraphrase_drops_tag(self):
        # LLM paraphrased — no exact / fuzzy / substring match
        out, status = _normalize_enabling_objective(
            "How to figure out cost price when given a profit margin",
            self.EOS,
        )
        self.assertEqual(status, "dropped")
        self.assertEqual(out, "")

    def test_invented_eo_drops(self):
        out, status = _normalize_enabling_objective(
            "Recite the multiplication tables fluently",
            self.EOS,
        )
        self.assertEqual(status, "dropped")
        self.assertEqual(out, "")

    def test_empty_input_returns_empty(self):
        out, status = _normalize_enabling_objective("", self.EOS)
        self.assertEqual(status, "empty")
        self.assertEqual(out, "")

    def test_whitespace_only_input_returns_empty(self):
        out, status = _normalize_enabling_objective("   ", self.EOS)
        self.assertEqual(status, "empty")
        self.assertEqual(out, "")

    def test_no_canonical_eos_drops(self):
        out, status = _normalize_enabling_objective("anything", [])
        self.assertEqual(status, "dropped")
        self.assertEqual(out, "")

    def test_ambiguous_substring_match_drops(self):
        """When multiple canonical EOs match as substrings, refuse
        to guess — drop the tag."""
        eos = [
            "Calculate profit",
            "Calculate profit or loss from given cost price",
        ]
        # Input is a substring of both → ambiguous → drop
        out, status = _normalize_enabling_objective(
            "Calculate profit", eos,
        )
        # Exact match wins over the ambiguity case
        self.assertEqual(status, "exact")
        self.assertEqual(out, "Calculate profit")

    def test_short_substring_drops_when_ambiguous(self):
        eos = [
            "Identify cost price in a profit problem",
            "Identify selling price in a profit problem",
        ]
        out, status = _normalize_enabling_objective(
            "Identify", eos,
        )
        # "Identify" is a substring of both — ambiguous, drop
        self.assertEqual(status, "dropped")
