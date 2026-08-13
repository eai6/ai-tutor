"""Angle diagram templates."""

from __future__ import annotations

import math
from typing import List, Optional

from . import _common as c


CANVAS_W = 360
CANVAS_H = 280


def _angle_arc_at(cx, cy, r, start_deg, end_deg, label_deg, *, color):
    parts = [c.angle_arc(cx, cy, r, start_deg, end_deg, stroke=color)]
    # Label at midpoint of arc, slightly outside
    mid = (start_deg + end_deg) / 2
    lx, ly = c.deg_to_xy(cx, cy, r + 14, mid)
    parts.append(c.label(f"{c.fmt(label_deg)}°", x=lx, y=ly + 4,
                          color=color, weight=600, size=12))
    return parts


def angle(spec: dict) -> Optional[str]:
    """Single angle. Spec: degrees, label?, ray_len?"""
    deg = float(spec.get('degrees', 0))
    if deg <= 0 or deg >= 360:
        return None
    title = spec.get('title') or ''
    ray_len = float(spec.get('ray_len', 110))

    cx, cy = CANVAS_W / 2 - 30, CANVAS_H / 2 + 30
    # ray 1 along positive x-axis
    p_a = (cx + ray_len, cy)
    # ray 2 at +deg counter-clockwise
    p_b = c.deg_to_xy(cx, cy, ray_len, deg)
    parts = [c.svg_open(CANVAS_W, CANVAS_H), c.title(title, x=CANVAS_W // 2)]
    parts.append(c.line(*p_a, cx, cy, stroke=c.STROKE, width=2))
    parts.append(c.line(*p_b, cx, cy, stroke=c.STROKE, width=2))
    # Vertex dot
    parts.append(c.circle(cx, cy, 3, fill=c.STROKE, stroke=c.STROKE))
    # Arc + label
    arc_r = 36
    parts += _angle_arc_at(cx, cy, arc_r, 0, deg, deg, color=c.ACCENT)
    if spec.get('label'):
        parts.append(c.label(spec['label'], x=CANVAS_W // 2, y=CANVAS_H - 18, color=c.MUTED))
    parts.append(c.svg_close())
    return ''.join(parts)


def straight_line_angles(spec: dict) -> Optional[str]:
    """Spec: angles[] (must sum to 180), labels?[]"""
    angles_v = [float(a) for a in (spec.get('angles') or [])]
    if not angles_v or abs(sum(angles_v) - 180) > 1e-3:
        return None
    title = spec.get('title') or ''
    cx, cy = CANVAS_W / 2, CANVAS_H / 2 + 20
    L = 130
    p_l = (cx - L, cy)
    p_r = (cx + L, cy)
    parts = [c.svg_open(CANVAS_W, CANVAS_H), c.title(title, x=CANVAS_W // 2)]
    parts.append(c.line(*p_l, *p_r, stroke=c.STROKE, width=2))

    # Each angle is measured from the previous ray. Start from 180° (going left).
    # We sweep CCW by `angles_v[i]` each step.
    cumulative = 180
    arc_r = 32
    color_idx = 0
    for i, a in enumerate(angles_v):
        new = cumulative - a
        # Draw ray at `new` direction (intermediate rays only, not endpoints).
        if i < len(angles_v) - 1:
            p = c.deg_to_xy(cx, cy, L * 0.85, new)
            parts.append(c.line(cx, cy, *p, stroke=c.STROKE, width=2))
        col = c.PALETTE[color_idx % len(c.PALETTE)]
        parts += _angle_arc_at(cx, cy, arc_r + 10 * (i % 2), new, cumulative, a, color=col)
        cumulative = new
        color_idx += 1

    parts.append(c.circle(cx, cy, 3, fill=c.STROKE, stroke=c.STROKE))
    parts.append(c.label("Sum = 180°", x=cx, y=CANVAS_H - 10, color=c.MUTED, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


def point_angles(spec: dict) -> Optional[str]:
    """Spec: angles[] sum to 360, labels?[]. Renders a circle with sectors."""
    angles_v = [float(a) for a in (spec.get('angles') or [])]
    labels = spec.get('labels') or []
    if not angles_v or abs(sum(angles_v) - 360) > 1e-2:
        return None
    title = spec.get('title') or ''
    cx, cy = CANVAS_W / 2, CANVAS_H / 2 + 12
    R = 95
    parts = [c.svg_open(CANVAS_W, CANVAS_H), c.title(title, x=CANVAS_W // 2)]

    # Draw each sector as a wedge.
    cur = 90  # start at top (12 o'clock)
    for i, a in enumerate(angles_v):
        end = cur - a
        x1, y1 = c.deg_to_xy(cx, cy, R, cur)
        x2, y2 = c.deg_to_xy(cx, cy, R, end)
        large = 1 if a > 180 else 0
        d = (
            f"M {cx} {cy} L {c.fmt(x1)} {c.fmt(y1)} "
            f"A {R} {R} 0 {large} 1 {c.fmt(x2)} {c.fmt(y2)} Z"
        )
        col = c.PALETTE[i % len(c.PALETTE)]
        parts.append(c.path(d, fill=col, stroke='#fff', width=1.5))
        # Label inside sector
        mid = cur - a / 2
        lx, ly = c.deg_to_xy(cx, cy, R * 0.62, mid)
        deg_str = f"{c.fmt(a)}°"
        if labels and i < len(labels) and labels[i]:
            parts.append(c.label(labels[i], x=lx, y=ly - 4, color='#fff', weight=700, size=11))
            parts.append(c.label(deg_str, x=lx, y=ly + 12, color='#fff', weight=600, size=11))
        else:
            parts.append(c.label(deg_str, x=lx, y=ly + 4, color='#fff', weight=700, size=12))
        cur = end

    # Vertex marker
    parts.append(c.circle(cx, cy, 3, fill=c.STROKE, stroke=c.STROKE))
    parts.append(c.label("Sum = 360°", x=cx, y=CANVAS_H - 10, color=c.MUTED, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


def triangle_angles(spec: dict) -> Optional[str]:
    """Spec: angles[3] (sum 180), labels?[3]"""
    angles_v = [float(a) for a in (spec.get('angles') or [])]
    if len(angles_v) != 3 or abs(sum(angles_v) - 180) > 1e-3:
        return None
    title = spec.get('title') or 'Interior angles of a triangle'

    # Place a triangle; angles purely informational.
    cx = CANVAS_W / 2
    base_y = CANVAS_H - 60
    w = 200
    p1 = (cx - w / 2, base_y)
    p2 = (cx + w / 2, base_y)
    # Apex computed so triangle has rough proportions to the angles
    # (visual cue, not metrically correct).
    p3 = (cx, base_y - 130)
    parts = [c.svg_open(CANVAS_W, CANVAS_H), c.title(title, x=CANVAS_W // 2)]
    parts.append(c.polygon([p1, p2, p3], fill='#ede9fe', stroke=c.STROKE))

    arc_r = 22
    # angle at p1: between (p3-p1) and (p2-p1) — open to the right and up.
    def _vec_angle(v):
        return math.degrees(math.atan2(-v[1], v[0]))  # SVG y flipped

    def _draw_vertex_angle(p, neighbours, deg, value):
        v1 = (neighbours[0][0] - p[0], neighbours[0][1] - p[1])
        v2 = (neighbours[1][0] - p[0], neighbours[1][1] - p[1])
        a1 = _vec_angle(v1) % 360
        a2 = _vec_angle(v2) % 360
        # Pick the arc that goes through the interior (smaller sweep)
        sweep = (a2 - a1) % 360
        if sweep > 180:
            a1, a2 = a2, a1
        out = []
        out.append(c.angle_arc(p[0], p[1], arc_r, a1, a1 + (a2 - a1) % 360, stroke=c.ACCENT))
        mid = a1 + ((a2 - a1) % 360) / 2
        lx, ly = c.deg_to_xy(p[0], p[1], arc_r + 18, mid)
        out.append(c.label(value, x=lx, y=ly + 4, color=c.ACCENT, weight=600, size=12))
        return out

    parts += _draw_vertex_angle(p1, (p2, p3), 'p1', f"{c.fmt(angles_v[0])}°")
    parts += _draw_vertex_angle(p2, (p3, p1), 'p2', f"{c.fmt(angles_v[1])}°")
    parts += _draw_vertex_angle(p3, (p1, p2), 'p3', f"{c.fmt(angles_v[2])}°")

    parts.append(c.label("Sum = 180°", x=cx, y=CANVAS_H - 10, color=c.MUTED, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


def parallel_lines(spec: dict) -> Optional[str]:
    """Spec: configuration ∈ {alternate,corresponding,co-interior},
              known_angle (degrees of the labelled angle)"""
    config = (spec.get('configuration') or 'alternate').lower()
    angle_v = float(spec.get('known_angle', 50))
    if angle_v <= 0 or angle_v >= 180:
        return None
    title = spec.get('title') or 'Parallel lines cut by a transversal'

    parts = [c.svg_open(CANVAS_W, CANVAS_H), c.title(title, x=CANVAS_W // 2)]
    # Two horizontal parallel lines
    L_y = 100
    M_y = 200
    margin = 30
    parts.append(c.line(margin, L_y, CANVAS_W - margin, L_y, stroke=c.STROKE))
    parts.append(c.line(margin, M_y, CANVAS_W - margin, M_y, stroke=c.STROKE))
    # Arrow markers (parallel indicator)
    for y in (L_y, M_y):
        parts.append(c.path(f"M {CANVAS_W / 2 - 12} {y - 6} L {CANVAS_W / 2 - 4} {y} L {CANVAS_W / 2 - 12} {y + 6}",
                             stroke=c.STROKE, width=1.5))

    # Transversal — diagonal line crossing both
    # angle of transversal from horizontal
    t_deg = 60
    t_slope = math.tan(math.radians(90 - t_deg))  # for lines we use rise/run; this is intentionally just a visual
    # use 2 explicit points to keep within canvas
    t_y1, t_y2 = 50, CANVAS_H - 30
    dx = (t_y2 - t_y1) * math.tan(math.radians(20))
    p_top = (CANVAS_W / 2 - dx, t_y1)
    p_bot = (CANVAS_W / 2 + dx, t_y2)
    parts.append(c.line(*p_top, *p_bot, stroke=c.ACCENT, width=2))

    # Find intersections with the two parallels
    # Param: (1-t) * p_top + t * p_bot, solve y = L_y, M_y
    def _intersect(y_target):
        t = (y_target - p_top[1]) / (p_bot[1] - p_top[1])
        x = p_top[0] + t * (p_bot[0] - p_top[0])
        return (x, y_target)

    P1 = _intersect(L_y)
    P2 = _intersect(M_y)

    # Mark known angle at P1 (above-left of P1: between transversal going up-left and line going right).
    # Just mark with arc + label.
    parts.append(c.angle_arc(P1[0], P1[1], 22, 180 - angle_v, 180, stroke=c.ACCENT))
    parts.append(c.label(f"{c.fmt(angle_v)}°", x=P1[0] - 24, y=P1[1] - 6, color=c.ACCENT, weight=600))

    # Unknown — depends on config
    unknown = ''
    if config == 'alternate':
        # alternate interior — at P2, on the opposite side
        parts.append(c.angle_arc(P2[0], P2[1], 22, 0, angle_v, stroke=c.ACCENT))
        parts.append(c.label("?", x=P2[0] + 24, y=P2[1] + 18, color=c.ACCENT, weight=700, size=14))
        unknown = f"Alternate angles are equal → ? = {c.fmt(angle_v)}°"
    elif config == 'corresponding':
        # corresponding — at P2, same side as P1
        parts.append(c.angle_arc(P2[0], P2[1], 22, 180 - angle_v, 180, stroke=c.ACCENT))
        parts.append(c.label("?", x=P2[0] - 24, y=P2[1] - 6, color=c.ACCENT, weight=700, size=14))
        unknown = f"Corresponding angles are equal → ? = {c.fmt(angle_v)}°"
    else:  # co-interior
        parts.append(c.angle_arc(P2[0], P2[1], 22, 180, 180 + angle_v, stroke=c.ACCENT))
        parts.append(c.label("?", x=P2[0] - 24, y=P2[1] + 18, color=c.ACCENT, weight=700, size=14))
        unknown = f"Co-interior angles add to 180° → ? = {c.fmt(180 - angle_v)}°"

    parts.append(c.label(unknown, x=CANVAS_W / 2, y=CANVAS_H - 10, color=c.MUTED, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


def polygon_angles(spec: dict) -> Optional[str]:
    """Spec: sides (3-12). Renders the polygon + label of interior-angle sum."""
    n = int(spec.get('sides', 0))
    if not 3 <= n <= 12:
        return None
    title = spec.get('title') or f"Interior angles of an {n}-gon"
    interior = (n - 2) * 180

    cx = CANVAS_W / 2
    cy = CANVAS_H / 2 + 12
    R = 95
    offset = -math.pi / 2 - math.pi / n
    pts = [
        (cx + R * math.cos(offset + 2 * math.pi * i / n),
         cy + R * math.sin(offset + 2 * math.pi * i / n))
        for i in range(n)
    ]
    parts = [c.svg_open(CANVAS_W, CANVAS_H), c.title(title, x=CANVAS_W // 2)]
    parts.append(c.polygon(pts, fill='#ede9fe', stroke=c.STROKE))
    each = interior / n
    parts.append(c.label(f"Sum of interior angles = {(n - 2) * 180}°",
                          x=cx, y=CANVAS_H - 26, color=c.ACCENT, weight=600, size=12))
    parts.append(c.label(f"Each = {c.fmt(each)}° (regular)",
                          x=cx, y=CANVAS_H - 10, color=c.MUTED, size=11))
    parts.append(c.svg_close())
    return ''.join(parts)


RENDERERS = {
    'angle': angle,
    'straight_line_angles': straight_line_angles,
    'point_angles': point_angles,
    'triangle_angles': triangle_angles,
    'parallel_lines': parallel_lines,
    'polygon_angles': polygon_angles,
}
