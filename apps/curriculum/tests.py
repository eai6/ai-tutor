"""
Tests for the interactive-widget pipeline.

Covers:
- apps.curriculum.widgets    — MediaWidget schema, safe expression validator,
                                per-type Pydantic params, author-side eval helper.
- apps.curriculum.widget_presets — every preset validates; formulas match
                                authoritative sources (UN HDI, WHO BMI, etc.).
- _build_media_catalog       — widgets appear in the numbered media catalog
                                alongside images and resolve via the
                                |||MEDIA:N||| signal.
"""

import math

from django.test import SimpleTestCase, TestCase

from apps.curriculum.widgets import (
    CompositeIndexParams,
    FractionDecimalPercentParams,
    FunctionPlotterParams,
    MediaWidget,
    eval_expression,
    validate_expression,
)
from apps.curriculum.widget_presets import (
    PRESETS,
    get_preset_spec,
    preset_groups,
)


# ===========================================================================
# Expression validator
# ===========================================================================

class ExpressionValidatorTests(SimpleTestCase):
    """validate_expression must accept only the declared grammar subset."""

    def test_accepts_arithmetic_with_allowed_names(self):
        validate_expression("a + b * 2 - 1", allowed_names=["a", "b"])
        validate_expression("(a + b) / 3", allowed_names=["a", "b"])
        validate_expression("x ** 2 + 1", allowed_names=["x"])

    def test_accepts_whitelisted_functions(self):
        validate_expression("sqrt(a * b)", allowed_names=["a", "b"])
        validate_expression("log(x) + log10(x)", allowed_names=["x"])
        validate_expression("sin(t) + cos(t)", allowed_names=["t"])

    def test_accepts_constants(self):
        validate_expression("pi * r**2", allowed_names=["r"])

    def test_rejects_unknown_identifier(self):
        with self.assertRaisesMessage(ValueError, "unknown identifier"):
            validate_expression("a + z", allowed_names=["a"])

    def test_rejects_disallowed_function(self):
        with self.assertRaisesMessage(ValueError, "function '__import__' not allowed"):
            validate_expression("__import__('os')", allowed_names=[])

    def test_rejects_attribute_access(self):
        with self.assertRaises(ValueError):
            validate_expression("a.__class__", allowed_names=["a"])

    def test_rejects_subscript(self):
        with self.assertRaises(ValueError):
            validate_expression("a[0]", allowed_names=["a"])

    def test_rejects_string_literal(self):
        with self.assertRaisesMessage(ValueError, "only numeric literals allowed"):
            validate_expression("'hello'", allowed_names=[])

    def test_rejects_lambda(self):
        with self.assertRaises(ValueError):
            validate_expression("(lambda: 1)()", allowed_names=[])

    def test_rejects_overlong_expression(self):
        with self.assertRaisesMessage(ValueError, "expression too long"):
            validate_expression("a+" * 300 + "a", allowed_names=["a"])

    def test_rejects_syntax_error(self):
        with self.assertRaisesMessage(ValueError, "does not parse"):
            validate_expression("a +", allowed_names=["a"])


class ExpressionEvalTests(SimpleTestCase):
    """eval_expression computes the same value as arithmetic by hand."""

    def test_basic_arithmetic(self):
        self.assertAlmostEqual(
            eval_expression("(a + b) / 2", {"a": 2.0, "b": 4.0}),
            3.0,
        )

    def test_power_and_sqrt(self):
        self.assertAlmostEqual(
            eval_expression("sqrt(a * b)", {"a": 9.0, "b": 4.0}),
            6.0,
        )

    def test_constants(self):
        self.assertAlmostEqual(eval_expression("pi", {}), math.pi)


# ===========================================================================
# Per-type Pydantic params
# ===========================================================================

class CompositeIndexParamsTests(SimpleTestCase):

    def _valid(self, **over):
        base = {
            "inputs": [
                {"key": "a", "label": "A", "min": 0, "max": 1, "default": 0.5, "step": 0.01},
                {"key": "b", "label": "B", "min": 0, "max": 1, "default": 0.5, "step": 0.01},
            ],
            "formula": "(a + b) / 2",
            "output_label": "Score", "output_min": 0.0, "output_max": 1.0, "precision": 3,
            "bands": [
                {"label": "Low", "min": 0.0, "color": "#ef4444"},
                {"label": "High", "min": 0.5, "color": "#10b981"},
            ],
            "references": [{"label": "Ref", "value": 0.9}],
        }
        base.update(over)
        return base

    def test_valid_spec(self):
        CompositeIndexParams.model_validate(self._valid())

    def test_duplicate_input_keys_rejected(self):
        spec = self._valid(inputs=[
            {"key": "a", "label": "A", "min": 0, "max": 1, "default": 0.5, "step": 0.01},
            {"key": "a", "label": "A2", "min": 0, "max": 1, "default": 0.5, "step": 0.01},
        ])
        with self.assertRaisesMessage(ValueError, "input keys must be unique"):
            CompositeIndexParams.model_validate(spec)

    def test_default_outside_range_rejected(self):
        spec = self._valid(inputs=[
            {"key": "a", "label": "A", "min": 0, "max": 1, "default": 2.0, "step": 0.01},
            {"key": "b", "label": "B", "min": 0, "max": 1, "default": 0.5, "step": 0.01},
        ])
        with self.assertRaises(ValueError):
            CompositeIndexParams.model_validate(spec)

    def test_formula_references_unknown_key_rejected(self):
        spec = self._valid(formula="a + c")
        with self.assertRaises(ValueError):
            CompositeIndexParams.model_validate(spec)

    def test_unsorted_bands_rejected(self):
        spec = self._valid(bands=[
            {"label": "High", "min": 0.5, "color": "#10b981"},
            {"label": "Low", "min": 0.0, "color": "#ef4444"},
        ])
        with self.assertRaisesMessage(ValueError, "ascending min order"):
            CompositeIndexParams.model_validate(spec)


class FunctionPlotterParamsTests(SimpleTestCase):

    def test_valid(self):
        FunctionPlotterParams.model_validate({
            "expression": "m*x + c",
            "x_min": -10, "x_max": 10,
            "parameters": [
                {"key": "m", "label": "m", "min": -5, "max": 5, "default": 1, "step": 0.1},
                {"key": "c", "label": "c", "min": -10, "max": 10, "default": 0, "step": 0.5},
            ],
        })

    def test_reserves_x(self):
        with self.assertRaisesMessage(ValueError, "'x' is reserved"):
            FunctionPlotterParams.model_validate({
                "expression": "x",
                "parameters": [
                    {"key": "x", "label": "x", "min": 0, "max": 1, "default": 0.5, "step": 0.1},
                ],
            })

    def test_rejects_unknown_parameter_in_expression(self):
        with self.assertRaises(ValueError):
            FunctionPlotterParams.model_validate({
                "expression": "m*x + k",
                "parameters": [
                    {"key": "m", "label": "m", "min": -1, "max": 1, "default": 0, "step": 0.1},
                ],
            })

    def test_x_range_must_be_ordered(self):
        with self.assertRaisesMessage(ValueError, "x_min must be < x_max"):
            FunctionPlotterParams.model_validate({"expression": "x", "x_min": 10, "x_max": -10})


class FractionDecimalPercentParamsTests(SimpleTestCase):

    def test_valid_defaults(self):
        FractionDecimalPercentParams.model_validate({})

    def test_default_numerator_bounded(self):
        with self.assertRaisesMessage(ValueError, "default_numerator cannot exceed denominator"):
            FractionDecimalPercentParams.model_validate({"denominator": 4, "default_numerator": 5})

    def test_at_least_one_representation_required(self):
        with self.assertRaisesMessage(ValueError, "at least one visual representation"):
            FractionDecimalPercentParams.model_validate({
                "show_bar": False, "show_pie": False, "show_number_line": False,
            })


# ===========================================================================
# MediaWidget envelope
# ===========================================================================

class MediaWidgetTests(SimpleTestCase):

    def test_type_drives_params_validation(self):
        widget = MediaWidget.model_validate({
            "widget_type": "fraction_decimal_percent",
            "title": "Tenths",
            "alt_text": "Tenths.",
            "params": {"denominator": 10, "default_numerator": 3},
        })
        # Params are normalized to dict form after validation.
        self.assertIsInstance(widget.params, dict)
        self.assertEqual(widget.params["denominator"], 10)

    def test_unknown_widget_type_rejected(self):
        with self.assertRaises(Exception):
            MediaWidget.model_validate({
                "widget_type": "not_a_widget",
                "title": "x", "alt_text": "x",
                "params": {},
            })

    def test_mismatched_params_rejected(self):
        # function_plotter params shape fed to composite_index_explorer
        with self.assertRaises(Exception):
            MediaWidget.model_validate({
                "widget_type": "composite_index_explorer",
                "title": "x", "alt_text": "x",
                "params": {"expression": "m*x"},
            })


# ===========================================================================
# Preset library
# ===========================================================================

class WidgetPresetTests(SimpleTestCase):
    """Every preset must be a valid MediaWidget; authoritative formulas must
    match the relevant authoritative source (UN HDI, WHO BMI, etc.)."""

    def test_every_preset_validates(self):
        for preset in PRESETS:
            with self.subTest(preset=preset["key"]):
                MediaWidget.model_validate(preset["spec"])

    def test_preset_groups_are_unique_by_type(self):
        groups = preset_groups()
        types = [g["type"] for g in groups]
        self.assertEqual(sorted(types), sorted(set(types)))

    def test_preset_groups_cover_all_types(self):
        types = {g["type"] for g in preset_groups()}
        self.assertEqual(
            types,
            {"composite_index_explorer", "function_plotter", "fraction_decimal_percent"},
        )

    def test_get_preset_spec_returns_copy(self):
        # Mutating the returned spec must not affect later calls.
        a = get_preset_spec("hdi")
        a["title"] = "mutated"
        b = get_preset_spec("hdi")
        self.assertEqual(b["title"], "HDI Explorer")

    def test_hdi_formula_matches_un_methodology(self):
        # UNDP post-2010: HDI = geometric mean of three 0-1 dimension indices.
        spec = get_preset_spec("hdi")
        formula = spec["params"]["formula"]
        # Known triple: all three = 0.5 should give HDI = 0.5.
        self.assertAlmostEqual(
            eval_expression(formula, {"life_idx": 0.5, "edu_idx": 0.5, "income_idx": 0.5}),
            0.5, places=6,
        )
        # Known triple: indices 0.7, 0.6, 0.6 → (0.7*0.6*0.6)**(1/3) ≈ 0.6316
        self.assertAlmostEqual(
            eval_expression(formula, {"life_idx": 0.7, "edu_idx": 0.6, "income_idx": 0.6}),
            (0.7 * 0.6 * 0.6) ** (1 / 3), places=6,
        )
        # Zero in any dimension drags HDI to zero (the pedagogical point).
        self.assertEqual(
            eval_expression(formula, {"life_idx": 0.9, "edu_idx": 0.0, "income_idx": 0.9}),
            0.0,
        )

    def test_hdi_bands_match_undp(self):
        # UNDP HDR: Low (<0.550), Medium [0.550,0.700), High [0.700,0.800), Very High (>=0.800).
        spec = get_preset_spec("hdi")
        band_thresholds = [(b["label"], b["min"]) for b in spec["params"]["bands"]]
        self.assertEqual(
            band_thresholds,
            [("Low", 0.0), ("Medium", 0.550), ("High", 0.700), ("Very High", 0.800)],
        )

    def test_bmi_formula_matches_who(self):
        # WHO: BMI = weight(kg) / height(m)^2. 70 / 1.7^2 ≈ 24.22.
        spec = get_preset_spec("bmi")
        formula = spec["params"]["formula"]
        self.assertAlmostEqual(
            eval_expression(formula, {"weight": 70.0, "height": 1.70}),
            70.0 / (1.70 * 1.70), places=6,
        )

    def test_bmi_bands_match_who(self):
        spec = get_preset_spec("bmi")
        thresholds = [(b["label"], b["min"]) for b in spec["params"]["bands"]]
        self.assertEqual(
            thresholds,
            [("Underweight", 0.0), ("Normal", 18.5), ("Overweight", 25.0), ("Obese", 30.0)],
        )

    def test_linear_plotter_computes_correctly(self):
        spec = get_preset_spec("linear")
        expr = spec["params"]["expression"]
        # y = 2x + 3 at x=4 → 11.
        self.assertEqual(eval_expression(expr, {"m": 2, "c": 3, "x": 4}), 11)

    def test_quadratic_plotter_computes_correctly(self):
        spec = get_preset_spec("quadratic")
        expr = spec["params"]["expression"]
        # y = x^2 - 2x + 1 at x=3 → 4.
        self.assertEqual(
            eval_expression(expr, {"a": 1, "b": -2, "c": 1, "x": 3}),
            4,
        )


# ===========================================================================
# Media catalog integration
# ===========================================================================

class MediaCatalogWidgetTests(TestCase):
    """Widgets must appear in the numbered media catalog alongside images."""

    @classmethod
    def setUpTestData(cls):
        from apps.accounts.models import Institution
        from apps.curriculum.models import Course, Unit, Lesson, LessonStep
        cls.institution = Institution.objects.create(name="Test", slug="t")
        cls.course = Course.objects.create(
            institution=cls.institution, title="Test Course",
            grade_level="S3", is_published=True,
        )
        cls.unit = Unit.objects.create(course=cls.course, title="Test Unit", order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title="Development Indicators",
            objective="Understand HDI.", order_index=0, is_published=True,
        )
        cls.step = LessonStep.objects.create(
            lesson=cls.lesson, order_index=0, phase="explain", step_type="teach",
            teacher_script="Look at the HDI sliders and try different combinations.",
            media={
                "images": [
                    {"url": "/media/x.png", "alt": "HDI world map",
                     "caption": "Fig 1", "type": "map"},
                ],
                "widgets": [get_preset_spec("hdi")],
            },
        )

    def _make_tutor(self):
        # Build a minimal tutor without running __init__'s full DB setup.
        from apps.tutoring.conversational_tutor import ConversationalTutor
        tutor = ConversationalTutor.__new__(ConversationalTutor)
        tutor.lesson = self.lesson
        tutor.steps = [self.step]
        tutor._media_id_map = {}
        tutor._step_media_ids = {}
        return tutor

    def test_widget_in_catalog(self):
        tutor = self._make_tutor()
        catalog = tutor._build_media_catalog()
        self.assertIn("HDI Explorer", catalog)
        self.assertIn("widget: composite_index_explorer", catalog)
        # Image is still present.
        self.assertIn("HDI world map", catalog)

    def test_widget_signal_resolves(self):
        tutor = self._make_tutor()
        tutor._build_media_catalog()  # populates _media_id_map
        # Find widget ID by scanning the built map.
        widget_id = next(
            mid for mid, m in tutor._media_id_map.items()
            if m.get("type") == "widget"
        )
        response = f"Here's the HDI explorer.\n|||MEDIA:{widget_id}|||"
        clean, media_dict, _gen = tutor._parse_media_signal(response)
        self.assertEqual(clean.strip(), "Here's the HDI explorer.")
        self.assertIsNotNone(media_dict)
        self.assertEqual(media_dict["type"], "widget")
        self.assertEqual(media_dict["widget_type"], "composite_index_explorer")
        self.assertEqual(media_dict["title"], "HDI Explorer")
        self.assertIn("life_idx", str(media_dict["params"]))
