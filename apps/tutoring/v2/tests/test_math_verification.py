"""MathVerificationTool — fixture suite covering every opcode + both
DSL-validation branches.

Per Phase 1 §5: coverage target — every opcode exercised at least
once, both DSL-validation pass branches (structured + LLM-mediated)
exercised. Composed grading pipeline (canonical vs student value)
via ``values_equivalent``.
"""

from unittest import TestCase

from apps.tutoring.v2.tools.math_verification import (
    DSLValidationError,
    MathVerificationResult,
    MathVerificationTool,
    values_equivalent,
)


# ----------------------------------------------------------------------
# Opcode coverage
# ----------------------------------------------------------------------


class OpcodeCoverageTest(TestCase):
    def setUp(self):
        self.tool = MathVerificationTool()

    def _eval(self, problem, program):
        return self.tool.evaluate(problem, program)

    def test_add(self):
        result = self._eval(
            "Add 2 and 3",
            {"variables": {"a": 2, "b": 3},
             "expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}},
        )
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.canonical_value, 5)

    def test_sub(self):
        result = self._eval(
            "Subtract 3 from 10",
            {"variables": {"a": 10, "b": 3},
             "expression": {"op": "sub", "args": [{"var": "a"}, {"var": "b"}]}},
        )
        self.assertEqual(result.canonical_value, 7)

    def test_mul_div_pow_sqrt(self):
        # 2^3 = 8, 8 * 2 = 16, 16 / 4 = 4, sqrt(4) = 2
        program = {
            "variables": {"a": 2, "b": 3, "c": 2, "d": 4},
            "expression": {
                "op": "sqrt",
                "args": [
                    {
                        "op": "div",
                        "args": [
                            {
                                "op": "mul",
                                "args": [
                                    {"op": "pow", "args": [{"var": "a"}, {"var": "b"}]},
                                    {"var": "c"},
                                ],
                            },
                            {"var": "d"},
                        ],
                    }
                ],
            },
        }
        result = self._eval("Compute sqrt((2^3 * 2) / 4) on 2 3 2 4", program)
        self.assertAlmostEqual(result.canonical_value, 2.0)

    def test_log_and_exp(self):
        program = {
            "variables": {"a": 2.718281828, "b": 1},
            "expression": {
                "op": "log",
                "args": [{"op": "exp", "args": [{"var": "b"}]}],
            },
        }
        result = self._eval("log(exp(1)) on 2.718281828 1", program)
        self.assertAlmostEqual(result.canonical_value, 1.0, places=4)

    def test_trig(self):
        program = {
            "variables": {"a": 0},
            "expression": {
                "op": "add",
                "args": [
                    {"op": "sin", "args": [{"var": "a"}]},
                    {"op": "cos", "args": [{"var": "a"}]},
                ],
            },
        }
        result = self._eval("sin(0) + cos(0) on 0", program)
        self.assertAlmostEqual(result.canonical_value, 1.0)

    def test_eq_lt_gt(self):
        program = {
            "variables": {"a": 5, "b": 5},
            "expression": {"op": "eq", "args": [{"var": "a"}, {"var": "b"}]},
        }
        result = self._eval("5 == 5 on 5 5", program)
        self.assertTrue(result.canonical_value)

    def test_min_max_abs_neg_round(self):
        program = {
            "variables": {"a": 7, "b": 3},
            "expression": {"op": "min", "args": [{"var": "a"}, {"var": "b"}]},
        }
        result = self._eval("min(7, 3) on 7 3", program)
        self.assertEqual(result.canonical_value, 3)

        program = {
            "variables": {"a": -5},
            "expression": {"op": "abs", "args": [{"var": "a"}]},
        }
        result = self._eval("abs(-5) on -5", program)
        self.assertEqual(result.canonical_value, 5)

        program = {
            "variables": {"a": 3.14159},
            "expression": {"op": "round", "args": [{"var": "a"}, 2]},
        }
        result = self._eval("round(3.14159, 2) on 3.14159 2", program)
        self.assertAlmostEqual(result.canonical_value, 3.14)

    def test_solve_linear(self):
        program = {
            "variables": {},
            "expression": {
                "op": "solve",
                "equation": "2*x + 3 = 11",
                "var": "x",
            },
        }
        # "2*x + 3 = 11" contains the numbers 2, 3, 11 — no variables.
        result = self._eval("Solve 2x + 3 = 11 for x", program)
        self.assertAlmostEqual(result.canonical_value, 4.0)


# ----------------------------------------------------------------------
# DSL validation
# ----------------------------------------------------------------------


class DSLValidationTest(TestCase):
    def setUp(self):
        self.tool = MathVerificationTool()

    def test_unwhitelisted_opcode_rejected(self):
        result = self.tool.evaluate(
            "compute something",
            {"variables": {"a": 1}, "expression": {"op": "import", "args": []}},
        )
        self.assertFalse(result.ok)
        self.assertIn("not whitelisted", result.error)

    def test_missing_expression_rejected(self):
        result = self.tool.evaluate("compute", {"variables": {}})
        self.assertFalse(result.ok)
        self.assertIn("missing 'expression'", result.error)

    def test_unbound_var_rejected(self):
        result = self.tool.evaluate(
            "compute",
            {"variables": {}, "expression": {"var": "x"}},
        )
        self.assertFalse(result.ok)
        self.assertIn("unbound", result.error)

    def test_variable_not_in_problem_text_structured(self):
        """Structured branch: numeric value missing from problem text."""
        result = self.tool.evaluate(
            "There are no numbers here",
            {"variables": {"a": 42},
             "expression": {"op": "add", "args": [{"var": "a"}, 0]}},
        )
        self.assertFalse(result.ok)
        self.assertIn("variable_bindings_invalid", result.error)
        self.assertIn("does not appear", result.error)

    def test_variable_not_in_problem_text_llm_branch(self):
        """LLM-mediated branch: structured check says no, LLM also says no."""
        def llm_validator(problem_text, variables):
            return "LLM also rejects"

        tool = MathVerificationTool(llm_validator=llm_validator)
        result = tool.evaluate(
            "no numbers",
            {"variables": {"a": 42},
             "expression": {"op": "add", "args": [{"var": "a"}, 0]}},
        )
        self.assertFalse(result.ok)
        self.assertIn("LLM also rejects", result.error)


# ----------------------------------------------------------------------
# Comparator
# ----------------------------------------------------------------------


class ComparatorTest(TestCase):
    def test_numeric_within_tolerance(self):
        self.assertTrue(values_equivalent(3.14, 3.139, abs_tolerance=0.01))
        self.assertTrue(values_equivalent("5", 5.0))

    def test_numeric_outside_tolerance(self):
        self.assertFalse(values_equivalent(3.14, 3.5, abs_tolerance=0.01))

    def test_symbolic_equivalence(self):
        self.assertTrue(values_equivalent("(x+1)**2", "x**2 + 2*x + 1"))

    def test_symbolic_not_equivalent(self):
        self.assertFalse(values_equivalent("x + 1", "x + 2"))

    def test_none_handling(self):
        self.assertTrue(values_equivalent(None, None))
        self.assertFalse(values_equivalent(None, 0))
