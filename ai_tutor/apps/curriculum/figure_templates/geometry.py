"""Geometry shape templates.

Every shape is laid out at a fixed canvas size with the figure
auto-scaled inside. Side/angle labels are positioned with calculated
offsets so they never overlap the shape.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from . import _common as c


CANVAS_W = 360
CANVAS_H = 280


def _box_with_title(title: Optional[str]) -> str:
    return c.title(title or '', x=CANVAS_W // 2)


def rectangle(spec: dict) -> Optional[str]:
    """Spec: width, height, units?, label?, label_w?, label_h?,
              show_diagonal?"""
    w_val = float(spec.get('width', 0))
    h_val = float(spec.get('height', 0))
    if w_val <= 0 or h_val <= 0:
        return None
    units = spec.get('units') or ''
    title = spec.get('title') or ''
    show_diag = bool(spec.get('show_diagonal'))

    # Scale to fit the canvas with margin.
    margin_x = 60
    margin_y = 70
    max_w = CANVAS_W - 2 * margin_x
    max_h = CANVAS_H - 2 * margin_y
    scale = min(max_w / w_val, max_h / h_val)
    pw = w_val * scale
    ph = h_val * scale
    x = (CANVAS_W - pw) / 2
    y = margin_y

    label_w = spec.get('label_w') or f"{c.fmt(w_val)}{(' ' + units) if units else ''}"
    label_h = spec.get('label_h') or f"{c.fmt(h_val)}{(' ' + units) if units else ''}"

    parts = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    parts.append(c.rect(x, y, pw, ph, fill='#ede9fe', stroke=c.STROKE))
    if show_diag:
        parts.append(c.line(x, y, x + pw, y + ph, stroke=c.MUTED, width=1, dash="4 3"))
    # Width label below
    parts.append(c.label(label_w, x=x + pw / 2, y=y + ph + 22))
    # Height label right of shape
    parts.append(c.label(label_h, x=x + pw + 8, y=y + ph / 2 + 4, anchor='start'))
    inner_label = spec.get('label')
    if inner_label:
        parts.append(c.label(inner_label, x=x + pw / 2, y=y + ph / 2 + 4, color=c.MUTED, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


def square(spec: dict) -> Optional[str]:
    side = float(spec.get('side', 0))
    if side <= 0:
        return None
    return rectangle({**spec, 'width': side, 'height': side, 'label_h': '', 'label_w': spec.get('label_w') or f"{c.fmt(side)}{(' ' + (spec.get('units') or '')).rstrip()}"})


def triangle(spec: dict) -> Optional[str]:
    """Spec: type ∈ {right, equilateral, isoceles, scalene}, sides? [a,b,c],
              angles? [A,B,C], units?, title?"""
    kind = (spec.get('type') or 'right').lower()
    sides = spec.get('sides') or []
    angles = spec.get('angles') or []
    units = spec.get('units') or ''
    title = spec.get('title') or ''

    cx = CANVAS_W / 2
    base_y = CANVAS_H - 60
    base_w = 200
    apex_x = cx
    apex_y = base_y - 130

    if kind == 'right':
        # right angle at bottom-left
        p1 = (cx - base_w / 2, base_y)
        p2 = (cx + base_w / 2, base_y)
        p3 = (cx - base_w / 2, base_y - 140)
    elif kind == 'equilateral':
        h = base_w * math.sqrt(3) / 2
        p1 = (cx - base_w / 2, base_y)
        p2 = (cx + base_w / 2, base_y)
        p3 = (cx, base_y - h)
    elif kind == 'isoceles':
        p1 = (cx - base_w / 2, base_y)
        p2 = (cx + base_w / 2, base_y)
        p3 = (apex_x, apex_y)
    else:  # scalene — slight asymmetry
        p1 = (cx - base_w / 2, base_y)
        p2 = (cx + base_w / 2, base_y)
        p3 = (cx + 30, apex_y)

    pts = [p1, p2, p3]
    parts = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    parts.append(c.polygon(pts, fill='#ede9fe', stroke=c.STROKE))

    # Right-angle marker
    if kind == 'right':
        sq = 12
        parts.append(c.path(
            f"M {p1[0]} {p1[1] - sq} L {p1[0] + sq} {p1[1] - sq} L {p1[0] + sq} {p1[1]}",
            stroke=c.STROKE, width=1.5,
        ))

    # Side labels (a = opposite to angle A; convention: a opposite p1, b opposite p2, c opposite p3 → here we label p1-p2 (bottom), p2-p3 (right), p1-p3 (left))
    def _side_lbl(a, b, value, where):
        if value is None:
            return ''
        mx, my = c.midpoint(a, b)
        # offset perpendicular outward
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1
        ox, oy = -dy / L * 14, dx / L * 14
        # flip outward sign by side hint
        if where == 'top':
            oy = -abs(oy)
        elif where == 'bottom':
            oy = abs(oy)
        elif where == 'left':
            ox = -abs(ox)
        elif where == 'right':
            ox = abs(ox)
        return c.label(value, x=mx + ox, y=my + oy + 4)

    if sides and len(sides) >= 3:
        a, b, c_side = sides[0], sides[1], sides[2]
        sfx = (' ' + units).rstrip()
        parts.append(_side_lbl(p1, p2, f"{c.fmt(a)}{sfx}" if a is not None else None, 'bottom'))
        parts.append(_side_lbl(p2, p3, f"{c.fmt(b)}{sfx}" if b is not None else None, 'right'))
        parts.append(_side_lbl(p3, p1, f"{c.fmt(c_side)}{sfx}" if c_side is not None else None, 'left'))

    if angles and len(angles) >= 3:
        # Place angle labels just inside each vertex
        def _angle_lbl(p, deg, ox, oy):
            if deg is None:
                return ''
            return c.label(f"{c.fmt(deg)}°", x=p[0] + ox, y=p[1] + oy, size=11, color=c.ACCENT, weight=600)

        parts.append(_angle_lbl(p1, angles[0], 16, -8))
        parts.append(_angle_lbl(p2, angles[1], -16, -8))
        parts.append(_angle_lbl(p3, angles[2], 0, 18))

    parts.append(c.svg_close())
    return ''.join(parts)


def circle(spec: dict) -> Optional[str]:
    """Spec: radius? OR diameter?, units?, show_radius_line?, label?"""
    r_val = spec.get('radius')
    d_val = spec.get('diameter')
    if r_val is None and d_val is None:
        return None
    if r_val is None:
        r_val = float(d_val) / 2
    else:
        r_val = float(r_val)
    units = spec.get('units') or ''
    title = spec.get('title') or ''
    show_radius = spec.get('show_radius_line', True)
    show_diameter = bool(spec.get('show_diameter_line'))

    cx = CANVAS_W / 2
    cy = CANVAS_H / 2 + 12
    pr = 90  # rendered radius (constant for visual consistency)

    parts = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    parts.append(c.circle(cx, cy, pr, fill='#ede9fe', stroke=c.STROKE))
    parts.append(c.circle(cx, cy, 2.5, fill=c.STROKE, stroke=c.STROKE))

    sfx = (' ' + units).rstrip()
    if show_diameter:
        parts.append(c.line(cx - pr, cy, cx + pr, cy, stroke=c.ACCENT, width=2))
        parts.append(c.label(f"d = {c.fmt(2 * r_val)}{sfx}", x=cx, y=cy - 8, color=c.ACCENT, weight=600))
    elif show_radius:
        parts.append(c.line(cx, cy, cx + pr, cy, stroke=c.ACCENT, width=2))
        parts.append(c.label(f"r = {c.fmt(r_val)}{sfx}", x=cx + pr / 2, y=cy - 8, color=c.ACCENT, weight=600))

    if spec.get('label'):
        parts.append(c.label(spec['label'], x=cx, y=cy + pr + 28, color=c.MUTED))
    parts.append(c.svg_close())
    return ''.join(parts)


def regular_polygon(spec: dict) -> Optional[str]:
    """Spec: sides (3-12), side_length?, units?, show_interior_angle?"""
    n = int(spec.get('sides', 0))
    if not 3 <= n <= 12:
        return None
    units = spec.get('units') or ''
    side_length = spec.get('side_length')
    title = spec.get('title') or f"Regular {n}-gon"
    show_interior = bool(spec.get('show_interior_angle'))

    cx = CANVAS_W / 2
    cy = CANVAS_H / 2 + 8
    R = 100
    # angle offset so a flat side is at the bottom
    offset = -math.pi / 2 - math.pi / n
    pts = [
        (cx + R * math.cos(offset + 2 * math.pi * i / n),
         cy + R * math.sin(offset + 2 * math.pi * i / n))
        for i in range(n)
    ]
    parts = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    parts.append(c.polygon(pts, fill='#ede9fe', stroke=c.STROKE))

    if side_length is not None:
        sfx = (' ' + units).rstrip()
        # Label the bottom side
        b1, b2 = pts[0], pts[-1] if pts[-1][1] >= pts[0][1] else pts[1]
        # find pair with max y (bottom)
        idx_pairs = sorted(
            [(i, (pts[i], pts[(i + 1) % n])) for i in range(n)],
            key=lambda kv: -(kv[1][0][1] + kv[1][1][1]) / 2,
        )
        bot = idx_pairs[0][1]
        mx, my = c.midpoint(bot[0], bot[1])
        parts.append(c.label(f"{c.fmt(side_length)}{sfx}", x=mx, y=my + 18))

    if show_interior:
        interior = (n - 2) * 180 / n
        parts.append(c.label(f"Interior angle = {c.fmt(interior)}°",
                              x=cx, y=CANVAS_H - 18, color=c.ACCENT, weight=600, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


def parallelogram(spec: dict) -> Optional[str]:
    base = float(spec.get('base', 0))
    height = float(spec.get('height', 0))
    if base <= 0 or height <= 0:
        return None
    units = spec.get('units') or ''
    title = spec.get('title') or ''

    margin_x, margin_y = 60, 70
    max_w = CANVAS_W - 2 * margin_x
    max_h = CANVAS_H - 2 * margin_y
    scale = min(max_w / (base + 0.4 * height), max_h / height)
    pb = base * scale
    ph = height * scale
    skew = 0.4 * ph
    x = (CANVAS_W - (pb + skew)) / 2
    y = margin_y

    pts = [(x + skew, y), (x + skew + pb, y), (x + pb, y + ph), (x, y + ph)]
    parts = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    parts.append(c.polygon(pts, fill='#ede9fe', stroke=c.STROKE))
    # height (perpendicular dashed line)
    parts.append(c.line(x + skew, y, x + skew, y + ph, stroke=c.MUTED, width=1.2, dash="4 3"))
    # Right-angle marker on the height/base
    parts.append(c.path(
        f"M {x + skew} {y + ph - 12} L {x + skew + 12} {y + ph - 12} L {x + skew + 12} {y + ph}",
        stroke=c.MUTED, width=1.2,
    ))
    sfx = (' ' + units).rstrip()
    parts.append(c.label(f"{c.fmt(base)}{sfx}", x=x + pb / 2 + skew / 2, y=y + ph + 22))
    parts.append(c.label(f"h = {c.fmt(height)}{sfx}", x=x + skew - 8, y=y + ph / 2 + 4, anchor='end'))
    parts.append(c.svg_close())
    return ''.join(parts)


def trapezium(spec: dict) -> Optional[str]:
    a = float(spec.get('a', 0))
    b = float(spec.get('b', 0))
    h = float(spec.get('h', 0))
    if a <= 0 or b <= 0 or h <= 0:
        return None
    units = spec.get('units') or ''
    title = spec.get('title') or ''

    margin = 60
    max_w = CANVAS_W - 2 * margin
    max_h = CANVAS_H - 2 * margin - 20
    scale = min(max_w / max(a, b), max_h / h)
    pa, pb_, ph = a * scale, b * scale, h * scale
    cx = CANVAS_W / 2
    y_top = margin + 10
    y_bot = y_top + ph
    pts = [
        (cx - pa / 2, y_top),
        (cx + pa / 2, y_top),
        (cx + pb_ / 2, y_bot),
        (cx - pb_ / 2, y_bot),
    ]
    parts = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    parts.append(c.polygon(pts, fill='#ede9fe', stroke=c.STROKE))
    parts.append(c.line(cx, y_top, cx, y_bot, stroke=c.MUTED, width=1.2, dash="4 3"))
    sfx = (' ' + units).rstrip()
    parts.append(c.label(f"a = {c.fmt(a)}{sfx}", x=cx, y=y_top - 8))
    parts.append(c.label(f"b = {c.fmt(b)}{sfx}", x=cx, y=y_bot + 18))
    parts.append(c.label(f"h = {c.fmt(h)}{sfx}", x=cx + 8, y=(y_top + y_bot) / 2 + 4, anchor='start', color=c.MUTED))
    parts.append(c.svg_close())
    return ''.join(parts)


def cuboid(spec: dict) -> Optional[str]:
    """Spec: length, width, height, units?"""
    L = float(spec.get('length', 0))
    W = float(spec.get('width', 0))
    Hv = float(spec.get('height', 0))
    if L <= 0 or W <= 0 or Hv <= 0:
        return None
    units = spec.get('units') or ''
    title = spec.get('title') or ''

    # Iso projection: x_screen = x - 0.5 * y; y_screen = z + 0.5 * y
    # Scale all dims to fit canvas.
    scale = min(180 / max(L, W), 90 / Hv)
    if scale <= 0:
        return None
    pl, pw_, ph = L * scale, W * scale, Hv * scale

    ox, oy = (CANVAS_W - (pl + 0.5 * pw_)) / 2, 60 + ph + 0.5 * pw_
    p_blf = (ox, oy)
    p_brf = (ox + pl, oy)
    p_trf = (ox + pl, oy - ph)
    p_tlf = (ox, oy - ph)
    p_blb = (ox + 0.5 * pw_, oy - 0.5 * pw_)
    p_brb = (ox + pl + 0.5 * pw_, oy - 0.5 * pw_)
    p_trb = (ox + pl + 0.5 * pw_, oy - ph - 0.5 * pw_)
    p_tlb = (ox + 0.5 * pw_, oy - ph - 0.5 * pw_)

    parts = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    # Hidden edges (dashed)
    parts.append(c.line(*p_blf, *p_blb, stroke=c.MUTED, width=1.2, dash="3 3"))
    parts.append(c.line(*p_blb, *p_brb, stroke=c.MUTED, width=1.2, dash="3 3"))
    parts.append(c.line(*p_blb, *p_tlb, stroke=c.MUTED, width=1.2, dash="3 3"))
    # Front face
    parts.append(c.polygon([p_blf, p_brf, p_trf, p_tlf], fill='#ede9fe', stroke=c.STROKE))
    # Top face
    parts.append(c.polygon([p_tlf, p_trf, p_trb, p_tlb], fill='#ddd6fe', stroke=c.STROKE))
    # Right face
    parts.append(c.polygon([p_brf, p_brb, p_trb, p_trf], fill='#c4b5fd', stroke=c.STROKE))

    sfx = (' ' + units).rstrip()
    parts.append(c.label(f"{c.fmt(L)}{sfx}", x=(p_blf[0] + p_brf[0]) / 2, y=p_blf[1] + 18))
    parts.append(c.label(f"{c.fmt(Hv)}{sfx}", x=p_brf[0] + 8, y=(p_brf[1] + p_trf[1]) / 2 + 4, anchor='start'))
    parts.append(c.label(f"{c.fmt(W)}{sfx}", x=(p_trf[0] + p_trb[0]) / 2 + 8, y=(p_trf[1] + p_trb[1]) / 2 + 4, anchor='start'))
    parts.append(c.svg_close())
    return ''.join(parts)


def cylinder(spec: dict) -> Optional[str]:
    r_val = float(spec.get('radius', 0))
    h_val = float(spec.get('height', 0))
    if r_val <= 0 or h_val <= 0:
        return None
    units = spec.get('units') or ''
    title = spec.get('title') or ''

    scale = min(120 / r_val, 160 / h_val)
    pr = r_val * scale
    ph = h_val * scale
    cx = CANVAS_W / 2
    cy_top = 70
    cy_bot = cy_top + ph
    ry = pr * 0.32  # ellipse y-radius for perspective

    parts = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    # back-half ellipse of bottom (dashed)
    parts.append(c.path(
        f"M {cx - pr} {cy_bot} A {pr} {ry} 0 0 0 {cx + pr} {cy_bot}",
        stroke=c.MUTED, width=1.2, dash="3 3",
    ))
    # body sides
    parts.append(c.line(cx - pr, cy_top, cx - pr, cy_bot, stroke=c.STROKE, width=2))
    parts.append(c.line(cx + pr, cy_top, cx + pr, cy_bot, stroke=c.STROKE, width=2))
    # bottom front-half (solid)
    parts.append(c.path(
        f"M {cx - pr} {cy_bot} A {pr} {ry} 0 0 1 {cx + pr} {cy_bot}",
        stroke=c.STROKE, width=2,
    ))
    # top ellipse
    parts.append(c.path(
        f"M {cx - pr} {cy_top} A {pr} {ry} 0 1 0 {cx + pr} {cy_top} A {pr} {ry} 0 1 0 {cx - pr} {cy_top}",
        fill='#ede9fe', stroke=c.STROKE, width=2,
    ))
    sfx = (' ' + units).rstrip()
    parts.append(c.label(f"r = {c.fmt(r_val)}{sfx}", x=cx + pr / 2, y=cy_top - 6, color=c.ACCENT, weight=600))
    parts.append(c.line(cx, cy_top, cx + pr, cy_top, stroke=c.ACCENT, width=2))
    parts.append(c.label(f"h = {c.fmt(h_val)}{sfx}", x=cx + pr + 10, y=(cy_top + cy_bot) / 2 + 4, anchor='start'))
    parts.append(c.svg_close())
    return ''.join(parts)


def compound_shape(spec: dict) -> Optional[str]:
    """Spec: parts[] — each {kind: 'rectangle'|'square', x, y, width, height,
              label_w?, label_h?}. Coordinates are in user units; the
              renderer scales to fit. units?, title?"""
    parts_in = spec.get('parts') or []
    if not parts_in:
        return None
    units = spec.get('units') or ''
    title = spec.get('title') or ''

    xs, ys = [], []
    for p in parts_in:
        x = float(p.get('x', 0))
        y = float(p.get('y', 0))
        w = float(p.get('width', 0))
        h = float(p.get('height', 0))
        xs += [x, x + w]
        ys += [y, y + h]
    if not xs or not ys:
        return None
    bbx = max(xs) - min(xs)
    bby = max(ys) - min(ys)
    if bbx <= 0 or bby <= 0:
        return None

    margin = 60
    max_w = CANVAS_W - 2 * margin
    max_h = CANVAS_H - 2 * margin - 20
    scale = min(max_w / bbx, max_h / bby)
    ox = (CANVAS_W - bbx * scale) / 2 - min(xs) * scale
    oy = margin + 10 - min(ys) * scale

    parts_out = [c.svg_open(CANVAS_W, CANVAS_H), _box_with_title(title)]
    sfx = (' ' + units).rstrip()
    for p in parts_in:
        x = float(p.get('x', 0)); y = float(p.get('y', 0))
        w = float(p.get('width', 0)); h = float(p.get('height', 0))
        parts_out.append(c.rect(ox + x * scale, oy + y * scale, w * scale, h * scale,
                                 fill='#ede9fe', stroke=c.STROKE))
        if p.get('label_w') or w > 0:
            parts_out.append(c.label(p.get('label_w') or f"{c.fmt(w)}{sfx}",
                                      x=ox + (x + w / 2) * scale,
                                      y=oy + (y + h) * scale + 16, size=11))
        if p.get('label_h') or h > 0:
            parts_out.append(c.label(p.get('label_h') or f"{c.fmt(h)}{sfx}",
                                      x=ox + (x + w) * scale + 6,
                                      y=oy + (y + h / 2) * scale + 4, anchor='start', size=11))
    parts_out.append(c.svg_close())
    return ''.join(parts_out)


RENDERERS = {
    'rectangle': rectangle,
    'square': square,
    'triangle': triangle,
    'circle': circle,
    'regular_polygon': regular_polygon,
    'parallelogram': parallelogram,
    'trapezium': trapezium,
    'cuboid': cuboid,
    'cylinder': cylinder,
    'compound_shape': compound_shape,
}
