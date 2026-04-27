"""Shared SVG primitives + style constants for every template.

No external CSS. Every renderer returns a self-contained `<svg>` string
that displays identically in any browser. Colors and fonts match the
chart palette in figure_render.py for visual consistency.
"""

from __future__ import annotations

import math
from html import escape
from typing import Iterable, Optional


# ─── Style ────────────────────────────────────────────────────────────

PALETTE = [
    '#7c3aed',  # primary purple
    '#10b981',  # green
    '#f59e0b',  # amber
    '#3b82f6',  # blue
    '#ec4899',  # pink
    '#14b8a6',  # teal
    '#ef4444',  # red
    '#8b5cf6',  # violet
]
STROKE = '#18181b'
TEXT = '#18181b'
MUTED = '#71717a'
GRID = '#e4e4e7'
ACCENT = '#7c3aed'
FONT = "Nunito, system-ui, -apple-system, sans-serif"


# ─── Helpers ──────────────────────────────────────────────────────────

def fmt(n) -> str:
    """Format a number for SVG: drop trailing zeros, keep a few decimals."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if f == int(f):
        return str(int(f))
    return f"{f:.3f}".rstrip('0').rstrip('.')


def esc(s) -> str:
    return escape(str(s))


def svg_open(width: int = 480, height: int = 320, view_box: Optional[str] = None) -> str:
    vb = view_box or f"0 0 {width} {height}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" '
        f'viewBox="{vb}" preserveAspectRatio="xMidYMid meet" '
        f'style="font-family:{FONT};max-width:{width}px;height:auto;">'
    )


def svg_close() -> str:
    return '</svg>'


def title(text: str, *, x: int, y: int = 24, anchor: str = 'middle', size: int = 16) -> str:
    if not text:
        return ''
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'fill="{TEXT}" font-size="{size}" font-weight="700">'
        f'{esc(text)}</text>'
    )


def label(text: str, *, x: float, y: float, anchor: str = 'middle',
          size: int = 12, color: str = TEXT, weight: int = 400) -> str:
    if text == '' or text is None:
        return ''
    return (
        f'<text x="{fmt(x)}" y="{fmt(y)}" text-anchor="{anchor}" '
        f'fill="{color}" font-size="{size}" font-weight="{weight}">'
        f'{esc(text)}</text>'
    )


def line(x1, y1, x2, y2, *, stroke=STROKE, width=2, dash: Optional[str] = None) -> str:
    extra = f' stroke-dasharray="{dash}"' if dash else ''
    return (
        f'<line x1="{fmt(x1)}" y1="{fmt(y1)}" x2="{fmt(x2)}" y2="{fmt(y2)}" '
        f'stroke="{stroke}" stroke-width="{width}" stroke-linecap="round"{extra}/>'
    )


def rect(x, y, w, h, *, fill='none', stroke=STROKE, width=2, rx: int = 0) -> str:
    return (
        f'<rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(w)}" height="{fmt(h)}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{width}" rx="{rx}"/>'
    )


def circle(cx, cy, r, *, fill='none', stroke=STROKE, width=2) -> str:
    return (
        f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{fmt(r)}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>'
    )


def polygon(points: Iterable[tuple], *, fill='none', stroke=STROKE, width=2) -> str:
    pts = ' '.join(f"{fmt(x)},{fmt(y)}" for x, y in points)
    return (
        f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="{width}" stroke-linejoin="round"/>'
    )


def polyline(points: Iterable[tuple], *, fill='none', stroke=STROKE, width=2,
             dash: Optional[str] = None) -> str:
    pts = ' '.join(f"{fmt(x)},{fmt(y)}" for x, y in points)
    extra = f' stroke-dasharray="{dash}"' if dash else ''
    return (
        f'<polyline points="{pts}" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="{width}" stroke-linejoin="round" '
        f'stroke-linecap="round"{extra}/>'
    )


def path(d: str, *, fill='none', stroke=STROKE, width=2,
         dash: Optional[str] = None) -> str:
    extra = f' stroke-dasharray="{dash}"' if dash else ''
    return (
        f'<path d="{d}" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="{width}" stroke-linejoin="round" '
        f'stroke-linecap="round"{extra}/>'
    )


def arc_path(cx, cy, r, start_deg, end_deg) -> str:
    """SVG path data for an arc on a circle from start_deg → end_deg
    (angles measured from the positive x-axis, going counter-clockwise
    in math convention; we flip y so it renders correctly in SVG).
    """
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    x1, y1 = cx + r * math.cos(start), cy - r * math.sin(start)
    x2, y2 = cx + r * math.cos(end), cy - r * math.sin(end)
    sweep = abs(end_deg - start_deg)
    large = 1 if sweep > 180 else 0
    # SVG arc sweep flag: 0 means counter-clockwise in screen coords,
    # which is clockwise in our math convention because y is flipped.
    sweep_flag = 0 if end_deg > start_deg else 1
    return (
        f"M {fmt(x1)} {fmt(y1)} "
        f"A {fmt(r)} {fmt(r)} 0 {large} {sweep_flag} {fmt(x2)} {fmt(y2)}"
    )


def angle_arc(cx, cy, r, start_deg, end_deg, *, stroke=ACCENT, width=2) -> str:
    return path(arc_path(cx, cy, r, start_deg, end_deg), stroke=stroke, width=width)


def deg_to_xy(cx, cy, r, deg) -> tuple:
    """Convert (deg from positive x-axis, ccw) to (x, y) in SVG screen coords."""
    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy - r * math.sin(rad)


def midpoint(p1: tuple, p2: tuple) -> tuple:
    return (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2


def caption(text: str, *, x: int, y: int, anchor: str = 'middle') -> str:
    """Italicized muted source/caption line below the figure."""
    if not text:
        return ''
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'fill="{MUTED}" font-size="10" font-style="italic">{esc(text)}</text>'
    )
