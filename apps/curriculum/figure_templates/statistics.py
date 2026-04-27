"""Statistics-specific templates: box plot, stem-and-leaf, pictogram."""

from __future__ import annotations

from typing import Dict, List, Optional

from . import _common as c


CANVAS_W = 480
CANVAS_H = 240


def box_plot(spec: dict) -> Optional[str]:
    """Spec: min, q1, median, q3, max, label?, units?"""
    try:
        vmin = float(spec.get('min'))
        q1 = float(spec.get('q1'))
        med = float(spec.get('median'))
        q3 = float(spec.get('q3'))
        vmax = float(spec.get('max'))
    except (TypeError, ValueError):
        return None
    if not (vmin <= q1 <= med <= q3 <= vmax):
        return None
    title = spec.get('title') or 'Box plot'
    units = spec.get('units') or ''
    pad_l, pad_r = 50, 30
    plot_w = CANVAS_W - pad_l - pad_r
    span = vmax - vmin if vmax > vmin else 1

    def x_at(v):
        return pad_l + (v - vmin) / span * plot_w

    cy = 110
    box_h = 50

    parts = [c.svg_open(CANVAS_W, CANVAS_H), c.title(title, x=CANVAS_W // 2)]
    # Whiskers
    parts.append(c.line(x_at(vmin), cy, x_at(q1), cy, stroke=c.STROKE, width=1.5))
    parts.append(c.line(x_at(q3), cy, x_at(vmax), cy, stroke=c.STROKE, width=1.5))
    # Whisker caps
    parts.append(c.line(x_at(vmin), cy - box_h / 3, x_at(vmin), cy + box_h / 3, stroke=c.STROKE, width=1.5))
    parts.append(c.line(x_at(vmax), cy - box_h / 3, x_at(vmax), cy + box_h / 3, stroke=c.STROKE, width=1.5))
    # Box
    parts.append(c.rect(x_at(q1), cy - box_h / 2, x_at(q3) - x_at(q1), box_h,
                        fill='#ede9fe', stroke=c.STROKE))
    # Median
    parts.append(c.line(x_at(med), cy - box_h / 2, x_at(med), cy + box_h / 2,
                        stroke=c.ACCENT, width=2.5))
    # Axis below
    axis_y = cy + box_h / 2 + 24
    parts.append(c.line(pad_l, axis_y, pad_l + plot_w, axis_y, stroke=c.STROKE, width=1))
    # Tick + label every key value
    sfx = (' ' + units).rstrip()
    for v, name in [(vmin, 'min'), (q1, 'Q1'), (med, 'med'), (q3, 'Q3'), (vmax, 'max')]:
        x = x_at(v)
        parts.append(c.line(x, axis_y - 4, x, axis_y + 4, stroke=c.STROKE, width=1))
        parts.append(c.label(f"{c.fmt(v)}{sfx}", x=x, y=axis_y + 18, color=c.MUTED, size=11))
        parts.append(c.label(name, x=x, y=axis_y + 32, color=c.ACCENT, weight=600, size=10))
    parts.append(c.svg_close())
    return ''.join(parts)


def stem_leaf(spec: dict) -> Optional[str]:
    """Spec: rows [{stem: int, leaves: [int,...]}], key?"""
    rows = spec.get('rows') or []
    if not rows:
        return None
    title = spec.get('title') or 'Stem-and-leaf plot'
    key = spec.get('key')

    line_h = 22
    width = 360
    height = 60 + line_h * (len(rows) + 1) + (40 if key else 16)

    parts = [c.svg_open(width, height), c.title(title, x=width // 2)]
    x_stem = 80
    x_leaves = 130

    parts.append(c.line(x_stem + 18, 50, x_stem + 18, 50 + line_h * len(rows), stroke=c.STROKE, width=1.5))
    parts.append(c.label('Stem', x=x_stem - 4, y=42, anchor='end', color=c.MUTED, size=11, weight=600))
    parts.append(c.label('Leaves', x=x_leaves, y=42, anchor='start', color=c.MUTED, size=11, weight=600))

    for i, row in enumerate(rows):
        try:
            stem = int(row.get('stem'))
        except (TypeError, ValueError):
            continue
        leaves = row.get('leaves') or []
        y = 50 + line_h * i + 16
        parts.append(c.label(str(stem), x=x_stem - 4, y=y, anchor='end', size=14, weight=600))
        leaves_str = ' '.join(str(l) for l in leaves)
        parts.append(c.label(leaves_str, x=x_leaves, y=y, anchor='start', size=14))

    if key:
        parts.append(c.label(f"Key: {key}", x=width / 2, y=height - 18, color=c.MUTED, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


def pictogram(spec: dict) -> Optional[str]:
    """Spec: rows [{label, count}], symbol? (single char or emoji),
              key? (string e.g. '☺ = 4 students')"""
    rows = spec.get('rows') or []
    if not rows:
        return None
    title = spec.get('title') or ''
    symbol = spec.get('symbol') or '★'
    key = spec.get('key') or ''

    line_h = 32
    width = 480
    height = 70 + line_h * len(rows) + (30 if key else 10)

    parts = [c.svg_open(width, height), c.title(title, x=width // 2)]
    x_label = 30
    x_symbols = 150

    for i, row in enumerate(rows):
        lbl = row.get('label') or ''
        try:
            cnt = int(row.get('count', 0))
        except (TypeError, ValueError):
            cnt = 0
        y = 60 + line_h * i
        parts.append(c.label(lbl, x=x_label, y=y, anchor='start', size=13, weight=600))
        # Repeat symbol cnt times, spaced.
        s = symbol * cnt
        if cnt > 18:
            s = symbol * 18 + f"  (×{cnt})"
        parts.append(c.label(s, x=x_symbols, y=y, anchor='start', color=c.ACCENT, size=18))
    if key:
        parts.append(c.label(f"Key: {key}", x=width / 2, y=height - 12, color=c.MUTED, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


RENDERERS = {
    'box_plot': box_plot,
    'stem_leaf': stem_leaf,
    'pictogram': pictogram,
}
