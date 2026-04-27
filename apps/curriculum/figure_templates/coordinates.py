"""Number line, fraction bar, coordinate grid templates."""

from __future__ import annotations

import math
from typing import List, Optional

from . import _common as c


CANVAS_W = 480
CANVAS_H = 240


def number_line(spec: dict) -> Optional[str]:
    """Spec: min, max, step?, marks? [{value, label?, type? in
              {dot, arrow_left, arrow_right, open}}]"""
    try:
        lo = float(spec.get('min'))
        hi = float(spec.get('max'))
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None
    step = float(spec.get('step') or _nice_step(hi - lo, 8))
    marks = spec.get('marks') or []
    title = spec.get('title') or ''

    pad_l, pad_r = 40, 40
    plot_w = CANVAS_W - pad_l - pad_r
    y = CANVAS_H / 2 + 10

    def x_at(v):
        return pad_l + (v - lo) / (hi - lo) * plot_w

    parts = [c.svg_open(CANVAS_W, CANVAS_H), c.title(title, x=CANVAS_W // 2)]
    # main axis
    parts.append(c.line(pad_l - 10, y, pad_l + plot_w + 10, y, stroke=c.STROKE, width=2))
    # arrow heads
    parts.append(c.path(f"M {pad_l - 12} {y - 6} L {pad_l - 4} {y} L {pad_l - 12} {y + 6} Z",
                         fill=c.STROKE, stroke=c.STROKE))
    parts.append(c.path(f"M {pad_l + plot_w + 12} {y - 6} L {pad_l + plot_w + 4} {y} L {pad_l + plot_w + 12} {y + 6} Z",
                         fill=c.STROKE, stroke=c.STROKE))

    # Tick marks at every step
    v = lo
    while v <= hi + 1e-6:
        x = x_at(v)
        parts.append(c.line(x, y - 5, x, y + 5, stroke=c.STROKE, width=1.5))
        # Label integers / nice values
        parts.append(c.label(c.fmt(v), x=x, y=y + 22, color=c.MUTED, size=11))
        v += step

    # Marks
    for m in marks:
        try:
            val = float(m.get('value'))
        except (TypeError, ValueError):
            continue
        if val < lo or val > hi:
            continue
        x = x_at(val)
        kind = (m.get('type') or 'dot').lower()
        col = c.ACCENT
        if kind == 'open':
            parts.append(c.circle(x, y, 6, fill='#fff', stroke=col, width=2))
        elif kind == 'dot':
            parts.append(c.circle(x, y, 6, fill=col, stroke=col))
        elif kind == 'arrow_right':
            parts.append(c.circle(x, y, 6, fill=col, stroke=col))
            parts.append(c.line(x + 6, y, pad_l + plot_w + 4, y, stroke=col, width=3))
        elif kind == 'arrow_left':
            parts.append(c.circle(x, y, 6, fill=col, stroke=col))
            parts.append(c.line(x - 6, y, pad_l - 4, y, stroke=col, width=3))
        if m.get('label'):
            parts.append(c.label(m['label'], x=x, y=y - 14, color=col, weight=600, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


def fraction_bar(spec: dict) -> Optional[str]:
    """Spec: numerator, denominator, label?"""
    try:
        num = int(spec.get('numerator'))
        den = int(spec.get('denominator'))
    except (TypeError, ValueError):
        return None
    if den <= 0 or num < 0 or num > den:
        return None
    title = spec.get('title') or ''
    parts = [c.svg_open(CANVAS_W, 200), c.title(title, x=CANVAS_W // 2)]

    margin = 40
    bar_w = CANVAS_W - 2 * margin
    bar_h = 60
    y = 70
    cell_w = bar_w / den

    for i in range(den):
        x = margin + i * cell_w
        fill = c.PALETTE[0] if i < num else '#fff'
        parts.append(c.rect(x, y, cell_w, bar_h, fill=fill, stroke=c.STROKE))
    parts.append(c.label(spec.get('label') or f"{num}/{den}",
                          x=CANVAS_W // 2, y=y + bar_h + 30,
                          color=c.ACCENT, weight=600, size=14))
    parts.append('</svg>')
    return ''.join(parts)


def coordinate_grid(spec: dict) -> Optional[str]:
    """Spec: xmin, xmax, ymin, ymax, step?, points? [{x,y,label?}],
              lines? [{m, c, label?}], curves? [{points: [[x,y],...]},...]"""
    try:
        xmin = float(spec.get('xmin', -5))
        xmax = float(spec.get('xmax', 5))
        ymin = float(spec.get('ymin', -5))
        ymax = float(spec.get('ymax', 5))
    except (TypeError, ValueError):
        return None
    if xmax <= xmin or ymax <= ymin:
        return None
    step = float(spec.get('step') or 1)
    title = spec.get('title') or ''
    points = spec.get('points') or []
    lines = spec.get('lines') or []
    curves = spec.get('curves') or []

    W, H = 380, 380
    pad = 40
    plot_w = W - 2 * pad
    plot_h = H - 2 * pad

    def x_at(v): return pad + (v - xmin) / (xmax - xmin) * plot_w
    def y_at(v): return pad + (ymax - v) / (ymax - ymin) * plot_h

    parts = [c.svg_open(W, H), c.title(title, x=W // 2)]
    # Grid lines
    v = math.ceil(xmin / step) * step
    while v <= xmax + 1e-6:
        x = x_at(v)
        parts.append(c.line(x, pad, x, pad + plot_h, stroke=c.GRID, width=1))
        v += step
    v = math.ceil(ymin / step) * step
    while v <= ymax + 1e-6:
        y = y_at(v)
        parts.append(c.line(pad, y, pad + plot_w, y, stroke=c.GRID, width=1))
        v += step

    # Axes (x-axis at y=0 if in range, else at bottom; same for y)
    x_axis_y = y_at(0) if ymin <= 0 <= ymax else pad + plot_h
    y_axis_x = x_at(0) if xmin <= 0 <= xmax else pad
    parts.append(c.line(pad, x_axis_y, pad + plot_w, x_axis_y, stroke=c.STROKE, width=1.5))
    parts.append(c.line(y_axis_x, pad, y_axis_x, pad + plot_h, stroke=c.STROKE, width=1.5))

    # Tick labels (integers within range)
    v = math.ceil(xmin / step) * step
    while v <= xmax + 1e-6:
        if abs(v) > 1e-6:
            parts.append(c.label(c.fmt(v), x=x_at(v), y=x_axis_y + 14, color=c.MUTED, size=10))
        v += step
    v = math.ceil(ymin / step) * step
    while v <= ymax + 1e-6:
        if abs(v) > 1e-6:
            parts.append(c.label(c.fmt(v), x=y_axis_x - 8, y=y_at(v) + 4, anchor='end', color=c.MUTED, size=10))
        v += step
    parts.append(c.label('0', x=y_axis_x - 8, y=x_axis_y + 14, anchor='end', color=c.MUTED, size=10))

    # Curves
    for ci, curve in enumerate(curves):
        pts = curve.get('points') or []
        screen = [(x_at(float(p[0])), y_at(float(p[1]))) for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(screen) >= 2:
            col = c.PALETTE[(ci + 1) % len(c.PALETTE)]
            parts.append(c.polyline(screen, stroke=col, width=2))
            if curve.get('label'):
                parts.append(c.label(curve['label'], x=screen[-1][0], y=screen[-1][1] - 10,
                                      color=col, weight=600, size=11, anchor='end'))

    # Lines y = m*x + c
    for li, ln in enumerate(lines):
        try:
            m = float(ln.get('m', 0))
            ck = float(ln.get('c', 0))
        except (TypeError, ValueError):
            continue
        # Sample at xmin and xmax
        y1 = m * xmin + ck
        y2 = m * xmax + ck
        # Clip to ymin/ymax if outside
        x1, x2 = xmin, xmax
        if y1 < ymin: x1 = (ymin - ck) / m if m else x1; y1 = ymin
        if y1 > ymax: x1 = (ymax - ck) / m if m else x1; y1 = ymax
        if y2 < ymin: x2 = (ymin - ck) / m if m else x2; y2 = ymin
        if y2 > ymax: x2 = (ymax - ck) / m if m else x2; y2 = ymax
        col = c.PALETTE[li % len(c.PALETTE)]
        parts.append(c.line(x_at(x1), y_at(y1), x_at(x2), y_at(y2), stroke=col, width=2))
        if ln.get('label'):
            parts.append(c.label(ln['label'], x=x_at(x2) - 4, y=y_at(y2) - 6,
                                  color=col, weight=600, size=11, anchor='end'))

    # Points
    for pt in points:
        try:
            px = float(pt.get('x'))
            py = float(pt.get('y'))
        except (TypeError, ValueError):
            continue
        if px < xmin or px > xmax or py < ymin or py > ymax:
            continue
        sx, sy = x_at(px), y_at(py)
        parts.append(c.circle(sx, sy, 5, fill=c.ACCENT, stroke=c.ACCENT))
        if pt.get('label'):
            parts.append(c.label(pt['label'], x=sx + 8, y=sy - 6, anchor='start',
                                  color=c.STROKE, weight=600, size=11))
        else:
            parts.append(c.label(f"({c.fmt(px)},{c.fmt(py)})", x=sx + 8, y=sy - 6,
                                  anchor='start', color=c.MUTED, size=10))

    parts.append(c.svg_close())
    return ''.join(parts)


def _nice_step(span, n_ticks):
    raw = span / max(1, n_ticks)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    norm = raw / mag
    if norm < 1.5: return 1 * mag
    if norm < 3: return 2 * mag
    if norm < 7: return 5 * mag
    return 10 * mag


RENDERERS = {
    'number_line': number_line,
    'fraction_bar': fraction_bar,
    'coordinate_grid': coordinate_grid,
}
