"""Tests for Layer 4 — parametric question template renderer.

Coverage:
  - ParameterSpec validation (max >= min, step grids)
  - Sampling with and without constraints
  - Constraint resampling + give-up
  - Answer-formula evaluation with substitution
  - Full render_template round trip
  - Bad templates handled gracefully (None return)
"""

from __future__ import annotations

import unittest

from ai_tutor.apps.curriculum.parametric_renderer import (
    ParameterSpec,
    ParametricFillBlankTemplate,
    ParametricMCQTemplate,
    ParametricMatchingTemplate,
    ParametricQuestionTemplate,
    ParametricShortAnswerTemplate,
    TemplateValidationError,
    _check_constraint,
    _compute_answer,
    _sample_parameters,
    parse_template,
    render_fill_blank,
    render_matching,
    render_mcq,
    render_short_answer,
    render_template,
    render_typed,
    validate_template,
)


# ============================================================================
# P2a — schema for new template types (MCQ / fill / matching / short_answer)
# ============================================================================


class TestParametricMCQTemplate(unittest.TestCase):
    def test_valid_mcq_template(self):
        t = ParametricMCQTemplate(
            template_text="Three angles around a point are {a}°, {b}°, and x°. What is x?",
            parameters={
                "a": ParameterSpec(type="int", min=30, max=150, step=5),
                "b": ParameterSpec(type="int", min=30, max=150, step=5),
            },
            correct_formula="360 - a - b",
            distractor_formulas=["a + b", "180 - a - b", "360 - a"],
            answer_unit="°",
            explanation_template="x = 360 - {a} - {b} = {answer}.",
        )
        self.assertEqual(t.correct_formula, "360 - a - b")
        self.assertEqual(len(t.distractor_formulas), 3)

    def test_two_distractors_rejected(self):
        with self.assertRaises(Exception):
            ParametricMCQTemplate(
                template_text="x",
                parameters={},
                correct_formula="1",
                distractor_formulas=["1", "2"],  # only 2
                explanation_template="x",
            )

    def test_four_distractors_rejected(self):
        with self.assertRaises(Exception):
            ParametricMCQTemplate(
                template_text="x",
                parameters={},
                correct_formula="1",
                distractor_formulas=["1", "2", "3", "4"],  # too many
                explanation_template="x",
            )


class TestParametricFillBlankTemplate(unittest.TestCase):
    def test_valid_fill_template(self):
        t = ParametricFillBlankTemplate(
            template_text="Third is ___° and sum is ___°.",
            parameters={
                "a": ParameterSpec(type="int", min=30, max=150),
                "b": ParameterSpec(type="int", min=30, max=150),
            },
            blank_formulas=["360 - a - b", "360"],
            explanation_template="x",
        )
        self.assertEqual(len(t.blank_formulas), 2)

    def test_zero_blanks_rejected(self):
        with self.assertRaises(Exception):
            ParametricFillBlankTemplate(
                template_text="No blanks here.",
                parameters={},
                blank_formulas=[],
                explanation_template="x",
            )


class TestParametricMatchingTemplate(unittest.TestCase):
    def test_valid_matching_template(self):
        t = ParametricMatchingTemplate(
            framing_text="Match each angle pair to its sum.",
            parameters={
                "a": ParameterSpec(type="int", min=10, max=80),
                "b": ParameterSpec(type="int", min=10, max=80),
            },
            pair_count=5,
            left_formula="{a}° + {b}°",
            right_formula="a + b",
            distractor_count=2,
            explanation_template="x",
        )
        self.assertEqual(t.pair_count, 5)
        self.assertEqual(t.distractor_count, 2)

    def test_pair_count_below_4_rejected(self):
        with self.assertRaises(Exception):
            ParametricMatchingTemplate(
                framing_text="x", parameters={}, pair_count=3,
                left_formula="x", right_formula="1",
                explanation_template="x",
            )

    def test_pair_count_above_6_rejected(self):
        with self.assertRaises(Exception):
            ParametricMatchingTemplate(
                framing_text="x", parameters={}, pair_count=7,
                left_formula="x", right_formula="1",
                explanation_template="x",
            )


class TestParametricShortAnswerTemplate(unittest.TestCase):
    def test_valid_short_answer_template(self):
        t = ParametricShortAnswerTemplate(
            template_text="Three angles are {a}°, {b}°, x°.",
            parameters={
                "a": ParameterSpec(type="int", min=30, max=150),
                "b": ParameterSpec(type="int", min=30, max=150),
            },
            final_answer_formula="360 - a - b",
            canonical_working="Step 1: Sum to 360. Step 2: x = 360 - {a} - {b} = {answer}.",
            answer_unit="°",
        )
        self.assertEqual(t.final_answer_formula, "360 - a - b")
        # Two-field design: canonical_working is the LLM-review reference
        self.assertIn("Step 1", t.canonical_working)


class TestParseTemplateDispatch(unittest.TestCase):
    def test_routes_to_mcq(self):
        t = parse_template("mcq", {
            "template_text": "x",
            "parameters": {},
            "correct_formula": "1",
            "distractor_formulas": ["2", "3", "4"],
            "explanation_template": "x",
        })
        self.assertIsInstance(t, ParametricMCQTemplate)

    def test_routes_to_short_numeric_existing_class(self):
        t = parse_template("short_numeric", {
            "template_text": "x = {a}",
            "parameters": {"a": {"type": "int", "min": 1, "max": 10}},
            "answer_formula": "a",
            "explanation_template": "x = {answer}",
        })
        self.assertIsInstance(t, ParametricQuestionTemplate)

    def test_unknown_question_type_raises(self):
        with self.assertRaises(ValueError):
            parse_template("totally_made_up", {})


# ============================================================================
# P2b — render functions for each new template type
# ============================================================================


_MCQ_TEMPLATE = ParametricMCQTemplate(
    template_text="Three angles around a point are {a}°, {b}°, x°. Find x.",
    parameters={
        "a": ParameterSpec(type="int", min=30, max=150, step=5),
        "b": ParameterSpec(type="int", min=30, max=150, step=5),
    },
    correct_formula="360 - a - b",
    distractor_formulas=["a + b", "180 - a - b", "360 - a"],
    answer_unit="°",
    explanation_template="x = 360 - {a} - {b} = {answer}.",
    constraints=["a + b < 350"],
)


class TestRenderMCQ(unittest.TestCase):
    def test_renders_full_payload(self):
        out = render_mcq(_MCQ_TEMPLATE, seed=42)
        self.assertIsNotNone(out)
        self.assertEqual(out["question_type"], "mcq")
        # All four options populated
        self.assertTrue(out["option_a"])
        self.assertTrue(out["option_b"])
        self.assertTrue(out["option_c"])
        self.assertTrue(out["option_d"])
        self.assertIn(out["correct_answer"], ("A", "B", "C", "D"))

    def test_correct_letter_corresponds_to_correct_value(self):
        out = render_mcq(_MCQ_TEMPLATE, seed=42)
        a, b = out["answer_data"]["parameters"]["a"], out["answer_data"]["parameters"]["b"]
        expected = f"{360 - a - b}°"
        # The option at the correct letter should equal expected
        opt = out[f"option_{out['correct_answer'].lower()}"]
        self.assertEqual(opt, expected)

    def test_seed_determinism(self):
        a = render_mcq(_MCQ_TEMPLATE, seed=99)
        b = render_mcq(_MCQ_TEMPLATE, seed=99)
        self.assertEqual(a, b)

    def test_different_seeds_produce_different_output(self):
        a = render_mcq(_MCQ_TEMPLATE, seed=1)
        b = render_mcq(_MCQ_TEMPLATE, seed=2)
        self.assertNotEqual(a["answer_data"]["parameters"], b["answer_data"]["parameters"])

    def test_returns_none_when_distractor_collides_with_correct(self):
        # Force correct == distractor for every sample → render returns None
        bad = ParametricMCQTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            correct_formula="a",
            distractor_formulas=["a", "a + 1", "a + 2"],  # 1st distractor == correct
            explanation_template="x = {answer}",
        )
        self.assertIsNone(render_mcq(bad, seed=0))


_FILL_TEMPLATE = ParametricFillBlankTemplate(
    template_text="Two angles are {a}° and {b}°. Third is ___° and sum is ___°.",
    parameters={
        "a": ParameterSpec(type="int", min=30, max=150),
        "b": ParameterSpec(type="int", min=30, max=150),
    },
    blank_formulas=["360 - a - b", "360"],
    answer_unit="°",
    explanation_template="Third = {answer}.",
    constraints=["a + b < 350"],
)


class TestRenderFillBlank(unittest.TestCase):
    def test_renders_blanks(self):
        out = render_fill_blank(_FILL_TEMPLATE, seed=42)
        self.assertIsNotNone(out)
        self.assertEqual(out["question_type"], "fill_in_blank")
        self.assertEqual(len(out["answer_data"]["blanks"]), 2)
        # Stem keeps `___` slots intact for the UI
        self.assertEqual(out["question_text"].count("___"), 2)

    def test_blank_values_match_formulas(self):
        out = render_fill_blank(_FILL_TEMPLATE, seed=42)
        a = out["answer_data"]["parameters"]["a"]
        b = out["answer_data"]["parameters"]["b"]
        self.assertEqual(out["answer_data"]["blanks"][0], f"{360 - a - b}°")
        self.assertEqual(out["answer_data"]["blanks"][1], "360°")

    def test_mismatch_blank_count_returns_none(self):
        # Stem has 1 `___` but formula list has 2 → render rejects
        bad = ParametricFillBlankTemplate(
            template_text="One blank: ___",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            blank_formulas=["a", "a + 1"],
            explanation_template="x = {answer}",
        )
        self.assertIsNone(render_fill_blank(bad, seed=0))


_MATCH_TEMPLATE = ParametricMatchingTemplate(
    framing_text="Match each angle pair to its sum.",
    parameters={
        "a": ParameterSpec(type="int", min=10, max=80, step=5),
        "b": ParameterSpec(type="int", min=10, max=80, step=5),
    },
    pair_count=4,
    left_formula="{a}° + {b}°",
    right_formula="a + b",
    answer_unit="°",
    distractor_count=2,
    explanation_template="x",
)


class TestRenderMatching(unittest.TestCase):
    def test_renders_pair_count(self):
        out = render_matching(_MATCH_TEMPLATE, seed=42)
        self.assertIsNotNone(out)
        self.assertEqual(out["question_type"], "matching")
        self.assertEqual(len(out["answer_data"]["pairs"]), 4)

    def test_pairs_have_distinct_lefts_and_rights(self):
        out = render_matching(_MATCH_TEMPLATE, seed=42)
        lefts = [p["left"] for p in out["answer_data"]["pairs"]]
        rights = [p["right"] for p in out["answer_data"]["pairs"]]
        self.assertEqual(len(set(lefts)), len(lefts))
        self.assertEqual(len(set(rights)), len(rights))

    def test_distractors_dont_collide_with_correct_rights(self):
        out = render_matching(_MATCH_TEMPLATE, seed=42)
        rights = {p["right"] for p in out["answer_data"]["pairs"]}
        for d in out["answer_data"]["distractor_rights"]:
            self.assertNotIn(d, rights)

    def test_unit_appended_to_right_values(self):
        out = render_matching(_MATCH_TEMPLATE, seed=42)
        for p in out["answer_data"]["pairs"]:
            self.assertTrue(p["right"].endswith("°"))


_SHORTANS_TEMPLATE = ParametricShortAnswerTemplate(
    template_text="Three angles are {a}°, {b}°, x°. Find x and show working.",
    parameters={
        "a": ParameterSpec(type="int", min=30, max=150),
        "b": ParameterSpec(type="int", min=30, max=150),
    },
    final_answer_formula="360 - a - b",
    canonical_working="Step 1: Sum to 360. Step 2: x = 360 - {a} - {b} = {answer}.",
    answer_unit="°",
    constraints=["a + b < 350"],
)


class TestRenderShortAnswer(unittest.TestCase):
    def test_renders_two_field_payload(self):
        out = render_short_answer(_SHORTANS_TEMPLATE, seed=42)
        self.assertIsNotNone(out)
        self.assertEqual(out["question_type"], "short_answer")
        # Final answer (deterministic grade target)
        self.assertIn("model_answer", out["answer_data"])
        # Canonical working (LLM compares student working to this)
        self.assertIn("canonical_working", out["answer_data"])
        self.assertIn("Step 1", out["answer_data"]["canonical_working"])

    def test_model_answer_matches_formula(self):
        out = render_short_answer(_SHORTANS_TEMPLATE, seed=42)
        a = out["answer_data"]["parameters"]["a"]
        b = out["answer_data"]["parameters"]["b"]
        self.assertEqual(out["answer_data"]["model_answer"], f"{360 - a - b}°")


class TestRenderTypedDispatch(unittest.TestCase):
    def test_routes_to_mcq(self):
        out = render_typed("mcq", _MCQ_TEMPLATE, seed=42)
        self.assertEqual(out["question_type"], "mcq")

    def test_routes_to_fill(self):
        out = render_typed("fill_in_blank", _FILL_TEMPLATE, seed=42)
        self.assertEqual(out["question_type"], "fill_in_blank")

    def test_routes_to_matching(self):
        out = render_typed("matching", _MATCH_TEMPLATE, seed=42)
        self.assertEqual(out["question_type"], "matching")

    def test_routes_to_short_answer(self):
        out = render_typed("short_answer", _SHORTANS_TEMPLATE, seed=42)
        self.assertEqual(out["question_type"], "short_answer")

    def test_routes_to_existing_short_numeric(self):
        snt = ParametricQuestionTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a",
            explanation_template="x = {answer}",
        )
        out = render_typed("short_numeric", snt, seed=42)
        self.assertEqual(out["question_type"], "short_numeric")


# ============================================================================
# P2c — validate_template_typed
# ============================================================================


class TestValidateTypedMCQ(unittest.TestCase):
    def test_valid_mcq_passes(self):
        # Distractors deliberately distant from correct so no
        # collisions across samples.
        t = ParametricMCQTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            correct_formula="a",
            distractor_formulas=["a + 100", "a * 2 + 50", "a + 200"],
            explanation_template="x = {answer}",
        )
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        self.assertIsNone(validate_template_typed(t))

    def test_distractor_collides_with_correct(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        bad = ParametricMCQTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            correct_formula="a",
            distractor_formulas=["a", "a + 1", "a + 2"],  # 1st collides
            explanation_template="x = {answer}",
        )
        err = validate_template_typed(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "distractor_collision")

    def test_distractors_collide_with_each_other(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        bad = ParametricMCQTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            correct_formula="a + 100",
            distractor_formulas=["a + 1", "a + 1", "a + 2"],  # two are identical
            explanation_template="x = {answer}",
        )
        err = validate_template_typed(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "distractor_collision")


class TestValidateTypedFillBlank(unittest.TestCase):
    def test_valid_fill_passes(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        t = ParametricFillBlankTemplate(
            template_text="A: ___ and B: ___",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            blank_formulas=["a", "a + 1"],
            explanation_template="x = {answer}",
        )
        self.assertIsNone(validate_template_typed(t))

    def test_blank_count_mismatch(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        bad = ParametricFillBlankTemplate(
            template_text="One blank: ___",  # only 1 ___
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            blank_formulas=["a", "a + 1"],   # but 2 formulas
            explanation_template="x = {answer}",
        )
        err = validate_template_typed(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "blank_count_mismatch")

    def test_blank_formula_error(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        bad = ParametricFillBlankTemplate(
            template_text="A: ___",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            blank_formulas=["a + missing_var"],
            explanation_template="x = {answer}",
        )
        err = validate_template_typed(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "formula_error")


class TestValidateTypedMatching(unittest.TestCase):
    def test_valid_matching_passes(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        t = ParametricMatchingTemplate(
            framing_text="Match each pair to its sum.",
            parameters={
                "a": ParameterSpec(type="int", min=10, max=80, step=5),
                "b": ParameterSpec(type="int", min=10, max=80, step=5),
            },
            pair_count=4,
            left_formula="{a}° + {b}°",
            right_formula="a + b",
            answer_unit="°",
            explanation_template="x",
        )
        self.assertIsNone(validate_template_typed(t))

    def test_left_formula_slot_missing(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        bad = ParametricMatchingTemplate(
            framing_text="x",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            pair_count=4,
            left_formula="{nonexistent}",  # references undeclared param
            right_formula="a",
            explanation_template="x",
        )
        err = validate_template_typed(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "missing_template_slot")

    def test_pair_count_too_high_for_param_space(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        # Parameter space has only 1*1=1 unique value but pair_count=4
        bad = ParametricMatchingTemplate(
            framing_text="x",
            parameters={"a": ParameterSpec(type="int", min=1, max=1)},
            pair_count=4,
            left_formula="{a}",
            right_formula="a",
            explanation_template="x",
        )
        err = validate_template_typed(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "matching_pair_collision")


class TestValidateTypedShortAnswer(unittest.TestCase):
    def test_valid_short_answer_passes(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        t = ParametricShortAnswerTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            final_answer_formula="a",
            canonical_working="x = {a} = {answer}",
        )
        self.assertIsNone(validate_template_typed(t))

    def test_canonical_working_slot_missing(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        bad = ParametricShortAnswerTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            final_answer_formula="a",
            canonical_working="bad: {missing_var}",
        )
        err = validate_template_typed(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "missing_explanation_slot")


class TestValidateTypedDispatch(unittest.TestCase):
    def test_passthrough_to_existing_short_numeric(self):
        from ai_tutor.apps.curriculum.parametric_renderer import validate_template_typed
        t = ParametricQuestionTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a",
            explanation_template="x = {answer}",
        )
        # validate_template_typed routes to validate_template, which
        # this template passes.
        self.assertIsNone(validate_template_typed(t))


# Reusable: the canonical "sum to 360°" template.
SUM_TO_360_TEMPLATE = ParametricQuestionTemplate(
    template_text=(
        "Three angles around a point are {a}°, {b}°, and x°. Find x."
    ),
    parameters={
        "a": ParameterSpec(type="int", min=30, max=150, step=5),
        "b": ParameterSpec(type="int", min=30, max=150, step=5),
    },
    answer_formula="360 - a - b",
    answer_unit="°",
    explanation_template=(
        "Angles around a point sum to 360°. So x = 360 - {a} - {b} "
        "= {answer}."
    ),
    constraints=["a + b < 350"],
)


# ============================================================================
# ParameterSpec validation
# ============================================================================


class TestParameterSpec(unittest.TestCase):
    def test_max_below_min_raises(self):
        with self.assertRaises(Exception):  # pydantic ValidationError
            ParameterSpec(type="int", min=10, max=5)

    def test_int_no_step(self):
        spec = ParameterSpec(type="int", min=1, max=10)
        self.assertEqual(spec.type, "int")
        self.assertIsNone(spec.step)

    def test_int_with_step(self):
        spec = ParameterSpec(type="int", min=30, max=150, step=5)
        self.assertEqual(spec.step, 5)


# ============================================================================
# Sampling
# ============================================================================


class TestSampling(unittest.TestCase):
    def test_int_step_sampling_lands_on_grid(self):
        spec = ParameterSpec(type="int", min=30, max=150, step=5)
        # Sample many; every value should be on the grid.
        import random
        rng = random.Random(42)
        from ai_tutor.apps.curriculum.parametric_renderer import _sample_one
        for _ in range(50):
            v = _sample_one(spec, rng)
            self.assertGreaterEqual(v, 30)
            self.assertLessEqual(v, 150)
            # On grid: (v - 30) divisible by 5
            self.assertEqual((v - 30) % 5, 0)

    def test_constraint_holds(self):
        params = _sample_parameters(SUM_TO_360_TEMPLATE, __import__("random").Random(0))
        self.assertIsNotNone(params)
        self.assertLess(params["a"] + params["b"], 350)

    def test_constraint_unsatisfiable_returns_none(self):
        # Force a contradiction: parameters can never satisfy.
        impossible = ParametricQuestionTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a",
            explanation_template="x = {answer}",
            constraints=["a > 100"],  # impossible given range 1-10
        )
        result = _sample_parameters(impossible, __import__("random").Random(0))
        self.assertIsNone(result)


# ============================================================================
# Constraint evaluation
# ============================================================================


class TestCheckConstraint(unittest.TestCase):
    def test_simple_lt(self):
        self.assertTrue(_check_constraint("a + b < 350", {"a": 100, "b": 200}))
        self.assertFalse(_check_constraint("a + b < 100", {"a": 100, "b": 200}))

    def test_le(self):
        self.assertTrue(_check_constraint("a <= 5", {"a": 5}))

    def test_eq(self):
        self.assertTrue(_check_constraint("a == 5", {"a": 5}))
        self.assertFalse(_check_constraint("a == 5", {"a": 6}))

    def test_no_comparator_fails_closed(self):
        # Malformed constraint — treat as not satisfied.
        self.assertFalse(_check_constraint("a + b", {"a": 1, "b": 2}))


# ============================================================================
# Answer computation
# ============================================================================


class TestComputeAnswer(unittest.TestCase):
    def test_sum_subtract(self):
        result = _compute_answer("360 - a - b", {"a": 95, "b": 70})
        self.assertEqual(result, 195.0)

    def test_multiplication(self):
        result = _compute_answer("a * b", {"a": 8, "b": 7})
        self.assertEqual(result, 56.0)

    def test_division(self):
        result = _compute_answer("a / b", {"a": 100, "b": 4})
        self.assertEqual(result, 25.0)

    def test_parens_precedence(self):
        result = _compute_answer("(a + b) * 2", {"a": 5, "b": 10})
        self.assertEqual(result, 30.0)

    def test_undefined_var_returns_none(self):
        result = _compute_answer("a + missing", {"a": 1})
        self.assertIsNone(result)


# ============================================================================
# Full render
# ============================================================================


class TestRenderTemplate(unittest.TestCase):
    def test_sum_to_360_renders_correctly(self):
        # Fixed seed → reproducible parameters.
        result = render_template(SUM_TO_360_TEMPLATE, seed=1)
        self.assertIsNotNone(result)

        a = result["answer_data"]["parameters"]["a"]
        b = result["answer_data"]["parameters"]["b"]

        # Question stem has params filled in.
        self.assertIn(f"{a}°", result["question_text"])
        self.assertIn(f"{b}°", result["question_text"])

        # Answer is computed correctly: 360 - a - b.
        expected = 360 - a - b
        self.assertEqual(result["answer_data"]["computed"], float(expected))
        self.assertEqual(result["correct_answer"], f"{expected}°")

        # Explanation includes the answer (with unit suffix).
        self.assertIn(f"= {expected}°.", result["explanation"])

        # template_data is preserved for retake re-rendering.
        self.assertEqual(
            result["template_data"]["template_text"],
            SUM_TO_360_TEMPLATE.template_text,
        )

    def test_seeds_produce_reproducible_output(self):
        r1 = render_template(SUM_TO_360_TEMPLATE, seed=42)
        r2 = render_template(SUM_TO_360_TEMPLATE, seed=42)
        self.assertEqual(r1["correct_answer"], r2["correct_answer"])
        self.assertEqual(r1["question_text"], r2["question_text"])

    def test_different_seeds_produce_different_output(self):
        # Statistically — with a step=5 grid this should hold.
        r1 = render_template(SUM_TO_360_TEMPLATE, seed=1)
        r2 = render_template(SUM_TO_360_TEMPLATE, seed=2)
        # Either the question or the answer should differ.
        self.assertTrue(
            r1["question_text"] != r2["question_text"]
            or r1["correct_answer"] != r2["correct_answer"]
        )

    def test_unsatisfiable_template_returns_none(self):
        template = ParametricQuestionTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a",
            explanation_template="x = {answer}",
            constraints=["a > 100"],
        )
        self.assertIsNone(render_template(template, seed=0))

    def test_template_with_unknown_slot_returns_none(self):
        template = ParametricQuestionTemplate(
            template_text="x = {a} + {missing}",  # 'missing' not in parameters
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a",
            explanation_template="x = {answer}",
        )
        self.assertIsNone(render_template(template, seed=0))

    def test_no_unit_omits_suffix(self):
        template = ParametricQuestionTemplate(
            template_text="What is {a} + {b}?",
            parameters={
                "a": ParameterSpec(type="int", min=1, max=10),
                "b": ParameterSpec(type="int", min=1, max=10),
            },
            answer_formula="a + b",
            explanation_template="The sum is {answer}.",
        )
        result = render_template(template, seed=5)
        self.assertIsNotNone(result)
        # No unit on the answer string.
        self.assertNotIn("°", result["correct_answer"])
        # Just the integer.
        self.assertTrue(result["correct_answer"].isdigit())

    def test_answer_data_includes_parameters(self):
        result = render_template(SUM_TO_360_TEMPLATE, seed=99)
        self.assertIn("parameters", result["answer_data"])
        self.assertIn("a", result["answer_data"]["parameters"])
        self.assertIn("b", result["answer_data"]["parameters"])

    def test_question_type_default_is_short_numeric(self):
        result = render_template(SUM_TO_360_TEMPLATE, seed=7)
        self.assertEqual(result["question_type"], "short_numeric")


# ============================================================================
# validate_template (F2)
# ============================================================================


class TestValidateTemplate(unittest.TestCase):
    def test_valid_template_returns_none(self):
        self.assertIsNone(validate_template(SUM_TO_360_TEMPLATE))

    def test_constraint_unsatisfiable_caught(self):
        bad = ParametricQuestionTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a",
            explanation_template="x = {answer}",
            constraints=["a > 100"],  # impossible given range 1..10
        )
        err = validate_template(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "constraint_unsatisfiable")

    def test_formula_error_caught(self):
        bad = ParametricQuestionTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a + missing_var",  # references undefined var
            explanation_template="x = {answer}",
        )
        err = validate_template(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "formula_error")

    def test_division_by_zero_caught(self):
        bad = ParametricQuestionTemplate(
            template_text="x = {a}/{b}",
            parameters={
                "a": ParameterSpec(type="int", min=1, max=10),
                "b": ParameterSpec(type="int", min=0, max=0),  # always 0
            },
            answer_formula="a / b",
            explanation_template="x = {answer}",
        )
        err = validate_template(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "formula_error")

    def test_missing_template_slot_caught(self):
        bad = ParametricQuestionTemplate(
            template_text="x = {a} + {missing}",  # `missing` not declared
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a",
            explanation_template="x = {answer}",
        )
        err = validate_template(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "missing_template_slot")

    def test_missing_explanation_slot_caught(self):
        bad = ParametricQuestionTemplate(
            template_text="x = {a}",
            parameters={"a": ParameterSpec(type="int", min=1, max=10)},
            answer_formula="a",
            # `b` not declared, and not the special `{answer}` slot
            explanation_template="x = {answer} ({b})",
        )
        err = validate_template(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "missing_explanation_slot")

    def test_unreasonable_magnitude_caught(self):
        # 10^10 + ... blows past the 1e9 bound on every sample
        bad = ParametricQuestionTemplate(
            template_text="x = {a} ** {b}",
            parameters={
                "a": ParameterSpec(type="int", min=10, max=10),
                "b": ParameterSpec(type="int", min=15, max=15),
            },
            answer_formula="a ** b",  # 10^15 = 1e15
            explanation_template="x = {answer}",
        )
        err = validate_template(bad)
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "unreasonable_magnitude")

    def test_validation_error_to_audit_entry(self):
        err = TemplateValidationError(
            kind="formula_error",
            message="…",
            sample_params={"a": 5},
            sample_index=2,
        )
        d = err.to_audit_entry()
        self.assertEqual(d["kind"], "formula_error")
        self.assertEqual(d["sample_index"], 2)
        self.assertEqual(d["sample_params"], {"a": 5})


if __name__ == "__main__":
    unittest.main()


class TestSampleOneFloatNoise(unittest.TestCase):
    """Step-grid sampling must not leak float noise into rendered stems.

    min + k*step in binary floating point produced values like
    0.7000000000000001, which rendered verbatim into student-visible
    question text ("The probability that it rains tomorrow is
    0.7000000000000001") — found across the eval fixtures in the
    2026-07-18 multi-turn sweep."""

    def test_step_grid_values_are_clean(self):
        import random
        from ai_tutor.apps.curriculum.parametric_renderer import (
            ParameterSpec, _sample_one,
        )
        spec = ParameterSpec(name='p', type='float', min=0.1, max=0.9,
                             step=0.1)
        rng = random.Random(0)
        for _ in range(200):
            v = _sample_one(spec, rng)
            self.assertEqual(
                v, round(v, 6),
                f'float noise leaked from step grid: {v!r}',
            )
