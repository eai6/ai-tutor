"""Tests for the plot_spec validator + coercion (Python side)."""

import unittest

from apps.tutoring.plot_spec import (
    is_valid_plot_spec,
    validate_plot_spec,
    coerce_plot_spec,
)


class ValidationTest(unittest.TestCase):
    def test_minimal_bar_chart_valid(self):
        spec = {
            "type": "bar",
            "title": "Tourism arrivals",
            "labels": ["2018", "2019", "2020"],
            "datasets": [{"label": "Visitors", "data": [362, 384, 115]}],
        }
        self.assertIsNone(validate_plot_spec(spec))
        self.assertTrue(is_valid_plot_spec(spec))

    def test_pie_no_axis_labels_required(self):
        spec = {
            "type": "pie",
            "title": "Sector share",
            "labels": ["Tourism", "Fishing", "Other"],
            "datasets": [{"label": "Share", "data": [60, 25, 15]}],
        }
        self.assertIsNone(validate_plot_spec(spec))

    def test_scatter_uses_points(self):
        spec = {
            "type": "scatter",
            "title": "Temperature vs catch",
            "x_label": "°C",
            "y_label": "kg",
            "datasets": [{
                "label": "Catches",
                "points": [[20, 40], [25, 60], [30, 80]],
            }],
        }
        self.assertIsNone(validate_plot_spec(spec))

    def test_unknown_type_rejected(self):
        spec = {
            "type": "treemap",
            "title": "x",
            "labels": ["a"],
            "datasets": [{"label": "y", "data": [1]}],
        }
        self.assertIn("type", validate_plot_spec(spec))

    def test_label_data_length_mismatch_rejected(self):
        spec = {
            "type": "bar",
            "title": "x",
            "labels": ["a", "b", "c"],
            "datasets": [{"label": "y", "data": [1, 2]}],
        }
        self.assertIn("must match", validate_plot_spec(spec))

    def test_empty_datasets_rejected(self):
        spec = {"type": "bar", "title": "x", "labels": ["a"], "datasets": []}
        self.assertIn("datasets", validate_plot_spec(spec))


class CoercionTest(unittest.TestCase):
    def test_strips_commas_from_numeric_strings(self):
        spec = {
            "type": "bar",
            "title": "Population",
            "labels": ["A", "B"],
            "datasets": [{"label": "Pop", "data": ["200,000", "1,500,000"]}],
        }
        cleaned, err = coerce_plot_spec(spec)
        self.assertIsNone(err)
        self.assertEqual(cleaned["datasets"][0]["data"], [200000.0, 1500000.0])

    def test_strips_currency_and_percent_signs(self):
        spec = {
            "type": "bar",
            "title": "x",
            "labels": ["a"],
            "datasets": [{"label": "y", "data": ["75%"]}],
        }
        cleaned, err = coerce_plot_spec(spec)
        self.assertIsNone(err)
        self.assertEqual(cleaned["datasets"][0]["data"], [75.0])

    def test_scatter_dict_points_normalized(self):
        spec = {
            "type": "scatter",
            "title": "x",
            "datasets": [{
                "label": "Pts",
                "points": [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
            }],
        }
        cleaned, err = coerce_plot_spec(spec)
        self.assertIsNone(err)
        self.assertEqual(cleaned["datasets"][0]["points"], [[1.0, 2.0], [3.0, 4.0]])

    def test_drops_unparseable_data(self):
        # A non-numeric data point gets silently dropped, but if all
        # values fail the dataset becomes empty and validation fails.
        spec = {
            "type": "bar",
            "title": "x",
            "labels": ["a", "b"],
            "datasets": [{"label": "y", "data": ["nope", "also bad"]}],
        }
        cleaned, err = coerce_plot_spec(spec)
        self.assertIsNotNone(err)

    def test_default_type_when_missing(self):
        spec = {
            "title": "x",
            "labels": ["a"],
            "datasets": [{"label": "y", "data": [1]}],
        }
        cleaned, err = coerce_plot_spec(spec)
        self.assertIsNone(err)
        self.assertEqual(cleaned["type"], "bar")

    def test_real_world_example_seychelles_tourism(self):
        spec = {
            "type": "bar",
            "title": "Seychelles tourism arrivals",
            "x_label": "Year",
            "y_label": "Visitors (thousands)",
            "labels": ["2018", "2019", "2020", "2021", "2022"],
            "datasets": [{"label": "Tourism arrivals", "data": [362, 384, 115, 188, 333]}],
            "source": "Seychelles Tourism Bureau",
        }
        cleaned, err = coerce_plot_spec(spec)
        self.assertIsNone(err)
        self.assertEqual(cleaned["source"], "Seychelles Tourism Bureau")
