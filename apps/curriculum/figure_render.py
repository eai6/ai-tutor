"""Server-side SVG renderer for figure specs.

Replaces two failed approaches:
  - LLM-emitted SVG: positions are imagined, not calculated, so a bar
    with value 45 doesn't have a height proportional to 45.
  - Chart.js plot_spec: 70 KB JS dependency + interactive flakiness on
    mobile + no offline support.

The LLM emits a structured `figure_spec` (same shape as the old
`plot_spec`); this module converts it to inline SVG with positions
calculated from the actual data values. Output is hand-tuned: clean,
small (no embedded CSS, no <defs>), works in any browser, prints
sanely, and renders fully offline.

Supported types: bar, line, pie, scatter. (doughnut maps to pie.)
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from apps.tutoring.plot_spec import coerce_plot_spec


# ─── Style constants ──────────────────────────────────────────────────

WIDTH = 480
HEIGHT = 320
PAD_LEFT = 56
PAD_RIGHT = 16
PAD_TOP = 40
PAD_BOTTOM = 56  # leaves room for x-axis labels + caption
LEGEND_HEIGHT = 18

PALETTE = [
    '#7c3aed',  # primary purple
    '#10b981',  # green
    '#f59e0b',  # amber
    '#3b82f6',  # blue
    '#ec4899',  # pink
    '#14b8a6',  # teal
]
AXIS_COLOR = '#52525b'
GRID_COLOR = '#e4e4e7'
TEXT_COLOR = '#18181b'
MUTED_COLOR = '#71717a'
FONT_STACK = "Nunito, system-ui, -apple-system, sans-serif"


# ─── Public API ───────────────────────────────────────────────────────

def render_figure_spec(spec: dict) -> Optional[str]:
    """Render a figure_spec dict into inline SVG markup.

    Returns the SVG string on success or None when the spec is
    invalid / unrenderable. Callers should fall back to whatever
    they were going to do without a figure.
    """
    cleaned, err = coerce_plot_spec(spec)
    if err or not cleaned:
        return None
    chart_type = cleaned['type']
    if chart_type == 'bar':
        return _render_bar(cleaned)
    if chart_type == 'line':
        return _render_line(cleaned)
    if chart_type in ('pie', 'doughnut'):
        return _render_pie(cleaned)
    if chart_type == 'scatter':
        return _render_scatter(cleaned)
    return None


# ─── Renderers ────────────────────────────────────────────────────────

def _render_bar(spec: dict) -> str:
    title = spec.get('title') or ''
    x_label = spec.get('x_label') or ''
    y_label = spec.get('y_label') or ''
    labels = spec.get('labels') or []
    datasets = spec.get('datasets') or []
    source = spec.get('source') or ''

    values_per_series: List[List[float]] = [
        [_as_float(v) for v in (ds.get('data') or [])]
        for ds in datasets
    ]
    if not labels or not values_per_series or not values_per_series[0]:
        return _render_empty(title, 'No data to chart')

    plot = _plot_box(reserve_legend=len(datasets) > 1)
    y_min, y_max = _axis_bounds(values_per_series, include_zero=True)
    y_ticks = _nice_ticks(y_min, y_max, 5)

    parts: List[str] = []
    parts.append(_svg_open())
    parts.append(_title_block(title))
    parts.append(_axis_y(plot, y_ticks, y_min, y_max, y_label))
    parts.append(_axis_x_categorical(plot, labels))

    n_groups = len(labels)
    n_series = len(datasets)
    group_width = plot['w'] / n_groups
    bar_width = (group_width * 0.7) / max(n_series, 1)
    inner_pad = (group_width - bar_width * n_series) / 2

    for s_idx, ds in enumerate(datasets):
        color = (ds.get('color') or PALETTE[s_idx % len(PALETTE)])
        for i, raw in enumerate(values_per_series[s_idx]):
            x = plot['x'] + i * group_width + inner_pad + s_idx * bar_width
            y_top = _val_to_y(raw, y_min, y_max, plot)
            y_zero = _val_to_y(max(y_min, 0), y_min, y_max, plot)
            top = min(y_top, y_zero)
            h = abs(y_top - y_zero)
            parts.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_width:.1f}" '
                f'height="{h:.1f}" fill="{color}" rx="2"/>'
            )

    if x_label:
        parts.append(_x_axis_label(x_label, plot))
    if n_series > 1:
        parts.append(_legend([ds.get('label', f'Series {i+1}') for i, ds in enumerate(datasets)]))
    if source:
        parts.append(_source_caption(source))
    parts.append('</svg>')
    return ''.join(parts)


def _render_line(spec: dict) -> str:
    title = spec.get('title') or ''
    x_label = spec.get('x_label') or ''
    y_label = spec.get('y_label') or ''
    labels = spec.get('labels') or []
    datasets = spec.get('datasets') or []
    source = spec.get('source') or ''

    values_per_series: List[List[float]] = [
        [_as_float(v) for v in (ds.get('data') or [])]
        for ds in datasets
    ]
    if not labels or not values_per_series or not values_per_series[0]:
        return _render_empty(title, 'No data to chart')

    plot = _plot_box(reserve_legend=len(datasets) > 1)
    y_min, y_max = _axis_bounds(values_per_series, include_zero=False)
    y_ticks = _nice_ticks(y_min, y_max, 5)

    parts: List[str] = []
    parts.append(_svg_open())
    parts.append(_title_block(title))
    parts.append(_axis_y(plot, y_ticks, y_min, y_max, y_label))
    parts.append(_axis_x_categorical(plot, labels))

    n_points = len(labels)
    step = plot['w'] / max(n_points - 1, 1)

    for s_idx, ds in enumerate(datasets):
        color = (ds.get('color') or PALETTE[s_idx % len(PALETTE)])
        pts: List[Tuple[float, float]] = []
        for i, raw in enumerate(values_per_series[s_idx]):
            x = plot['x'] + i * step
            y = _val_to_y(raw, y_min, y_max, plot)
            pts.append((x, y))
        if len(pts) >= 2:
            d = ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)
            parts.append(
                f'<polyline points="{d}" fill="none" stroke="{color}" '
                f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            )
        for x, y in pts:
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>'
            )

    if x_label:
        parts.append(_x_axis_label(x_label, plot))
    if len(datasets) > 1:
        parts.append(_legend([ds.get('label', f'Series {i+1}') for i, ds in enumerate(datasets)]))
    if source:
        parts.append(_source_caption(source))
    parts.append('</svg>')
    return ''.join(parts)


def _render_pie(spec: dict) -> str:
    title = spec.get('title') or ''
    labels = spec.get('labels') or []
    datasets = spec.get('datasets') or []
    source = spec.get('source') or ''

    if not datasets or not labels:
        return _render_empty(title, 'No data to chart')

    values = [max(_as_float(v), 0) for v in (datasets[0].get('data') or [])]
    total = sum(values)
    if total <= 0:
        return _render_empty(title, 'All values are zero')

    parts: List[str] = []
    parts.append(_svg_open())
    parts.append(_title_block(title))

    cx, cy, r = WIDTH * 0.30, HEIGHT * 0.55, 92
    angle_start = -math.pi / 2  # start at 12 o'clock
    for i, value in enumerate(values):
        if value <= 0:
            continue
        slice_angle = (value / total) * 2 * math.pi
        angle_end = angle_start + slice_angle
        x1 = cx + r * math.cos(angle_start)
        y1 = cy + r * math.sin(angle_start)
        x2 = cx + r * math.cos(angle_end)
        y2 = cy + r * math.sin(angle_end)
        large_arc = 1 if slice_angle > math.pi else 0
        color = PALETTE[i % len(PALETTE)]
        # Each slice as a path: M center → L start → A radius → Z
        parts.append(
            f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
            f'A{r},{r} 0 {large_arc} 1 {x2:.1f},{y2:.1f} Z" '
            f'fill="{color}" stroke="#fff" stroke-width="1.5"/>'
        )
        # Percent label inside the slice if it's >= 5%
        pct = (value / total) * 100
        if pct >= 5:
            mid = angle_start + slice_angle / 2
            tx = cx + (r * 0.6) * math.cos(mid)
            ty = cy + (r * 0.6) * math.sin(mid)
            parts.append(
                f'<text x="{tx:.1f}" y="{ty:.1f}" font-family="{FONT_STACK}" '
                f'font-size="11" font-weight="600" fill="#fff" '
                f'text-anchor="middle" dominant-baseline="middle">{pct:.0f}%</text>'
            )
        angle_start = angle_end

    # Legend on the right
    legend_x = WIDTH * 0.55
    legend_y = HEIGHT * 0.35
    for i, label in enumerate(labels[:len(values)]):
        color = PALETTE[i % len(PALETTE)]
        parts.append(
            f'<rect x="{legend_x:.1f}" y="{legend_y + i*22:.1f}" '
            f'width="12" height="12" fill="{color}" rx="2"/>'
        )
        parts.append(
            f'<text x="{legend_x + 18:.1f}" y="{legend_y + i*22 + 10:.1f}" '
            f'font-family="{FONT_STACK}" font-size="11" fill="{TEXT_COLOR}">'
            f'{_escape(label)} ({values[i]:g})</text>'
        )

    if source:
        parts.append(_source_caption(source))
    parts.append('</svg>')
    return ''.join(parts)


def _render_scatter(spec: dict) -> str:
    title = spec.get('title') or ''
    x_label = spec.get('x_label') or ''
    y_label = spec.get('y_label') or ''
    datasets = spec.get('datasets') or []
    source = spec.get('source') or ''

    points_per_series: List[List[Tuple[float, float]]] = []
    for ds in datasets:
        pts = []
        for p in (ds.get('points') or []):
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                pts.append((_as_float(p[0]), _as_float(p[1])))
        points_per_series.append(pts)

    flat = [pt for series in points_per_series for pt in series]
    if not flat:
        return _render_empty(title, 'No data to chart')

    plot = _plot_box(reserve_legend=len(datasets) > 1)

    xs = [pt[0] for pt in flat]
    ys = [pt[1] for pt in flat]
    x_min, x_max = _padded_range(min(xs), max(xs))
    y_min, y_max = _padded_range(min(ys), max(ys))
    x_ticks = _nice_ticks(x_min, x_max, 5)
    y_ticks = _nice_ticks(y_min, y_max, 5)

    parts: List[str] = []
    parts.append(_svg_open())
    parts.append(_title_block(title))
    parts.append(_axis_y(plot, y_ticks, y_min, y_max, y_label))
    parts.append(_axis_x_numeric(plot, x_ticks, x_min, x_max))

    for s_idx, pts in enumerate(points_per_series):
        color = (datasets[s_idx].get('color') if s_idx < len(datasets) else None) \
            or PALETTE[s_idx % len(PALETTE)]
        for x_val, y_val in pts:
            x = plot['x'] + (x_val - x_min) / (x_max - x_min) * plot['w']
            y = _val_to_y(y_val, y_min, y_max, plot)
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" '
                f'fill-opacity="0.85" stroke="#fff" stroke-width="1"/>'
            )

    if x_label:
        parts.append(_x_axis_label(x_label, plot))
    if len(datasets) > 1:
        parts.append(_legend([ds.get('label', f'Series {i+1}') for i, ds in enumerate(datasets)]))
    if source:
        parts.append(_source_caption(source))
    parts.append('</svg>')
    return ''.join(parts)


# ─── Helpers ──────────────────────────────────────────────────────────

def _svg_open() -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="100%" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="Chart">'
    )


def _plot_box(reserve_legend: bool) -> Dict[str, float]:
    extra_top = LEGEND_HEIGHT if reserve_legend else 0
    return {
        'x': PAD_LEFT,
        'y': PAD_TOP + extra_top,
        'w': WIDTH - PAD_LEFT - PAD_RIGHT,
        'h': HEIGHT - PAD_TOP - extra_top - PAD_BOTTOM,
    }


def _title_block(title: str) -> str:
    if not title:
        return ''
    return (
        f'<text x="{WIDTH/2}" y="22" font-family="{FONT_STACK}" '
        f'font-size="14" font-weight="700" fill="{TEXT_COLOR}" '
        f'text-anchor="middle">{_escape(title)}</text>'
    )


def _axis_y(plot: Dict[str, float], ticks: Sequence[float], y_min: float,
            y_max: float, y_label: str) -> str:
    parts: List[str] = []
    # Plot frame baseline
    parts.append(
        f'<line x1="{plot["x"]}" y1="{plot["y"]}" '
        f'x2="{plot["x"]}" y2="{plot["y"]+plot["h"]}" '
        f'stroke="{AXIS_COLOR}" stroke-width="1"/>'
    )
    for tick in ticks:
        y = _val_to_y(tick, y_min, y_max, plot)
        parts.append(
            f'<line x1="{plot["x"]}" y1="{y:.1f}" '
            f'x2="{plot["x"]+plot["w"]}" y2="{y:.1f}" '
            f'stroke="{GRID_COLOR}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{plot["x"]-6}" y="{y+3:.1f}" '
            f'font-family="{FONT_STACK}" font-size="10" fill="{MUTED_COLOR}" '
            f'text-anchor="end">{_format_tick(tick)}</text>'
        )
    if y_label:
        # Vertical y-axis label, rotated.
        cx = 14
        cy = plot['y'] + plot['h'] / 2
        parts.append(
            f'<text x="{cx}" y="{cy}" font-family="{FONT_STACK}" font-size="11" '
            f'fill="{MUTED_COLOR}" text-anchor="middle" '
            f'transform="rotate(-90,{cx},{cy})">{_escape(y_label)}</text>'
        )
    return ''.join(parts)


def _axis_x_categorical(plot: Dict[str, float], labels: Sequence[str]) -> str:
    parts: List[str] = []
    baseline_y = plot['y'] + plot['h']
    parts.append(
        f'<line x1="{plot["x"]}" y1="{baseline_y}" '
        f'x2="{plot["x"]+plot["w"]}" y2="{baseline_y}" '
        f'stroke="{AXIS_COLOR}" stroke-width="1"/>'
    )
    n = len(labels)
    if n == 0:
        return ''.join(parts)
    step = plot['w'] / n
    for i, label in enumerate(labels):
        cx = plot['x'] + (i + 0.5) * step
        parts.append(
            f'<text x="{cx:.1f}" y="{baseline_y+14}" '
            f'font-family="{FONT_STACK}" font-size="10" fill="{MUTED_COLOR}" '
            f'text-anchor="middle">{_escape(_truncate(label, 16))}</text>'
        )
    return ''.join(parts)


def _axis_x_numeric(plot: Dict[str, float], ticks: Sequence[float],
                    x_min: float, x_max: float) -> str:
    parts: List[str] = []
    baseline_y = plot['y'] + plot['h']
    parts.append(
        f'<line x1="{plot["x"]}" y1="{baseline_y}" '
        f'x2="{plot["x"]+plot["w"]}" y2="{baseline_y}" '
        f'stroke="{AXIS_COLOR}" stroke-width="1"/>'
    )
    for tick in ticks:
        if x_max == x_min:
            x = plot['x'] + plot['w'] / 2
        else:
            x = plot['x'] + (tick - x_min) / (x_max - x_min) * plot['w']
        parts.append(
            f'<line x1="{x:.1f}" y1="{plot["y"]}" x2="{x:.1f}" y2="{baseline_y}" '
            f'stroke="{GRID_COLOR}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{baseline_y+14}" '
            f'font-family="{FONT_STACK}" font-size="10" fill="{MUTED_COLOR}" '
            f'text-anchor="middle">{_format_tick(tick)}</text>'
        )
    return ''.join(parts)


def _x_axis_label(label: str, plot: Dict[str, float]) -> str:
    return (
        f'<text x="{plot["x"]+plot["w"]/2}" y="{HEIGHT-22}" '
        f'font-family="{FONT_STACK}" font-size="11" fill="{MUTED_COLOR}" '
        f'text-anchor="middle">{_escape(label)}</text>'
    )


def _legend(labels: Sequence[str]) -> str:
    parts: List[str] = []
    # Place legend in the slot above the plot area (reserved by _plot_box).
    y = PAD_TOP + 4
    spacing = 90
    total_width = spacing * len(labels)
    start_x = max(PAD_LEFT, (WIDTH - total_width) / 2)
    for i, label in enumerate(labels):
        color = PALETTE[i % len(PALETTE)]
        x = start_x + i * spacing
        parts.append(
            f'<rect x="{x:.1f}" y="{y}" width="10" height="10" fill="{color}" rx="2"/>'
        )
        parts.append(
            f'<text x="{x+14:.1f}" y="{y+9}" font-family="{FONT_STACK}" '
            f'font-size="10" fill="{TEXT_COLOR}">{_escape(_truncate(label, 14))}</text>'
        )
    return ''.join(parts)


def _source_caption(source: str) -> str:
    return (
        f'<text x="{WIDTH-PAD_RIGHT}" y="{HEIGHT-4}" font-family="{FONT_STACK}" '
        f'font-size="9" fill="{MUTED_COLOR}" text-anchor="end" font-style="italic">'
        f'Source: {_escape(_truncate(source, 50))}</text>'
    )


def _render_empty(title: str, message: str) -> str:
    return (
        f'{_svg_open()}'
        f'{_title_block(title)}'
        f'<text x="{WIDTH/2}" y="{HEIGHT/2}" font-family="{FONT_STACK}" '
        f'font-size="12" fill="{MUTED_COLOR}" text-anchor="middle">'
        f'{_escape(message)}</text></svg>'
    )


# ─── Math helpers ─────────────────────────────────────────────────────

def _val_to_y(val: float, y_min: float, y_max: float, plot: Dict[str, float]) -> float:
    if y_max == y_min:
        return plot['y'] + plot['h'] / 2
    norm = (val - y_min) / (y_max - y_min)
    return plot['y'] + plot['h'] * (1 - norm)


def _axis_bounds(values_per_series: List[List[float]], include_zero: bool) -> Tuple[float, float]:
    flat = [v for series in values_per_series for v in series]
    if not flat:
        return (0, 1)
    lo, hi = min(flat), max(flat)
    if include_zero:
        lo = min(lo, 0)
        hi = max(hi, 0)
    if lo == hi:
        # Single-value series — give it some breathing room
        return (lo - 1, hi + 1)
    pad = (hi - lo) * 0.05
    return (lo - pad if lo > 0 or not include_zero else lo, hi + pad)


def _padded_range(lo: float, hi: float) -> Tuple[float, float]:
    if lo == hi:
        return (lo - 1, hi + 1)
    pad = (hi - lo) * 0.08
    return (lo - pad, hi + pad)


def _nice_ticks(lo: float, hi: float, target_count: int) -> List[float]:
    """Choose 'nice' tick values using a 1-2-5 progression."""
    if hi <= lo:
        return [lo]
    raw_step = (hi - lo) / max(target_count, 1)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    residual = raw_step / magnitude
    if residual < 1.5:
        nice_step = 1
    elif residual < 3:
        nice_step = 2
    elif residual < 7:
        nice_step = 5
    else:
        nice_step = 10
    step = nice_step * magnitude
    start = math.floor(lo / step) * step
    ticks: List[float] = []
    v = start
    while v <= hi + step * 0.5:
        ticks.append(round(v, 6))
        v += step
        if len(ticks) > 12:
            break
    return ticks


def _format_tick(v: float) -> str:
    if v == int(v):
        return f'{int(v)}'
    if abs(v) >= 1000:
        return f'{v:,.0f}'
    if abs(v) < 0.01:
        return f'{v:.3g}'
    return f'{v:.2g}'


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _truncate(s: str, n: int) -> str:
    s = s or ''
    if len(s) <= n:
        return s
    return s[: n - 1] + '…'


_ESC = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
}


def _escape(s: str) -> str:
    s = '' if s is None else str(s)
    return ''.join(_ESC.get(c, c) for c in s)
