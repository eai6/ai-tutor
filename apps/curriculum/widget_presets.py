"""
Curated library of pre-filled widget specs.

Teachers pick a preset from the "+ Add Widget" dropdown in step_edit and the
spec lands pre-populated. This is the widget equivalent of a stock-image
library or DALL-E — a vetted source of authoritative specs the teacher can
drop into a lesson and tweak. Formulas here must match the relevant
authoritative source (UN, WHO, textbook) and are covered by unit tests.

Add new entries by extending PRESETS below. Each entry's spec is validated by
``apps.curriculum.widgets.MediaWidget`` at registration time (see
``get_preset_spec``), so a malformed preset fails loudly on startup/test rather
than silently on click.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Dict, List


# -----------------------------------------------------------------------------
# composite_index_explorer presets
# -----------------------------------------------------------------------------

_HDI = {
    "widget_type": "composite_index_explorer",
    "title": "HDI Explorer",
    "caption": (
        "Move each slider to change a country's life, education, and income "
        "indices. The Human Development Index is the geometric mean of the "
        "three — see how a weakness in one dimension drags the whole score down."
    ),
    "alt_text": (
        "Three sliders — life expectancy index, education index, and income "
        "index — feeding a bar that shows the composite HDI with bands for "
        "Low, Medium, High, and Very High development. Reference markers "
        "show Norway, Seychelles, and Niger."
    ),
    "params": {
        "inputs": [
            {"key": "life_idx", "label": "Life expectancy index",
             "min": 0.0, "max": 1.0, "default": 0.7, "step": 0.01},
            {"key": "edu_idx", "label": "Education index",
             "min": 0.0, "max": 1.0, "default": 0.6, "step": 0.01},
            {"key": "income_idx", "label": "Income index",
             "min": 0.0, "max": 1.0, "default": 0.6, "step": 0.01},
        ],
        # UNDP post-2010 methodology: geometric mean of the three dimension indices.
        "formula": "(life_idx * edu_idx * income_idx) ** (1/3)",
        "output_label": "HDI", "output_min": 0.0, "output_max": 1.0, "precision": 3,
        # UNDP Human Development Report bands (Low / Medium / High / Very High).
        "bands": [
            {"label": "Low", "min": 0.0, "color": "#ef4444"},
            {"label": "Medium", "min": 0.550, "color": "#f59e0b"},
            {"label": "High", "min": 0.700, "color": "#10b981"},
            {"label": "Very High", "min": 0.800, "color": "#3b82f6"},
        ],
        # Values from UN Human Development Report 2021/22.
        "references": [
            {"label": "Norway", "value": 0.961},
            {"label": "Seychelles", "value": 0.785},
            {"label": "Niger", "value": 0.400},
        ],
    },
}


_BMI = {
    "widget_type": "composite_index_explorer",
    "title": "BMI Explorer",
    "caption": (
        "Body Mass Index = weight (kg) ÷ height² (m²). Move the sliders to "
        "see which WHO category a body of given height and weight lands in."
    ),
    "alt_text": (
        "Two sliders — weight in kilograms and height in metres — feeding a "
        "bar that shows the BMI with WHO bands Underweight, Normal, "
        "Overweight, and Obese."
    ),
    "params": {
        "inputs": [
            {"key": "weight", "label": "Weight",
             "min": 30.0, "max": 150.0, "default": 70.0, "step": 0.5, "unit": "kg"},
            {"key": "height", "label": "Height",
             "min": 1.20, "max": 2.10, "default": 1.70, "step": 0.01, "unit": "m"},
        ],
        "formula": "weight / (height * height)",
        "output_label": "BMI", "output_min": 10.0, "output_max": 45.0, "precision": 1,
        # WHO adult BMI classifications.
        "bands": [
            {"label": "Underweight", "min": 0.0, "color": "#3b82f6"},
            {"label": "Normal", "min": 18.5, "color": "#10b981"},
            {"label": "Overweight", "min": 25.0, "color": "#f59e0b"},
            {"label": "Obese", "min": 30.0, "color": "#ef4444"},
        ],
        "references": [],
    },
}


_COMPOSITE_BLANK = {
    "widget_type": "composite_index_explorer",
    "title": "New Composite Index",
    "caption": "Move the sliders to see how inputs combine into a score.",
    "alt_text": "Sliders feeding a weighted composite index.",
    "params": {
        "inputs": [
            {"key": "a", "label": "Input A", "min": 0, "max": 1, "default": 0.5, "step": 0.01},
            {"key": "b", "label": "Input B", "min": 0, "max": 1, "default": 0.5, "step": 0.01},
        ],
        "formula": "(a + b) / 2",
        "output_label": "Score", "output_min": 0, "output_max": 1, "precision": 2,
        "bands": [
            {"label": "Low", "min": 0.0, "color": "#ef4444"},
            {"label": "High", "min": 0.5, "color": "#10b981"},
        ],
        "references": [],
    },
}


# -----------------------------------------------------------------------------
# function_plotter presets
# -----------------------------------------------------------------------------

_LINEAR = {
    "widget_type": "function_plotter",
    "title": "Linear function y = mx + c",
    "caption": "Slide m (slope) and c (intercept) and watch the line tilt and shift.",
    "alt_text": "Graph of a straight line with adjustable slope and y-intercept.",
    "params": {
        "expression": "m*x + c",
        "x_min": -10, "x_max": 10,
        "x_label": "x", "y_label": "y",
        "parameters": [
            {"key": "m", "label": "slope m",
             "min": -5, "max": 5, "default": 1, "step": 0.1},
            {"key": "c", "label": "intercept c",
             "min": -10, "max": 10, "default": 0, "step": 0.5},
        ],
    },
}


_QUADRATIC = {
    "widget_type": "function_plotter",
    "title": "Quadratic function y = ax² + bx + c",
    "caption": "Adjust a, b, c and watch the parabola change shape and position.",
    "alt_text": "Parabola with adjustable a, b, and c coefficients.",
    "params": {
        "expression": "a*x**2 + b*x + c",
        "x_min": -8, "x_max": 8,
        "x_label": "x", "y_label": "y",
        "parameters": [
            {"key": "a", "label": "a",
             "min": -3, "max": 3, "default": 1, "step": 0.1},
            {"key": "b", "label": "b",
             "min": -5, "max": 5, "default": 0, "step": 0.5},
            {"key": "c", "label": "c",
             "min": -10, "max": 10, "default": 0, "step": 0.5},
        ],
    },
}


_PLOTTER_BLANK = {
    "widget_type": "function_plotter",
    "title": "New Function Plot",
    "caption": "Move the slider to change the slope.",
    "alt_text": "Line plot of y = m*x with an adjustable slope.",
    "params": {
        "expression": "m*x",
        "x_min": -10, "x_max": 10,
        "x_label": "x", "y_label": "y",
        "parameters": [
            {"key": "m", "label": "slope m",
             "min": -5, "max": 5, "default": 1, "step": 0.1},
        ],
    },
}


# -----------------------------------------------------------------------------
# fraction_decimal_percent presets
# -----------------------------------------------------------------------------

_TENTHS = {
    "widget_type": "fraction_decimal_percent",
    "title": "Tenths — Fraction · Decimal · Percent",
    "caption": "Move the slider to see how a tenth is written as a fraction, decimal, and percent.",
    "alt_text": "Synchronized bar, pie, and number-line showing fractions of ten with decimal and percent equivalents.",
    "params": {
        "denominator": 10,
        "default_numerator": 3,
        "show_bar": True, "show_pie": True, "show_number_line": True,
    },
}


_HUNDREDTHS = {
    "widget_type": "fraction_decimal_percent",
    "title": "Hundredths — Fraction · Decimal · Percent",
    "caption": "Move the slider to see the percentage as a fraction of 100 and as a decimal.",
    "alt_text": "Synchronized bar, pie, and number-line showing fractions of a hundred with decimal and percent equivalents.",
    "params": {
        "denominator": 100,
        "default_numerator": 35,
        "show_bar": True, "show_pie": True, "show_number_line": False,
    },
}


_FDP_BLANK = {
    "widget_type": "fraction_decimal_percent",
    "title": "Fraction · Decimal · Percent",
    "caption": "Move the slider to see the three equivalent forms.",
    "alt_text": "Synchronized fraction, decimal, and percent view of the same value.",
    "params": {
        "denominator": 10, "default_numerator": 3,
        "show_bar": True, "show_pie": True, "show_number_line": True,
    },
}


# -----------------------------------------------------------------------------
# Preset registry
# -----------------------------------------------------------------------------

# Ordered so the dropdown lists meaningful presets before the blanks.
PRESETS: List[Dict] = [
    {"key": "hdi", "label": "HDI (Human Development Index)", "spec": _HDI},
    {"key": "bmi", "label": "BMI (Body Mass Index)", "spec": _BMI},
    {"key": "composite_blank", "label": "Blank composite index", "spec": _COMPOSITE_BLANK},
    {"key": "linear", "label": "Linear  y = mx + c", "spec": _LINEAR},
    {"key": "quadratic", "label": "Quadratic  y = ax² + bx + c", "spec": _QUADRATIC},
    {"key": "plotter_blank", "label": "Blank function plot", "spec": _PLOTTER_BLANK},
    {"key": "fdp_tenths", "label": "Tenths (0–10)", "spec": _TENTHS},
    {"key": "fdp_hundredths", "label": "Hundredths (0–100)", "spec": _HUNDREDTHS},
    {"key": "fdp_blank", "label": "Blank fraction/decimal/percent", "spec": _FDP_BLANK},
]

# Human-friendly section headings for the dropdown optgroups.
TYPE_LABELS: Dict[str, str] = {
    "composite_index_explorer": "Composite Index Explorer",
    "function_plotter": "Function Plotter",
    "fraction_decimal_percent": "Fraction · Decimal · Percent",
}


def get_preset_spec(key: str) -> Dict:
    """Return a deep copy of the preset spec for ``key``, or KeyError.

    Deep-copied so callers can mutate it (e.g. teacher edits) without poisoning
    the module-level constant.
    """
    for preset in PRESETS:
        if preset["key"] == key:
            return deepcopy(preset["spec"])
    raise KeyError(key)


def preset_groups() -> List[Dict]:
    """Return presets grouped by widget_type for rendering as <optgroup>s.

    Output shape:
        [
            {"type": "composite_index_explorer", "label": "...",
             "presets": [{"key": "hdi", "label": "HDI..."}, ...]},
            ...
        ]
    """
    groups: Dict[str, Dict] = {}
    for preset in PRESETS:
        wtype = preset["spec"]["widget_type"]
        groups.setdefault(wtype, {
            "type": wtype,
            "label": TYPE_LABELS.get(wtype, wtype),
            "presets": [],
        })
        groups[wtype]["presets"].append({"key": preset["key"], "label": preset["label"]})
    return list(groups.values())
