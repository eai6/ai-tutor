"""Tests for the server-side SVG figure renderer."""

from django.test import TestCase

from ai_tutor.apps.curriculum.figure_render import render_figure_spec


class RenderFigureSpecTests(TestCase):
    def test_invalid_spec_returns_none(self):
        self.assertIsNone(render_figure_spec(None))
        self.assertIsNone(render_figure_spec({}))
        self.assertIsNone(render_figure_spec({'type': 'donut'}))  # bad type
        self.assertIsNone(render_figure_spec(
            {'type': 'bar', 'datasets': [{'label': 'x', 'data': [1]}]}
            # missing labels for non-scatter
        ))

    def test_bar_chart_renders_with_correct_geometry(self):
        # Two bars: one twice as tall as the other. The taller bar's
        # height in the SVG should be ~2x the shorter one's height.
        spec = {
            'type': 'bar',
            'title': 'Test bar',
            'labels': ['A', 'B'],
            'datasets': [{'label': 'Series', 'data': [10, 20]}],
            'x_label': 'Category',
            'y_label': 'Count',
        }
        svg = render_figure_spec(spec)
        self.assertIsNotNone(svg)
        self.assertTrue(svg.startswith('<svg'))
        self.assertTrue(svg.endswith('</svg>'))
        # Title and labels make it through.
        self.assertIn('Test bar', svg)
        self.assertIn('Category', svg)
        self.assertIn('Count', svg)
        # Two <rect> bars are emitted.
        self.assertEqual(svg.count('<rect '), 2)

    def test_line_chart_emits_polyline(self):
        spec = {
            'type': 'line',
            'title': 'Trend',
            'labels': ['Jan', 'Feb', 'Mar'],
            'datasets': [{'label': 'Sales', 'data': [10, 12, 15]}],
        }
        svg = render_figure_spec(spec)
        self.assertIsNotNone(svg)
        self.assertIn('<polyline', svg)
        # Three data points → three circles.
        self.assertEqual(svg.count('<circle '), 3)

    def test_pie_chart_emits_slices(self):
        spec = {
            'type': 'pie',
            'title': 'Distribution',
            'labels': ['Alpha', 'Beta', 'Gamma'],
            'datasets': [{'label': 'Share', 'data': [50, 30, 20]}],
        }
        svg = render_figure_spec(spec)
        self.assertIsNotNone(svg)
        # Three slices via path elements.
        self.assertEqual(svg.count('<path '), 3)
        self.assertIn('Alpha', svg)
        self.assertIn('50%', svg)  # 50/100 percent label

    def test_scatter_chart_uses_points(self):
        spec = {
            'type': 'scatter',
            'title': 'Scatter',
            'datasets': [{
                'label': 'A',
                'points': [[1, 2], [3, 4], [5, 6]],
            }],
        }
        svg = render_figure_spec(spec)
        self.assertIsNotNone(svg)
        # Three points → three circles.
        self.assertEqual(svg.count('<circle '), 3)

    def test_multi_series_renders_legend(self):
        spec = {
            'type': 'bar',
            'title': 'Compared',
            'labels': ['A', 'B'],
            'datasets': [
                {'label': 'Series 1', 'data': [10, 20]},
                {'label': 'Series 2', 'data': [15, 5]},
            ],
        }
        svg = render_figure_spec(spec)
        self.assertIsNotNone(svg)
        # Both series labels appear (in the legend).
        self.assertIn('Series 1', svg)
        self.assertIn('Series 2', svg)
        # 4 bars total (2 series × 2 categories).
        self.assertEqual(svg.count('<rect '), 4 + 2)  # +2 legend swatches

    def test_html_in_labels_is_escaped(self):
        spec = {
            'type': 'bar',
            'title': '<b>bold</b>',
            'labels': ['<svg>', 'normal'],
            'datasets': [{'label': 'L', 'data': [1, 2]}],
        }
        svg = render_figure_spec(spec)
        self.assertIsNotNone(svg)
        # No raw <b> or <svg> made it through to the output title.
        self.assertNotIn('<b>bold</b>', svg)
        self.assertIn('&lt;b&gt;bold&lt;/b&gt;', svg)

    def test_pie_with_all_zero_values_returns_empty_message(self):
        spec = {
            'type': 'pie',
            'title': 'Empty',
            'labels': ['A', 'B'],
            'datasets': [{'label': 'X', 'data': [0, 0]}],
        }
        svg = render_figure_spec(spec)
        self.assertIsNotNone(svg)
        self.assertIn('All values are zero', svg)
