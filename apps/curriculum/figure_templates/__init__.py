"""Figure templates — deterministic, hand-tuned SVG renderers.

Every figure used in exit tickets / summatives / lessons goes through one
of these templates. The LLM picks a `kind` and supplies labelled values;
the template guarantees correctness.

Catalog:
  Charts:        bar, line, pie, doughnut, scatter, histogram
  Geometry:      rectangle, square, triangle, circle, regular_polygon,
                 parallelogram, trapezium, cuboid, cylinder, compound_shape
  Angles:        angle, straight_line_angles, point_angles,
                 triangle_angles, parallel_lines, polygon_angles
  Coords/lines:  number_line, fraction_bar, coordinate_grid
  Statistics:    box_plot, stem_leaf, pictogram
  Geography:     earth_layers, volcano_cross, plate_boundary,
                 river_profile, meander_oxbow, coastal_features,
                 weathering, rock_cycle, water_cycle, compass_rose,
                 seychelles_map, lat_long_grid

Public API:
    from apps.curriculum.figure_templates import render_template
    svg = render_template({'kind': 'rectangle', 'width': 8, 'height': 5,
                            'units': 'cm', 'label': 'Garden'})

A template registry maps `kind` → callable(spec: dict) -> str. Unknown
kinds return None; callers fall back to no-figure.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from . import angles, charts, coordinates, geography, geometry, statistics


REGISTRY: Dict[str, Callable[[dict], Optional[str]]] = {
    **charts.RENDERERS,
    **geometry.RENDERERS,
    **angles.RENDERERS,
    **coordinates.RENDERERS,
    **statistics.RENDERERS,
    **geography.RENDERERS,
}


def render_template(spec: dict) -> Optional[str]:
    """Render a figure spec into inline SVG markup.

    Dispatches on `spec['kind']`. Returns the SVG string or None if the
    kind is unknown / spec is invalid.
    """
    if not isinstance(spec, dict):
        return None
    kind = (spec.get('kind') or spec.get('type') or '').strip().lower()
    if not kind:
        return None
    fn = REGISTRY.get(kind)
    if fn is None:
        return None
    try:
        return fn(spec)
    except Exception:
        return None


def list_kinds() -> list:
    """For prompt-construction: the full set of known kinds."""
    return sorted(REGISTRY.keys())
