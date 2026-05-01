"""Tests for Layer S — student_working_analyzer.

Coverage targets:
  - All 5 terminal states (NO_WORKING, PARTIAL_CORRECT, PARTIAL_WRONG,
    COMPLETE_CORRECT, COMPLETE_WRONG)
  - All 6 separator formats (newlines, semicolons, commas, periods,
    spaces, prose connectives) all extract identically
  - Chain analysis: FIRST_ERROR detection + propagation
  - Edge cases: units, parens, decimals, mixed signs, sequential
    equals, unicode operators, bare answers, prose-only

See `memory/llm_arithmetic_defense_plan.md` (Layer S section) for the
design intent.
"""

from __future__ import annotations

import unittest

from apps.tutoring.student_working_analyzer import (
    Step,
    WorkingAnalysis,
    WorkingState,
    analyze_chain,
    analyze_working,
    build_working_analysis_block,
    extract_steps,
    safe_eval_arithmetic,
    verify_steps,
)


# ============================================================================
# safe_eval_arithmetic — the AST walker
# ============================================================================


class TestSafeEvalArithmetic(unittest.TestCase):
    def test_simple_addition(self):
        self.assertEqual(safe_eval_arithmetic("3 + 4"), 7.0)

    def test_n_term_sum(self):
        self.assertEqual(safe_eval_arithmetic("60 + 80 + 75 + 70 + 75"), 360.0)

    def test_subtraction(self):
        self.assertEqual(safe_eval_arithmetic("360 - 275"), 85.0)

    def test_mixed_precedence(self):
        # BIDMAS: 3 + 4 × 2 = 11, not 14
        self.assertEqual(safe_eval_arithmetic("3 + 4 * 2"), 11.0)

    def test_parens_override_precedence(self):
        self.assertEqual(safe_eval_arithmetic("(3 + 4) * 2"), 14.0)

    def test_negative_unary(self):
        self.assertEqual(safe_eval_arithmetic("-5 + 10"), 5.0)

    def test_decimal(self):
        self.assertEqual(safe_eval_arithmetic("8 * 2.5"), 20.0)

    def test_division_by_zero_returns_none(self):
        self.assertIsNone(safe_eval_arithmetic("5 / 0"))

    def test_rejects_variable(self):
        # 'x' is a Name node — not allowed
        self.assertIsNone(safe_eval_arithmetic("x + 1"))

    def test_rejects_function_call(self):
        self.assertIsNone(safe_eval_arithmetic("max(1, 2)"))

    def test_rejects_attribute_access(self):
        self.assertIsNone(safe_eval_arithmetic("__import__('os').system('rm')"))

    def test_empty_returns_none(self):
        self.assertIsNone(safe_eval_arithmetic(""))
        self.assertIsNone(safe_eval_arithmetic("   "))

    def test_garbage_returns_none(self):
        self.assertIsNone(safe_eval_arithmetic("not a number"))


# ============================================================================
# extract_steps — separator robustness
# ============================================================================


class TestExtractStepsSeparators(unittest.TestCase):
    """All 6 separator formats from the plan should yield identical
    step extraction. The walker is separator-agnostic by design."""

    EXPECTED_STEPS = [
        ("95+70", "165"),
        ("165+110", "275"),
        ("360-275", "85"),
    ]

    def _check(self, text: str) -> None:
        steps = extract_steps(text)
        actual = [(s.expr.replace(" ", ""), s.claim) for s in steps]
        self.assertEqual(actual, self.EXPECTED_STEPS, f"failed for: {text!r}")

    def test_newlines(self):
        self._check("95+70=165\n165+110=275\n360-275=85")

    def test_semicolons(self):
        self._check("95+70=165;165+110=275;360-275=85")

    def test_commas(self):
        self._check("95+70=165, 165+110=275, 360-275=85")

    def test_periods(self):
        self._check("95+70=165. 165+110=275. 360-275=85.")

    def test_just_spaces(self):
        self._check("95+70=165   165+110=275   360-275=85")

    def test_prose_connectives(self):
        self._check("First, 95+70=165. Then 165+110=275. So 360-275=85.")

    def test_mixed_then_so(self):
        self._check(
            "95+70=165 then I did 165+110=275 and finally 360-275=85"
        )

    def test_no_separator_at_all(self):
        # Adjacent equations with only the natural `=`-boundary.
        self._check("95+70=165 165+110=275 360-275=85")


# ============================================================================
# extract_steps — what NOT to extract
# ============================================================================


class TestExtractStepsNonExtraction(unittest.TestCase):
    def test_pure_prose_extracts_nothing(self):
        steps = extract_steps(
            "I added 95 and 70 to get 165, then added 110 to make 275."
        )
        self.assertEqual(steps, [])

    def test_bare_number_extracts_nothing(self):
        self.assertEqual(extract_steps("85"), [])
        self.assertEqual(extract_steps("  85  "), [])

    def test_variable_assignment_alone_skipped(self):
        # `x = 85` has no expression on the LHS — just a number on RHS.
        # No operator → not a step.
        self.assertEqual(extract_steps("x = 85"), [])

    def test_inequality_not_extracted(self):
        # `≤ < > ≥` aren't claim signals.
        self.assertEqual(extract_steps("x ≤ 10"), [])

    def test_single_number_rhs(self):
        self.assertEqual(extract_steps("answer = 100"), [])


# ============================================================================
# extract_steps — edge cases
# ============================================================================


class TestExtractStepsEdgeCases(unittest.TestCase):
    def test_unicode_operators(self):
        # × and ÷ should normalize to * and /
        steps = extract_steps("8 × 2.5 = 20")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].claim, "20")

    def test_unicode_minus(self):
        # − (Unicode minus) should normalize to ASCII -
        steps = extract_steps("100 − 25 = 75")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].claim, "75")

    def test_degree_symbol_stripped(self):
        steps = extract_steps("95° + 70° + 110° = 275°")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].claim, "275")

    def test_units_stripped(self):
        steps = extract_steps("5 cm + 3 cm = 8 cm")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].claim, "8")

    def test_parentheses(self):
        steps = extract_steps("(95 + 70) * 2 = 330")
        self.assertEqual(len(steps), 1)

    def test_decimals(self):
        steps = extract_steps("3.14 * 2 = 6.28")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].claim, "6.28")

    def test_negative_numbers_in_expr(self):
        # `-5 + 10 = 5` — negative starts the expression.
        steps = extract_steps("-5 + 10 = 5")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].claim, "5")


# ============================================================================
# verify_steps
# ============================================================================


class TestVerifySteps(unittest.TestCase):
    def test_correct_step_marked_ok(self):
        steps = extract_steps("3 + 4 = 7")
        verify_steps(steps)
        self.assertTrue(steps[0].ok)
        self.assertEqual(steps[0].computed, 7.0)

    def test_wrong_step_marked_not_ok(self):
        steps = extract_steps("3 + 4 = 8")
        verify_steps(steps)
        self.assertFalse(steps[0].ok)
        self.assertEqual(steps[0].computed, 7.0)

    def test_unevaluable_step_marked_not_ok(self):
        # Manually construct a step with garbage expr (extract_steps
        # wouldn't produce this, but test defensive behavior).
        step = Step(idx=1, expr="not arithmetic", claim="5")
        verify_steps([step])
        self.assertIsNone(step.computed)
        self.assertFalse(step.ok)

    def test_multi_step_all_correct(self):
        steps = extract_steps("3 + 4 = 7\n7 * 2 = 14")
        verify_steps(steps)
        self.assertTrue(all(s.ok for s in steps))

    def test_multi_step_one_wrong(self):
        steps = extract_steps("3 + 4 = 7\n7 * 2 = 15")
        verify_steps(steps)
        self.assertTrue(steps[0].ok)
        self.assertFalse(steps[1].ok)


# ============================================================================
# analyze_chain — FIRST_ERROR + propagation
# ============================================================================


class TestAnalyzeChain(unittest.TestCase):
    def test_no_error_returns_none(self):
        steps = extract_steps("3 + 4 = 7\n7 * 2 = 14")
        verify_steps(steps)
        first, propagated = analyze_chain(steps)
        self.assertIsNone(first)
        self.assertEqual(propagated, set())

    def test_simple_first_error(self):
        steps = extract_steps("3 + 4 = 8\n10 - 5 = 5")
        verify_steps(steps)
        first, propagated = analyze_chain(steps)
        self.assertEqual(first, 1)
        # Step 2 doesn't depend on step 1 (no shared values), so
        # not propagated.
        self.assertEqual(propagated, set())

    def test_propagation_via_dependency(self):
        # Step 2's expression uses step 1's claim (165). Step 1 is
        # right; step 2 has its own arithmetic error.
        # Wait — for propagation to fire, step 1 must be the wrong
        # one. Reorder: step 1 wrong, step 2 uses step 1's claim.
        steps = extract_steps(
            "95 + 70 + 110 = 285\n"   # wrong (correct: 275)
            "360 - 285 = 75"          # internally correct given 285
        )
        verify_steps(steps)
        first, propagated = analyze_chain(steps)
        self.assertEqual(first, 1)
        # Step 2 depends on step 1 (uses 285) — propagated.
        self.assertEqual(propagated, {2})

    def test_propagation_chain_three_deep(self):
        # 1: a + b = c1 (wrong)
        # 2: c1 + d = c2 (uses c1)
        # 3: c2 + e = c3 (uses c2)
        # All three should be flagged: 1 as first_error, 2+3 as propagated.
        steps = extract_steps(
            "10 + 20 = 31\n"   # wrong, should be 30
            "31 + 5 = 36\n"    # internally correct given 31
            "36 + 1 = 37"      # internally correct given 36
        )
        verify_steps(steps)
        first, propagated = analyze_chain(steps)
        self.assertEqual(first, 1)
        self.assertEqual(propagated, {2, 3})

    def test_no_propagation_when_independent(self):
        # Two independent errors, no shared values.
        steps = extract_steps("2 + 2 = 5\n10 / 5 = 1")
        verify_steps(steps)
        first, propagated = analyze_chain(steps)
        self.assertEqual(first, 1)
        # Step 2 is wrong but doesn't depend on step 1.
        self.assertEqual(propagated, set())


# ============================================================================
# analyze_working — the 5 terminal states
# ============================================================================


class TestAnalyzeWorkingStates(unittest.TestCase):
    def test_no_working_bare_answer(self):
        analysis = analyze_working("85", expected_answer="85")
        self.assertEqual(analysis.state, WorkingState.NO_WORKING)
        self.assertEqual(analysis.steps, [])

    def test_no_working_pure_prose(self):
        analysis = analyze_working(
            "I think the answer is 85 because of the rule.",
            expected_answer="85",
        )
        self.assertEqual(analysis.state, WorkingState.NO_WORKING)

    def test_complete_correct(self):
        analysis = analyze_working(
            "95 + 70 + 110 = 275\n360 - 275 = 85",
            expected_answer="85",
        )
        self.assertEqual(analysis.state, WorkingState.COMPLETE_CORRECT)
        self.assertEqual(len(analysis.steps), 2)
        self.assertIsNone(analysis.first_error_idx)
        self.assertEqual(analysis.final_claim, 85.0)
        self.assertEqual(analysis.expected_answer, 85.0)

    def test_partial_correct_stopped_at_intermediate(self):
        # Student showed correct first step but didn't finish.
        analysis = analyze_working(
            "95 + 70 + 110 = 275",
            expected_answer="85",
        )
        self.assertEqual(analysis.state, WorkingState.PARTIAL_CORRECT)
        self.assertEqual(analysis.final_claim, 275.0)
        self.assertEqual(analysis.expected_answer, 85.0)

    def test_partial_wrong_first_step_error(self):
        # Wrong arithmetic, stopped at intermediate.
        analysis = analyze_working(
            "95 + 70 + 110 = 285",
            expected_answer="85",
        )
        self.assertEqual(analysis.state, WorkingState.PARTIAL_WRONG)
        self.assertEqual(analysis.first_error_idx, 1)

    def test_partial_wrong_with_propagation(self):
        # Step 1 wrong; step 2 propagates and lands not at expected.
        analysis = analyze_working(
            "95 + 70 + 110 = 285\n360 - 285 = 75",
            expected_answer="85",
        )
        self.assertEqual(analysis.state, WorkingState.PARTIAL_WRONG)
        self.assertEqual(analysis.first_error_idx, 1)
        self.assertEqual(analysis.propagated_idxs, {2})
        self.assertEqual(analysis.final_claim, 75.0)

    def test_no_expected_answer_defaults_partial(self):
        # Without expected_answer, can't decide complete vs partial.
        # Falls through to PARTIAL_CORRECT (cheap heuristic).
        analysis = analyze_working("3 + 4 = 7", expected_answer=None)
        self.assertEqual(analysis.state, WorkingState.PARTIAL_CORRECT)


# ============================================================================
# analyze_working — edge cases
# ============================================================================


class TestAnalyzeWorkingEdgeCases(unittest.TestCase):
    def test_expected_answer_with_units_parses(self):
        # expected_answer="85°" should still match a bare 85 final claim
        analysis = analyze_working(
            "95 + 70 + 110 = 275\n360 - 275 = 85",
            expected_answer="85°",
        )
        self.assertEqual(analysis.state, WorkingState.COMPLETE_CORRECT)

    def test_unparseable_expected_treated_as_none(self):
        analysis = analyze_working(
            "3 + 4 = 7",
            expected_answer="any positive integer",
        )
        # Falls through to PARTIAL_CORRECT default.
        self.assertEqual(analysis.state, WorkingState.PARTIAL_CORRECT)

    def test_correct_landing_with_broken_working(self):
        # Edge case from the algorithm: errors upstream but final
        # claim = expected. Should still flag PARTIAL_WRONG so the
        # tutor diagnoses the broken step.
        analysis = analyze_working(
            "100 + 100 = 250\n"     # wrong (200)
            "250 - 165 = 85",       # internally correct given 250
            expected_answer="85",
        )
        self.assertEqual(analysis.state, WorkingState.PARTIAL_WRONG)
        self.assertEqual(analysis.first_error_idx, 1)

    def test_decimal_tolerance(self):
        # Recurring decimals in division — tolerance is 0.01.
        analysis = analyze_working(
            "280 / 6 = 46.67",
            expected_answer="46.67",
        )
        self.assertEqual(analysis.state, WorkingState.COMPLETE_CORRECT)

    def test_mixed_separators(self):
        # Real-world: student mixes formats freely.
        analysis = analyze_working(
            "95+70=165, then 165+110=275; finally 360-275=85",
            expected_answer="85",
        )
        self.assertEqual(analysis.state, WorkingState.COMPLETE_CORRECT)
        self.assertEqual(len(analysis.steps), 3)


# ============================================================================
# build_working_analysis_block — prompt block rendering (S2)
# ============================================================================


class TestBuildWorkingAnalysisBlock(unittest.TestCase):
    """Each state must produce a block with the right header,
    the right ACTION directives, and the right surrounding
    context. Tests assert key phrases that drive tutor behaviour
    so future edits don't accidentally weaken the contract."""

    def _block_for(self, student_input: str, expected: str = "85") -> str:
        analysis = analyze_working(student_input, expected_answer=expected)
        return build_working_analysis_block(analysis)

    def test_block_wraps_in_tags(self):
        block = self._block_for("85", "85")
        self.assertTrue(block.startswith("<student_working_analysis>"))
        self.assertTrue(block.endswith("</student_working_analysis>"))

    def test_no_working_block_includes_separator_request(self):
        block = self._block_for("I added 95 + 70 to get 165")
        self.assertIn("NO_WORKING", block)
        self.assertIn("Politely ask them to write each step", block)
        # Sample format is shown so the tutor can quote it
        self.assertIn("95 + 70 = 165", block)

    def test_no_working_block_echoes_student_input(self):
        block = self._block_for("the answer is 85")
        self.assertIn("the answer is 85", block)

    def test_partial_correct_block_forbids_completing(self):
        block = self._block_for("95 + 70 + 110 = 275", expected="85")
        self.assertIn("PARTIAL_CORRECT", block)
        self.assertIn("DO NOT compute the remaining step", block)
        self.assertIn("DO NOT state the final answer", block)

    def test_partial_wrong_block_directs_to_first_error(self):
        block = self._block_for("95 + 70 + 110 = 285", expected="85")
        self.assertIn("PARTIAL_WRONG", block)
        self.assertIn("FIRST ERROR: Step 1", block)
        # "recompute" is the key directive — line wrap separates it
        # from "the specific step", so just check for the phrase
        # without the literal line-broken whitespace.
        self.assertIn("recompute", block)
        self.assertIn("specific step", block)
        self.assertIn("Do NOT state the correct value yet", block)

    def test_complete_correct_block_forbids_blind_praise(self):
        block = self._block_for(
            "95 + 70 + 110 = 275\n360 - 275 = 85",
            expected="85",
        )
        self.assertIn("COMPLETE_CORRECT", block)
        # Must NOT just say "great, next problem" — explicitly
        # require articulation
        self.assertIn("DO NOT just say", block)
        self.assertIn("articulate", block)

    def test_complete_wrong_block_focuses_on_setup(self):
        # COMPLETE_WRONG is not currently produced by analyze_working
        # in v1 — the cheap heuristic absorbs it into PARTIAL_CORRECT
        # because tutor behaviour is the same in both ("ask the
        # student to walk through it"). We still test the block
        # builder renders the correct content for forward-compat.
        analysis = WorkingAnalysis(
            state=WorkingState.COMPLETE_WRONG,
            steps=[
                Step(idx=1, expr="95 * 2", claim="190", computed=190.0, ok=True),
                Step(idx=2, expr="190 + 0", claim="190", computed=190.0, ok=True),
            ],
            final_claim=190.0,
            expected_answer=85.0,
            raw_input="95 * 2 = 190\n190 + 0 = 190",
        )
        block = build_working_analysis_block(analysis)
        self.assertIn("COMPLETE_WRONG", block)
        self.assertIn("setup", block.lower())
        self.assertIn("Do NOT focus on the arithmetic", block)

    def test_block_shows_step_markers(self):
        block = self._block_for("3 + 4 = 8\n10 + 5 = 15", expected="15")
        # Step 1 wrong (3+4=8 → claim 8, computed 7) → ✗
        # Step 2 right
        self.assertIn("✗", block)
        self.assertIn("✓", block)
        # The corrected value (7) appears so the LLM knows what's
        # right (it decides whether to reveal it to the student)
        self.assertIn("(correct: 7)", block)

    def test_propagated_steps_annotated(self):
        block = self._block_for(
            "95 + 70 + 110 = 285\n360 - 285 = 75",
            expected="85",
        )
        self.assertIn("propagates", block.lower())

    def test_expected_answer_shown(self):
        block = self._block_for("95 + 70 + 110 = 275", expected="85")
        self.assertIn("expected_answer", block)
        self.assertIn("85", block)


if __name__ == "__main__":
    unittest.main()
