"""Lock-in test for the prompt-rule ↔ eval-check registry.

This test runs both as a standalone unittest module
(``python -m unittest evals.test_rule_registry``) and as part of the
simple_tutor suite if discovered there.

The contract the registry encodes:

  1. Every entry in RULES that lists ``deterministic_verbs`` must point
     to a verb the deterministic scorer actually handles.
  2. Every entry that lists ``judge_dimensions`` must point to a
     dimension the pedagogical-dimensions judge actually evaluates.

A rule with NO checks at all is allowed — those rules are documented
with a ``notes:`` field explaining where enforcement lives (schema
level, handler level, etc.). The registry catches orphan REFERENCES
(typos in verb / dimension names), which would silently no-op at
scoring time.
"""
from __future__ import annotations

import unittest

from evals.rule_registry import build_coverage_report, RULES


class RuleRegistryTest(unittest.TestCase):
    def test_no_unknown_deterministic_verbs(self):
        report = build_coverage_report()
        self.assertEqual(
            report.unknown_verbs, [],
            msg=(
                "Registry references deterministic verbs that don't exist "
                "in evals.scorers.deterministic._HANDLERS. Either add the "
                "verb to the scorer or fix the typo in the registry."
            ),
        )

    def test_no_unknown_judge_dimensions(self):
        report = build_coverage_report()
        self.assertEqual(
            report.unknown_dimensions, [],
            msg=(
                "Registry references judge dimensions that don't exist "
                "in evals.scorers.llm_rubric.PEDAGOGICAL_DIMENSIONS. "
                "Either add the dimension or fix the typo."
            ),
        )

    def test_registry_is_non_empty(self):
        # Sanity: at least the M12-era rules should be registered.
        self.assertGreater(len(RULES), 5)
        ids = {r.id for r in RULES}
        # The high-leverage rules whose violations have been observed in
        # production must be in the registry — these are the ones the
        # 2026-05-27 audit specifically targeted.
        for required in ('R02', 'R07', 'R14', 'R15'):
            self.assertIn(
                required, ids,
                msg=(
                    f"Rule {required} must be registered — it was "
                    "called out in the 2026-05-27 prompt audit."
                ),
            )

    def test_every_covered_rule_has_real_checks(self):
        # If a rule lists deterministic_verbs OR judge_dimensions, both
        # lists must be non-empty-or-absent (not [['']]) and contain only
        # known names. The combined unknown_verbs / unknown_dimensions
        # tests above already cover unknown names; this guards against
        # an empty-but-truthy [""].
        for rule in RULES:
            for verb in rule.deterministic_verbs:
                self.assertTrue(
                    verb.strip(),
                    msg=f"Rule {rule.id}: empty deterministic verb string",
                )
            for dim in rule.judge_dimensions:
                self.assertTrue(
                    dim.strip(),
                    msg=f"Rule {rule.id}: empty judge dimension string",
                )


if __name__ == '__main__':
    unittest.main()
